"""Synthetic assertions only; no Phemex observations or authenticated authority."""
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext
from itertools import permutations
import ast
import multiprocessing
from pathlib import Path
import tempfile
from unittest.mock import patch

import ioc_reconciliation as engine
from execution_policy import Provenance
from ioc_reconciliation import (CandidateOutcome, ClockInfo, Correlation, Decision, FillCoverage,
                                FillObservation, ObservationKind, ObservationStamp,
                                OperationContext, OrderObservation, OrderStatus,
                                PositionObservation, reconcile)
from offline_simulation import Outcome, SimulationJournal, simulate_submission
from operation_journal import OperationJournal, State
from tests.execution_fixtures import NOW, evidence, validated
from tests.helpers import OfflineTestCase
from tests.test_phemex_request_compiler import compiled
from tests.test_offline_simulation import crash_simulation


CLOCK = ClockInfo('synthetic_shared', timedelta(0))


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def stamp(name, kind, start, end, **changes):
    return replace(ObservationStamp(name, kind, at(start), at(end), True, clock=CLOCK), **changes)


def scenario(status=OrderStatus.FILLED, filled='0.001', *, leg='long', action='reduce'):
    values = list(evidence())
    values[0] = replace(values[0], position_side=leg, side='sell' if leg == 'long' else 'buy',
                        action=action, quantity=Decimal('0.002' if action == 'close' else '0.001'))
    values[2] = replace(values[2], position_side=leg)
    values[3] = replace(values[3], quantity_step=Decimal('0.0001'), contract_step=Decimal('0.0001'),
                        min_contracts=Decimal('0.0001'))
    request = compiled(values)
    baseline = PositionObservation(stamp('baseline', ObservationKind.POSITIONS, 0, 0.1),
                                   request.plan.account_binding, Decimal('0.002'), Decimal('0.002'))
    op = OperationContext('op_' + '1' * 32, request, baseline, at(1), at(31), clock=CLOCK)
    binding = Correlation(op.operation_id, request.client_order_id, 'exchange_1',
                          request.plan.account_binding, request.request_fingerprint, position_side=leg)
    order = OrderObservation(stamp('order', ObservationKind.ORDER, 2, 2.1), binding,
                             request.parameters, status, Decimal(filled), at(1.5), event_clock=CLOCK)
    executions = (() if Decimal(filled) == 0 else
                  (FillObservation(binding, 'execution_1', at(1.25), Decimal(filled), Decimal('60000'), clock=CLOCK),))
    fills = FillCoverage(stamp('fills', ObservationKind.FILLS, 2.2, 2.3), binding, at(1), at(2.2),
                         True, executions)
    remaining = Decimal('0.002') - Decimal(filled)
    post = replace(baseline, stamp=stamp('post', ObservationKind.POSITIONS, 2.4, 2.5),
                   **{leg + '_quantity': remaining})
    return op, (order,), fills, (post,)


