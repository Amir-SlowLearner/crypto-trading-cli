from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext
import inspect
import json
from unittest.mock import patch

import ccxt
from ccxt.base.decimal_to_precision import decimal_to_precision
from ccxt.base.exchange import Exchange
from ccxt.abstract.phemex import ImplicitAPI

from phemex_request_compiler import (CompilationError, check_compiled, compile_request,
                                    verify_installed_ccxt)
from tests.execution_fixtures import NOW, evidence
from tests.helpers import OfflineTestCase


CLIENT_ID = 'tn_' + '1' * 32


def compiled(values=None, client_id=CLIENT_ID):
    return compile_request(*(evidence() if values is None else values), now=NOW, client_order_id=client_id)


class PrecisionView:
    """Only the data required by CCXT's pure precision methods, no exchange."""
    precisionMode = ccxt.TICK_SIZE
    paddingMode = ccxt.NO_PADDING
    decimal_to_precision = staticmethod(decimal_to_precision)

    def market(self, symbol):
        return {'symbol': symbol, 'precision': {'amount': '0.001', 'price': '0.1'}}


class RequestCompilerTests(OfflineTestCase):
    def test_installed_ccxt_source_is_exact_reviewed_version(self):
        self.assertEqual(verify_installed_ccxt(), 'phemex-4.5.90-btc-hedge-ioc-v1')
        # Generated implicit entry is inspected, never invoked or instantiated.
        self.assertIn("private_post_g_orders = privatePostGOrders = Entry[_Dict]('g-orders', ['private'], 'POST'",
                      inspect.getsource(ImplicitAPI))

    def test_version_or_source_change_requires_review(self):
        with patch('ccxt.__version__', 'UNKNOWN'):
            with self.assertRaises(CompilationError): verify_installed_ccxt()
            with self.assertRaises(CompilationError): compiled()
        with patch('phemex_request_compiler.inspect.getsource', return_value='changed'):
            with self.assertRaises(CompilationError): verify_installed_ccxt()

    def test_exact_hedge_reduce_and_close_request_matrix(self):
        for action in ('reduce', 'close'):
            for leg, side in (('long', 'sell'), ('short', 'buy')):
                values = list(evidence())
                values[0] = replace(values[0], action=action, position_side=leg, side=side,
                                    quantity=Decimal('0.002' if action == 'close' else '0.001'))
                values[2] = replace(values[2], position_side=leg)
                with self.subTest(action=action, leg=leg):
                    request = compiled(values)
                    self.assertEqual(request.unsigned_business_request, {
                        'environment': 'testnet', 'origin': 'https://testnet-api.phemex.com',
                        'method': 'POST', 'api': 'private', 'path': '/g-orders',
                        'parameters': {'symbol': 'BTCUSDT', 'side': side.capitalize(), 'ordType': 'Limit',
                            'posSide': leg.capitalize(), 'reduceOnly': True, 'timeInForce': 'ImmediateOrCancel',
                            'clOrdID': CLIENT_ID, 'orderQtyRq': '0.002' if action == 'close' else '0.001',
                            'priceRp': '60000'}})
                    self.assertEqual(check_compiled(request), request)
                    self.assertFalse(request.submission_enabled)
                    self.assertFalse(request.authenticated_evidence_verified)

    def test_pure_ccxt_precision_helpers_match_final_strings(self):
        values = list(evidence())
        values[0] = replace(values[0], quantity=Decimal('0.0019'), price=Decimal('60000.1'))
        request = compiled(values)
        params = dict(request.parameters)
        self.assertEqual(params['orderQtyRq'], Exchange.amount_to_precision(PrecisionView(), 'BTC/USDT:USDT', '0.001'))
        self.assertEqual(params['priceRp'], Exchange.price_to_precision(PrecisionView(), 'BTC/USDT:USDT', '60000.1'))
        self.assertEqual(dict(request.plan.payload)['requested_quantity'], '0.0019')
        # CCXT would round this price; our policy rejects it instead.
        values[0] = replace(values[0], price=Decimal('60000.15'))
        with self.assertRaises(CompilationError): compiled(values)

    def test_deterministic_and_not_affected_by_callers_decimal_precision(self):
        expected = compiled()
        with localcontext() as context:
            context.prec = 6
            actual = compiled()
        self.assertEqual(actual, expected)
        self.assertEqual(actual.request_fingerprint, expected.request_fingerprint)
        self.assertEqual(json.loads(actual.canonical_request), actual.unsigned_business_request)

    def test_non_unit_contract_mapping_fails_closed(self):
        values = list(evidence())
        values[3] = replace(values[3], contract_size=Decimal('0.1'), contract_step=Decimal('0.01'))
        with self.assertRaisesRegex(CompilationError, 'Non-unit'): compiled(values)

    def test_missing_unknown_stale_and_synthetic_evidence_rejected(self):
        from execution_policy import Provenance
        for index, changes in ((1, {'accounting_type': None}), (2, {'base_quantity': None}),
                               (3, {'quantity_step': None}), (5, {'explicit_hedge_reduce_only': None})):
            values = list(evidence())
            values[index] = replace(values[index], **changes)
            with self.subTest(index=index), self.assertRaises(CompilationError): compiled(values)
        for index in range(1, 6):
            values = list(evidence())
            values[index] = replace(values[index], stamp=replace(values[index].stamp, provenance=Provenance.SYNTHETIC))
            with self.subTest(index=index), self.assertRaises(CompilationError): compiled(values)
        with self.assertRaises(CompilationError):
            compile_request(*evidence(), now=NOW + timedelta(seconds=5), client_order_id=CLIENT_ID)

    def test_invalid_intent_combinations_and_hedge_direction_rejected(self):
        for changes in ({'side': 'buy'}, {'position_side': 'short'}, {'position_side': 'Merged'},
                        {'action': 'open'}, {'action': 'increase'}, {'reduce_only': False},
                        {'time_in_force': 'GTC'}, {'order_type': 'market'}, {'tp': Decimal('61000')},
                        {'quantity': Decimal('0.002')}, {'quantity': Decimal('0.0001')}):
            values = list(evidence())
            values[0] = replace(values[0], **changes)
            with self.subTest(changes=changes), self.assertRaises(CompilationError): compiled(values)

    def test_no_silent_close_rounding_or_native_maximum_overrun(self):
        for action in ('reduce', 'close'):
            values = list(evidence())
            values[0] = replace(values[0], action=action, quantity=Decimal('0.0019'))
            values[2] = replace(values[2], base_quantity=Decimal('0.0019') if action == 'close' else Decimal('0.002'))
            with self.subTest(action=action):
                if action == 'close':
                    with self.assertRaises(CompilationError): compiled(values)
                else:
                    self.assertEqual(dict(compiled(values).parameters)['orderQtyRq'], '0.001')
        values = list(evidence())
        values[3] = replace(values[3], max_base_quantity=Decimal('0.00099999999999999999999999999999'))
        with self.assertRaises(CompilationError): compiled(values)

    def test_exact_request_allowlist_rejects_each_tampered_missing_and_extra_parameter(self):
        request = compiled()
        for key, value in request.parameters:
            changes = dict(request.parameters)
            changes[key] = False if type(value) is bool else 'SECRET_SENTINEL'
            for pairs in (tuple(sorted(changes.items())), tuple(pair for pair in request.parameters if pair[0] != key)):
                with self.subTest(key=key), self.assertRaises(CompilationError) as error:
                    check_compiled(replace(request, parameters=pairs))
                self.assertNotIn('SECRET_SENTINEL', str(error.exception))
        for pair in (('secret', 'SECRET_SENTINEL'), ('posSide', 'Long')):
            with self.assertRaises(CompilationError):
                check_compiled(replace(request, parameters=request.parameters + (pair,)))

    def test_mutable_or_wrongly_typed_fields_rejected(self):
        request = compiled()
        for changes in ({'parameters': list(request.parameters)}, {'context': list(request.context)},
                        {'parameters': (('reduceOnly', 1),)}, {'profile': 'other'},
                        {'client_order_id': 'tn_' + '2' * 32},
                        {'plan': replace(request.plan, payload_fingerprint='0' * 64)}):
            with self.subTest(changes=changes), self.assertRaises(CompilationError):
                check_compiled(replace(request, **changes))
        with self.assertRaises(FrozenInstanceError): request.profile = 'changed'
        copy = request.unsigned_business_request
        copy['parameters']['reduceOnly'] = False
        self.assertTrue(dict(request.parameters)['reduceOnly'])

    def test_client_id_is_explicit_and_never_generated_or_replaced(self):
        for value in (None, '', 'auto', 'tn_' + 'a' * 40, 'tn_' + 'A' * 32):
            with self.subTest(value=value), self.assertRaises(CompilationError): compiled(client_id=value)
        self.assertNotEqual(compiled().request_fingerprint, compiled(client_id='tn_' + '2' * 32).request_fingerprint)

    def test_fabricated_labels_remain_offline_assertions_only(self):
        request = compiled()  # Deliberately fabricated AUTHENTICATED_TESTNET tags.
        self.assertFalse(request.authenticated_evidence_verified)
        self.assertFalse(request.submission_enabled)

    def test_compilation_does_not_construct_exchange_read_files_or_load_credentials(self):
        with patch('ccxt.phemex', side_effect=AssertionError('Exchange construction')), \
             patch('builtins.open', side_effect=AssertionError('File access')), \
             patch('order_preparation.uuid4', side_effect=AssertionError('Random client ID')), \
             patch('config.load_settings', side_effect=AssertionError('Credentials')):
            self.assertEqual(compiled(), compiled())

    def test_high_precision_request_quantity_is_exact_not_rounded(self):
        maximum = Decimal('0.99999999999999999999999999999')
        for action in ('reduce', 'close'):
            values = list(evidence())
            values[0] = replace(values[0], action=action, quantity=maximum, price=Decimal('100'))
            values[2] = replace(values[2], base_quantity=maximum if action == 'close' else Decimal('2'))
            values[3] = replace(values[3], quantity_step=Decimal('1e-29'), contract_step=Decimal('1e-29'),
                                min_contracts=Decimal('1e-29'), max_contracts=maximum, max_base_quantity=maximum)
            with self.subTest(action=action), localcontext() as context:
                context.prec = 6
                request = compiled(values)
                self.assertEqual(dict(request.parameters)['orderQtyRq'], str(maximum))
                self.assertEqual(check_compiled(request), request)
