"""Pure, synthetic-only IOC reconciliation proposals. No execution authority.

Inputs are normalized assertions, NOT native Phemex schemas or authenticated
receipts. Even a successful proposal cannot resolve a journal or permit a send.
The caller must retain the full observation history across invocations.
"""
from dataclasses import dataclass, field
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from fractions import Fraction
import re

from execution_policy import Provenance, utc_time
from phemex_request_compiler import CompiledRequest, check_compiled
from safety import SafetyError


class Decision(str, Enum):
    WAIT = 'WAIT'
    UNRESOLVED = 'UNRESOLVED'
    PROPOSED_OUTCOME = 'PROPOSED_OUTCOME'


class CandidateOutcome(str, Enum):
    UNFILLED = 'UNFILLED'
    PARTIALLY_FILLED = 'PARTIALLY_FILLED'
    FILLED = 'FILLED'
    REJECTED = 'REJECTED'


class OrderStatus(str, Enum):
    ACCEPTED = 'ACCEPTED'
    PARTIALLY_FILLED = 'PARTIALLY_FILLED'
    FILLED = 'FILLED'
    CANCELED = 'CANCELED'
    EXPIRED = 'EXPIRED'
    REJECTED = 'REJECTED'


class ObservationKind(str, Enum):
    ORDER = 'ORDER'
    FILLS = 'FILLS'
    POSITIONS = 'POSITIONS'


@dataclass(frozen=True)
class ClockInfo:
    """Synthetic timing assertion only, not clock synchronization evidence."""
    domain: str
    uncertainty: timedelta


@dataclass(frozen=True)
class ObservationStamp:
    evidence_id: str
    kind: ObservationKind
    started_at: datetime
    completed_at: datetime
    complete: bool
    provenance: Provenance = Provenance.SYNTHETIC
    clock: ClockInfo = None


@dataclass(frozen=True)
class Correlation:
    operation_id: str
    client_order_id: str
    exchange_order_id: str
    account_binding: str = field(repr=False)
    request_fingerprint: str
    environment: str = 'testnet'
    symbol: str = 'BTC/USDT:USDT'
    position_side: str = 'long'


@dataclass(frozen=True)
class PositionObservation:
    stamp: ObservationStamp
    account_binding: str = field(repr=False)
    long_quantity: Decimal
    short_quantity: Decimal
    environment: str = 'testnet'
    symbol: str = 'BTC/USDT:USDT'
    position_mode: str = 'hedge'
    unit: str = 'BTC_BASE'


@dataclass(frozen=True)
class OperationContext:
    operation_id: str
    request: CompiledRequest = field(repr=False)
    baseline: PositionObservation
    attempted_at: datetime
    deadline: datetime
    exchange_order_id: str = None
    clock: ClockInfo = None


@dataclass(frozen=True)
class OrderObservation:
    stamp: ObservationStamp
    correlation: Correlation
    # Exact normalized business fields, checked against the existing compiler.
    parameters: tuple
    status: OrderStatus
    cumulative_filled: Decimal
    updated_at: datetime
    unit: str = 'BTC_BASE'
    event_clock: ClockInfo = None


@dataclass(frozen=True)
class FillObservation:
    correlation: Correlation
    execution_id: str
    executed_at: datetime
    quantity: Decimal
    price: Decimal
    unit: str = 'BTC_BASE'
    clock: ClockInfo = None


@dataclass(frozen=True)
class FillCoverage:
    stamp: ObservationStamp
    correlation: Correlation
    window_start: datetime
    window_end: datetime
    pages_complete: bool
    fills: tuple


@dataclass(frozen=True)
class ReconciliationResult:
    decision: Decision
    reason: str
    outcome: CandidateOutcome = None
    terminal_status: OrderStatus = None
    filled_quantity: Decimal = None
    remaining_position: Decimal = None
    supporting_evidence: tuple = ()

    @property
    def execution_authority(self):
        return False

    @property
    def authenticated_evidence_verified(self):
        return False

    @property
    def may_resolve_journal(self):
        return False

    @property
    def may_resubmit(self):
        return False


TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED,
                      OrderStatus.EXPIRED, OrderStatus.REJECTED})
FRESHNESS = timedelta(seconds=5)


class _Invalid(Exception):
    pass


