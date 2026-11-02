"""T1B: fixed-plan mock HTTP collection. No client, credentials or real transport.

Mock fixtures are assertions, never authenticated exchange evidence. The T1B
provider cannot be replaced with a callable/network backend. T1C Step 1 has a
separate exact-type, activation-blocked runtime seam; its reports stay synthetic.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from threading import RLock
from types import SimpleNamespace
import json

from ccxt import TICK_SIZE

from account_discovery import unknown
from diagnostics import ResponseEvidence, diagnose, exchange_code, response_reason
from instrument_specs import inspect_instrument
from safety import SafetyError
from testnet_compatibility import (Observation, Requirement, Status, exact_observation_number,
                                   interpret_mock_status)
from testnet_transport import HTTP_SUCCESS_STATUS, PLAN, PlannedGet, decode_response_json


MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class MockFailure(str, Enum):
    TIMEOUT = 'timeout'
    NETWORK = 'network'


@dataclass(frozen=True)
class MockHttpResponse:
    label: str
    body: bytes = field(repr=False)
    url: str = field(repr=False)
    status: int = 200
    redirected: bool = False
    failure: MockFailure = None


class MockResponseProvider:
    """Closed in-memory transcript, single use. Not independent run-wide authority."""
    def __init__(self, responses):
        if (type(responses) is not tuple or len(responses) > len(PLAN)
                or any(type(item) is not MockHttpResponse for item in responses)):
            raise SafetyError('Expected a bounded mock HTTP transcript')
        self._responses = responses
        self._lock = RLock()
        self._claimed = self._active = False
        self._index = 0

    @property
    def attempts(self):
        return tuple(entry.label for entry in PLAN[:self._index])

    def begin(self):
        with self._lock:
            if self._claimed:
                raise SafetyError('Mock collection is single-use; no retry is permitted')
            self._claimed = self._active = True

    def end(self):
        with self._lock:
            self._active = False

    def get(self, entry):
        with self._lock:
            if (not self._active or type(entry) is not PlannedGet or self._index >= len(PLAN)
                    or entry != PLAN[self._index]):
                raise SafetyError('Mock collection rejected an out-of-plan request')
            index = self._index
            self._index += 1  # Even missing/failed mock responses consume their attempt.
            if index >= len(self._responses):
                raise SafetyError('Mock response is unavailable; no retry is permitted')
            return self._responses[index]


@dataclass(frozen=True)
class EndpointObservation:
    label: str
    source: str
    started_at: object = None
    completed_at: object = None
    attempted: bool = False
    success: bool = False
    http_status: object = None
    diagnostic: str = 'not attempted after a conservative stop'
    provenance: str = field(default='synthetic_http_fixture', init=False)


REQUIRED_STATUS_FIELDS = (
    'authentication', 'instrument.active', 'instrument.contract_size',
    'instrument.quantity_step', 'instrument.price_tick', 'explicit_hedge_legs',
    'position_account_scope', 'position_crosscheck',
    'Long.observed_base_quantity', 'Short.observed_base_quantity',
    'Long.margin_mode', 'Short.margin_mode',
    'Long.configured_leverage', 'Short.configured_leverage',
)


@dataclass(frozen=True)
class StatusReport:
    started_at: datetime
    checked_at: datetime
    endpoints: tuple
    requirements: tuple
    collection_mode: str = 't1b_mock'

    @property
    def synthetic(self):
        return True

    @property
    def authenticated_evidence_verified(self):
        return False

    @property
    def execution_authorized(self):
        return False

    @property
    def may_resolve_journal(self):
        return False

    @property
    def collection_complete(self):
        return len(self.endpoints) == len(PLAN) and all(row.success for row in self.endpoints)

    @property
    def complete(self):
        rows = {row.name: row for row in self.requirements}
        return (self.collection_complete and all(name in rows and rows[name].status is Status.VERIFIED
                                                for name in REQUIRED_STATUS_FIELDS))

    def render(self):
        runtime = self.collection_mode == 't1c_mock'
        lines = ['PHEMEX TESTNET STATUS — READ ONLY',
                 ('T1C STEP 1 MOCK TRANSPORT REPORT — SYNTHETIC; NOT EXCHANGE VERIFICATION' if runtime else
                  'T1B MOCK REPORT — SYNTHETIC; NOT AUTHENTICATED EXCHANGE OBSERVATIONS'),
                 'Read window: ' + self.started_at.isoformat() + ' -> ' + self.checked_at.isoformat(),
                 'Sequential snapshot; VERIFIED below means a check passed in mocked evidence only.',
                 'Position quantities/quantity steps: BTC base units. Price ticks: USDT/BTC. Leverage: x.']
        for endpoint in self.endpoints:
            state = 'MOCK_RESPONSE_ACCEPTED' if endpoint.success else 'FAILED' if endpoint.attempted else 'NOT_ATTEMPTED'
            lines.append(f'Endpoint {endpoint.label}: {state}; {endpoint.source}; '
                         f'http={endpoint.http_status if endpoint.http_status is not None else "UNKNOWN"}; '
                         f'collected={endpoint.started_at.isoformat() if endpoint.started_at else "UNKNOWN"}'
                         f' -> {endpoint.completed_at.isoformat() if endpoint.completed_at else "UNKNOWN"}; '
                         f'provenance={endpoint.provenance}; {endpoint.diagnostic}')
        for row in self.requirements:
            lines.append(f'{row.status.value} {row.name}: {row.value}; {row.reason} '
                         f'[{row.provenance}; {row.source}; observed={row.observed_at}]')
        lines += ['Four-response collection: ' + ('COMPLETE' if self.collection_complete else 'PARTIAL/FAILED'),
                  'Scoped mock status checks: ' + ('COMPLETE' if self.complete else 'PARTIAL/FAILED'),
                  'Full account verification: NOT ESTABLISHED',
                  ('Step 1 transport mocked or blocked; no real reads authorized. Profile origin unverified. Mutations disabled.' if runtime else
                   'No HTTP requests sent. No credentials loaded. Exchange mutations remain disabled.'),
                  'Execution authority: NONE; journal resolution: NONE.']
        return '\n'.join(lines)


def _utc(clock):
    try:
        value = clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except Exception:
        raise SafetyError('Status collection requires an aware valid clock') from None


def _instrument(products, legacy):
    """Use the existing pure inspector on a local, explicitly native metadata view.

    No CCXT parser/client is constructed. No contract-size convention, minimum
    size or independent normalized-grid confirmation is manufactured.
    """
    rows = products.get('perpProductsV2')
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise SafetyError('Mock instrument metadata is incomplete')
    matches = [row for row in rows + (products.get('products') or [])
               if type(row) is dict and row.get('symbol') == 'BTCUSDT']
    if len(matches) != 1:
        raise SafetyError('Mock instrument metadata is ambiguous')
    row = matches[0]
    if (row.get('type') not in ('Perpetual', 'PerpetualV2', 'PerpetualPilot')
            or row.get('baseCurrency') != 'BTC' or row.get('quoteCurrency') != 'USDT'
            or row.get('settleCurrency') != 'USDT'):
        raise SafetyError('Mock instrument scope is unsupported')
    if any(type(other) is not dict for other in legacy):
        raise SafetyError('Mock companion instrument metadata is malformed')
    companion = [other for other in legacy if other.get('symbol') == 'BTCUSDT']
    if len(companion) > 1:
        raise SafetyError('Mock companion instrument metadata is ambiguous')
    numeric = ('contractSize', 'qtyStepSize', 'tickSize', 'maxOrderQtyRq', 'minOrderValueRv', 'maxLeverage')
    if companion:
        other = companion[0]
        for key in numeric + ('baseCurrency', 'quoteCurrency', 'settleCurrency', 'status', 'type'):
            if key in other and key in row:
                left, right = row[key], other[key]
                if key in numeric:
                    left, right = exact_observation_number(left), exact_observation_number(right)
                if left is None or right is None or left != right:
                    raise SafetyError('Mock public instrument sources contradict one another')
    size = exact_observation_number(row.get('contractSize'))
    view = SimpleNamespace(precisionMode=TICK_SIZE, markets={'BTC/USDT:USDT': {
        'id': 'BTCUSDT', 'symbol': 'BTC/USDT:USDT', 'swap': True, 'linear': True,
        'settle': 'USDT', 'quote': 'USDT', 'active': row.get('status') == 'Listed',
        'contractSize': size, 'info': row, 'limits': {},
        'precision': {'amount': row.get('qtyStepSize') if size == 1 else None,
                      'price': row.get('tickSize')},
    }})
    specs = inspect_instrument(view, 'BTCUSDT')
    for key, native in (('contract_size', 'contractSize'), ('quantity_step', 'qtyStepSize'),
                        ('price_tick', 'tickSize'), ('max_base_quantity', 'maxOrderQtyRq'),
                        ('min_notional', 'minOrderValueRv')):
        value = exact_observation_number(row.get(native))
        if value is None or value <= 0:
            specs[key] = unknown('Native field missing, invalid or outside exact precision bounds')
    return specs


def collect_status(provider, *, clock=None):
    """Consume one fixed mock transcript. No real/provider-injected HTTP backend."""
    if type(provider) is not MockResponseProvider:
        raise SafetyError('T1B accepts only its in-memory mock response provider')
    return _collect_status(provider, clock=clock)


def collect_runtime_status(runtime, *, clock=None):
    """Separate Step 1 seam: permanently synthetic, never an actual-read permit."""
    from testnet_status_runtime import TestnetReadOnlyRuntime
    if type(runtime) is not TestnetReadOnlyRuntime:
        raise SafetyError('Expected the dedicated, activation-blocked Testnet runtime')
    return _collect_status(runtime, clock=clock, collection_mode='t1c_mock')


def _collect_status(provider, *, clock=None, collection_mode='t1b_mock'):
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(clock)
    observations, endpoints, data = {}, [], {}
    specs = None
    authentication_failed = False
    provider.begin()
    try:
        for index, entry in enumerate(PLAN):
            begin = _utc(clock)
            success, status, diagnostic, payload = False, None, '', None
            try:
                response = provider.get(entry)
                if (response.label != entry.label or response.url != entry.url
                        or type(response.redirected) is not bool or response.redirected is not False
                        or type(response.status) is not int or not 100 <= response.status <= 599):
                    raise SafetyError('Mock response scope or status is invalid')
                if response.failure is not None:
                    if type(response.failure) is not MockFailure:
                        raise SafetyError('Mock failure type is invalid')
                    diagnostic = response.failure.value + '; no retry'
                    raise SafetyError('Mock transport failed')
                status = response.status
                if type(response.body) is not bytes or len(response.body) > MAX_RESPONSE_BYTES:
                    raise SafetyError('Mock response encoding or size is invalid')
                payload = decode_response_json(response.body, exact_numbers=True)
                expected = list if index == 1 else dict
                valid = (type(payload) is dict and type(payload.get('code')) in (int, str)
                         and payload['code'] in (0, '0') and not payload.get('error')
                         and type(payload.get('data')) is expected)
                if status != HTTP_SUCCESS_STATUS or not valid:
                    failure = diagnose(evidence=ResponseEvidence(status, exchange_code(payload), response_reason(payload)),
                                       response=payload)
                    diagnostic = str(failure)
                    authentication_failed |= failure.category == 'authentication'
                    raise SafetyError('Mock response was unsuccessful')
                value = payload['data']
                if index >= 2 and (type(value.get('account')) is not dict
                                   or type(value.get('positions')) is not list):
                    raise SafetyError('Mock private response is incomplete')
                if index == 1:
                    specs = _instrument(data['products'], value)
                data[entry.label] = value
                success = True
            except Exception as error:
                diagnostic = diagnostic or str(diagnose(error))
            completed = _utc(clock)
            if not started <= begin <= completed:
                success = False
                data.pop(entry.label, None)
                diagnostic = 'Collection clock is inconsistent; no retry'
            observation = Observation(begin, completed, success, diagnostic if not success else '')
            observations[entry.label] = observation
            endpoints.append(EndpointObservation(entry.label, 'GET ' + entry.url, begin, completed,
                                                 True, success, status, diagnostic or 'mock success envelope'))
            if not success:
                break
        checked = _utc(clock)
        for entry in PLAN[len(endpoints):]:
            endpoints.append(EndpointObservation(entry.label, 'GET ' + entry.url))
        public = [observations.get(entry.label) for entry in PLAN[:2]]
        public_ok = all(item is not None and item.success for item in public)
        instrument_observation = Observation(public[0].started_at if public[0] else started,
            public[1].completed_at if public[1] else checked, bool(public_ok))
        private = {entry.label: data[entry.label] for entry in PLAN[2:]
                   if entry.label in data and observations[entry.label].success}
        interpreted = interpret_mock_status(specs if public_ok else None, instrument_observation,
            {key: value for key, value in observations.items() if key in private or key in
             ('account_positions', 'identity_crosscheck')}, private, now=checked,
            authentication_failed=authentication_failed)
        unavailable = tuple(Requirement(name, Status.UNKNOWN, '-', 'not_established',
            'Not established by the four approved GET responses', 'unavailable', reason)
            for name, reason in (
                ('account.balance', 'Accounting and account-wide wallet scope are unknown; no total inferred.'),
                ('account.equity', 'No authoritative account-wide equity established.'),
                ('account.available_margin', 'Available margin is not inferred from balance or used funds.'),
            ))
        # Reuse compatibility interpretation, but do not present checks we did
        # not perform (CCXT endpoint configuration/normalization) as performed.
        rows = []
        for row in interpreted.requirements:
            if row.name.endswith('.reducible_position') or row.name in (
                    'reduce_only_limit_IOC_acceptance', 'execution_assessment'):
                continue
            if row.name == 'testnet_endpoint_profile':
                row = Requirement(row.name, row.status, row.value, row.provenance, row.source,
                    row.observed_at, 'Mock response URLs matched the fixed Testnet plan; no HTTP transport was run.')
            elif row.name == 'authentication':
                row = Requirement(row.name, row.status,
                    'accepted private mock success envelope' if row.status is Status.VERIFIED else '-',
                    row.provenance, row.source, row.observed_at,
                    'Modeled authentication only; no real credentials or stable account identity verified.')
            elif row.name == 'instrument.minimum_order_size':
                row = Requirement(row.name, row.status, row.value, row.provenance, row.source,
                    row.observed_at, 'No reviewed native minimum quantity mapping; quantity step is not a minimum.')
            rows.append(row)
        return StatusReport(started, checked, tuple(endpoints), tuple(rows) + unavailable, collection_mode)
    except Exception:
        raise SafetyError('Mock status collection failed; private details withheld') from None
    finally:
        provider.end()


def mock_demo():
    """Invented deterministic responses, never a Phemex fixture capture."""
    product = {'symbol': 'BTCUSDT', 'type': 'PerpetualV2', 'baseCurrency': 'BTC',
        'quoteCurrency': 'USDT', 'settleCurrency': 'USDT', 'status': 'Listed',
        'contractSize': '1', 'qtyStepSize': '0.001', 'tickSize': '0.1',
        'maxOrderQtyRq': '100', 'minOrderValueRv': '1'}
    positions = [{'symbol': 'BTCUSDT', 'accountID': '222222222222', 'posSide': leg,
        'posMode': 'Hedged', 'side': side, 'sizeRq': quantity, 'leverageRr': leverage,
        'positionStatus': 'Normal'} for leg, side, quantity, leverage in
        (('Long', 'Buy', '0.002', '-15'), ('Short', 'None', '0', '3'))]
    private = {'account': {'currency': 'USDT', 'accountId': '222222222222',
                           'userID': '111111111111'}, 'positions': positions}
    bodies = ({'perpProductsV2': [product], 'products': []}, [], private, private)
    provider = MockResponseProvider(tuple(MockHttpResponse(entry.label,
        json.dumps({'code': 0, 'data': body}).encode(), entry.url) for entry, body in zip(PLAN, bodies)))
    tick = iter(range(32))
    epoch = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    return provider, lambda: epoch + timedelta(milliseconds=50 * next(tick))
