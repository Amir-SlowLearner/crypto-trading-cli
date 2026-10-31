"""Checkpoint 3 Phase A: observational compatibility, never execution authority.

Phase A blocks real transport unconditionally. Tests replace only the final
HTTP send with mocked responses. No credential loader or order route exists.
"""
from dataclasses import dataclass
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from fractions import Fraction
import hashlib
import re

import ccxt

from account_discovery import AccountDiscovery, reported, unknown
from diagnostics import ResponseEvidence, diagnose
from instrument_specs import inspect_instrument
from readonly_exchange import READ_ROUTES, ReadOnlyPhemex
from safety import SafetyError
from testnet_transport import (HOST, HTTP_SUCCESS_STATUS, PLAN, BlockedSession, OfflineGetTransport,
                               check_transport_configuration, decode_response_json)


AUTHORIZATION = 'AUTHORIZE TESTNET READ ONLY BTCUSDT'
# Strict subset of the existing allowlist; fixed parameters, no caller additions.
ROUTES = {(entry.api, entry.path): dict(entry.query) for entry in PLAN}
SOURCES = {
    'instrument': 'public GET /public/products + /v1/exchange/public/products',
    'risk_mode': 'authenticated GET /uta-api/risk/risk-mode',
    'account_positions': 'authenticated GET /g-accounts/positions currency=USDT',
    'identity_crosscheck': 'authenticated GET /g-accounts/accountPositions currency=USDT',
    'risk_units': 'authenticated GET risk-unit (accounting-dependent route)',
    'asset': 'authenticated GET /uta-biz/assets currency=USDT',
}