def _require(condition, reason):
    if not condition:
        raise _Invalid(reason)


def _identifier(value, pattern=r'[A-Za-z0-9_-]{1,80}'):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _quantity(value, step, *, positive=False):
    _require(type(value) is Decimal and value.is_finite()
             and len(value.as_tuple().digits) <= 40 and abs(value.adjusted()) <= 40,
             'invalid_decimal')
    _require(value > 0 if positive else value >= 0, 'invalid_quantity')
    _require((Fraction(value) / Fraction(step)).denominator == 1, 'off_grid_quantity')


def _clock_info(clock, expected=None):
    _require(type(clock) is ClockInfo and _identifier(clock.domain, r'synthetic_[A-Za-z0-9_-]{1,64}'),
             'clock_alignment_unknown')
    # Only exact, explicitly comparable fixture clocks are supported. A UTC
    # timezone or matching label does not establish real clock synchronization.
    _require(type(clock.uncertainty) is timedelta and clock.uncertainty == timedelta(0),
             'timestamp_precision_unknown')
    _require(expected is None or clock == expected, 'clock_domain_mismatch')


def _stamp(stamp, kind, now, clock):
    _require(type(stamp) is ObservationStamp and stamp.kind is kind
             and _identifier(stamp.evidence_id) and type(stamp.complete) is bool,
             'invalid_observation_stamp')
    # There is deliberately no authenticated collector seam in this checkpoint.
    # Synthetic proposals are untrusted; an authenticated-looking label is refused.
    _require(stamp.provenance is Provenance.SYNTHETIC, 'observation_origin_not_established')
    _clock_info(stamp.clock, clock)
    start, end = utc_time(stamp.started_at), utc_time(stamp.completed_at)
    _require(start <= end <= now, 'invalid_observation_time')
    return start, end


def _correlation(value, op, exchange_id):
    _require(type(value) is Correlation, 'invalid_correlation')
    request = op.request
    _require(all(type(item) is str for item in (value.operation_id, value.client_order_id,
                                              value.exchange_order_id, value.account_binding,
                                              value.request_fingerprint, value.environment,
                                              value.symbol, value.position_side))
             and value.operation_id == op.operation_id
             and value.client_order_id == request.client_order_id
             and value.account_binding == request.plan.account_binding
             and value.request_fingerprint == request.request_fingerprint
             and value.environment == 'testnet' and value.symbol == 'BTC/USDT:USDT'
             and value.position_side == dict(request.plan.payload)['position_side']
             and _identifier(value.exchange_order_id)
             and (exchange_id is None or value.exchange_order_id == exchange_id),
             'correlation_mismatch')


def _position(value, op, now, step):
    _require(type(value) is PositionObservation, 'invalid_position_observation')
    start, end = _stamp(value.stamp, ObservationKind.POSITIONS, now, op.clock)
    _require(all(type(item) is str for item in (value.account_binding, value.environment,
                                              value.symbol, value.position_mode, value.unit))
             and value.account_binding == op.request.plan.account_binding
             and value.environment == 'testnet' and value.symbol == 'BTC/USDT:USDT'
             and value.position_mode == 'hedge' and value.unit == 'BTC_BASE',
             'position_binding_mismatch')
    _quantity(value.long_quantity, step)
    _quantity(value.short_quantity, step)
    return start, end


def reconcile(op, orders=(), fills=None, positions=(), *, now):
    """Return an untrusted proposal, WAIT, or a static UNRESOLVED diagnostic.

    All I/O and journal transitions are outside this module. Missing/incomplete
    evidence waits only before the supplied deadline. Invalid evidence fails
    immediately. A deadline never proves failure, and late fresh evidence may
    subsequently reconcile the SAME IDs. No automatic reads or retries exist.
    """
    try:
        return _reconcile(op, orders, fills, positions, utc_time(now))
    except _Invalid as error:
        return ReconciliationResult(Decision.UNRESOLVED, error.args[0])
    except (SafetyError, ValueError, TypeError, AttributeError, ArithmeticError):
        return ReconciliationResult(Decision.UNRESOLVED, 'invalid_reconciliation_input')


