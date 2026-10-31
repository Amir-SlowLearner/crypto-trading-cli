"""Offline eligibility models only. No credentials, transport or submission permit."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re

from account_discovery import reported
from config import RiskLimits
from order_preparation import prepare_order
from safety import SafetyError


class Provenance(str, Enum):
    AUTHENTICATED_TESTNET = 'authenticated_testnet'
    PUBLIC_TESTNET = 'public_testnet'
    REVIEWED_COMPATIBILITY = 'reviewed_compatibility'
    USER_SUPPLIED = 'user_supplied'
    SYNTHETIC = 'synthetic'
    UNKNOWN = 'unknown'


class Source(str, Enum):
    ACCOUNT = 'account'
    POSITIONS = 'positions'
    INSTRUMENTS = 'instruments'
    OPEN_ORDERS = 'open_orders'
    COMPATIBILITY_REVIEW = 'compatibility_review'


@dataclass(frozen=True)
class EvidenceStamp:
    source: Source
    provenance: Provenance
    observed_at: datetime
    account_binding: str = field(default='', repr=False)
    environment: str = 'testnet'
    complete: bool = False


@dataclass(frozen=True)
class AccountEvidence:
    stamp: EvidenceStamp
    authenticated: bool = None
    accounting_type: str = None
    status: str = None


@dataclass(frozen=True)
class PositionEvidence:
    stamp: EvidenceStamp
    symbol: str
    position_side: str
    base_quantity: Decimal = None
    position_mode: str = None
    margin_mode: str = None
    configured_leverage: Decimal = None
    status: str = None


@dataclass(frozen=True)
class InstrumentEvidence:
    stamp: EvidenceStamp
    symbol: str
    market_id: str
    active: bool = None
    market_type: str = None
    settlement: str = None
    contract_size: Decimal = None
    quantity_step: Decimal = None
    contract_step: Decimal = None
    min_contracts: Decimal = None
    max_contracts: Decimal = None
    max_base_quantity: Decimal = None
    min_notional: Decimal = None
    price_tick: Decimal = None
    price_precision_tick: Decimal = None
    min_price: Decimal = None
    max_price: Decimal = None
    max_notional: Decimal = None


@dataclass(frozen=True)
class OrdersEvidence:
    stamp: EvidenceStamp
    symbol: str
    active_count: int = None
    conditional_count: int = None


@dataclass(frozen=True)
class CapabilityEvidence:
    stamp: EvidenceStamp
    accounting_type: str = None
    explicit_hedge_reduce_only: bool = None
    limit_ioc: bool = None
    client_id_lookup: bool = None


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    action: str
    position_side: str
    quantity: Decimal
    price: Decimal
    order_type: str = 'limit'
    time_in_force: str = 'IOC'
    reduce_only: bool = True
    sl: Decimal = None
    tp: Decimal = None


def canonical_payload(payload):
    """Canonical semantic plan, NOT a compiled Phemex wire request."""
    return json.dumps(dict(payload), sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def utc_time(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SafetyError('An aware UTC-compatible timestamp is required')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ValidatedIntent:
    account_binding: str = field(repr=False)
    payload: tuple
    payload_fingerprint: str
    evidence_fingerprint: str
    validated_at: datetime
    expires_at: datetime
    environment: str = 'testnet'
    policy_version: str = 'offline-reduction-v1'

    @property
    def submission_enabled(self):
        return False


@dataclass(frozen=True)
class PolicyDecision:
    reasons: tuple = ()
    validated: ValidatedIntent = None

    @property
    def allowed(self):
        return self.validated is not None and not self.reasons

    @property
    def submission_enabled(self):
        return False


def evaluate(intent, account, position, instrument, orders, capabilities, *, now):
    """Reject incomplete evidence; callers cannot supply offline current_qty here.

    Provenance labels are assertions by a future trusted collector, not proof of
    network origin. Checkpoint 1 provides no collector and no execution authority.
    """
    models = (OrderIntent, AccountEvidence, PositionEvidence, InstrumentEvidence, OrdersEvidence, CapabilityEvidence)
    values = (intent, account, position, instrument, orders, capabilities)
    if any(type(value) is not model for value, model in zip(values, models)):
        return PolicyDecision(('invalid_evidence_type',))
    try:
        now = utc_time(now)
    except SafetyError:
        return PolicyDecision(('invalid_clock',))
    binding = account.stamp.account_binding if type(account.stamp) is EvidenceStamp else None
    if not isinstance(binding, str) or not re.fullmatch('[0-9a-f]{64}', binding):
        return PolicyDecision(('invalid_account_binding',))
    deadlines = []
    for value, source, provenance, age in (
        (account, Source.ACCOUNT, Provenance.AUTHENTICATED_TESTNET, timedelta(seconds=5)),
        (position, Source.POSITIONS, Provenance.AUTHENTICATED_TESTNET, timedelta(seconds=5)),
        (instrument, Source.INSTRUMENTS, Provenance.PUBLIC_TESTNET, timedelta(minutes=5)),
        (orders, Source.OPEN_ORDERS, Provenance.AUTHENTICATED_TESTNET, timedelta(seconds=5)),
        (capabilities, Source.COMPATIBILITY_REVIEW, Provenance.REVIEWED_COMPATIBILITY, timedelta(hours=24)),
    ):
        stamp = value.stamp
        if (type(stamp) is not EvidenceStamp or stamp.environment != 'testnet' or stamp.complete is not True
                or stamp.source is not source or stamp.provenance is not provenance
                or (source is not Source.INSTRUMENTS and stamp.account_binding != binding)
                or (source is Source.INSTRUMENTS and stamp.account_binding not in ('', binding))):
            return PolicyDecision(('untrusted_or_incomplete_evidence',))
        try:
            observed = utc_time(stamp.observed_at)
            deadline = observed + age
        except (SafetyError, OverflowError):
            return PolicyDecision(('invalid_evidence_time',))
        if observed > now or now >= deadline:
            return PolicyDecision(('stale_or_future_evidence',))
        deadlines.append(deadline)
    if (account.authenticated is not True or account.accounting_type not in ('classic', 'unified_multi_asset')
            or account.status != 'normal'):
        return PolicyDecision(('account_unknown_or_restricted',))
    if (capabilities.accounting_type != account.accounting_type
            or capabilities.explicit_hedge_reduce_only is not True or capabilities.limit_ioc is not True
            or capabilities.client_id_lookup is not True):
        return PolicyDecision(('compatibility_unverified',))
    if (intent.symbol != 'BTC/USDT:USDT' or instrument.market_id != 'BTCUSDT'
            or any(value.symbol != intent.symbol for value in (position, instrument, orders))
            or intent.position_side not in ('long', 'short') or position.position_side != intent.position_side
            or position.position_mode != 'hedge' or position.margin_mode not in ('cross', 'isolated')
            or position.status != 'normal'):
        return PolicyDecision(('position_or_instrument_mismatch',))
    if (intent.action not in ('reduce', 'close') or intent.order_type != 'limit'
            or intent.time_in_force != 'IOC' or intent.reduce_only is not True
            or intent.sl is not None or intent.tp is not None):
        return PolicyDecision(('unsupported_capability',))
    if (type(orders.active_count) is not int or orders.active_count != 0
            or type(orders.conditional_count) is not int or orders.conditional_count != 0):
        return PolicyDecision(('outstanding_orders_unknown_or_present',))
    numbers = [intent.quantity, intent.price, position.base_quantity, position.configured_leverage]
    numeric_keys = ('contract_size', 'quantity_step', 'contract_step', 'min_contracts', 'max_contracts',
                    'max_base_quantity', 'min_notional', 'price_tick', 'price_precision_tick')
    numbers += [getattr(instrument, key) for key in numeric_keys]
    numbers += [getattr(instrument, key) for key in ('min_price', 'max_price', 'max_notional')
                if getattr(instrument, key) is not None]
    if any(type(value) is not Decimal or not value.is_finite() or value <= 0
           or len(value.as_tuple().digits) > 40 or abs(value.adjusted()) > 40 for value in numbers):
        return PolicyDecision(('required_quantity_or_specification_unknown',))
    specs = {key: reported(getattr(instrument, key), 'typed instrument evidence') for key in numeric_keys}
    for key in ('symbol', 'active', 'market_type', 'settlement', 'min_price', 'max_price', 'max_notional'):
        specs[key] = reported(getattr(instrument, key), 'typed instrument evidence')
    specs['id'] = reported(instrument.market_id, 'typed instrument evidence')
    parts = [intent.side, 'symbol=' + intent.symbol, 'action=' + intent.action,
             'position_side=' + intent.position_side, 'current_qty=' + str(position.base_quantity),
             'qty=' + str(intent.quantity), 'price=' + str(intent.price), 'reduce_only=true',
             # This unused preview ID is not a reservation or exchange client ID.
             # Avoid randomness when reusing preparation for pure policy checks.
             'cl_id=offline_policy_validation', 'dry']
    try:
        order = prepare_order(specs, parts, RiskLimits())
    except (SafetyError, TypeError, AttributeError):
        return PolicyDecision(('order_validation_failed',))
    payload = tuple(sorted({
        'symbol': order.symbol, 'market_id': instrument.market_id, 'side': order.side,
        'action': order.action, 'position_side': intent.position_side, 'quantity': str(order.base_qty),
        'requested_quantity': str(intent.quantity), 'contracts': str(order.contracts),
        'position_quantity': str(position.base_quantity), 'price': str(order.price),
        'order_type': 'limit', 'time_in_force': 'IOC', 'reduce_only': 'true',
    }.items()))
    # A bounded serialization of immutable, typed evidence, never raw API data.
    context = repr(values[1:])
    validated = ValidatedIntent(binding, payload, fingerprint(canonical_payload(payload)),
                                fingerprint(context), now, min(deadlines))
    return PolicyDecision(validated=validated)
