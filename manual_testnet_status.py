"""Operator-run development status, not the protected T1C deployment runtime.

No import-time client construction, credential access or network activity.
This module grants no trading authority and has no journal integration.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import http.client
import io
import logging
import os
from pathlib import Path
import re
import stat
from threading import RLock, get_ident

import ccxt
from dotenv.parser import parse_stream
import requests
from urllib3.connection import HTTPConnection, HTTPSConnection

from account_discovery import AccountDiscovery, Fact, unknown
from diagnostics import ResponseEvidence, diagnose
from manual_market_catalog import MarketCatalog, build_catalog, on_grid, valid_selector
from readonly_exchange import ReadOnlyPhemex
from safety import SafetyError
from testnet_transport import PLAN, PROXY_OPTIONS, _fingerprint, _validate_wire, decode_response_json


MANUAL_PLAN = PLAN[:3]
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 15


@dataclass(frozen=True)
class ManualCredentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self):
        if any(type(v) is not str or not re.fullmatch(r'[^\s\x00-\x1f\x7f]{1,512}', v)
               for v in (self.key, self.secret)):
            raise SafetyError('Invalid explicit Testnet credentials; details withheld')


def load_credentials(env_file=None, *, use_environment=False, environ=None):
    """Explicit file OR dedicated Testnet variables, never a generic fallback."""
    try:
        if type(use_environment) is not bool or (env_file is None) != use_environment:
            raise ValueError
        if use_environment:
            values = os.environ if environ is None else environ
            return ManualCredentials(values.get('PHEMEX_TESTNET_API_KEY'),
                                     values.get('PHEMEX_TESTNET_API_SECRET'))
        path = Path(env_file)
        if path.suffix != '.env':
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError
        values = {}
        for item in parse_stream(io.StringIO(raw.decode('utf-8'))):
            if item.error or (item.key is not None and item.key in values):
                raise ValueError
            if item.key is not None:
                values[item.key] = item.value
        if (set(values) - {'PHEMEX_API_KEY', 'PHEMEX_API_SECRET', 'PHEMEX_ALLOW_LIVE',
                           'MAX_NOTIONAL_USDT', 'MAX_LEVERAGE'}
                or values.get('PHEMEX_ALLOW_LIVE', 'false') != 'false'):
            raise ValueError
        # parse_stream does not interpolate variables or evaluate shell syntax.
        return ManualCredentials(values.get('PHEMEX_API_KEY'), values.get('PHEMEX_API_SECRET'))
    except Exception:
        raise SafetyError('Explicit Testnet credential source unavailable or unsafe; details withheld') from None


def _wire(url, method, headers, body):
    entry = _validate_wire(url, method, headers, body)
    if entry not in MANUAL_PLAN:
        raise SafetyError('Manual status blocked an unapproved endpoint')
    return entry


def _safe_failure(error):
    message = 'Manual Testnet request failed; private details withheld; no retry'
    if isinstance(error, requests.exceptions.Timeout):
        return ccxt.RequestTimeout(message)
    if isinstance(error, requests.exceptions.RequestException):
        return ccxt.NetworkError(message)
    if isinstance(error, (ccxt.BaseError, SafetyError)):
        return type(error)(message)
    return SafetyError(message)


class _StatusSession(requests.Session):
    """Final prepared-request guard, also reached by inherited CCXT fetch paths."""
    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self.trust_env = False
        self.verify, self.max_redirects = True, 0
        self.headers.clear()
        self.proxies.clear()
        self.adapters.clear()
        self.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))

    def resolve_redirects(self, *args, **kwargs):
        return iter(())  # Also prevent Requests' hidden redirect preparation.

    def request(self, method, url, **kwargs):
        if (kwargs.get('proxies') not in (None, {}) or kwargs.get('verify', True) is not True
                or kwargs.get('files') is not None or kwargs.get('timeout') != TIMEOUT):
            raise SafetyError('Unexpected manual HTTP configuration')
        kwargs.update(proxies={}, verify=True, allow_redirects=False, stream=True)
        return super().request(method, url, **kwargs)

    def send(self, request, **kwargs):
        owner = self._owner
        with owner._lock:
            owner._check()
            if (type(request) is not requests.PreparedRequest or request.hooks != {'response': []}
                    or self.trust_env is not False or self.verify is not True or self.proxies != {}
                    or self.headers != {} or self.auth is not None or self.cookies
                    or self.params or self.cert is not None or self.max_redirects != 0
                    or kwargs.get('verify') is not True or kwargs.get('allow_redirects') is not False
                    or kwargs.get('proxies') != {} or kwargs.get('stream') is not True
                    or kwargs.get('timeout') != TIMEOUT or kwargs.get('cert') is not None
                    or set(self.adapters) != {'https://'}
                    or type(self.adapters['https://']) is not requests.adapters.HTTPAdapter
                    or self.adapters['https://'].max_retries.total != 0):
                raise SafetyError('Manual HTTP boundary rejected unsafe settings')
            entry = _wire(request.url, request.method, dict(request.headers), request.body)
            stamp = _fingerprint(request.url, request.method, dict(request.headers), request.body)
            if owner._index >= 3 or MANUAL_PLAN[owner._index] != entry or owner._pending != stamp:
                raise SafetyError('Manual request is unbound, duplicated or outside the plan')
            owner._pending = None
            owner._index += 1  # Irreversible before send, including timeout/error.
            response = None
            try:
                response = super().send(request, **kwargs)
                status = response.status_code if type(response) is requests.Response else None
                owner.last_read_diagnostic = ResponseEvidence(
                    status if type(status) is int and 100 <= status <= 599 else None)
                if (type(response) is not requests.Response or response.url != request.url
                        or response.history or type(response.status_code) is not int
                        or response.status_code not in (200, *range(400, 600))
                        or response.headers.get('Content-Encoding', 'identity').lower() not in ('identity', '')):
                    raise SafetyError('Unexpected HTTP status, redirect or encoding')
                data = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if type(chunk) is not bytes or len(data) + len(chunk) > MAX_BYTES:
                        raise SafetyError('Manual response exceeded its fixed size limit')
                    data.extend(chunk)
                bytes(data).decode('utf-8')  # Strict encoding before CCXT text handling.
                detached = requests.Response()
                detached.url, detached.status_code = response.url, response.status_code
                detached.encoding, detached._content = 'utf-8', bytes(data)
                return detached
            except Exception as error:
                owner._failed = True
                raise _safe_failure(error) from None
            finally:
                if type(response) is requests.Response:
                    response.close()


class ManualTestnetPhemex(ReadOnlyPhemex):
    """Three sequential GETs; caller trusted, not an in-process Python sandbox."""
    def __init__(self, credentials):
        self._ready, self._running, self._used, self._failed = False, None, False, False
        self._index, self._pending = 0, None
        self._public_catalog, self._legacy_catalog = None, None
        self._lock = RLock()
        self._guard_session = _StatusSession(self)
        if type(credentials) is not ManualCredentials:
            raise SafetyError('Explicit Testnet credentials required')
        super().__init__({'apiKey': credentials.key, 'secret': credentials.secret,
            'session': self._guard_session, 'timeout': TIMEOUT * 1000, 'enableRateLimit': True,
            'fetchHistoryCacheSize': 0, 'options': {'defaultType': 'swap', 'maxRetriesOnFailure': 0}})
        self.number = Decimal
        self.set_sandbox_mode(True)
        self._private_output_off()
        self._ready = True

    def _private_output_off(self):
        super()._private_output_off()
        self.fetchHistoryCacheSize = 0
        self.fetchHistoryCache = deque(maxlen=0)
        self.returnResponseHeaders = False
        self.last_request_headers = self.last_request_body = self.last_request_url = None

    def set_last_request(self, request):
        self.last_request_headers = self.last_request_body = self.last_request_url = None

    def add_fetch_cache(self, data):
        self.fetchHistoryCache = deque(maxlen=0)

    def get_fetch_cache(self):
        return deque(maxlen=0)

    def begin(self):
        with self._lock:
            if self._used or not self._ready:
                raise SafetyError('Manual collection is single-use')
            self._used, self._running = True, get_ident()

    def _check(self):
        if (not self._ready or self._failed or self._running != get_ident()
                or self.session is not self._guard_session or not self.isSandboxModeEnabled
                or self.verify is not True or self.validateServerSsl is not True
                or self.options.get('maxRetriesOnFailure') != 0
                or self.headers not in (None, {})
                or any(getattr(self, p, None) is not None for p in PROXY_OPTIONS)
                or any(c.debuglevel for c in (http.client.HTTPConnection, http.client.HTTPSConnection,
                                              HTTPConnection, HTTPSConnection))):
            raise SafetyError('Manual Testnet collection inactive or unsafe')

    def request(self, path, api='public', method='GET', params=None, config=None):
        self._check()
        if params is not None and type(params) is not dict:
            raise SafetyError('Manual query parameters must be an exact mapping')
        return super().request(path, api, method, params, config)

    def fetch2(self, path, api=('public',), method='GET', params=None, config=None):
        with self._lock:
            self._check()
            api = [api] if type(api) is str else api
            entry = MANUAL_PLAN[self._index] if self._index < 3 else None
            values = dict(params or {})
            retries = values.pop('maxRetriesOnFailure', 0)
            if (entry is None or list(api) != [entry.api] or path != entry.path or method != 'GET'
                    or values != dict(entry.query) or type(retries) is not int or retries != 0
                    or config not in (None, {}, {'cost': 25 if entry == MANUAL_PLAN[2] else 5})):
                raise SafetyError('Manual CCXT route is outside the fixed GET plan')
            self._private_output_off()
            try:
                return super().fetch2(path, list(api), method, values, config or {})
            except Exception as error:
                self._failed = True
                raise _safe_failure(error) from None

    def fetch(self, url, method='GET', headers=None, body=None):
        with self._lock:
            self._check()
            _wire(url, method, headers, body)
            if self._pending is not None:
                raise SafetyError('Nested manual request rejected')
            self._private_output_off()
            self.logger = logging.Logger('manual.testnet.private', level=logging.CRITICAL + 1)
            self.logger.disabled, self.logger.propagate = True, False
            self.userAgent = None
            self._pending = _fingerprint(url, method, headers, body)
            try:
                return super().fetch(url, method, headers, body)
            except Exception as error:
                self._failed = True
                raise _safe_failure(error) from None
            finally:
                self._pending = None
                self._private_output_off()

    def prepare_request_headers(self, headers=None):
        # CCXT normally adds compression and user-agent headers here. Keep the
        # exact reviewed public/three-header private wire form instead.
        return dict(headers or {})

    def parse_swap_market(self, market):
        # Pure parsing only. Keep malformed/unfamiliar products in the native
        # inventory for an explicit UNSUPPORTED result instead of losing them.
        try:
            return super().parse_swap_market(market)
        except Exception:
            return None

    def parse_spot_market(self, market):
        try:
            return super().parse_spot_market(market)
        except Exception:
            return None

    def parse_json(self, text):
        value = decode_response_json(text, exact_numbers=True)
        entry = MANUAL_PLAN[self._index - 1]
        expected = list if entry == MANUAL_PLAN[1] else dict
        if (type(value) is not dict or type(value.get('code')) not in (int, str)
                or value['code'] not in (0, '0') or value.get('error')
                or type(value.get('data')) is not expected):
            raise SafetyError('Unsuccessful or malformed manual response envelope')
        if entry == MANUAL_PLAN[2]:
            data = value['data']
            if (type(data.get('account')) is not dict or data['account'].get('currency') != 'USDT'
                    or type(data.get('positions')) is not list):
                raise SafetyError('Private USDT response scope or inventory unavailable')
        elif entry == MANUAL_PLAN[0]:
            self._public_catalog = value['data']
        elif entry == MANUAL_PLAN[1]:
            self._legacy_catalog = value['data']
        return value

    def close(self, *args, **kwargs):
        self._ready, self._running, self._failed = False, None, True
        self._pending = None
        self.apiKey = self.secret = None
        self._private_output_off()
        super().close(*args, **kwargs)


@dataclass(frozen=True)
class LegStatus:
    side: str
    quantity: Fact = field(default_factory=unknown)
    leverage: Fact = field(default_factory=unknown)
    margin: Fact = field(default_factory=unknown)
    mode: Fact = field(default_factory=unknown)
    grid_compatible: Fact = field(default_factory=unknown)
    contracts: Fact = field(default_factory=unknown)


@dataclass(frozen=True)
class ManualStatus:
    started_at: str
    completed_at: str
    authenticated: bool
    legs: tuple
    issues: tuple
    market: object = None
    catalog: MarketCatalog = field(default_factory=MarketCatalog)
    list_only: bool = False

    @property
    def execution_authorized(self):
        return False

    @property
    def may_resolve_journal(self):
        return False

    @property
    def complete(self):
        if self.list_only:
            return bool(self.catalog.markets) and not self.catalog.issues and not self.issues
        return self.authenticated and not self.issues and all(
            f.value is not None for leg in self.legs for f in (leg.quantity, leg.leverage, leg.margin, leg.mode)
        ) and all(leg.grid_compatible.value is True for leg in self.legs)

    def render(self):
        def display(f):
            return str(f) if f.value is not None else 'UNKNOWN (' + f.reason + ')'
        lines = ['MANUAL PHEMEX TESTNET STATUS — READ ONLY; DEVELOPMENT USE',
                 'Authentication: ' + ('successful private Testnet response' if self.authenticated else 'UNKNOWN'),
                 'Observation window (UTC): ' + self.started_at + ' -> ' + self.completed_at]
        if self.list_only or self.market is None:
            lines.append(self.catalog.render())
        if self.market is not None:
            m = self.market
            lines += [f'{m.native} / {m.symbol}; quantities in {m.base} base units.',
                      f'Contract size={display(m.contract_size)} {m.base}; quantity step={display(m.quantity_step)} {m.base}; '
                      f'price tick={display(m.price_tick)} USDT; minimum quantity={display(m.minimum_quantity)}.']
        for leg in (() if self.list_only else self.legs):
            lines.append(f'{leg.side}: quantity={display(leg.quantity)}; leverage={display(leg.leverage)}; '
                         f'margin={display(leg.margin)}; position_mode={display(leg.mode)}; '
                         f'contracts={display(leg.contracts)}; quantity_grid={display(leg.grid_compatible)}')
        if self.market is not None:
            unsupported = [m for m in self.catalog.markets if m.status == 'unsupported']
            if unsupported:
                lines.append(f'Catalog: {len(unsupported)} unsupported products; inspect --markets for details.')
            lines.extend('Catalog issue: ' + issue for issue in self.catalog.issues)
        lines += ['Issue: ' + issue for issue in self.issues]
        lines += ['Accounting, account-wide funds, stable account identity and order coverage: UNKNOWN.',
                  'Single sequential snapshot; no order acceptance or trading safety established.',
                  'No exchange mutations; no execution authority or journal resolution.']
        return '\n'.join(lines)



def _interpret(exchange, data, market):
    missing = lambda reason: tuple(LegStatus(s, *(unknown(reason) for _ in range(4))) for s in ('Long','Short'))
    if (type(data) is not dict or type(data.get('account')) is not dict
            or data['account'].get('currency') != 'USDT' or type(data.get('positions')) is not list):
        return missing('USDT account/position scope unavailable'), ('Invalid USDT position response scope',)
    raw_rows = data['positions']
    if any(type(r) is not dict or type(r.get('symbol')) is not str for r in raw_rows):
        return missing('ambiguous position inventory'), ('Malformed position row',)
    selected = [r for r in raw_rows if r['symbol'] == market.native]
    if any(r.get('posSide') not in ('Long','Short') or r.get('posMode') != 'Hedged'
           or r.get('currency') not in (None,'USDT') for r in selected):
        return missing('consistent selected-market Hedge Mode not established'), ('Position mode, leg or currency unavailable/conflicting',)
    reader = AccountDiscovery(exchange)  # Interpret only; never call discover().
    legs, issues = [], []
    for side in ('Long','Short'):
        rows = [r for r in selected if r['posSide'] == side]
        if len(rows) != 1:
            legs.append(LegStatus(side, *(unknown('missing or duplicate leg') for _ in range(4))))
            issues.append(side + ': missing or duplicate leg')
            continue
        parsed = reader.position(rows[0])
        if parsed['base_quantity'].value and parsed['direction'].value != side.lower():
            legs.append(LegStatus(side, *(unknown('contradictory position direction') for _ in range(4))))
            issues.append(side + ': contradictory position direction')
            continue
        quantity = parsed['base_quantity']
        # Raw base quantity is an observation, not proof of grid compatibility.
        grid = on_grid(quantity.value, market.quantity_step.value)
        grid_fact = Fact(True, 'derived', 'sizeRq / native quantity step', '') if grid else unknown(
            'position quantity is missing, off-grid or exceeds verified arithmetic bounds')
        contracts = Fact(quantity.value, 'derived', 'base quantity / reviewed contractSize=1', '') if grid else unknown(
            'contract quantity interpretation unavailable')
        legs.append(LegStatus(side, quantity, parsed['configured_leverage'],
                              parsed['margin_mode'], parsed['position_mode'], grid_fact, contracts))
    issues.extend(reader.report.issues)
    if data.get('nextPageArg'):
        issues.append('Position inventory is paginated/incomplete')
    return tuple(legs), tuple(issues)


def collect_manual_status(credentials, *, symbol='BTCUSDT', list_markets=False, clock=None):
    """One explicit manual attempt. Actual-origin display, never trusted policy evidence.

    Offline tests mock the HTTP adapter; no provenance label authenticates data.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock().isoformat()
    legs = (LegStatus('Long'), LegStatus('Short'))
    issues, authenticated, exchange = (), False, None
    catalog, market = MarketCatalog(), None
    try:
        if not valid_selector(symbol) or type(list_markets) is not bool:
            raise SafetyError('Invalid market selection')
        if ccxt.__version__ != '4.5.90':
            raise SafetyError('Manual CCXT version requires review')
        exchange = ManualTestnetPhemex(credentials)
        exchange.begin()
        # fetch_markets performs exactly the two reviewed public GETs. Avoid
        # load_markets(), whose implicit fetch_currencies adds another request.
        parsed = exchange.fetch_markets()
        catalog = build_catalog(exchange._public_catalog, exchange._legacy_catalog, parsed, exchange.precisionMode)
        if not list_markets:
            market = catalog.resolve(symbol)
            if catalog.issues:
                raise SafetyError('Incomplete public product inventory; interpretation unavailable')
            # Register only audited supported markets. No duplicate coalescing or
            # unrelated malformed product may change the selected mapping.
            supported_keys = {(m.native, m.symbol) for m in catalog.markets if m.supported}
            exchange.set_markets([m for m in parsed if m.get('type') == 'swap'
                                 and (m.get('id'), m.get('symbol')) in supported_keys])
            value = exchange.privateGetGAccountsPositions({'currency':'USDT'})
            authenticated = True  # Accepted HTTP 200 and successful private envelope only.
            legs, issues = _interpret(exchange, value['data'], market)
    except Exception as error:
        evidence = exchange.last_read_diagnostic if exchange is not None else None
        issues = (str(diagnose(error, evidence)),)
        if exchange is not None and not catalog.markets and exchange._public_catalog is not None:
            catalog = build_catalog(exchange._public_catalog, exchange._legacy_catalog, [], exchange.precisionMode)
    finally:
        if exchange is not None:
            exchange.close()
    return ManualStatus(started, clock().isoformat(), authenticated, legs, issues, market, catalog, list_markets)
