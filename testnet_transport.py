"""Offline-only final HTTP boundary for the four-request compatibility plan.

There is no network implementation or runtime enable flag. Tests replace only
_send_http. Python method replacement is not an OS security boundary.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from threading import RLock, get_ident

import ccxt
import requests

from safety import SafetyError


HOST = 'https://testnet-api.phemex.com'
HTTP_SUCCESS_STATUS = 200  # These four GETs require a complete JSON success envelope.


def _unique_response_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            # Never include the field name or either private value in errors.
            raise ValueError('Duplicate JSON response field')
        result[key] = value
    return result


def decode_response_json(raw):
    """Reject ambiguous objects at every depth before interpreting evidence."""
    try:
        # Use one encoding and parser for worker acceptance and verifier evidence.
        text = raw.decode('utf-8') if type(raw) is bytes else raw
        if type(text) is not str:
            raise ValueError('Invalid JSON response type')
        return json.loads(text, object_pairs_hook=_unique_response_pairs)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise SafetyError('Response JSON is malformed or ambiguous; details withheld') from None


@dataclass(frozen=True)
class PlannedGet:
    api: str
    path: str
    query: tuple
    label: str

    @property
    def url(self):
        # These are fixed literals, not a general-purpose URL encoder.
        prefix = '/v1/' if self.api == 'v1' else '/'
        return HOST + prefix + self.path + ('?currency=USDT' if self.query else '')


PLAN = (
    PlannedGet('v2', 'public/products', (), 'products'),
    PlannedGet('v1', 'exchange/public/products', (), 'legacy_products'),
    PlannedGet('private', 'g-accounts/positions', (('currency', 'USDT'),), 'account_positions'),
    PlannedGet('private', 'g-accounts/accountPositions', (('currency', 'USDT'),), 'identity_crosscheck'),
)
AUTH_HEADERS = frozenset({'x-phemex-access-token', 'x-phemex-request-expiry',
                          'x-phemex-request-signature'})
PROXY_OPTIONS = (
    'proxy', 'proxyUrl', 'proxy_url', 'proxyUrlCallback', 'proxy_url_callback',
    'httpProxy', 'http_proxy', 'httpProxyCallback', 'http_proxy_callback',
    'httpsProxy', 'https_proxy', 'httpsProxyCallback', 'https_proxy_callback',
    'socksProxy', 'socks_proxy', 'socksProxyCallback', 'socks_proxy_callback',
    'wsProxy', 'ws_proxy', 'wssProxy', 'wss_proxy', 'wsSocksProxy', 'ws_socks_proxy',
    'aiohttp_proxy', 'proxies',
)


def _utc(clock):
    value = clock()
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SafetyError('Transport requires an aware collection clock')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TransportReceipt:
    label: str
    url: str
    started_at: datetime
    completed_at: datetime
    http_status: object
    provenance: str = 'synthetic_http_fixture'


class BlockedSession:
    """No Requests adapters, connection pool or HTTP-capable session is retained."""
    def __init__(self):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.proxies = {}
        self.trust_env = False
        self.verify = True
        self.max_redirects = 0

    def request(self, *args, **kwargs):
        raise SafetyError('Direct session transport is disabled')

    send = get = post = put = patch = delete = head = options = request

    def close(self):
        pass


def check_transport_configuration(exchange):
    if (type(exchange.session) is not BlockedSession
            or exchange.session.trust_env is not False or exchange.session.verify is not True
            or exchange.session.max_redirects != 0 or exchange.session.proxies != {}
            or exchange.session.headers != {} or exchange.headers not in (None, {})
            or exchange.verify is not True or exchange.validateServerSsl is not True
            or any(getattr(exchange, name, None) is not None for name in PROXY_OPTIONS)
            or exchange.options.get('maxRetriesOnFailure', 0) != 0):
        raise SafetyError('Transport requires verified TLS, no proxies, no extra headers and no retries')


def _validate_wire(url, method, headers, body):
    # Exact serialization rejects normalization, encodings, duplicate queries,
    # userinfo, fragments, alternate ports (including explicit :443) and hosts.
    spec = next((entry for entry in PLAN if type(url) is str and url == entry.url), None)
    if spec is None or method != 'GET' or body is not None:
        raise SafetyError('Transport blocked an unapproved URL, method or body')
    if headers is None:
        headers = {}
    if type(headers) is not dict or any(type(k) is not str or type(v) is not str for k, v in headers.items()):
        raise SafetyError('Transport blocked unexpected headers')
    if spec.api != 'private':
        if headers:
            raise SafetyError('Public transport must not carry caller or authentication headers')
    elif (set(headers) != AUTH_HEADERS
          or not re.fullmatch('[^\s\x00-\x1f\x7f]{1,512}', headers.get('x-phemex-access-token', ''))
          or not re.fullmatch('[0-9]{1,13}', headers.get('x-phemex-request-expiry', ''))
          or not re.fullmatch('[0-9a-f]{64}', headers.get('x-phemex-request-signature', ''))):
        raise SafetyError('Private transport requires exactly the reviewed Phemex signing headers')
    return spec


def _fingerprint(url, method, headers, body):
    # Ephemeral equality check only: neither signed material nor digest is reported.
    return hashlib.sha256(json.dumps((url, method, headers or {}, body), sort_keys=True).encode()).digest()


class OfflineGetTransport:
    def __init__(self):
        self._lock = RLock()
        self._owner = None
        self._used = False
        self._index = 0
        self._permit = None
        self._clock = None
        self.receipts = {}

    def begin(self, clock):
        with self._lock:
            if self._used:
                raise SafetyError('Transport plan is single-use')
            _utc(clock)
            self._used = True
            self._owner = get_ident()
            self._clock = clock

    def end(self):
        with self._lock:
            self._owner = None
            self._permit = None

    def _active(self):
        if self._owner != get_ident() or self._clock is None:
            raise SafetyError('No active bounded request plan for this thread')

    @contextmanager
    def signed_request(self, request):
        with self._lock:
            self._active()
            if self._permit is not None or type(request) is not dict or set(request) != {'url', 'method', 'headers', 'body'}:
                raise SafetyError('Transport requires one exact reviewed signed request')
            spec = _validate_wire(request['url'], request['method'], request['headers'], request['body'])
            if self._index >= len(PLAN) or spec != PLAN[self._index]:
                raise SafetyError('Request is outside the remaining ordered plan')
            self._permit = _fingerprint(request['url'], request['method'], request['headers'], request['body'])
            try:
                yield
            finally:
                self._permit = None

    def dispatch(self, exchange, url, method='GET', headers=None, body=None):
        with self._lock:
            self._active()
            check_transport_configuration(exchange)
            spec = _validate_wire(url, method, headers, body)
            if (self._index >= len(PLAN) or spec != PLAN[self._index] or self._permit is None
                    or _fingerprint(url, method, headers, body) != self._permit):
                raise SafetyError('Transport rejected an unbound, replayed or out-of-plan request')
            # Offline Requests preparation cannot normalize an approved URL into
            # a different destination or add unexpected business/auth headers.
            prepared = requests.Request('GET', url, headers=headers or {}).prepare()
            _validate_wire(prepared.url, prepared.method, dict(prepared.headers), prepared.body)
            if _fingerprint(prepared.url, prepared.method, dict(prepared.headers), prepared.body) != self._permit:
                raise SafetyError('Prepared HTTP request differs from the signed request')
            self._permit = None
            self._index += 1  # Consume before any send, including blocked/failed sends.
            started = _utc(self._clock)
            status = None
            try:
                response = self._send_http(prepared, timeout=15, verify=True,
                                           allow_redirects=False, proxies={})
                if (type(response) is not requests.Response or response.url != url
                        or response.history or type(response.status_code) is not int
                        or not 100 <= response.status_code <= 599):
                    raise SafetyError('Transport received an unexpected HTTP response')
                status = response.status_code
                if status != HTTP_SUCCESS_STATUS and status < 400:
                    raise ccxt.ExchangeError('Unapproved HTTP success or provisional/redirect response rejected')
                # Error responses reach only sanitized decoding/error handling;
                # their body can never turn them into a successful observation.
                return response
            except requests.exceptions.Timeout:
                raise ccxt.RequestTimeout('Testnet read timed out; no retry permitted') from None
            except requests.exceptions.RequestException:
                raise ccxt.NetworkError('Testnet read transport failed; no retry permitted') from None
            except (SafetyError, ccxt.BaseError) as error:
                raise type(error)('Testnet read failed or was blocked; no retry permitted') from None
            except Exception:
                raise SafetyError('Testnet read failed; private error details withheld') from None
            finally:
                self.receipts[spec.label] = TransportReceipt(spec.label, spec.url, started,
                                                            _utc(self._clock), status)

    def _send_http(self, prepared, *, timeout, verify, allow_redirects, proxies):
        # Intentionally no HTTP adapter or requests.Session.send call. Enabling
        # Phase B requires separately reviewed code, not a flag or injected client.
        raise SafetyError('Phase A network transport is disabled; Phase B approval is required')
