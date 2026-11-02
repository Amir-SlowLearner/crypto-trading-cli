"""Run-bound Testnet GET runtime; protected production bootstrap is unavailable.

Tests replace ONLY the separate bootstrap block and mock final HTTP sends.
Durable authorization checks remain active. Reports have no execution authority.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import os
from pathlib import Path
import re
import stat
from threading import RLock, get_ident
import time

import ccxt
import requests
from urllib3.connection import HTTPConnection as PoolHTTPConnection, HTTPSConnection as PoolHTTPSConnection

from phemex_request_compiler import verify_installed_ccxt
from safety import SafetyError
from testnet_read_run_store import ReadBinding, ReadLease, ReadRunStore
from testnet_status_collection import MAX_RESPONSE_BYTES, MockHttpResponse, collect_runtime_status
from testnet_transport import (HOST, PLAN, BlockedSession, OfflineGetTransport, PlannedGet,
                               _fingerprint, _validate_wire, check_transport_configuration, decode_response_json)


PROFILE_PURPOSE = 'phemex-testnet-readonly-profile-v1'
MAX_PROFILE_BYTES = 8192
RUN_SECONDS = 30
CONNECT_SECONDS = 5
READ_SECONDS = 10
CHUNK_BYTES = 8192


def _require_protected_bootstrap():
    """Unconditional production boundary until bootstrap AND OS review.

    No object, root UID, approval, environment or command option enables this.
    Do not replace with a boolean or remove as an operational procedure.
    """
    raise SafetyError('Protected Testnet bootstrap and OS confinement are unavailable; activation is disabled')


@dataclass
class _ContextState:
    lock: object = field(default_factory=RLock, repr=False)
    failed: bool = False


@dataclass(frozen=True)
class ReadRunContext:
    """Expected bootstrap scope plus a lease, NOT authentication by construction.

    Production must obtain expected fields from the future protected bootstrap.
    Each use rechecks SQLite scope/UID/PID/expiry. Copies share failure state.
    """
    store: ReadRunStore = field(repr=False)
    lease: ReadLease = field(repr=False)
    binding: ReadBinding = field(repr=False)
    storage_id: str = field(repr=False)
    store_path: Path = field(repr=False)
    profile_path: Path = field(repr=False)
    _state: _ContextState = field(default_factory=_ContextState, init=False, repr=False, compare=False)

    def _checked(self, action):
        with self._state.lock:
            if self._state.failed:
                raise SafetyError('Read authorization failed or became uncertain; no retry')
            try:
                if (type(self.store) is not ReadRunStore or type(self.lease) is not ReadLease
                        or type(self.binding) is not ReadBinding):
                    raise ValueError
                self.binding.__post_init__()
                if (self.lease.binding != self.binding or self.lease.storage_id != self.storage_id
                        or self.store.storage_id != self.storage_id or self.store.path != self.store_path
                        or self.store.worker_uid != self.binding.worker_uid
                        or any(not isinstance(p, Path) or not p.is_absolute() or '..' in p.parts
                               for p in (self.store_path, self.profile_path))):
                    raise ValueError
                self.store.check_active(self.lease)
                return action()
            except Exception:
                self._state.failed = True
                raise SafetyError('Read authorization failed or became uncertain; no retry') from None

    def check(self):
        self._checked(lambda: None)

    def reserve(self, entry):
        return self._checked(lambda: self.store.reserve(self.lease, entry))

    def consume_dispatch(self, entry, permit):
        return self._checked(lambda: self.store.consume_dispatch(self.lease, entry, permit))

    def record(self, entry, success):
        return self._checked(lambda: self.store.record(self.lease, entry, success))


def _require_step2_activation(context=None, *, stage=None, entry=None, path=None, profile_id=None):
    """Stage-specific run checks; successful checks do NOT enable production I/O."""
    if type(context) is not ReadRunContext:
        raise SafetyError('A durable run-bound Testnet authorization is required')
    context.check()
    valid = False
    if stage == 'worker':
        valid = entry is None and path is None and profile_id is None
    elif stage == 'runtime':
        valid = entry is None and path is None and profile_id == context.binding.profile_id
    elif stage == 'profile':
        valid = entry is None and profile_id is None and path == context.profile_path
    elif stage in ('dispatch', 'send'):
        valid = type(entry) is PlannedGet and entry in PLAN and path is None and profile_id is None
    if not valid:
        context._state.failed = True
        raise SafetyError('Read authorization stage or binding mismatch')


def _no_redirects(*args, **kwargs):
    # Requests otherwise prepares a redirect even with allow_redirects=False,
    # potentially consuming its body before our streaming size checks.
    return iter(())


@dataclass(frozen=True)
class TestnetCredentialProfile:
    profile_id: str = field(repr=False)
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self):
        if (type(self.profile_id) is not str or not re.fullmatch('[0-9a-f]{32}', self.profile_id)
                or any(type(value) is not str or not re.fullmatch(r'[^\s\x00-\x1f\x7f]{1,512}', value)
                       for value in (self.api_key, self.api_secret))):
            raise SafetyError('Invalid dedicated Testnet credential profile; details withheld')

    @property
    def environment(self):
        return 'testnet'

    @property
    def origin(self):
        return HOST


def parse_testnet_profile(raw):
    """Pure profile decoder. Tests supply invented bytes; no file/env access."""
    try:
        if type(raw) is not bytes or len(raw) > MAX_PROFILE_BYTES:
            raise ValueError
        value = decode_response_json(raw, exact_numbers=True)
        if (type(value) is not dict or set(value) != {'version', 'purpose', 'environment',
                'origin', 'profile_id', 'api_key', 'api_secret'}
                or type(value['version']) is not int or value['version'] != 1
                or value['purpose'] != PROFILE_PURPOSE or value['environment'] != 'testnet'
                or value['origin'] != HOST):
            raise ValueError
        return TestnetCredentialProfile(value['profile_id'], value['api_key'], value['api_secret'])
    except Exception:
        raise SafetyError('Invalid dedicated Testnet credential profile; details withheld') from None


def load_testnet_profile(path, *, context=None):
    """Trusted-bootstrap input, NOT a caller/CLI path. Credential I/O stays blocked.

    Opens every component without symlink traversal and binds reads to FDs.
    OS installation must additionally supply protected mounts/ACLs and identity.
    """
    _require_step2_activation(context, stage='profile', path=path)
    _require_protected_bootstrap()  # Still denies BEFORE any credential-file I/O.
    directory = descriptor = None
    try:
        path = Path(path)
        if not path.is_absolute() or any(part in ('.', '..') for part in path.parts[1:]):
            raise ValueError
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise ValueError
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_PROFILE_BYTES):
            raise ValueError
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(MAX_PROFILE_BYTES + 1)
        profile = parse_testnet_profile(raw)
        if profile.profile_id != context.binding.profile_id:
            raise ValueError
        return profile
    except Exception:
        raise SafetyError('Testnet credential profile unavailable or unsafe; details withheld') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


class _PhemexGetSigner:
    """Reuse pinned CCXT signing without constructing an Exchange or HTTP client.

    Only the helpers reached by the pinned GET signer are present. There are no
    fetch/request/create/cancel/settings methods or general CCXT operation API.
    """
    omit = staticmethod(ccxt.Exchange.omit)
    extract_params = staticmethod(ccxt.Exchange.extract_params)
    implode_params = staticmethod(ccxt.Exchange.implode_params)
    urlencode_with_array_repeat = staticmethod(ccxt.Exchange.urlencode_with_array_repeat)
    safe_integer = staticmethod(ccxt.Exchange.safe_integer)
    safe_string = staticmethod(ccxt.Exchange.safe_string)
    sum = staticmethod(ccxt.Exchange.sum)
    encode = staticmethod(ccxt.Exchange.encode)
    hmac = staticmethod(ccxt.Exchange.hmac)
    implode_hostname = ccxt.Exchange.implode_hostname
    check_required_credentials = ccxt.Exchange.check_required_credentials

    def __init__(self, profile):
        self.apiKey, self.secret = profile.api_key, profile.api_secret
        self.id, self.hostname = 'phemex', 'testnet-api.phemex.com'
        self.requiredCredentials = {'apiKey': True, 'secret': True}
        self.options = {'maxRetriesOnFailure': 0, 'x-phemex-request-expiry': 60}
        self.urls = {'api': {'v1': HOST + '/v1', 'v2': HOST, 'private': HOST,
                             'public': HOST + '/exchange/public'}}
        self.session = BlockedSession()
        self.headers = None
        self.verify = self.validateServerSsl = True

    def seconds(self):
        return ccxt.Exchange.seconds()

    def sign_get(self, entry):
        try:
            if type(entry) is not PlannedGet or entry not in PLAN or ccxt.__version__ != '4.5.90':
                raise ValueError
            expected = {'v1': HOST + '/v1', 'v2': HOST, 'private': HOST,
                        'public': HOST + '/exchange/public'}
            if self.urls != {'api': expected}:
                raise ValueError
            check_transport_configuration(self)
            signed = ccxt.phemex.sign(self, entry.path, [entry.api], 'GET', dict(entry.query))
            if _validate_wire(signed['url'], signed['method'], signed['headers'], signed['body']) != entry:
                raise ValueError
            return signed
        except Exception:
            raise SafetyError('Testnet GET signing failed; private details withheld') from None

    def close(self):
        self.apiKey = self.secret = None


class _RunBudget:
    """Shared in-process quota, NOT durable actual-read approval or OS isolation."""
    def __init__(self):
        self.lock = RLock()
        self.used = False
        self.owner = None
        self.next_slot = 0
        self.deadline = None
        self.pending = None

    def begin(self):
        with self.lock:
            if self.used:
                raise SafetyError('Testnet runtime is single-use; no restart or retry')
            self.used, self.owner = True, get_ident()
            self.deadline = time.monotonic() + RUN_SECONDS

    def check(self):
        if self.owner != get_ident() or self.deadline is None or time.monotonic() >= self.deadline:
            raise SafetyError('Testnet run inactive or deadline exceeded; no retry')

    def reserve(self, entry, fingerprint):
        with self.lock:
            self.check()
            if self.pending is not None or self.next_slot >= len(PLAN) or entry != PLAN[self.next_slot]:
                raise SafetyError('Testnet run budget exhausted or out of order')
            self.next_slot += 1
            self.pending = fingerprint

    def consume(self, fingerprint):
        with self.lock:
            self.check()
            if self.pending is None or self.pending != fingerprint:
                raise SafetyError('HTTP send is not bound to one reserved signed GET')
            self.pending = None

    def end(self):
        with self.lock:
            self.owner = None
            self.pending = None


class _AuthorizedBudget(_RunBudget):
    """Per-runtime memory binding PLUS the independent durable dispatch token."""
    def __init__(self, context):
        super().__init__()
        self.context = context
        self._permit = self._entry = None

    def reserve(self, entry, fingerprint):
        with self.lock:
            super().reserve(entry, fingerprint)
            self._permit = self.context.reserve(entry)
            self._entry = entry

    def consume(self, fingerprint):
        with self.lock:
            super().consume(fingerprint)  # Forget memory permission even if COMMIT fails.
            permit, entry = self._permit, self._entry
            self._permit = self._entry = None
            self.context.consume_dispatch(entry, permit)

    def end(self):
        with self.lock:
            self._permit = self._entry = None
            super().end()


class _RuntimeGetTransport(OfflineGetTransport):
    """Separate gated backend; the original OfflineGetTransport stays blocked."""
    def __init__(self, budget, context=None):
        super().__init__()
        self._budget = budget
        self._context = context

    def _check_run(self, entry, stage):
        _require_step2_activation(self._context, stage=stage, entry=entry)
        if type(self._budget) is not _AuthorizedBudget or self._budget.context is not self._context:
            raise SafetyError('HTTP transport is not bound to its durable run budget')

    def _send_http(self, prepared, *, timeout, verify, allow_redirects, proxies):
        entry = _validate_wire(prepared.url, prepared.method, dict(prepared.headers), prepared.body)
        if timeout != 15 or verify is not True or allow_redirects is not False or proxies != {}:
            raise SafetyError('Unexpected Testnet HTTP configuration')
        self._check_run(entry, 'dispatch')
        self._budget.reserve(entry, _fingerprint(prepared.url, prepared.method, dict(prepared.headers), prepared.body))
        return self._read_https(prepared)

    def _read_https(self, prepared):
        if type(prepared) is not requests.PreparedRequest or prepared.hooks != {'response': []}:
            raise SafetyError('Unexpected prepared request type or response hooks')
        entry = _validate_wire(prepared.url, prepared.method, dict(prepared.headers), prepared.body)
        self._check_run(entry, 'send')
        self._budget.consume(_fingerprint(prepared.url, prepared.method, dict(prepared.headers), prepared.body))
        _require_protected_bootstrap()  # Unconditional block before HTTP session creation.
        # A confirmed COMMIT/bootstrap check can take time. Do not start an HTTP
        # session on a run that expired or exceeded its deadline in that window.
        self._budget.check()
        self._check_run(entry, 'send')
        if any(connection.debuglevel for connection in (http.client.HTTPConnection,
                http.client.HTTPSConnection, PoolHTTPConnection, PoolHTTPSConnection)):
            raise SafetyError('HTTP debug output must be disabled before handling Testnet credentials')
        with requests.Session() as session:
            session.trust_env = False
            session.verify, session.max_redirects = True, 0
            session.headers.clear()
            session.cookies.clear()
            session.proxies.clear()
            session.auth = None
            session.params = {}
            session.hooks = {'response': []}
            session.resolve_redirects = _no_redirects
            session.adapters.clear()
            session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0,
                pool_connections=1, pool_maxsize=1))
            response = session.send(prepared, timeout=(CONNECT_SECONDS, READ_SECONDS), verify=True,
                allow_redirects=False, proxies={}, stream=True)
            try:
                if (type(response) is not requests.Response or response.url != prepared.url
                        or response.history or type(response.status_code) is not int
                        or not 100 <= response.status_code <= 599
                        or response.headers.get('Content-Encoding', 'identity').lower() not in ('identity', '')):
                    raise SafetyError('Unexpected Testnet HTTP response')
                content = bytearray()
                for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                    self._budget.check()
                    if type(chunk) is not bytes or len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise SafetyError('Testnet response exceeds size or encoding limits')
                    content.extend(chunk)
                self._budget.check()
                # Detach from Requests' signed request/session/connection history.
                detached = requests.Response()
                detached.url, detached.status_code = response.url, response.status_code
                detached._content = bytes(content)
                return detached
            finally:
                if type(response) is requests.Response:
                    response.close()


class TestnetReadOnlyRuntime:
    """Fixed-plan runtime; run-bound dispatch with a blocked production boundary."""
    def __init__(self, profile, *, context=None):
        if type(profile) is not TestnetCredentialProfile:
            raise SafetyError('An explicit dedicated Testnet profile is required')
        self._profile = profile
        if context is not None and type(context) is not ReadRunContext:
            raise SafetyError('Invalid Testnet run context')
        self._context = context
        self._budget = _AuthorizedBudget(context) if context is not None else _RunBudget()
        self._transport = _RuntimeGetTransport(self._budget, context)
        self._signer = None

    @property
    def attempts(self):
        return tuple(entry.label for entry in PLAN[:self._transport._index])

    def collect(self, *, clock=None):
        return collect_runtime_status(self, clock=clock)

    def begin(self):
        self._budget.begin()
        try:
            if self._context is not None:
                _require_step2_activation(self._context, stage='runtime', profile_id=self._profile.profile_id)
            verify_installed_ccxt()  # Local source/version checks; no Exchange construction.
            self._signer = _PhemexGetSigner(self._profile)
            self._profile = None
            self._transport.begin(lambda: datetime.now(timezone.utc))
        except Exception:
            self.end()
            raise SafetyError('Testnet runtime initialization failed; private details withheld') from None

    def get(self, entry):
        try:
            self._budget.check()
            signed = self._signer.sign_get(entry)
            with self._transport.signed_request(signed):
                response = self._transport.dispatch(self._signer, signed['url'], signed['method'],
                                                     signed['headers'], signed['body'])
            result = MockHttpResponse(entry.label, response.content, entry.url, response.status_code)
            success = False
            try:
                value = decode_response_json(result.body, exact_numbers=True)
                expected = list if entry == PLAN[1] else dict
                success = (result.status == 200 and type(value) is dict
                           and type(value.get('code')) in (int, str) and value['code'] in (0, '0')
                           and not value.get('error') and type(value.get('data')) is expected)
            except Exception:
                pass
            self._context.record(entry, bool(success))
            return result
        except (ccxt.BaseError, SafetyError):
            raise
        except Exception:
            raise SafetyError('Testnet read failed; private details withheld') from None

    def end(self):
        self._transport.end()
        self._budget.end()
        self._profile = None
        if self._signer is not None:
            self._signer.close()