class IOCReconciliationTests(OfflineTestCase):
    def result(self, values=None, *, now=None):
        return reconcile(*(scenario() if values is None else values), now=at(3) if now is None else now)

    def unresolved(self, values, reason=None, *, now=None):
        result = self.result(values, now=now)
        self.assertIs(result.decision, Decision.UNRESOLVED)
        if reason:
            self.assertEqual(result.reason, reason)
        self.assertIsNone(result.outcome)
        return result

    def test_terminal_outcome_matrix_both_legs(self):
        cases = ((OrderStatus.CANCELED, '0', CandidateOutcome.UNFILLED),
                 (OrderStatus.EXPIRED, '0', CandidateOutcome.UNFILLED),
                 (OrderStatus.CANCELED, '0.0004', CandidateOutcome.PARTIALLY_FILLED),
                 (OrderStatus.EXPIRED, '0.0004', CandidateOutcome.PARTIALLY_FILLED),
                 (OrderStatus.FILLED, '0.001', CandidateOutcome.FILLED),
                 (OrderStatus.REJECTED, '0', CandidateOutcome.REJECTED))
        for leg in ('long', 'short'):
            for status, filled, outcome in cases:
                with self.subTest(leg=leg, status=status, filled=filled):
                    values = scenario(status, filled, leg=leg)
                    result = self.result(values)
                    self.assertIs(result.decision, Decision.PROPOSED_OUTCOME)
                    self.assertIs(result.outcome, outcome)
                    self.assertIs(result.terminal_status, status)
                    self.assertEqual(result.filled_quantity, Decimal(filled))
                    self.assertEqual(result.remaining_position, Decimal('0.002') - Decimal(filled))
                    self.assertEqual(result.supporting_evidence, ('baseline', 'fills', 'order', 'post'))

    def test_close_requires_observed_zero_for_both_legs(self):
        for leg in ('long', 'short'):
            values = scenario(OrderStatus.FILLED, '0.002', leg=leg, action='close')
            self.assertEqual(self.result(values).remaining_position, 0)
            op, orders, fills, posts = values
            self.unresolved((op, orders, fills, (replace(posts[0], **{leg + '_quantity': Decimal('0.0001')}),)),
                            'position_effect_mismatch')

    def test_close_ioc_partial_fill_is_not_completed_close(self):
        result = self.result(scenario(OrderStatus.CANCELED, '0.001', action='close'))
        self.assertIs(result.outcome, CandidateOutcome.PARTIALLY_FILLED)
        self.assertEqual(result.remaining_position, Decimal('0.001'))

    def test_accepted_and_nonterminal_partial_wait_then_remain_unresolved(self):
        for status, qty in ((OrderStatus.ACCEPTED, '0'), (OrderStatus.PARTIALLY_FILLED, '0.0004')):
            values = scenario(status, qty)
            self.assertIs(self.result(values).decision, Decision.WAIT)
            # Fresh reads of an old still-open order after the deadline.
            op, orders, fills, posts = values
            orders = (replace(orders[0], stamp=stamp('late_order', ObservationKind.ORDER, 32, 32.1)),)
            fills = replace(fills, stamp=stamp('late_fills', ObservationKind.FILLS, 32.2, 32.3), window_end=at(32.2))
            posts = (replace(posts[0], stamp=stamp('late_post', ObservationKind.POSITIONS, 32.4, 32.5)),)
            self.unresolved((op, orders, fills, posts), 'terminal_order_missing', now=at(33))

    def test_missing_orders_history_or_timeout_never_proves_failure(self):
        op, _, _, _ = scenario()
        for now, expected in ((at(3), Decision.WAIT), (at(31), Decision.UNRESOLVED), (at(1000), Decision.UNRESOLVED)):
            result = self.result((op, (), None, ()), now=now)
            self.assertIs(result.decision, expected)
            self.assertFalse(result.may_resubmit)
            self.assertIsNone(result.outcome)
            self.assertEqual(op.operation_id, 'op_' + '1' * 32)
            self.assertEqual(op.request.client_order_id, 'tn_' + '1' * 32)

    def test_not_attempted_does_not_infer_execution(self):
        op, _, _, _ = scenario()
        self.unresolved((replace(op, attempted_at=None), (), None, ()), 'submission_not_attempted')

    def test_missing_order_fills_or_positions_cannot_propose(self):
        op, orders, fills, posts = scenario()
        for values in ((op, (), fills, posts), (op, orders, None, posts), (op, orders, fills, ())):
            self.assertIs(self.result(values).decision, Decision.WAIT)

    def test_position_change_alone_cannot_establish_execution(self):
        op, _, _, posts = scenario()
        self.assertIs(self.result((op, (), None, posts)).decision, Decision.WAIT)

    def test_incomplete_pages_and_missing_completeness_do_not_propose(self):
        op, orders, fills, posts = scenario()
        cases = ((op, orders, replace(fills, pages_complete=False), posts),
                 (op, orders, replace(fills, stamp=replace(fills.stamp, complete=False)), posts),
                 (op, (replace(orders[0], stamp=replace(orders[0].stamp, complete=False)),), fills, posts),
                 (op, orders, fills, (replace(posts[0], stamp=replace(posts[0].stamp, complete=False)),)))
        for values in cases:
            self.assertIs(self.result(values).decision, Decision.WAIT)

    def test_duplicate_fills_deduplicated_by_exact_execution_identity(self):
        op, orders, fills, posts = scenario()
        duplicate = replace(fills, fills=fills.fills + fills.fills)
        self.assertEqual(self.result((op, orders, duplicate, posts)).filled_quantity, Decimal('0.001'))

    def test_conflicting_duplicate_execution_id_is_rejected(self):
        op, orders, fills, posts = scenario()
        fill = fills.fills[0]
        for changes in ({'quantity': Decimal('0.0001')}, {'price': Decimal('60001')}, {'executed_at': at(1.3)}):
            self.unresolved((op, orders, replace(fills, fills=(fill, replace(fill, **changes))), posts),
                            'conflicting_execution_id')

    def test_missing_execution_id_never_deduplicated_by_time_or_price(self):
        op, orders, fills, posts = scenario()
        self.unresolved((op, orders, replace(fills, fills=(replace(fills.fills[0], execution_id=''),)), posts),
                        'invalid_execution_identity')

    def test_distinct_fill_ids_are_summed_and_order_independent(self):
        op, orders, fills, posts = scenario()
        fill = fills.fills[0]
        a = replace(fill, quantity=Decimal('0.0004'))
        b = replace(fill, execution_id='execution_2', quantity=Decimal('0.0006'), executed_at=at(1.3))
        self.assertEqual(self.result((op, orders, replace(fills, fills=(b, a, b)), posts)).outcome,
                         CandidateOutcome.FILLED)

    def test_order_history_reordering_and_exact_duplicates(self):
        op, orders, fills, posts = scenario()
        early = replace(orders[0], stamp=stamp('early', ObservationKind.ORDER, 1.1, 1.2),
                        updated_at=at(1.05), status=OrderStatus.ACCEPTED, cumulative_filled=Decimal('0'))
        a = self.result((op, (orders[0], early, early), fills, posts))
        b = self.result((op, (early, orders[0]), fills, posts))
        self.assertEqual(a, b)

    def test_repeated_terminal_read_timestamps_have_deterministic_tie_break(self):
        op, orders, fills, posts = scenario()
        repeated = replace(orders[0], stamp=stamp('repeat', ObservationKind.ORDER, 2, 2.25))
        a = self.result((op, orders + (repeated,), fills, posts))
        b = self.result((op, (repeated,) + orders, fills, posts))
        self.assertEqual(a, b)
        self.assertIs(a.decision, Decision.WAIT)

    def test_observation_and_fill_collections_are_bounded(self):
        op, orders, fills, posts = scenario()
        for values in ((op, orders * 257, fills, posts), (op, orders, fills, posts * 257),
                       (op, orders, replace(fills, fills=fills.fills * 4097), posts)):
            self.unresolved(values)

    def test_decreasing_cumulative_fill_rejected_in_either_input_order(self):
        op, orders, fills, posts = scenario(OrderStatus.CANCELED, '0.0004')
        early = replace(orders[0], stamp=stamp('early', ObservationKind.ORDER, 1.1, 1.2),
                        updated_at=at(1.05), status=OrderStatus.PARTIALLY_FILLED, cumulative_filled=Decimal('0.0005'))
        for history in ((early, orders[0]), (orders[0], early)):
            self.unresolved((op, history, fills, posts), 'decreasing_cumulative_fill')

    def test_conflicting_terminal_and_equal_timestamp_states_rejected(self):
        op, orders, fills, posts = scenario(OrderStatus.CANCELED, '0')
        for time in (1.5, 1.6):
            other = replace(orders[0], stamp=stamp('other', ObservationKind.ORDER, 2, 2.1),
                            status=OrderStatus.REJECTED, updated_at=at(time))
            self.unresolved((op, (other, orders[0]), fills, posts), 'conflicting_order_history')

    def test_nonterminal_after_terminal_rejected(self):
        op, orders, fills, posts = scenario(OrderStatus.CANCELED, '0.0004')
        other = replace(orders[0], stamp=stamp('other', ObservationKind.ORDER, 2, 2.1),
                        status=OrderStatus.PARTIALLY_FILLED, updated_at=at(1.6))
        self.unresolved((op, orders + (other,), fills, posts), 'conflicting_order_history')

    def test_reused_evidence_id_cannot_conceal_different_observations(self):
        op, orders, fills, posts = scenario()
        other = replace(orders[0], updated_at=at(1.6))
        self.unresolved((op, orders + (other,), fills, posts), 'conflicting_evidence_id')

    def test_all_correlation_bindings_enforced_for_orders_coverage_and_fills(self):
        op, orders, fills, posts = scenario()
        changes = ({'operation_id': 'op_' + '2' * 32}, {'client_order_id': 'tn_' + '2' * 32},
                   {'exchange_order_id': 'other'}, {'account_binding': 'b' * 64},
                   {'request_fingerprint': '0' * 64}, {'environment': 'live'},
                   {'symbol': 'ETH/USDT:USDT'}, {'position_side': 'short'})
        op = replace(op, exchange_order_id='exchange_1')
        for change in changes:
            bad = replace(orders[0].correlation, **change)
            for values in ((op, (replace(orders[0], correlation=bad),), fills, posts),
                           (op, orders, replace(fills, correlation=bad), posts),
                           (op, orders, replace(fills, fills=(replace(fills.fills[0], correlation=bad),)), posts)):
                with self.subTest(change=change): self.unresolved(values, 'correlation_mismatch')

    def test_unknown_exchange_id_can_only_be_established_by_matching_order(self):
        op, orders, fills, posts = scenario()
        self.assertIs(self.result((op, orders, fills, posts)).decision, Decision.PROPOSED_OUTCOME)
        other = replace(orders[0], stamp=replace(orders[0].stamp, evidence_id='other'),
                        correlation=replace(orders[0].correlation, exchange_order_id='other'))
        self.unresolved((op, orders + (other,), fills, posts), 'conflicting_exchange_ids')

    def test_order_business_fields_and_extra_fields_cannot_change(self):
        op, orders, fills, posts = scenario()
        for key, value in op.request.parameters:
            changed = dict(op.request.parameters)
            changed[key] = False if type(value) is bool else 'changed'
            params = tuple(sorted(changed.items()))
            self.unresolved((op, (replace(orders[0], parameters=params),), fills, posts), 'order_parameters_mismatch')
        for params in (op.request.parameters[:-1], op.request.parameters + (('extra', 'x'),),
                       tuple((k, 1 if k == 'reduceOnly' else v) for k, v in op.request.parameters)):
            self.unresolved((op, (replace(orders[0], parameters=params),), fills, posts), 'order_parameters_mismatch')

    def test_compiler_rejects_tampered_operation_without_new_validation(self):
        op, orders, fills, posts = scenario()
        request = replace(op.request, parameters=op.request.parameters + (('extra', 'value'),))
        self.unresolved((replace(op, request=request), orders, fills, posts), 'invalid_reconciliation_input')

    def test_position_scope_mode_and_base_units_are_required(self):
        op, orders, fills, posts = scenario()
        for changes in ({'account_binding': 'b' * 64}, {'environment': 'live'},
                        {'symbol': 'ETH/USDT:USDT'}, {'position_mode': 'one_way'}, {'unit': 'CONTRACTS'}):
            self.unresolved((op, orders, fills, (replace(posts[0], **changes),)), 'position_binding_mismatch')
            self.unresolved((replace(op, baseline=replace(op.baseline, **changes)), orders, fills, posts),
                            'position_binding_mismatch')

    def test_opposite_leg_change_and_selected_leg_increase_are_unresolved(self):
        op, orders, fills, posts = scenario()
        self.unresolved((op, orders, fills, (replace(posts[0], short_quantity=Decimal('0.001')),)),
                        'opposite_leg_changed')
        early = replace(posts[0], stamp=stamp('early_post', ObservationKind.POSITIONS, 1.7, 1.8),
                        long_quantity=Decimal('0.0005'))
        self.unresolved((op, orders, fills, posts + (early,)), 'selected_leg_increased')

    def test_position_effect_and_all_terminal_snapshots_must_agree(self):
        op, orders, fills, posts = scenario()
        bad = replace(posts[0], stamp=stamp('later', ObservationKind.POSITIONS, 2.6, 2.7),
                      long_quantity=Decimal('0.0009'))
        self.unresolved((op, orders, fills, posts + (bad,)), 'position_effect_mismatch')

    def test_conflicting_equal_or_overlapping_position_reads_rejected(self):
        op, orders, fills, posts = scenario()
        for start in (2.4, 2.45):
            other = replace(posts[0], stamp=stamp('other', ObservationKind.POSITIONS, start, 2.5),
                            long_quantity=Decimal('0.0009'))
            result = self.unresolved((op, orders, fills, posts + (other,)))
            self.assertIn(result.reason, ('conflicting_position_snapshot', 'overlapping_position_snapshots'))

    def test_reordered_identical_position_snapshots_are_deterministic(self):
        op, orders, fills, posts = scenario()
        repeated = replace(posts[0], stamp=stamp('repeat', ObservationKind.POSITIONS, 2.6, 2.7))
        self.assertEqual(self.result((op, orders, fills, posts + (repeated,))),
                         self.result((op, orders, fills, (repeated,) + posts)))

    def test_bad_status_quantity_combinations_and_overfills(self):
        op, orders, fills, posts = scenario()
        for status, qty in ((OrderStatus.ACCEPTED, '0.0001'), (OrderStatus.PARTIALLY_FILLED, '0'),
                            (OrderStatus.PARTIALLY_FILLED, '0.001'), (OrderStatus.FILLED, '0.0009'),
                            (OrderStatus.CANCELED, '0.001'), (OrderStatus.REJECTED, '0.0001')):
            self.unresolved((op, (replace(orders[0], status=status, cumulative_filled=Decimal(qty)),), fills, posts),
                            'status_quantity_conflict')
        self.unresolved((op, (replace(orders[0], cumulative_filled=Decimal('0.0011')),), fills, posts), 'overfill')

    def test_order_cumulative_and_complete_fill_sum_must_match(self):
        op, orders, fills, posts = scenario()
        self.unresolved((op, orders, replace(fills, fills=()), posts), 'order_fill_mismatch')
        op, orders, fills, posts = scenario(OrderStatus.REJECTED, '0')
        execution = scenario()[2].fills[0]
        self.unresolved((op, orders, replace(fills, fills=(execution,)), posts), 'order_fill_mismatch')

    def test_quantity_types_precision_and_units_fail_closed(self):
        op, orders, fills, posts = scenario()
        for bad in (0.001, True, '0.001', Decimal('NaN'), Decimal('Infinity'), Decimal('-0.001'),
                    Decimal('0.00001'), Decimal('1e41')):
            with self.subTest(bad=bad):
                self.unresolved((op, (replace(orders[0], cumulative_filled=bad),), fills, posts))
                self.unresolved((op, orders, replace(fills, fills=(replace(fills.fills[0], quantity=bad),)), posts))
                self.unresolved((op, orders, fills, (replace(posts[0], long_quantity=bad),)))
        self.unresolved((op, (replace(orders[0], unit='CONTRACTS'),), fills, posts), 'order_parameters_mismatch')
        self.unresolved((op, orders, replace(fills, fills=(replace(fills.fills[0], unit='CONTRACTS'),)), posts),
                        'fill_unit_mismatch')

    def test_fill_price_respects_limit_and_tick(self):
        for leg, price in (('long', '59999'), ('short', '60001'), ('long', '60000.05')):
            op, orders, fills, posts = scenario(leg=leg)
            self.unresolved((op, orders, replace(fills, fills=(replace(fills.fills[0], price=Decimal(price)),)), posts))

    def test_stale_order_fill_and_position_at_exact_boundary(self):
        op, orders, fills, posts = scenario()
        self.unresolved((op, orders, fills, posts), 'stale_order', now=at(7))
        fresh_order = replace(orders[0], stamp=stamp('fresh_order', ObservationKind.ORDER, 7, 7.1))
        self.unresolved((op, (fresh_order,), fills, posts), 'stale_fill_coverage', now=at(7.2))
        fresh_fills = replace(fills, stamp=stamp('fresh_fills', ObservationKind.FILLS, 7.2, 7.3), window_end=at(7.2))
        self.unresolved((op, (fresh_order,), fresh_fills, posts), 'stale_positions', now=at(7.4))

    def test_baseline_checked_at_attempt_not_at_later_reconciliation(self):
        op, orders, fills, posts = scenario()
        orders = (replace(orders[0], stamp=stamp('late_order', ObservationKind.ORDER, 40, 40.1)),)
        fills = replace(fills, stamp=stamp('late_fills', ObservationKind.FILLS, 40.2, 40.3), window_end=at(40.2))
        posts = (replace(posts[0], stamp=stamp('late_post', ObservationKind.POSITIONS, 40.4, 40.5)),)
        self.assertIs(self.result((op, orders, fills, posts), now=at(41)).decision, Decision.PROPOSED_OUTCOME)

    def test_future_naive_and_inverted_observation_times(self):
        op, orders, fills, posts = scenario()
        for changes in ({'started_at': at(4)}, {'completed_at': at(4)}, {'started_at': at(2.2)},
                        {'started_at': NOW.replace(tzinfo=None)}):
            self.unresolved((op, (replace(orders[0], stamp=replace(orders[0].stamp, **changes)),), fills, posts))
        self.unresolved((op, orders, fills, posts), now=NOW.replace(tzinfo=None))

    def test_operation_attempt_deadline_and_baseline_time_contract(self):
        op, orders, fills, posts = scenario()
        for changes in ({'attempted_at': at(-1)}, {'attempted_at': at(5)}, {'attempted_at': at(4)},
                        {'deadline': at(1)}):
            self.unresolved((replace(op, **changes), orders, fills, posts), 'invalid_attempt_time')
        for changes in ({'completed_at': at(1.1)}, {'started_at': at(-4)}, {'complete': False}):
            baseline = replace(op.baseline, stamp=replace(op.baseline.stamp, **changes))
            self.unresolved((replace(op, baseline=baseline), orders, fills, posts), 'invalid_baseline')

    def test_fill_window_and_execution_times(self):
        op, orders, fills, posts = scenario()
        for changes in ({'window_start': at(1.1)}, {'window_end': at(0.5)}, {'window_end': at(4)}):
            self.unresolved((op, orders, replace(fills, **changes), posts), 'invalid_fill_window')
        for time in (0.9, 2.5, 1.6):
            self.unresolved((op, orders, replace(fills, fills=(replace(fills.fills[0], executed_at=at(time)),)), posts))

    def test_fill_query_and_post_read_must_follow_terminal_read(self):
        op, orders, fills, posts = scenario()
        early = replace(fills, stamp=stamp('early_fills', ObservationKind.FILLS, 1.7, 1.8), window_end=at(1.7))
        self.assertIs(self.result((op, orders, early, posts)).decision, Decision.WAIT)
        post = replace(posts[0], stamp=stamp('early_post', ObservationKind.POSITIONS, 2.1, 2.2))
        self.assertIs(self.result((op, orders, fills, (post,))).decision, Decision.WAIT)

    def test_synthetic_user_and_forged_authenticated_labels_never_authenticate(self):
        op, orders, fills, posts = scenario()
        result = self.result()
        self.assertEqual(result.reason, 'synthetic_evidence_only')
        for provenance in (Provenance.AUTHENTICATED_TESTNET, Provenance.USER_SUPPLIED,
                           Provenance.UNKNOWN, 'synthetic', 'authenticated_testnet'):
            for values in ((replace(op, baseline=replace(op.baseline, stamp=replace(op.baseline.stamp, provenance=provenance))), orders, fills, posts),
                           (op, (replace(orders[0], stamp=replace(orders[0].stamp, provenance=provenance)),), fills, posts),
                           (op, orders, replace(fills, stamp=replace(fills.stamp, provenance=provenance)), posts),
                           (op, orders, fills, (replace(posts[0], stamp=replace(posts[0].stamp, provenance=provenance)),))):
                self.unresolved(values, 'observation_origin_not_established')
        for name in ('execution_authority', 'authenticated_evidence_verified', 'may_resolve_journal', 'may_resubmit'):
            self.assertIs(getattr(result, name), False)

    def test_strict_models_collections_booleans_and_missing_fields(self):
        op, orders, fills, posts = scenario()
        for values in (({}, orders, fills, posts), (op, list(orders), fills, posts),
                       (op, orders, {}, posts), (op, orders, fills, list(posts)),
                       (op, (replace(orders[0], status='FILLED'),), fills, posts),
                       (op, orders, replace(fills, pages_complete=1), posts),
                       (op, (replace(orders[0], stamp=replace(orders[0].stamp, complete=1)),), fills, posts)):
            self.unresolved(values)
        with self.assertRaises(FrozenInstanceError): op.operation_id = 'changed'
        with self.assertRaises(FrozenInstanceError): orders[0].cumulative_filled = Decimal('0')

    def test_baseline_quantity_must_match_compiled_position(self):
        op, orders, fills, posts = scenario()
        self.unresolved((replace(op, baseline=replace(op.baseline, long_quantity=Decimal('0.003'))), orders, fills, posts),
                        'baseline_quantity_mismatch')

    def test_exact_high_precision_arithmetic_ignores_ambient_decimal_context(self):
        values = list(evidence())
        qty = Decimal('0.99999999999999999999999999999')
        values[0] = replace(values[0], quantity=qty, price=Decimal('100'), action='close')
        values[2] = replace(values[2], base_quantity=qty)
        values[3] = replace(values[3], quantity_step=Decimal('1e-29'), contract_step=Decimal('1e-29'),
                            min_contracts=Decimal('1e-29'), max_base_quantity=qty, max_contracts=qty)
        request = compiled(values)
        op, orders, fills, posts = scenario()
        op = replace(op, request=request, baseline=replace(op.baseline, long_quantity=qty))
        binding = replace(orders[0].correlation, request_fingerprint=request.request_fingerprint)
        order = replace(orders[0], correlation=binding, parameters=request.parameters, cumulative_filled=qty)
        a = replace(fills.fills[0], correlation=binding, quantity=Decimal('0.9'), price=Decimal('100'))
        b = replace(a, execution_id='execution_2', quantity=Decimal('0.09999999999999999999999999999'))
        fills = replace(fills, correlation=binding, fills=(a, b, a))
        posts = (replace(posts[0], long_quantity=Decimal('0')),)
        with localcontext() as context:
            context.prec = 6
            result = self.result((op, (order,), fills, posts))
        self.assertIs(result.outcome, CandidateOutcome.FILLED)
        self.assertEqual(result.filled_quantity, qty)
        partial = Decimal('0.99999999999999999999999999998')
        canceled = replace(order, status=OrderStatus.CANCELED, cumulative_filled=partial)
        partial_fills = replace(fills, fills=(replace(a, quantity=partial),))
        partial_post = (replace(posts[0], long_quantity=Decimal('1e-29')),)
        with localcontext() as context:
            context.prec = 6
            partial_result = self.result((op, (canceled,), partial_fills, partial_post))
            self.unresolved((op, (replace(order, cumulative_filled=Decimal('1')),), fills, posts), 'overfill')
            self.unresolved((op, (order,), replace(fills, fills=(replace(a, quantity=Decimal('1')),)), posts),
                            'fill_total_exceeds_order')
        self.assertIs(partial_result.outcome, CandidateOutcome.PARTIALLY_FILLED)
        self.assertEqual(partial_result.remaining_position, Decimal('1e-29'))

    def test_purity_and_no_submission_or_journal_api(self):
        values = scenario()
        before = repr(values)
        with patch('config.load_settings', side_effect=AssertionError('No credentials')), \
                patch.object(OperationJournal, 'resolve', side_effect=AssertionError('No journal transition')), \
                patch('ccxt.phemex.__init__', side_effect=AssertionError('No exchange construction')):
            self.assertEqual(self.result(values), self.result(values))
        self.assertEqual(repr(values), before)
        source = Path(engine.__file__).read_text()
        imports = {node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)}
        self.assertFalse(imports & {'operation_journal', 'offline_simulation', 'readonly_exchange', 'exchange_client'})

    def test_simulation_crash_recovery_preserves_blocker_after_candidate(self):
        values = scenario()
        op, orders, fills, posts = values
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'simulation.sqlite3'
            with SimulationJournal(path, create=True, clock=lambda: NOW) as journal:
                operation = journal.reserve_compiled(op.request, operation_id=op.operation_id)
                journal.arm('ARM OFFLINE MODEL')
                token = journal.authorize(operation.operation_id, operation.version, op.request,
                                          'AUTHORIZE OFFLINE ' + operation.operation_id)
                simulate_submission(journal, token, op.request, Outcome.TIMEOUT)
                self.assertIs(journal.get(operation.operation_id).state, State.UNCERTAIN)
            with SimulationJournal(path, clock=lambda: at(3)) as recovered:
                before = recovered.get(operation.operation_id)
                result = self.result(values)
                self.assertIs(result.decision, Decision.PROPOSED_OUTCOME)
                self.assertEqual(recovered.get(operation.operation_id), before)
                self.assertEqual(len(recovered.unresolved()), 1)

    def test_actual_process_crash_before_and_after_ack_does_not_release_simulation_blocker(self):
        for boundary in (State.SUBMITTING, State.ACKNOWLEDGED):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'crashed.sqlite3'
                process = multiprocessing.get_context('fork').Process(target=crash_simulation, args=(path, boundary))
                process.start()
                process.join(8)
                if process.is_alive():
                    process.terminate()
                    process.join(2)
                    self.fail('Offline crash fixture timed out')
                self.assertEqual(process.exitcode, 0)
                with SimulationJournal(path, clock=lambda: at(3)) as journal:
                    operation, = journal.unresolved()
                    self.assertIs(operation.state, State.UNCERTAIN)
                    op, orders, fills, posts = scenario()
                    op = replace(op, operation_id=operation.operation_id, request=compiled(),
                                 exchange_order_id=operation.exchange_order_id)
                    binding = replace(orders[0].correlation, operation_id=operation.operation_id,
                                      exchange_order_id=operation.exchange_order_id or 'exchange_1')
                    orders = (replace(orders[0], correlation=binding),)
                    fills = replace(fills, correlation=binding,
                                    fills=tuple(replace(fill, correlation=binding) for fill in fills.fills))
                    self.assertIs(self.result((op, orders, fills, posts)).decision, Decision.PROPOSED_OUTCOME)
                    self.assertEqual(journal.get(operation.operation_id), operation)

    def test_ordinary_journal_blocker_never_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ordinary.sqlite3'
            with OperationJournal(path, create=True, clock=lambda: NOW) as journal:
                operation = journal.reserve(validated())
                journal.arm('ARM OFFLINE MODEL')
                token = journal.authorize(operation.operation_id, operation.version, operation.payload_fingerprint,
                                          'AUTHORIZE OFFLINE ' + operation.operation_id)
                journal.consume_authorization(token, operation.payload_fingerprint)
                before = journal.get(operation.operation_id)
                self.assertIs(self.result().decision, Decision.PROPOSED_OUTCOME)
                self.assertEqual(journal.get(operation.operation_id), before)
                self.assertEqual(len(journal.unresolved()), 1)

    def test_every_terminal_observation_must_follow_its_required_executions(self):
        for status, quantity in ((OrderStatus.FILLED, '0.001'),
                                 (OrderStatus.CANCELED, '0.0004'), (OrderStatus.EXPIRED, '0.0004')):
            for complete in (True, False):
                op, orders, fills, posts = scenario(status, quantity)
                early = replace(orders[0], stamp=stamp('early_terminal', ObservationKind.ORDER, 1.1, 1.2,
                                                     complete=complete), updated_at=at(1.05))
                for history in permutations((early, orders[0])):
                    with self.subTest(status=status, complete=complete):
                        self.unresolved((op, history, fills, posts), 'execution_after_terminal')

    def test_execution_history_must_match_earlier_cumulative_observations(self):
        op, orders, fills, posts = scenario()
        for updated in (1.15, 1.4):
            early = replace(orders[0], stamp=stamp('early_partial', ObservationKind.ORDER, 1.4, 1.45),
                            status=OrderStatus.PARTIALLY_FILLED, cumulative_filled=Decimal('0.0004'),
                            updated_at=at(updated))
            self.unresolved((op, (early,) + orders, fills, posts), 'order_fill_history_mismatch')
        # Known executions are a lower bound even when other pages are missing.
        self.unresolved((op, (early,) + orders, replace(fills, pages_complete=False), posts),
                        'order_fill_history_mismatch')

    def test_consistent_repeated_terminal_observations_are_accepted(self):
        for status, quantity in ((OrderStatus.FILLED, '0.001'), (OrderStatus.CANCELED, '0.0004'),
                                 (OrderStatus.EXPIRED, '0.0004'), (OrderStatus.REJECTED, '0')):
            op, orders, fills, posts = scenario(status, quantity)
            early = replace(orders[0], stamp=stamp('earlier_terminal', ObservationKind.ORDER, 1.7, 1.8),
                            updated_at=at(1.4))
            for history in permutations((early, orders[0])):
                self.assertIs(self.result((op, history, fills, posts)).decision, Decision.PROPOSED_OUTCOME)

    def test_execution_timestamp_ties_cannot_establish_ordering(self):
        op, orders, fills, posts = scenario()
        for status, quantity in ((OrderStatus.ACCEPTED, '0'), (OrderStatus.FILLED, '0.001')):
            tied = replace(orders[0], stamp=stamp('tied_order', ObservationKind.ORDER, 1.3, 1.4),
                           status=status, cumulative_filled=Decimal(quantity), updated_at=at(1.25))
            self.unresolved((op, (tied,) + orders, fills, posts), 'ambiguous_execution_order_time')

    def test_selected_leg_can_never_exceed_baseline_for_any_terminal_outcome(self):
        cases = ((OrderStatus.FILLED, '0.001', 'reduce'), (OrderStatus.CANCELED, '0', 'reduce'),
                 (OrderStatus.REJECTED, '0', 'reduce'), (OrderStatus.FILLED, '0.002', 'close'))
        for leg in ('long', 'short'):
            for status, quantity, action in cases:
                op, orders, fills, posts = scenario(status, quantity, leg=leg, action=action)
                increased = replace(posts[0], stamp=stamp('increased', ObservationKind.POSITIONS, 1.1, 1.2),
                                    **{leg + '_quantity': Decimal('0.003')})
                for positions in permutations((increased, posts[0])):
                    with self.subTest(leg=leg, status=status, action=action):
                        self.unresolved((op, orders, fills, positions), 'selected_leg_exceeds_baseline')

    def test_valid_monotonic_reductions_from_baseline_remain_accepted(self):
        for leg in ('long', 'short'):
            op, orders, fills, posts = scenario(leg=leg)
            first = replace(fills.fills[0], quantity=Decimal('0.0004'), executed_at=at(1.15))
            second = replace(first, execution_id='execution_2', quantity=Decimal('0.0006'), executed_at=at(1.35))
            fills = replace(fills, fills=(second, first))
            unchanged = replace(posts[0], stamp=stamp('unchanged', ObservationKind.POSITIONS, 1.01, 1.04),
                                **{leg + '_quantity': Decimal('0.002')})
            mid = replace(posts[0], stamp=stamp('intermediate', ObservationKind.POSITIONS, 1.2, 1.3),
                          **{leg + '_quantity': Decimal('0.0016')})
            for positions in permutations((unchanged, mid, posts[0])):
                self.assertIs(self.result((op, orders, fills, positions)).decision, Decision.PROPOSED_OUTCOME)

    def test_nested_nonadjacent_position_conflicts_rejected_in_all_permutations(self):
        op, orders, fills, posts = scenario()
        outer = replace(posts[0], stamp=stamp('outer', ObservationKind.POSITIONS, 1.1, 2.8),
                        long_quantity=Decimal('0.002'))
        inner = replace(outer, stamp=stamp('inner', ObservationKind.POSITIONS, 1.2, 1.3))
        for positions in permutations((outer, inner, posts[0])):
            self.unresolved((op, orders, fills, positions), 'overlapping_position_snapshots')

    def test_multiple_intervening_intervals_cannot_hide_position_conflicts(self):
        op, orders, fills, posts = scenario()
        outer = replace(posts[0], stamp=stamp('outer', ObservationKind.POSITIONS, 1.01, 2.8),
                        long_quantity=Decimal('0.002'))
        inner = replace(outer, stamp=stamp('inner', ObservationKind.POSITIONS, 1.05, 1.1))
        extra = replace(outer, stamp=stamp('extra', ObservationKind.POSITIONS, 1.1, 1.2))
        for positions in permutations((outer, inner, extra, posts[0])):
            self.unresolved((op, orders, fills, positions), 'overlapping_position_snapshots')

    def test_consistent_nested_position_intervals_remain_accepted(self):
        op, orders, fills, posts = scenario()
        outer = replace(posts[0], stamp=stamp('outer', ObservationKind.POSITIONS, 1.6, 2.8))
        inner = replace(posts[0], stamp=stamp('inner', ObservationKind.POSITIONS, 1.7, 1.8))
        for positions in permutations((outer, inner, posts[0])):
            self.assertIs(self.result((op, orders, fills, positions)).decision, Decision.PROPOSED_OUTCOME)

    def test_later_complete_read_of_exact_order_version_supersedes_incompleteness(self):
        op, orders, fills, posts = scenario()
        for status, qty, updated in ((OrderStatus.ACCEPTED, '0', 1.05), (OrderStatus.FILLED, '0.001', 1.5)):
            incomplete = replace(orders[0], stamp=stamp('incomplete', ObservationKind.ORDER, 1.6, 1.7, complete=False),
                                 status=status, cumulative_filled=Decimal(qty), updated_at=at(updated))
            complete = replace(incomplete, stamp=stamp('completed_version', ObservationKind.ORDER, 1.8, 1.9))
            for history in permutations((incomplete, complete, orders[0])):
                result = self.result((op, history, fills, posts))
                self.assertIs(result.decision, Decision.PROPOSED_OUTCOME)
                self.assertIn('incomplete', result.supporting_evidence)
                self.assertIn('completed_version', result.supporting_evidence)

    def test_newer_order_version_does_not_invent_missing_historical_completeness(self):
        op, orders, fills, posts = scenario()
        incomplete = replace(orders[0], stamp=stamp('incomplete', ObservationKind.ORDER, 1.1, 1.2, complete=False),
                             status=OrderStatus.ACCEPTED, cumulative_filled=Decimal('0'), updated_at=at(1.05))
        result = self.result((op, (incomplete,) + orders, fills, posts))
        self.assertIs(result.decision, Decision.WAIT)
        self.assertEqual(result.reason, 'incomplete_order')

    def test_overlapping_or_incomplete_reread_cannot_supersede_incomplete_order(self):
        op, orders, fills, posts = scenario()
        incomplete = replace(orders[0], stamp=stamp('incomplete', ObservationKind.ORDER, 1.6, 1.8, complete=False),
                             status=OrderStatus.ACCEPTED, cumulative_filled=Decimal('0'), updated_at=at(1.05))
        for replacement_stamp in (stamp('overlapping', ObservationKind.ORDER, 1.7, 1.9),
                                  stamp('still_incomplete', ObservationKind.ORDER, 1.9, 2, complete=False)):
            reread = replace(incomplete, stamp=replacement_stamp)
            result = self.result((op, (incomplete, reread) + orders, fills, posts))
            self.assertIs(result.decision, Decision.WAIT)
            self.assertEqual(result.reason, 'incomplete_order')

    def test_supersession_preserves_incomplete_order_contradictions(self):
        op, orders, fills, posts = scenario()
        incomplete = replace(orders[0], stamp=stamp('incomplete', ObservationKind.ORDER, 1.1, 1.2, complete=False),
                             status=OrderStatus.PARTIALLY_FILLED, cumulative_filled=Decimal('0.0004'), updated_at=at(1.05))
        complete = replace(incomplete, stamp=stamp('completed_version', ObservationKind.ORDER, 1.3, 1.4))
        self.unresolved((op, (incomplete, complete) + orders, fills, posts), 'order_fill_history_mismatch')

    def test_incomplete_order_recovery_after_deadline_requires_same_version_evidence(self):
        op, orders, fills, posts = scenario()
        incomplete = replace(orders[0], stamp=stamp('incomplete', ObservationKind.ORDER, 1.1, 1.2, complete=False),
                             status=OrderStatus.ACCEPTED, cumulative_filled=Decimal('0'), updated_at=at(1.05))
        complete = replace(incomplete, stamp=stamp('completed_version', ObservationKind.ORDER, 40, 40.1))
        late_order = replace(orders[0], stamp=stamp('late_order', ObservationKind.ORDER, 40.2, 40.3))
        late_fills = replace(fills, stamp=stamp('late_fills', ObservationKind.FILLS, 40.4, 40.5), window_end=at(40.4))
        late_post = replace(posts[0], stamp=stamp('late_post', ObservationKind.POSITIONS, 40.6, 40.7))
        self.unresolved((op, (incomplete, late_order), late_fills, (late_post,)), 'incomplete_order', now=at(41))
        result = self.result((op, (incomplete, complete, late_order), late_fills, (late_post,)), now=at(41))
        self.assertIs(result.decision, Decision.PROPOSED_OUTCOME)
        self.assertFalse(result.may_resubmit)
        self.assertFalse(result.may_resolve_journal)
        self.assertEqual(op.operation_id, 'op_' + '1' * 32)

    def test_new_position_snapshot_cannot_prove_old_incomplete_snapshot_coverage(self):
        op, orders, fills, posts = scenario()
        incomplete = replace(posts[0], stamp=stamp('incomplete_position', ObservationKind.POSITIONS, 1.7, 1.8,
                                                 complete=False))
        result = self.result((op, orders, fills, (incomplete,) + posts))
        self.assertIs(result.decision, Decision.WAIT)
        self.assertEqual(result.reason, 'incomplete_positions')
        late_order = replace(orders[0], stamp=stamp('late_order', ObservationKind.ORDER, 40, 40.1))
        late_fills = replace(fills, stamp=stamp('late_fills', ObservationKind.FILLS, 40.2, 40.3), window_end=at(40.2))
        late_post = replace(posts[0], stamp=stamp('late_post', ObservationKind.POSITIONS, 40.4, 40.5))
        self.unresolved((op, (late_order,), late_fills, (incomplete, late_post)), 'incomplete_positions', now=at(41))

    def test_incomplete_position_contradictions_are_never_discarded(self):
        op, orders, fills, posts = scenario()
        incomplete = replace(posts[0], stamp=stamp('incomplete_position', ObservationKind.POSITIONS, 1.1, 1.2,
                                                 complete=False), long_quantity=Decimal('0.003'))
        self.unresolved((op, orders, fills, (incomplete,) + posts), 'selected_leg_exceeds_baseline')

    def test_full_close_cannot_already_be_flat_before_its_only_execution(self):
        for leg in ('long', 'short'):
            op, orders, fills, posts = scenario(filled='0.002', action='close', leg=leg)
            premature = replace(posts[0], stamp=stamp('premature_flat', ObservationKind.POSITIONS, 1.1, 1.2),
                                **{leg + '_quantity': Decimal('0')})
            for positions in permutations((premature, posts[0])):
                self.unresolved((op, orders, fills, positions), 'position_execution_timeline_mismatch')

    def test_partial_reduction_cannot_appear_before_its_execution(self):
        for status, quantity in ((OrderStatus.FILLED, '0.001'), (OrderStatus.CANCELED, '0.0004')):
            for leg in ('long', 'short'):
                op, orders, fills, posts = scenario(status, quantity, leg=leg)
                premature = replace(posts[0], stamp=stamp('premature_reduction', ObservationKind.POSITIONS, 1.1, 1.2))
                for positions in permutations((premature, posts[0])):
                    self.unresolved((op, orders, fills, positions), 'position_execution_timeline_mismatch')

    def test_unchanged_before_execution_and_reduced_after_execution_are_consistent(self):
        op, orders, fills, posts = scenario()
        before = replace(posts[0], stamp=stamp('before_execution', ObservationKind.POSITIONS, 1.1, 1.2),
                         long_quantity=op.baseline.long_quantity)
        after = replace(posts[0], stamp=stamp('after_execution', ObservationKind.POSITIONS, 1.3, 1.4))
        for positions in permutations((before, after, posts[0])):
            self.assertIs(self.result((op, orders, fills, positions)).decision, Decision.PROPOSED_OUTCOME)

    def test_execution_inside_position_interval_does_not_select_an_endpoint(self):
        op, orders, fills, posts = scenario()
        for quantity in ('0.002', '0.0015', '0.001'):
            # Identical final quantity avoids an independent overlap conflict.
            interval = replace(posts[0], stamp=stamp('spans_execution', ObservationKind.POSITIONS, 1.2, 1.3),
                               long_quantity=Decimal(quantity))
            self.unresolved((op, orders, fills, (interval,) + posts), 'position_execution_time_ambiguous')

    def test_position_timestamp_boundary_ties_are_not_ordering_proof(self):
        op, orders, fills, posts = scenario()
        for start, end, quantity in ((1.25, 1.3, '0.001'), (1.2, 1.25, '0.002'), (1.25, 1.25, '0.001')):
            tied = replace(posts[0], stamp=stamp('tied_position', ObservationKind.POSITIONS, start, end),
                           long_quantity=Decimal(quantity))
            self.unresolved((op, orders, fills, (tied,) + posts), 'position_execution_time_ambiguous')

    def test_position_inside_interval_cannot_exceed_possible_executions(self):
        op, orders, fills, posts = scenario()
        impossible = replace(posts[0], stamp=stamp('impossible', ObservationKind.POSITIONS, 1.2, 1.3),
                             long_quantity=Decimal('0'))
        # Remove the later increase; interval alone must already be inconsistent.
        self.unresolved((op, orders, fills, (impossible,)), 'position_execution_timeline_mismatch')

    def test_position_after_execution_cannot_keep_preexecution_quantity(self):
        op, orders, fills, posts = scenario()
        unchanged = replace(posts[0], stamp=stamp('unchanged_too_late', ObservationKind.POSITIONS, 1.3, 1.4),
                            long_quantity=op.baseline.long_quantity)
        self.unresolved((op, orders, fills, (unchanged,) + posts), 'position_execution_timeline_mismatch')

    def test_multiple_execution_prefixes_match_intermediate_positions_exactly(self):
        op, orders, fills, posts = scenario()
        a = replace(fills.fills[0], quantity=Decimal('0.0004'), executed_at=at(1.15))
        b = replace(a, execution_id='execution_2', quantity=Decimal('0.0006'), executed_at=at(1.35))
        before = replace(posts[0], stamp=stamp('before_all', ObservationKind.POSITIONS, 1.01, 1.04),
                         long_quantity=Decimal('0.002'))
        middle = replace(posts[0], stamp=stamp('between_fills', ObservationKind.POSITIONS, 1.2, 1.3),
                         long_quantity=Decimal('0.0016'))
        for executions in ((a, b), (b, a, b)):
            coverage = replace(fills, fills=executions)
            for positions in permutations((before, middle, posts[0])):
                self.assertIs(self.result((op, orders, coverage, positions)).decision, Decision.PROPOSED_OUTCOME)
        bad = replace(middle, long_quantity=Decimal('0.0015'))
        for positions in permutations((before, bad, posts[0])):
            self.unresolved((op, orders, replace(fills, fills=(a, b)), positions), 'position_execution_timeline_mismatch')

    def test_missing_incomparable_and_malformed_clock_metadata_fail_closed(self):
        op, orders, fills, posts = scenario()
        for clock in (None, 'UTC', ClockInfo('native_exchange', timedelta(0)),
                      ClockInfo('synthetic_other', timedelta(0)), ClockInfo('synthetic_shared', False)):
            cases = ((op, orders, fills, (replace(posts[0], stamp=replace(posts[0].stamp, clock=clock)),)),
                     (op, orders, replace(fills, fills=(replace(fills.fills[0], clock=clock),)), posts),
                     (op, (replace(orders[0], event_clock=clock),), fills, posts),
                     (op, orders, replace(fills, stamp=replace(fills.stamp, clock=clock)), posts),
                     (replace(op, baseline=replace(op.baseline, stamp=replace(op.baseline.stamp, clock=clock))), orders, fills, posts))
            for values in cases:
                self.unresolved(values)
        for clock in (None, ClockInfo('synthetic_shared', None), ClockInfo('native_exchange', timedelta(0))):
            self.unresolved((replace(op, clock=clock), orders, fills, posts))

    def test_timestamp_precision_uncertainty_is_not_silently_assumed_zero(self):
        op, orders, fills, posts = scenario()
        for error in (timedelta(microseconds=1), timedelta(milliseconds=100), timedelta(seconds=-1)):
            clock = ClockInfo('synthetic_shared', error)
            cases = ((replace(op, clock=clock), orders, fills, posts),
                     (op, orders, fills, (replace(posts[0], stamp=replace(posts[0].stamp, clock=clock)),)),
                     (op, orders, replace(fills, fills=(replace(fills.fills[0], clock=clock),)), posts),
                     (op, (replace(orders[0], event_clock=clock),), fills, posts))
            for values in cases:
                self.unresolved(values, 'timestamp_precision_unknown')

    def test_unknown_clock_alignment_is_not_repaired_by_matching_utc_timezone(self):
        op, orders, fills, posts = scenario()
        clock = ClockInfo('synthetic_exchange', timedelta(0))
        self.unresolved((op, orders, replace(fills, fills=(replace(fills.fills[0], clock=clock),)), posts),
                        'clock_domain_mismatch')

    def test_one_microsecond_separation_is_distinct_from_boundary_equality(self):
        op, orders, fills, posts = scenario()
        before = replace(posts[0], stamp=stamp('strictly_before', ObservationKind.POSITIONS, 1.1, 1.249999),
                         long_quantity=op.baseline.long_quantity)
        after = replace(posts[0], stamp=stamp('strictly_after', ObservationKind.POSITIONS, 1.250001, 1.3))
        self.assertIs(self.result((op, orders, fills, (before, after) + posts)).decision, Decision.PROPOSED_OUTCOME)

    def test_incomplete_fill_history_cannot_invent_zero_earlier_executions(self):
        op, orders, fills, posts = scenario()
        premature = replace(posts[0], stamp=stamp('unexplained_with_missing_pages', ObservationKind.POSITIONS, 1.1, 1.2))
        result = self.result((op, orders, replace(fills, pages_complete=False), (premature,) + posts))
        self.assertIs(result.decision, Decision.WAIT)
        self.assertIsNone(result.outcome)

    def test_nonterminal_history_cutoff_cannot_prove_later_position_state(self):
        op, orders, fills, posts = scenario(OrderStatus.PARTIALLY_FILLED, '0.0004')
        result = self.result((op, orders, fills, posts))
        self.assertIs(result.decision, Decision.WAIT)
        self.assertIsNone(result.outcome)