def _utc(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SafetyError('Compatibility verification requires an aware clock')
    return value.astimezone(timezone.utc)


class VerificationPhemex(ReadOnlyPhemex):
    """Existing GET guard plus testnet-only scope and an absolute Phase A block."""
    def __init__(self, api_key, api_secret):
        super().__init__({'apiKey': api_key, 'secret': api_secret, 'timeout': 15000,
                          'enableRateLimit': True, 'options': {'maxRetriesOnFailure': 0}})
        self.set_sandbox_mode(True)
        self.session.close()
        self.session = BlockedSession()
        self._transport = OfflineGetTransport()
        self._attempted = set()
        self._reads_authorized = False
        self.metadata_valid = False
        self._loading_metadata = False
        self._public_products = None

    def check_environment(self):
        expected = {'v1': HOST + '/v1', 'v2': HOST,
                    'public': HOST + '/exchange/public', 'private': HOST}
        if (ccxt.__version__ != '4.5.90' or self.isSandboxModeEnabled is not True
                or self.urls.get('api') != expected):
            raise SafetyError('Verification requires the pinned testnet endpoint profile')
        check_transport_configuration(self)

    def fetch(self, url, method='GET', headers=None, body=None):
        self.check_environment()
        try:
            response = self._transport.dispatch(self, url, method, headers, body)
            # Preserve only status until the entire raw JSON body passes the
            # duplicate-key check, before CCXT diagnostics/error interpretation.
            self.last_read_diagnostic = ResponseEvidence(response.status_code)
            payload = decode_response_json(response.content)
            text = response.content.decode('utf-8')
            self.on_rest_response(response.status_code, '', url, method, response.headers,
                                  text, headers, body)
            self.handle_errors(response.status_code, '', url, method, response.headers,
                               text, payload, headers, body)
            if response.status_code >= 400:
                self.handle_http_status_code(response.status_code, '', url, method, text)
            if response.status_code != HTTP_SUCCESS_STATUS:
                # CCXT's HTTP exception map is not exhaustive (e.g. HTTP 499).
                raise ccxt.ExchangeError('HTTP status cannot establish a successful GET')
            if payload is None:
                raise SafetyError('Testnet read returned an unsupported response')
            return payload
        except ccxt.BaseError as error:
            # Preserve the reviewed error class and numeric diagnostics, never
            # CCXT's exception text, raw response or signed request material.
            if self.last_read_diagnostic.http_status is None:
                receipt = next((item for item in self._transport.receipts.values() if item.url == url), None)
                if receipt is not None:
                    self.last_read_diagnostic = ResponseEvidence(receipt.http_status)
            raise type(error)('Testnet GET failed; private details withheld') from None
        except SafetyError:
            raise SafetyError('Testnet GET was blocked; private details withheld') from None
        except Exception:
            raise SafetyError('Testnet GET failed; private details withheld') from None

    def fetch2(self, path, api='public', method='GET', params=None, config=None):
        # All normal CCXT implicit/direct paths re-enter the same route/plan gate.
        return self.request(path, api, method, params, config)

    def set_last_request(self, request):
        # CCXT's default retains signed headers even with response caches off.
        self.last_request_headers = self.last_request_body = self.last_request_url = None

    @property
    def fetchHistoryCache(self):
        # A discard-only view also protects direct inherited cache writers/getters.
        return deque(maxlen=0)

    @fetchHistoryCache.setter
    def fetchHistoryCache(self, value):
        # CCXT's base writer tries to replace an empty cache with a nonzero deque.
        # Never retain that replacement, regardless of fetchHistoryCacheSize.
        pass

    def add_fetch_cache(self, data):
        # Base fetch2 calls this on both success and exception paths. In particular,
        # do not keep an exception object whose traceback holds private locals.
        pass

    def log(self, *args):
        # Even an unexpected inherited verbose path must not print signing data.
        pass

    def request(self, path, api='public', method='GET', params=None, config=None):
        self.check_environment()
        api = [api] if type(api) is str else api
        key = ((api[0], path) if type(api) in (list, tuple) and len(api) == 1
               and type(api[0]) is str and type(path) is str else None)
        cost = 25 if key == ('private', 'g-accounts/positions') else (
            1 if key in (('private', 'g-accounts/accountPositions'), ('private', 'g-accounts/risk-unit')) else 5)
        if (not self._reads_authorized or method != 'GET' or key not in ROUTES
                or key not in READ_ROUTES or params is not None and type(params) is not dict
                or (params or {}) != ROUTES[key]
                or config not in (None, {}, {'cost': cost})):
            raise SafetyError('Verification blocked an unreviewed request or parameters')
        if key in self._attempted:
            # CCXT load_markets asks for products once for currencies and again
            # for markets. Reuse only this run's same successful public response;
            # never issue a second GET or reuse a private response/failure.
            if key == ('v2', 'public/products') and self._loading_metadata and self._public_products is not None:
                return deepcopy(self._public_products)
            raise SafetyError('Verification permits one attempt per reviewed endpoint')
        self._attempted.add(key)
        self.reset_read_diagnostic()
        self._private_output_off()
        if self.enableRateLimit:
            self.throttle(self.calculate_rate_limiter_cost(list(api), method, path, params or {}, config or {}))
        signed = super().sign(path, list(api), method, dict(params or {}))
        with self._transport.signed_request(signed):
            response = self.fetch(signed['url'], signed['method'], signed['headers'], signed['body'])
        if key[0] in ('v1', 'v2'):
            expected = dict if key[0] == 'v2' else list
            if (type(response) is not dict or type(response.get('code')) not in (int, str)
                    or response['code'] not in (0, '0') or response.get('error')
                    or type(response.get('data')) is not expected):
                raise SafetyError('Public instrument response is unavailable or malformed')
        if key == ('v2', 'public/products'):
            data = response.get('data') if type(response) is dict else None
            rows = data.get('perpProductsV2') if type(data) is dict else None
            self.metadata_valid = (type(response) is dict and type(response.get('code')) in (int, str)
                and response['code'] in (0, '0') and not response.get('error')
                and type(rows) is list
                and sum(type(row) is dict and row.get('symbol') == 'BTCUSDT' for row in rows) == 1)
            self._public_products = deepcopy(response) if self.metadata_valid and self._loading_metadata else None
        return response


@dataclass(frozen=True)
class Observation:
    started_at: datetime
    completed_at: datetime
    success: bool
    diagnostic: str = ''

    def fresh(self, now, seconds):
        return (self.success and self.started_at <= self.completed_at <= now
                and now - self.started_at < timedelta(seconds=seconds))


def _identifier(row, names):
    """Only equality tokens survive; no identifier or digest enters a report."""
    values = [row[name] for name in names if name in row]
    if not values or any(type(v) not in (int, str) or not re.fullmatch('[1-9][0-9]{0,31}', str(v)) for v in values):
        return None
    values = {str(v) for v in values}
    return hashlib.sha256(next(iter(values)).encode()).digest() if len(values) == 1 else None


def _identity(data):
    account = data.get('account')
    rows = data.get('positions')
    if type(account) is not dict or account.get('currency') != 'USDT' or type(rows) is not list:
        return None
    account_id = _identifier(account, ('accountId', 'accountID'))
    user_id = _identifier(account, ('userID', 'userId'))
    if account_id is None or user_id is None:
        return None
    for row in rows:
        if type(row) is not dict or _identifier(row, ('accountId', 'accountID')) != account_id:
            return None
        if any(name in row for name in ('userID', 'userId')):
            if _identifier(row, ('userID', 'userId')) != user_id:
                return None
    return account_id, user_id


def _number(value):
    if type(value) not in (str, int, Decimal):
        return None  # A decoded float may already have lost native precision.
    if len(str(value)) > 128:
        return None
    try:
        number = Decimal(value)
        return number if (number.is_finite() and len(number.as_tuple().digits) <= 40
                          and abs(number.adjusted()) <= 40) else None
    except (ValueError, ArithmeticError):
        return None


@dataclass(frozen=True)
class _LegContext:
    side: object
    mode: object
    direction: object
    base_quantity: object
    margin_mode: object
    configured_leverage: object
    status: object
    direction_consistent: bool


@dataclass(frozen=True)
class _PositionContext:
    scope: str
    complete: bool
    legs: tuple

    def hedge_legs(self):
        return (self.complete and len(self.legs) == 2
                and {leg.side for leg in self.legs} == {'Long', 'Short'}
                and all(leg.mode == 'Hedged' and leg.direction_consistent for leg in self.legs))

    def one_way(self):
        return (self.complete and len(self.legs) == 1 and self.legs[0].side == 'Merged'
                and self.legs[0].mode == 'OneWay' and self.legs[0].direction_consistent)


def _scope(*objects, require_account=False):
    """Validate reviewed scope fields without retaining or displaying raw values."""
    states = []
    if require_account and (type(objects[0]) is not dict or 'currency' not in objects[0]):
        states.append('unknown')
    for obj in objects:
        if type(obj) is not dict:
            states.append('unknown')
            continue
        for key, expected in (('currency', 'USDT'), ('settleCurrency', 'USDT'),
                              ('quoteCurrency', 'USDT'), ('symbol', 'BTCUSDT')):
            if key in obj and obj[key] != expected:
                states.append('incompatible' if type(obj[key]) is str
                              and re.fullmatch('[A-Z0-9]{2,20}', obj[key]) else 'unknown')
    return 'incompatible' if 'incompatible' in states else 'unknown' if states else 'verified'


def _position_context(data):
    # Each field is parsed independently: absent quantity must never erase an
    # explicitly contradictory mode, side, leverage, margin or status.
    scope = _scope(data.get('account'), data, require_account=True)
    rows = data.get('positions')
    complete = type(rows) is list and data.get('nextPageArg') in (None, '')
    result = []
    for row in rows if type(rows) is list else ():
        if type(row) is not dict or type(row.get('symbol')) is not str or not row['symbol']:
            complete = False  # An unassignable row might conceal a selected leg.
            continue
        if row['symbol'] != 'BTCUSDT':
            continue
        row_scope = _scope(row)
        if row_scope == 'incompatible' or scope == 'incompatible':
            scope = 'incompatible'
        elif row_scope == 'unknown':
            scope = 'unknown'
        side = row.get('posSide') if row.get('posSide') in ('Long', 'Short', 'Merged') else None
        mode = row.get('posMode') if row.get('posMode') in ('Hedged', 'OneWay') else None
        quantities = [_number(row[key]) for key in ('sizeRq', 'size') if key in row]
        size = quantities[0] if quantities and all(q is not None and q >= 0 and q == quantities[0]
                                                  for q in quantities) else None
        leverage = _number(row.get('leverageRr'))
        sign_mode = None if leverage in (None, 0) else 'cross' if leverage < 0 else 'isolated'
        if 'crossMargin' not in row:
            margin = sign_mode
        elif type(row['crossMargin']) is bool:
            margin = 'cross' if row['crossMargin'] else 'isolated'
            if sign_mode is not None and sign_mode != margin:
                margin = None
        else:
            margin = None
        direction = row.get('side')
        direction_ok = (direction in ('Buy', 'Sell', 'None')
                        and not (size is not None and size > 0 and direction == 'None')
                        and not (size != 0 and side in ('Long', 'Short')
                                 and direction in ('Buy', 'Sell')
                                 and direction != ('Buy' if side == 'Long' else 'Sell')))
        status = {'Normal': 'normal', 'Liq': 'liquidation', 'Adl': 'adl'}.get(
            row.get('positionStatus')) if type(row.get('positionStatus')) is str else None
        result.append(_LegContext(side, mode,
                                  direction if direction in ('Buy', 'Sell', 'None') else None, size, margin,
                                  leverage.copy_abs() if leverage not in (None, 0) else None,
                                  status, direction_ok))
    return _PositionContext(scope, complete, tuple(result))


class _Discovery(AccountDiscovery):
    """Reuse discovery's parsing/error policy, retaining only safe receipts."""
    def __init__(self, exchange, clock):
        super().__init__(exchange)
        self.clock = clock
        self.observations = {}
        self.identities = {}  # Ephemeral digest pairs; never rendered or persisted.
        self.contexts = {}

    def read(self, label, method, params, expected):
        started = _utc(self.clock())
        data = super().read(label, method, params, expected)
        failure = self._last_failure
        receipt = self.ex._transport.receipts.get(label)
        usable_receipt = (receipt is not None and receipt.provenance == 'synthetic_http_fixture'
                          and type(receipt.http_status) is int and receipt.http_status == HTTP_SUCCESS_STATUS
                          and any(item.label == label and item.url == receipt.url for item in PLAN))
        self.observations[label] = Observation(receipt.started_at if usable_receipt else started,
            receipt.completed_at if usable_receipt else _utc(self.clock()), data is not None and usable_receipt,
            str(failure) if failure else ('skipped after a conservative stop' if data is None else ''))
        if failure and failure.category == 'permission':
            self.stopped = True  # Verifier is stricter; existing discovery is unchanged.
        if label in ('account_positions', 'identity_crosscheck') and data is not None:
            self.identities[label] = _identity(data)
            self.contexts[label] = _position_context(data)
        if label == 'account_positions' and data is not None:
            self.read('identity_crosscheck', self.ex.privateGetGAccountsAccountPositions,
                      {'currency': 'USDT'}, dict)
        return data

    def discover(self):
        # Reuse existing GET/error/position parsing, never its broader risk/account
        # request sequence. This verifier has exactly two private planned reads.
        data = self.read('account_positions', self.ex.privateGetGAccountsPositions,
                         {'currency': 'USDT'}, dict)
        if data is not None and type(data.get('positions')) is list:
            self.report.positions_available = True
            self.report.positions_complete = not bool(data.get('nextPageArg'))
            self.report.positions = [self.position(row) for row in data['positions']]
            self.validate_positions()
        self.report.wallet = self.unknown_wallet()
        if not self.authentication_failed and self.successes:
            self.report.authenticated = reported(True, 'successful authenticated GET envelope')
        return self.report


class Status(str, Enum):
    VERIFIED = 'VERIFIED'
    UNKNOWN = 'UNKNOWN'
    INCOMPATIBLE = 'INCOMPATIBLE'


@dataclass(frozen=True)
class Requirement:
    name: str
    status: Status
    value: str
    provenance: str
    source: str
    observed_at: str
    reason: str


@dataclass(frozen=True)
class CompatibilityReport:
    checked_at: str
    synthetic: bool
    requirements: tuple
    diagnostics: tuple

    def __post_init__(self):
        if self.synthetic is not True:
            raise SafetyError('Offline compatibility reports must remain synthetic')

    @property
    def execution_authorized(self):
        return False

    def render(self):
        lines = ['BTCUSDT READ-ONLY COMPATIBILITY — NO EXECUTION AUTHORITY',
                 'SYNTHETIC MOCK REPORT — NOT EXCHANGE VERIFICATION',
                 'Checked at: ' + self.checked_at,
                 'Statuses are evaluated at checked_at only; this report is not reusable execution evidence.']
        for row in self.requirements:
            lines.append(f'{row.status.value} {row.name}: {row.value}; {row.reason} '
                         f'[{row.provenance}; {row.source}; observed={row.observed_at}]')
        lines.extend(self.diagnostics)
        lines.append('No orders submitted. All mutations remain disabled. Phase B has not been run by this code.')
        return '\n'.join(lines)


class CompatibilityVerifier:
    def __init__(self, exchange, *, clock=None):
        if type(exchange) is not VerificationPhemex:
            raise SafetyError('Verifier requires the restricted testnet read-only adapter')
        self.ex = exchange
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._used = False

    def collect(self, *, authorization=None, synthetic=True):
        if authorization != AUTHORIZATION or synthetic is not True or self._used:
            raise SafetyError('One explicit testnet read-only authorization is required per verifier')
        self.ex.check_environment()
        started = _utc(self.clock())
        self._used = True
        self.ex._transport.begin(self.clock)
        self.ex._reads_authorized = True
        reader = _Discovery(self.ex, self.clock)
        instrument_error = ''
        try:
            try:
                self.ex._loading_metadata = True
                try:
                    self.ex.load_markets(reload=True)
                finally:
                    self.ex._loading_metadata = False
                    self.ex._public_products = None
                specs = inspect_instrument(self.ex, 'BTCUSDT') if self.ex.metadata_valid else None
            except Exception as error:
                # Any public failure halts collection; do not continue to private
                # signing when environment/instrument provenance is unavailable.
                specs = None
                instrument_error = ('Public instrument collection failed; private GETs were not attempted. '
                                    + str(diagnose(error, self.ex.last_read_diagnostic)))
            if specs is not None:
                native = self.ex.market('BTC/USDT:USDT').get('info') or {}
                for key, tick in (('quantity_step', 'contract_step'), ('price_tick', 'price_precision_tick')):
                    if specs[key].value != specs[tick].value:
                        specs[key] = unknown('inconsistent native and normalized instrument grids')
                for key, raw in (('quantity_step', 'qtyStepSize'), ('price_tick', 'tickSize'),
                                 ('contract_size', 'contractSize'), ('max_base_quantity', 'maxOrderQtyRq'),
                                 ('min_notional', 'minOrderValueRv')):
                    value = _number(native.get(raw))
                    if value is None or value <= 0:
                        specs[key] = unknown('native field unavailable, invalid or outside exact precision bounds')
            public_receipts = [self.ex._transport.receipts.get(label) for label in ('products', 'legacy_products')]
            public_collected = all(receipt is not None and receipt.provenance == 'synthetic_http_fixture'
                                   and type(receipt.http_status) is int and receipt.http_status == HTTP_SUCCESS_STATUS
                                   and receipt.label == spec.label and receipt.url == spec.url
                                   and receipt.started_at <= receipt.completed_at
                                   for receipt, spec in zip(public_receipts, PLAN[:2]))
            instrument_observation = Observation(
                public_receipts[0].started_at if public_collected else started,
                public_receipts[-1].completed_at if public_collected else _utc(self.clock()),
                specs is not None and public_collected)
            report = reader.discover() if specs is not None else reader.report
            return _report(report, specs, instrument_observation, reader, _utc(self.clock()), synthetic,
                           instrument_error)
        except Exception:
            raise SafetyError('Compatibility verification failed; no execution authority was issued') from None
        finally:
            self.ex._reads_authorized = False
            self.ex._transport.end()
            self.ex.markets = None  # No private responses or reusable market cache are retained here.
            reader.identities.clear()
            reader.contexts.clear()


def _report(account, specs, instrument_observation, reader, now, synthetic, instrument_error):
    rows = []
    step_fact = specs.get('quantity_step') if specs else None
    step = _number(step_fact.value) if step_fact else None
    grid_ready = (instrument_observation.fresh(now, 300) and step_fact is not None
                  and step_fact.origin == 'reported' and step is not None and step > 0)

    def add(name, status, value, label, reason, *, crosschecked=False, requires_grid=False):
        if label not in ('instrument', 'account_positions', 'identity_crosscheck'):
            rows.append(Requirement(name, Status.UNKNOWN, '-', 'not_collected',
                                    'Outside the approved four-GET plan', 'unavailable', reason))
            return
        observation = instrument_observation if label == 'instrument' else reader.observations.get(label)
        public = label == 'instrument'
        if observation is None or not observation.fresh(now, 300 if public else 5):
            status, value, reason = Status.UNKNOWN, '-', 'Observation missing, failed, stale or clock-inconsistent.'
        if crosschecked and not all(reader.observations.get(other)
                                   and reader.observations[other].fresh(now, 5)
                                   for other in ('account_positions', 'identity_crosscheck')):
            status, value, reason = Status.UNKNOWN, '-', 'Both fresh position observations are required.'
        if requires_grid and not grid_ready:
            status, value, reason = Status.UNKNOWN, '-', 'Current native quantity grid missing, invalid, contradictory or stale.'
        provenance = 'synthetic_public_fixture' if public else 'synthetic_authenticated_fixture'
        source = (SOURCES['account_positions'] + ' + ' + SOURCES['identity_crosscheck']
                  if crosschecked else SOURCES[label])
        if requires_grid:
            provenance = 'synthetic_private_and_public_fixture'
            source += ' + ' + SOURCES['instrument']
        rows.append(Requirement(name, status, value, provenance, source,
                                observation.completed_at.isoformat() if observation else 'unavailable', reason))

    def unknown(name, label, reason):
        add(name, Status.UNKNOWN, '-', label, reason)

    # Environment is configuration plus a successful response, not credential provenance.
    add('testnet_endpoint_profile', Status.VERIFIED, 'testnet only', 'instrument', 'Pinned sandbox URLs checked before each GET.')
    successful = [label for label, obs in reader.observations.items() if obs.fresh(now, 5)]
    auth_label = successful[0] if successful else 'account_positions'
    add('authentication', Status.VERIFIED if account.authenticated.value is True else Status.UNKNOWN,
        'successful authenticated GET' if account.authenticated.value is True else '-', auth_label,
        'Authentication does not establish stable account identity.')
    identity_ok = (all(reader.observations.get(label) and reader.observations[label].fresh(now, 5)
                       for label in ('account_positions', 'identity_crosscheck'))
                   and reader.identities.get('account_positions') is not None
                   and reader.identities.get('account_positions') == reader.identities.get('identity_crosscheck'))
    add('snapshot_identity_consistency', Status.VERIFIED if identity_ok else Status.UNKNOWN,
        'matching reported identity fields' if identity_ok else '-', 'account_positions',
        'Identifiers withheld; missing or conflicting identifiers never establish binding.')
    unknown('stable_account_binding', 'account_positions', 'No enrolled reference or reviewed cross-session/account binding protocol.')
    add('accounting_type', Status.VERIFIED if account.account_type.value else Status.UNKNOWN,
        account.account_type.value or '-', 'risk_mode', 'No Classic fallback; risk-unit scope is not account identity.')

    first, second = (reader.contexts.get(label) for label in ('account_positions', 'identity_crosscheck'))
    scope_ok = first is not None and second is not None and first.scope == second.scope == 'verified'
    incompatible_scope = any(context is not None and context.scope == 'incompatible'
                             for context in (first, second))
    add('position_account_scope', Status.VERIFIED if scope_ok else
        Status.INCOMPATIBLE if incompatible_scope else Status.UNKNOWN,
        'USDT' if scope_ok else '-', 'account_positions',
        'Both account currencies and all present reviewed envelope/selected-row scope fields must agree.',
        crosschecked=True)
    structure_matches = (scope_ok and ((first.hedge_legs() and second.hedge_legs())
                                      or (first.one_way() and second.one_way())))
    paired = ({leg.side: (leg, next(other for other in second.legs if other.side == leg.side))
               for leg in first.legs} if structure_matches else {})
    quantity_conflict = any(a.base_quantity is not None and b.base_quantity is not None
                            and a.base_quantity != b.base_quantity for a, b in paired.values())
    direction_conflict = any(a.direction != b.direction for a, b in paired.values())
    fields = ('base_quantity', 'margin_mode', 'configured_leverage', 'status')
    matching = (bool(paired) and not direction_conflict
                and all(getattr(a, key) is not None and getattr(a, key) == getattr(b, key)
                        for a, b in paired.values() for key in fields))
    add('position_crosscheck', Status.VERIFIED if matching else Status.UNKNOWN,
        'matching selected-symbol fields' if matching else '-', 'identity_crosscheck',
        'Mode, side, quantity, leverage, margin and status compared independently; missing is not agreement.',
        crosschecked=True)
    valid_legs = bool(structure_matches and first.hedge_legs() and not quantity_conflict and not direction_conflict)
    one_way = bool(structure_matches and first.one_way() and not quantity_conflict and not direction_conflict)
    add('explicit_hedge_legs', Status.VERIFIED if valid_legs else Status.INCOMPATIBLE if one_way else Status.UNKNOWN,
        'Long + Short' if valid_legs else 'one_way' if one_way else '-', 'account_positions',
        'Both explicit legs required; missing or contradictory context is unknown, never zero.', crosschecked=True)
    for leg in ('Long', 'Short'):
        pair = paired.get(leg) if valid_legs else None
        for key in fields:
            value = getattr(pair[0], key) if pair else None
            if pair is None or value != getattr(pair[1], key):
                value = None
            if key == 'base_quantity':
                add(leg + '.observed_base_quantity', Status.VERIFIED if value is not None else Status.UNKNOWN,
                    str(value) if value is not None else '-', 'account_positions',
                    'Raw reported base quantity, matching fresh private reads; no current grid compatibility claim.',
                    crosschecked=True)
                if (not grid_ready or _number(value) is None
                        or (Fraction(value) / Fraction(step)).denominator != 1):
                    value = None
            if key == 'configured_leverage' and (_number(value) is None or value <= 0):
                value = None
            add(leg + '.' + key, Status.VERIFIED if value is not None else Status.UNKNOWN,
                str(value) if value is not None else '-', 'account_positions',
                ('Configured leverage uses exact leverageRr absolute value; selected leg only.'
                 if key == 'configured_leverage' and value is not None else
                 'Margin uses crossMargin and/or documented leverageRr sign; selected leg only.'
                 if key == 'margin_mode' and value is not None else
                 'Base quantity checked exactly against the fresh native quantity step; no rounding or execution authority.'
                 if key == 'base_quantity' else
                 'Independent selected-leg fields must be present, valid and equal in both GETs.'),
                crosschecked=True, requires_grid=key == 'base_quantity')
        quantity_row = next(row for row in rows if row.name == leg + '.base_quantity')
        qty = Decimal(quantity_row.value) if quantity_row.status is Status.VERIFIED else None
        status_row = next(row for row in rows if row.name == leg + '.status')
        normal = status_row.status is Status.VERIFIED and status_row.value == 'normal'
        reducible = (Status.UNKNOWN if qty is None or status_row.status is not Status.VERIFIED else
                     Status.VERIFIED if qty > 0 and normal else Status.INCOMPATIBLE)
        add(leg + '.reducible_position', reducible,
            'positive quantity / normal status' if reducible is Status.VERIFIED else
            'flat or non-normal status' if reducible is Status.INCOMPATIBLE else '-', 'account_positions',
            'Grid-checked quantity and status must agree across GETs; missing never means flat.',
            crosschecked=True, requires_grid=True)

    for key in ('active', 'contract_size', 'quantity_step', 'price_tick', 'max_base_quantity', 'min_notional'):
        fact = specs.get(key) if specs else None
        value = fact.value if fact else None
        verified = value is not None and fact.origin == 'reported'
        if key == 'active':
            status = Status.VERIFIED if value is True else Status.INCOMPATIBLE if value is False else Status.UNKNOWN
        else:
            status = Status.VERIFIED if verified else Status.UNKNOWN
        add('instrument.' + key, status, str(value) if verified or key == 'active' and value is not None else '-',
            'instrument', 'Native/public metadata required; derived conventions are not native verification.')
    # The installed parse_swap_market leaves amount.min=None. Do not invent a
    # minimum from qtyStepSize or a guessed native field name.
    unknown('instrument.minimum_order_size', 'instrument', 'Reviewed CCXT market parser does not establish a native minimum quantity.')
    unknown('open_and_conditional_order_coverage', 'account_positions', 'Not collected; complete coverage remains an execution blocker.')
    unknown('account_restrictions', 'risk_units', 'No complete account-wide restriction/status attestation is established.')
    unknown('reduce_only_limit_IOC_acceptance', 'account_positions', 'GET observations cannot prove account-specific order acceptance.')
    unknown('execution_assessment', 'account_positions', 'Identity, minimum size, restrictions, order coverage and acceptance remain blockers.')
    diagnostics = tuple('Discovery: ' + item for item in account.issues)
    if instrument_error:
        diagnostics += (instrument_error,)
    for label, observation in reader.observations.items():
        if observation.diagnostic:
            diagnostics += (label + ': ' + observation.diagnostic,)
    return CompatibilityReport(now.isoformat(), synthetic, tuple(rows), diagnostics)
