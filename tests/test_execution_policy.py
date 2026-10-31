from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from account_discovery import AccountReport
from execution_policy import Provenance, Source, evaluate
from tests.execution_fixtures import NOW, evidence
from tests.helpers import OfflineTestCase


class ExecutionPolicyTests(OfflineTestCase):
    def decision(self, index=None, **changes):
        values = list(evidence())
        if index is not None:
            values[index] = replace(values[index], **changes)
        return evaluate(*values, now=NOW)

    def test_valid_reduction_matrix_is_nonexecutable(self):
        for action in ('reduce', 'close'):
            for leg, side in (('long', 'sell'), ('short', 'buy')):
                for mode in ('cross', 'isolated'):
                    values = list(evidence())
                    qty = Decimal('0.002' if action == 'close' else '0.001')
                    values[0] = replace(values[0], action=action, position_side=leg, side=side, quantity=qty)
                    values[2] = replace(values[2], position_side=leg, margin_mode=mode)
                    with self.subTest(action=action, leg=leg, mode=mode):
                        result = evaluate(*values, now=NOW)
                        self.assertTrue(result.allowed, result.reasons)
                        self.assertFalse(result.submission_enabled)
                        self.assertFalse(result.validated.submission_enabled)
                        self.assertEqual(dict(result.validated.payload)['position_quantity'], '0.002')

    def test_unknown_accounting_and_restricted_account_fail_closed(self):
        for changes in ({'accounting_type': None}, {'accounting_type': 'Classic'},
                        {'authenticated': None}, {'authenticated': False}, {'authenticated': 1},
                        {'status': None}, {'status': 'banned'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.decision(1, **changes).allowed)

    def test_account_report_or_offline_position_is_not_execution_evidence(self):
        values = list(evidence())
        values[1] = AccountReport('synthetic')
        self.assertEqual(evaluate(*values, now=NOW).reasons, ('invalid_evidence_type',))
        with self.assertRaises(TypeError):
            replace(evidence()[0], current_qty=Decimal('0.002'))

    def test_provenance_environment_completeness_source_and_binding(self):
        for index in range(1, 6):
            for changes in ({'provenance': Provenance.SYNTHETIC}, {'provenance': Provenance.USER_SUPPLIED},
                            {'provenance': Provenance.UNKNOWN}, {'environment': 'live'}, {'complete': False},
                            {'source': Source.ACCOUNT if index != 1 else Source.POSITIONS},
                            {'account_binding': 'b' * 64}):
                values = list(evidence())
                values[index] = replace(values[index], stamp=replace(values[index].stamp, **changes))
                with self.subTest(index=index, changes=changes):
                    self.assertFalse(evaluate(*values, now=NOW).allowed)

    def test_stale_future_and_naive_times_are_rejected_for_every_source(self):
        for index, age in ((1, 5), (2, 5), (3, 300), (4, 5), (5, 86400)):
            for observed in (NOW - timedelta(seconds=age), NOW + timedelta(microseconds=1), NOW.replace(tzinfo=None)):
                values = list(evidence())
                values[index] = replace(values[index], stamp=replace(values[index].stamp, observed_at=observed))
                with self.subTest(index=index, observed=observed):
                    self.assertFalse(evaluate(*values, now=NOW).allowed)
        self.assertFalse(evaluate(*evidence(), now=NOW.replace(tzinfo=None)).allowed)

    def test_missing_position_context_never_becomes_zero(self):
        for key in ('base_quantity', 'position_mode', 'margin_mode', 'configured_leverage', 'status'):
            with self.subTest(key=key):
                self.assertFalse(self.decision(2, **{key: None}).allowed)
        self.assertFalse(self.decision(2, base_quantity=Decimal('0')).allowed)
        self.assertFalse(self.decision(2, position_mode='one_way').allowed)

    def test_mismatched_symbol_leg_and_wrong_direction_are_rejected(self):
        for index, changes in ((0, {'side': 'buy'}), (0, {'position_side': 'short'}),
                               (2, {'position_side': 'short'}), (2, {'symbol': 'ETH/USDT:USDT'}),
                               (3, {'market_id': 'ETHUSDT'}), (4, {'symbol': 'ETH/USDT:USDT'})):
            with self.subTest(index=index, changes=changes):
                self.assertFalse(self.decision(index, **changes).allowed)

    def test_unsupported_orders_and_protection_are_rejected(self):
        for changes in ({'action': 'open'}, {'action': 'increase'}, {'order_type': 'market'},
                        {'time_in_force': 'GTC'}, {'reduce_only': False}, {'reduce_only': 1},
                        {'sl': Decimal('59000')}, {'tp': Decimal('61000')}):
            with self.subTest(changes=changes):
                self.assertFalse(self.decision(0, **changes).allowed)

    def test_compatibility_is_account_specific_and_never_assumed(self):
        for key in ('explicit_hedge_reduce_only', 'limit_ioc', 'client_id_lookup'):
            for value in (None, False, 1):
                with self.subTest(key=key, value=value):
                    self.assertFalse(self.decision(5, **{key: value}).allowed)
        self.assertFalse(self.decision(5, accounting_type='unified_multi_asset').allowed)
        values = list(evidence())
        for index in (1, 5):
            values[index] = replace(values[index], accounting_type='unified_multi_asset')
        self.assertTrue(evaluate(*values, now=NOW).allowed)

    def test_outstanding_and_missing_order_coverage_blocks(self):
        for key in ('active_count', 'conditional_count'):
            for value in (None, 1, -1, False):
                with self.subTest(key=key, value=value):
                    self.assertFalse(self.decision(4, **{key: value}).allowed)

    def test_required_specs_and_quantities_are_finite_decimals(self):
        for key in ('contract_size', 'quantity_step', 'contract_step', 'min_contracts', 'max_contracts',
                    'max_base_quantity', 'min_notional', 'price_tick', 'price_precision_tick'):
            for value in (None, Decimal('NaN'), Decimal('Infinity'), Decimal('0'), 0.001, True):
                with self.subTest(key=key, value=value):
                    self.assertFalse(self.decision(3, **{key: value}).allowed)
        self.assertFalse(self.decision(0, quantity=0.001).allowed)
        self.assertFalse(self.decision(3, active=None).allowed)

    def test_existing_close_reduce_and_grid_safeguards_remain(self):
        for changes in ({'quantity': Decimal('0.002')}, {'quantity': Decimal('0.0021')},
                        {'action': 'close', 'quantity': Decimal('0.0019')},
                        {'action': 'close', 'quantity': Decimal('0.0021')}, {'price': Decimal('60000.05')}):
            with self.subTest(changes=changes):
                self.assertFalse(self.decision(0, **changes).allowed)
        result = self.decision(0, quantity=Decimal('0.0019'))
        self.assertTrue(result.allowed)
        self.assertEqual(dict(result.validated.payload)['quantity'], '0.001')
        self.assertEqual(dict(result.validated.payload)['requested_quantity'], '0.0019')

    def test_validated_models_are_frozen_and_bound_to_payload_and_evidence(self):
        result = self.decision().validated
        with self.assertRaises(FrozenInstanceError):
            result.environment = 'live'
        with self.assertRaises(FrozenInstanceError):
            evidence()[2].base_quantity = Decimal('1')
        self.assertIsInstance(result.payload, tuple)
        self.assertEqual(result.expires_at, NOW + timedelta(seconds=5))
        self.assertNotEqual(result.payload_fingerprint, self.decision(0, price=Decimal('60001')).validated.payload_fingerprint)
        self.assertNotEqual(result.evidence_fingerprint,
                            self.decision(2, configured_leverage=Decimal('10')).validated.evidence_fingerprint)

    def test_high_precision_native_maximum_is_preserved_for_reduce_and_close(self):
        maximum = Decimal('0.99999999999999999999999999999')
        for action in ('reduce', 'close'):
            for quantity, accepted in ((maximum, True), (Decimal('1'), False)):
                values = list(evidence())
                values[0] = replace(values[0], action=action, quantity=quantity, price=Decimal('100'))
                values[2] = replace(values[2], base_quantity=quantity if action == 'close' else Decimal('2'))
                values[3] = replace(values[3], quantity_step=Decimal('1e-29'), contract_step=Decimal('1e-29'),
                                    min_contracts=Decimal('1e-29'), max_contracts=maximum,
                                    max_base_quantity=maximum)
                with self.subTest(action=action, quantity=quantity):
                    result = evaluate(*values, now=NOW)
                    self.assertEqual(result.allowed, accepted)
                    if accepted:
                        self.assertEqual(Decimal(dict(result.validated.payload)['quantity']), maximum)

    def test_no_credentials_or_exchange_construction(self):
        with patch('config.load_settings', side_effect=AssertionError('Credential access')), \
             patch('exchange_client.PhemexClient', side_effect=AssertionError('Exchange construction')):
            self.assertTrue(self.decision().allowed)
