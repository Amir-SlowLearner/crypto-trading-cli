"""Pure comparison of supplied position responses; no collection or authority.

All provenance, clocks, market scope and unit mappings are caller assertions.
No response/identifier is retained in the sanitized result. VERIFIED means only
internally consistent input, never authenticated exchange or execution evidence.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import re

from account_discovery import Fact, choice, unknown
from manual_market_catalog import valid_selector
from testnet_compatibility import _identifier, _identity, exact_observation_number
from testnet_transport import PLAN, decode_response_json


class Endpoint(str, Enum):
    POSITIONS = PLAN[2].url
    ACCOUNT_POSITIONS = PLAN[3].url


class Origin(str, Enum):
    SYNTHETIC = 'synthetic'
    SUPPLIED_RESPONSE = 'supplied_response_unverified'
    UNKNOWN = 'unknown'


class Clock(str, Enum):
    COLLECTION_UTC = 'asserted_common_collection_utc'
    UNKNOWN = 'unknown'


class Encoding(str, Enum):
    UNSCALED_BASE = 'asserted_unscaled_base_asset'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class PositionScope:
    native_symbol: str
    base_asset: str


@dataclass(frozen=True, repr=False)
class PositionObservation:
    endpoint: Endpoint
    body: object = field(repr=False)
    started_at: datetime
    completed_at: datetime
    window_id: str = field(repr=False)
    origin: Origin = Origin.SYNTHETIC
    clock: Clock = Clock.COLLECTION_UTC
    http_status: int = 200

    def __repr__(self):
        return 'PositionObservation(input_withheld)'


@dataclass(frozen=True)
class QuantityMapping:
    """Explicit endpoint/symbol/field convention, NOT evidence of its truth.

    No default alias, contract conversion, scaling factor or rounding is allowed.
    Both size fields, if present, need their own mappings and must agree exactly.
    """
    endpoint: Endpoint
    native_symbol: str
    base_asset: str
    field: str
    encoding: Encoding = Encoding.UNKNOWN
    origin: Origin = Origin.SYNTHETIC


@dataclass(frozen=True)
class LegObservation:
    side: str
    quantity: Fact
    mode: Fact
    direction: Fact
    margin_mode: Fact
    configured_leverage: Fact
    position_status: Fact


@dataclass(frozen=True)
class EndpointSummary:
    endpoint: Endpoint
    origin: Origin
    started_at: object
    completed_at: object
    accepted_response: Fact
    scope: Fact
    fresh: Fact
    explicit_hedge_legs: Fact
    observed_selected_zero: Fact
    legs: tuple
    issues: tuple


@dataclass(frozen=True)
class FieldComparison:
    side: str
    field: str
    agreement: Fact


@dataclass(frozen=True)
class InventoryCrosscheck:
    native_symbol: str
    base_asset: str
    endpoints: tuple
    snapshot_identity_consistency: Fact
    common_window: Fact
    comparisons: tuple
    crosscheck: Fact
    corroborated_selected_zero: Fact

    @property
    def execution_authorized(self):
        return False

    @property
    def may_resolve_journal(self):
        return False

    @property
    def authenticated_evidence_verified(self):
        return False

    @property
    def stable_account_binding(self):
        return unknown('No stable account binding established')

    @property
    def inventory_complete(self):
        return unknown('Missing-record semantics and full inventory coverage unestablished')

    @property
    def account_wide_exposure(self):
        return unknown('Other positions and ordinary/conditional orders are not covered')

    def render(self):
        def show(fact):
            return ('UNKNOWN (' + fact.reason + ')' if fact.value is None else
                    'VERIFIED-IN-MODEL ' + str(fact.value))
        lines = ['OFFLINE POSITION INVENTORY CROSS-CHECK — NO AUTHENTICATION OR EXECUTION AUTHORITY',
                 'Scope: ' + self.native_symbol + '; base units=' + self.base_asset,
                 'Provenance, clocks and quantity mappings are unverified input assertions.']
        for endpoint in self.endpoints:
            lines += ['Source: GET ' + endpoint.endpoint.value + '; origin=' + endpoint.origin.value,
                      'Collection UTC: ' + (endpoint.started_at.isoformat() if endpoint.started_at else 'UNKNOWN')
                      + ' -> ' + (endpoint.completed_at.isoformat() if endpoint.completed_at else 'UNKNOWN'),
                      'Accepted envelope: ' + show(endpoint.accepted_response) + '; scope: ' + show(endpoint.scope),
                      'Fresh: ' + show(endpoint.fresh) + '; hedge legs: ' + show(endpoint.explicit_hedge_legs),
                      'Observed selected-position zero: ' + show(endpoint.observed_selected_zero)]
            for leg in endpoint.legs:
                lines.append(leg.side + ': ' + '; '.join(name + '=' + show(getattr(leg, name)) for name in _FIELDS))
            lines.extend('Issue: ' + issue for issue in endpoint.issues)
        lines += ['Reported identity consistency: ' + show(self.snapshot_identity_consistency),
                  'Common collection window: ' + show(self.common_window)]
        lines.extend(item.side + '.' + item.field + ': ' + show(item.agreement) for item in self.comparisons)
        lines += ['Cross-check: ' + show(self.crosscheck),
                  'Corroborated selected-position zero: ' + show(self.corroborated_selected_zero),
                  'Inventory completeness: ' + show(self.inventory_complete),
                  'Account-wide/future exposure: ' + show(self.account_wide_exposure),
                  'No raw responses, account identifiers, balances or signed headers retained.',
                  'No collection, retries, mutations, journal resolution or execution authority.']
        return '\n'.join(lines)


_FIELDS = ('quantity', 'mode', 'direction', 'margin_mode', 'configured_leverage', 'position_status')
_MAX_ROWS = 10000
_MAX_FIELDS = 128
_MAX_BYTES = 4 * 1024 * 1024
_MAX_AGE = timedelta(seconds=5)
_CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', re.ASCII)


def _fact(value, source, reason='not established'):
    return unknown(reason, source) if value is None else Fact(value, 'input_assertion', source, reason)


def _scope_valid(scope):
    return (type(scope) is PositionScope and type(scope.native_symbol) is str
            and valid_selector(scope.native_symbol) and '/' not in scope.native_symbol
            and type(scope.base_asset) is str and _CODE.fullmatch(scope.base_asset) is not None)


def _number(value):
    if type(value) is int and value.bit_length() > 256:
        return None
    try:
        return exact_observation_number(value)
    except (ValueError, ArithmeticError):
        return None


def _utc(value):
    # Fixed-offset standard-library time zones only; no custom tzinfo callbacks.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        return None
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _blank(side, source, reason):
    return LegObservation(side, *(_fact(None, source, reason) for _ in _FIELDS))


def _quantity(row, endpoint, scope, origin, mappings):
    fields = [key for key in ('sizeRq', 'size') if key in row]
    if not fields:
        return None
    values = []
    for key in fields:
        candidates = [m for m in mappings if type(m) is QuantityMapping and m.endpoint is endpoint
                      and type(m.native_symbol) is str and m.native_symbol == scope.native_symbol
                      and type(m.base_asset) is str and m.base_asset == scope.base_asset
                      and type(m.field) is str and m.field == key]
        if (len(candidates) != 1 or candidates[0].encoding is not Encoding.UNSCALED_BASE
                or candidates[0].origin is not origin):
            return None
        value = _number(row[key])
        if value is None or value < 0:
            return None
        values.append(value)
    return values[0] if all(value == values[0] for value in values) else None


def _leg(row, side, endpoint, scope, origin, mappings):
    source = endpoint.value
    quantity = _quantity(row, endpoint, scope, origin, mappings)
    # Reuse reviewed enumeration interpretation, with strict primitive types.
    clean = {key: value for key, value in row.items()
             if type(key) is str and key in ('posMode', 'side', 'positionStatus') and type(value) is str}
    mode = choice(clean, 'posMode', {'Hedged':'hedge', 'OneWay':'one_way'}, source)
    direction = choice(clean, 'side', {'Buy':'long', 'Sell':'short', 'None':'flat'}, source)
    status = choice(clean, 'positionStatus', {'Normal':'normal', 'Liq':'liquidation', 'Adl':'adl'}, source)
    if quantity is not None and quantity > 0 and direction.value != side.lower():
        quantity = None
        direction = unknown('Contradictory selected-leg direction', source)
    leverage = _number(row.get('leverageRr'))
    sign_mode = None if leverage in (None, 0) else 'cross' if leverage < 0 else 'isolated'
    flag = row.get('crossMargin')
    if 'crossMargin' in row:
        margin = ('cross' if flag else 'isolated') if type(flag) is bool else None
        if sign_mode is not None and margin != sign_mode:
            margin = None
    else:
        margin = sign_mode
    return LegObservation(side, _fact(quantity, source, 'Quantity missing, invalid, conflicting or unit mapping unestablished'),
                          mode, direction, _fact(margin, source, 'Margin evidence missing or conflicting'),
                          _fact(leverage.copy_abs() if leverage not in (None, 0) else None, source,
                                'Configured leverage unestablished'), status)


def _summarize(observation, endpoint, scope, mappings, now):
    source = endpoint.value
    origin, start, end = Origin.UNKNOWN, None, None
    accepted = scoped = fresh = hedge = zero = None
    identity = None
    issues = []
    legs = tuple(_blank(side, source, 'Observation unavailable') for side in ('Long','Short'))
    def done():
        return EndpointSummary(endpoint, origin, start, end,
            _fact(accepted, source, 'No accepted HTTP 200 success envelope'),
            _fact(scoped, source, 'USDT/selected-symbol scope unestablished'),
            _fact(fresh, source, 'Observation stale, future-dated or clock unestablished'),
            _fact(hedge, source, 'Explicit unique consistent Hedge Mode legs unestablished'),
            _fact(zero, source, 'Selected-position zero unestablished'), legs, tuple(issues)), identity
    if (type(observation) is not PositionObservation or observation.endpoint is not endpoint
            or type(observation.origin) is not Origin or observation.origin is Origin.UNKNOWN):
        issues.append('Missing observation or invalid endpoint/provenance assertion')
        return done()
    origin = observation.origin
    start, end = _utc(observation.started_at), _utc(observation.completed_at)
    if (now is not None and start is not None and end is not None and start <= end <= now
            and now - start < _MAX_AGE and observation.clock is Clock.COLLECTION_UTC):
        fresh = True
    try:
        payload = observation.body
        if type(payload) is bytes:
            if len(payload) > _MAX_BYTES:
                raise ValueError
            payload = decode_response_json(payload, exact_numbers=True)
        if (type(observation.http_status) is not int or observation.http_status != 200
                or type(payload) is not dict or len(payload) > _MAX_FIELDS
                or type(payload.get('code')) not in (int,str)
                or payload['code'] not in (0,'0') or payload.get('error')
                or type(payload.get('data')) is not dict):
            raise ValueError
        data = payload['data']
        account, rows = data.get('account'), data.get('positions')
        if (type(account) is not dict or len(account) > _MAX_FIELDS or len(data) > _MAX_FIELDS
                or type(rows) is not list or len(rows) > _MAX_ROWS):
            raise ValueError
        accepted = True
        scope_ok = _scope_valid(scope) and account.get('currency') == 'USDT'
        for obj in (payload, data, account):
            for key, expected in (('currency','USDT'),('settleCurrency','USDT'),
                                  ('quoteCurrency','USDT'),('symbol',scope.native_symbol if _scope_valid(scope) else None)):
                if key in obj and (type(obj[key]) is not str or obj[key] != expected):
                    scope_ok = False
        if not scope_ok:
            issues.append('Incompatible or missing account/envelope scope')
            return done()
        scoped = True
        # Generic existing identity helper: no BTC-specific position filtering.
        identity = _identity(data)
        for obj in (payload, data):
            for aliases in (('accountId','accountID'),('userID','userId')):
                if any(key in obj for key in aliases) and _identifier(obj, aliases) != _identifier(account, aliases):
                    identity = None
                    issues.append('Conflicting envelope identity fields; identifiers withheld')
        selected, unassignable = [], False
        for row in rows:
            if (type(row) is not dict or len(row) > _MAX_FIELDS
                    or type(row.get('symbol')) is not str or not valid_selector(row['symbol'])):
                unassignable = True
            elif row['symbol'] == scope.native_symbol:
                selected.append(row)
        complete = not unassignable and data.get('nextPageArg') in (None,'')
        if not complete:
            issues.append('Position inventory unassignable or paginated; no pages followed')
        scope_conflict = any(type(row[key]) is not str or row[key] != 'USDT' for row in selected
                             for key in ('currency','settleCurrency','quoteCurrency') if key in row)
        if scope_conflict:
            scoped = None
            issues.append('Selected-row settlement scope incompatible or unknown')
            return done()
        if fresh is not True or not complete:
            return done()
        # Do not erase a contradictory mode/side simply because its size is absent.
        context_conflict = any(type(row.get('posSide')) is not str or row['posSide'] not in ('Long','Short')
                               or type(row.get('posMode')) is not str or row['posMode'] != 'Hedged'
                               for row in selected)
        if context_conflict:
            issues.append('Position mode or leg missing, unsupported or contradictory')
            return done()
        result = []
        for side in ('Long','Short'):
            matches = [row for row in selected if row['posSide'] == side]
            if len(matches) != 1:
                result.append(_blank(side, source, 'Missing or duplicate leg; never assumed zero'))
                issues.append(side + ': missing or duplicate leg')
            else:
                result.append(_leg(matches[0], side, endpoint, scope, origin, mappings))
        legs = tuple(result)
        hedge = True if len(selected) == 2 and all(leg.mode.value == 'hedge' for leg in legs) else None
        if hedge is True and identity is not None and all(leg.quantity.value is not None and leg.direction.value is not None for leg in legs):
            zero = all(leg.quantity.value == 0 for leg in legs)
        return done()
    except Exception:
        # Never retain input exceptions, raw error strings, identifiers or payloads.
        accepted = scoped = hedge = zero = None
        identity = None
        legs = tuple(_blank(side, source, 'Response unusable') for side in ('Long','Short'))
        issues.append('Response malformed, ambiguous or outside supported bounds')
        return done()


def compare_position_inventory(scope, primary, secondary, *, mappings=(), now):
    """Deterministic analytical comparison; no collection, clocks read or I/O.

    `body` is a full Phemex envelope (dict or strict UTF-8 JSON bytes). Parsed
    dicts cannot establish that their upstream decoder rejected duplicate keys.
    All results are non-authoritative, including already-collected inputs.
    """
    checked = _utc(now)
    mappings = mappings if type(mappings) is tuple and len(mappings) <= 8 else ()
    summaries, identities = zip(*(_summarize(obs, endpoint, scope, mappings, checked)
        for obs, endpoint in ((primary,Endpoint.POSITIONS),(secondary,Endpoint.ACCOUNT_POSITIONS))))
    left, right = summaries
    source = Endpoint.POSITIONS.value + ' + ' + Endpoint.ACCOUNT_POSITIONS.value
    valid_window = (type(primary) is PositionObservation and type(secondary) is PositionObservation
        and type(primary.window_id) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', primary.window_id) is not None
        and type(secondary.window_id) is str and primary.window_id == secondary.window_id
        and left.fresh.value is True and right.fresh.value is True
        and left.completed_at <= right.started_at and left.origin is right.origin)
    identity_ok = (valid_window and left.scope.value is True and right.scope.value is True
                   and identities[0] is not None and identities[0] == identities[1])
    comparable = identity_ok and all(item.explicit_hedge_legs.value is True for item in summaries)
    comparisons = []
    for first, second in zip(left.legs,right.legs):
        for name in _FIELDS:
            a, b = getattr(first,name).value, getattr(second,name).value
            ok = comparable and a is not None and b is not None and a == b
            comparisons.append(FieldComparison(first.side,name,_fact(True if ok else None,source,
                'Both bound fresh fields must be present, valid and equal; missing is not agreement')))
    matches = all(item.agreement.value is True for item in comparisons)
    zero = (all(leg.quantity.value == 0 for leg in left.legs) if matches else None)
    return InventoryCrosscheck(scope.native_symbol if _scope_valid(scope) else 'UNKNOWN',
        scope.base_asset if _scope_valid(scope) else 'UNKNOWN', summaries,
        _fact(True if identity_ok else None,source,'Reported identities missing, conflicting or not comparable; values withheld'),
        _fact(True if valid_window else None,source,'Fresh ordered common-window assertions required'),tuple(comparisons),
        _fact(True if matches else None,source,'One or more bound position fields missing, conflicting or unestablished'),
        _fact(zero,source,'Selected-position zero requires the complete matching field cross-check'))