def _reconcile(op, orders, fills, positions, now):
    _require(type(op) is OperationContext and type(op.request) is CompiledRequest,
             'invalid_operation')
    check_compiled(op.request)  # Reuse policy/preparation/compiler; no duplicated order validation.
    _clock_info(op.clock)
    _require(_identifier(op.operation_id, r'op_[0-9a-f]{32}'), 'invalid_operation_identity')
    _require(op.exchange_order_id is None or _identifier(op.exchange_order_id), 'invalid_exchange_identity')
    _require(type(orders) is tuple and len(orders) <= 256
             and type(positions) is tuple and len(positions) <= 256,
             'invalid_observation_collection')
    if op.attempted_at is None:
        return ReconciliationResult(Decision.UNRESOLVED, 'submission_not_attempted')
    attempt, deadline = utc_time(op.attempted_at), utc_time(op.deadline)
    _require(op.request.plan.validated_at <= attempt < op.request.plan.expires_at
             and attempt <= now and attempt < deadline, 'invalid_attempt_time')
    values = dict(op.request.plan.payload)
    quantity, current = Decimal(values['quantity']), Decimal(values['position_quantity'])
    instrument = op.request.context[3]
    step = instrument.quantity_step
    baseline_start, baseline_end = _position(op.baseline, op, now, step)
    _require(op.baseline.stamp.complete and baseline_end <= attempt
             and attempt - baseline_start < FRESHNESS, 'invalid_baseline')
    selected = 'long_quantity' if values['position_side'] == 'long' else 'short_quantity'
    opposite = 'short_quantity' if selected == 'long_quantity' else 'long_quantity'
    _require(getattr(op.baseline, selected) == current, 'baseline_quantity_mismatch')

    pending = []
    evidence = {op.baseline.stamp.evidence_id: op.baseline}

    def remember(item):
        key = item.stamp.evidence_id
        _require(key not in evidence or evidence[key] == item, 'conflicting_evidence_id')
        evidence[key] = item

    ordered = []
    exchange_ids = set()
    for order in orders:
        _require(type(order) is OrderObservation, 'invalid_order_observation')
        start, end = _stamp(order.stamp, ObservationKind.ORDER, now, op.clock)
        _clock_info(order.event_clock, op.clock)
        remember(order)
        _correlation(order.correlation, op, op.exchange_order_id)
        exchange_ids.add(order.correlation.exchange_order_id)
        updated = utc_time(order.updated_at)
        _require(attempt <= start and attempt <= updated <= end, 'invalid_order_time')
        _require(type(order.parameters) is tuple and order.parameters == op.request.parameters
                 and all(type(pair) is tuple and len(pair) == 2
                         and type(pair[0]) is str
                         and type(pair[1]) is type(expected[1])
                         for pair, expected in zip(order.parameters, op.request.parameters))
                 and type(order.unit) is str and order.unit == 'BTC_BASE' and type(order.status) is OrderStatus,
                 'order_parameters_mismatch')
        _quantity(order.cumulative_filled, step)
        filled = order.cumulative_filled
        _require(filled <= quantity, 'overfill')
        _require((order.status is not OrderStatus.ACCEPTED or filled == 0)
                 and (order.status is not OrderStatus.PARTIALLY_FILLED or 0 < filled < quantity)
                 and (order.status is not OrderStatus.FILLED or filled == quantity)
                 and (order.status not in (OrderStatus.CANCELED, OrderStatus.EXPIRED) or filled < quantity)
                 and (order.status is not OrderStatus.REJECTED or filled == 0), 'status_quantity_conflict')
        ordered.append((updated, order))
    _require(len(exchange_ids) <= 1, 'conflicting_exchange_ids')
    exchange_id = next(iter(exchange_ids), op.exchange_order_id)
    ordered.sort(key=lambda pair: pair[0])
    previous = None
    for updated, order in ordered:
        if previous:
            last_time, last = previous
            _require(order.cumulative_filled >= last.cumulative_filled, 'decreasing_cumulative_fill')
            if updated == last_time or last.status in TERMINAL:
                _require(order.status is last.status and order.cumulative_filled == last.cumulative_filled,
                         'conflicting_order_history')
        previous = updated, order
    for updated, order in ordered:
        if not order.stamp.complete:
            # Only a later complete read of this exact version can attest the
            # missing completeness. Preserve the old record for every fact check.
            superseded = any(
                other.stamp.complete and other_updated == updated
                and other.correlation == order.correlation and other.parameters == order.parameters
                and other.status is order.status and other.cumulative_filled == order.cumulative_filled
                and other.unit == order.unit
                and utc_time(other.stamp.started_at) >= utc_time(order.stamp.completed_at)
                for other_updated, other in ordered)
            if not superseded:
                pending.append('incomplete_order')
    latest = ordered[-1][1] if ordered else None
    if latest:
        # For equal exchange timestamps, require a fresh repeated observation too.
        latest = max((o for t, o in ordered if t == ordered[-1][0]),
                     key=lambda o: (utc_time(o.stamp.started_at), utc_time(o.stamp.completed_at),
                                    o.stamp.evidence_id))
        _require(now - utc_time(latest.stamp.started_at) < FRESHNESS, 'stale_order')
    else:
        pending.append('order_not_observed')

    total = Fraction(0)
    if fills is not None:
        _require(type(fills) is FillCoverage and type(fills.fills) is tuple
                 and len(fills.fills) <= 4096 and type(fills.pages_complete) is bool,
                 'invalid_fill_coverage')
        fill_start, fill_end = _stamp(fills.stamp, ObservationKind.FILLS, now, op.clock)
        remember(fills)
        _correlation(fills.correlation, op, exchange_id)
        window_start, window_end = utc_time(fills.window_start), utc_time(fills.window_end)
        _require(window_start <= attempt <= window_end <= fill_end and attempt <= fill_start,
                 'invalid_fill_window')
        executions = {}
        for fill in fills.fills:
            _require(type(fill) is FillObservation and _identifier(fill.execution_id), 'invalid_execution_identity')
            _correlation(fill.correlation, op, fills.correlation.exchange_order_id)
            _clock_info(fill.clock, op.clock)
            _require(type(fill.unit) is str and fill.unit == 'BTC_BASE', 'fill_unit_mismatch')
            _quantity(fill.quantity, step, positive=True)
            _quantity(fill.price, instrument.price_tick, positive=True)
            executed = utc_time(fill.executed_at)
            _require(attempt <= executed <= window_end, 'invalid_execution_time')
            _require((fill.price >= Decimal(values['price']) if values['side'] == 'sell'
                      else fill.price <= Decimal(values['price'])), 'fill_violates_limit')
            _require(fill.execution_id not in executions or executions[fill.execution_id] == fill,
                     'conflicting_execution_id')
            executions[fill.execution_id] = fill
        total = sum((Fraction(fill.quantity) for fill in executions.values()), Fraction(0))
        _require(total <= Fraction(quantity), 'fill_total_exceeds_order')
        _require(now - fill_start < FRESHNESS, 'stale_fill_coverage')
        if not fills.stamp.complete or not fills.pages_complete:
            pending.append('incomplete_fill_coverage')
        if latest and latest.status in TERMINAL:
            if fill_start < utc_time(latest.stamp.completed_at) or window_end < utc_time(latest.updated_at):
                pending.append('fill_coverage_precedes_terminal')
            _require(all(utc_time(fill.executed_at) <= utc_time(latest.updated_at)
                         for fill in executions.values()), 'execution_after_terminal')
            if fills.stamp.complete and fills.pages_complete:
                _require(total == Fraction(latest.cumulative_filled), 'order_fill_mismatch')
        # Check the whole retained history, not just its final-looking member.
        # Prefix sums keep exact arithmetic bounded without repeatedly summing
        # up to 4096 executions for each of the 256 order observations.
        events = sorted((utc_time(fill.executed_at), Fraction(fill.quantity))
                        for fill in executions.values())
        times = tuple(time for time, _ in events)
        cumulative = [Fraction(0)]
        for _, amount in events:
            cumulative.append(cumulative[-1] + amount)
        for updated, order in ordered:
            before = bisect_left(times, updated)
            _require(before == len(times) or times[before] != updated, 'ambiguous_execution_order_time')
            if order.status in TERMINAL:
                _require(before == len(times), 'execution_after_terminal')
            _require(cumulative[before] <= Fraction(order.cumulative_filled), 'order_fill_history_mismatch')
            if fills.stamp.complete and fills.pages_complete:
                if fill_start < utc_time(order.stamp.completed_at) or window_end < updated:
                    pending.append('fill_coverage_precedes_order_observation')
                else:
                    _require(cumulative[before] == Fraction(order.cumulative_filled), 'order_fill_history_mismatch')
    else:
        pending.append('fill_coverage_missing')

    position_rows = []
    for position in positions:
        start, end = _position(position, op, now, step)
        remember(position)
        _require(attempt <= start, 'position_precedes_attempt')
        _require(getattr(position, opposite) == getattr(op.baseline, opposite), 'opposite_leg_changed')
        _require(getattr(position, selected) <= current, 'selected_leg_exceeds_baseline')
        if not position.stamp.complete:
            pending.append('incomplete_positions')
        position_rows.append((start, end, position))
    position_rows.sort(key=lambda row: (row[0], row[1]))
    # All pairs: a short nested interval must not conceal a conflict with its
    # containing interval. At most 256 rows bound this to 32640 comparisons.
    for index, left in enumerate(position_rows):
        for right in position_rows[index + 1:]:
            if left[:2] == right[:2]:
                _require(getattr(left[2], selected) == getattr(right[2], selected), 'conflicting_position_snapshot')
            elif left[1] > right[0]:
                _require(getattr(left[2], selected) == getattr(right[2], selected), 'overlapping_position_snapshots')
            else:
                _require(getattr(right[2], selected) <= getattr(left[2], selected), 'selected_leg_increased')
    post = position_rows[-1][2] if position_rows else None
    if post:
        _require(now - utc_time(post.stamp.started_at) < FRESHNESS, 'stale_positions')
    else:
        pending.append('post_positions_missing')

    if latest and latest.status in TERMINAL and post and fills:
        if utc_time(post.stamp.started_at) < max(utc_time(latest.stamp.completed_at),
                                                utc_time(fills.stamp.completed_at)):
            pending.append('positions_precede_reconciliation_reads')
        else:
            for start, _, row in position_rows:
                if start >= max(utc_time(latest.stamp.completed_at), utc_time(fills.stamp.completed_at)):
                    _require(Fraction(getattr(row, selected)) == Fraction(current) - Fraction(latest.cumulative_filled),
                             'position_effect_mismatch')
    if latest is None or latest.status not in TERMINAL:
        pending.append('terminal_order_missing')
    if fills is not None:
        complete_timeline = fills.stamp.complete and fills.pages_complete
        terminal_coverage = (latest is not None and latest.status in TERMINAL
                             and window_end >= utc_time(latest.updated_at)
                             and fill_start >= utc_time(latest.stamp.completed_at))
        for start, end, position in position_rows:
            if not position.stamp.complete:
                # Known facts still participate in the existing progression and
                # contradiction checks. Never invent the missing completeness.
                continue
            before = bisect_left(times, start)    # Strictly before collection.
            through = bisect_right(times, end)   # Possibly by collection end, ties included.
            reduction = Fraction(current) - Fraction(getattr(position, selected))
            _require(reduction >= cumulative[before], 'position_execution_timeline_mismatch')
            if not complete_timeline:
                # Missing pages may contain earlier fills. Known fills provide
                # only a lower bound; their absence is not proof of zero fills.
                continue
            if end > window_end and not terminal_coverage:
                pending.append('position_timeline_not_covered')
                continue
            _require(reduction <= cumulative[through], 'position_execution_timeline_mismatch')
            # A value within the possible range does not prove the snapshot's
            # instant. Do not choose an endpoint, even if a later read matches.
            _require(before == through, 'position_execution_time_ambiguous')
    if pending:
        return ReconciliationResult(Decision.WAIT if now < deadline else Decision.UNRESOLVED, pending[0])
    outcome = (CandidateOutcome.REJECTED if latest.status is OrderStatus.REJECTED else
               CandidateOutcome.FILLED if latest.status is OrderStatus.FILLED else
               CandidateOutcome.PARTIALLY_FILLED if latest.cumulative_filled > 0 else CandidateOutcome.UNFILLED)
    return ReconciliationResult(Decision.PROPOSED_OUTCOME, 'synthetic_evidence_only', outcome,
                                latest.status, latest.cumulative_filled, getattr(post, selected),
                                tuple(sorted(evidence)))
