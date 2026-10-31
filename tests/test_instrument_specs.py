from copy import deepcopy
from decimal import Decimal

import ccxt

from safety import SafetyError
from tests.helpers import MARKET, SYMBOL, OfflineTestCase, make_client


class InstrumentTests(OfflineTestCase):
    def test_native_usdt_limits_omitted_by_ccxt_are_exposed_and_used(self):
        market = deepcopy(MARKET)
        market['info'] = {'status': 'Listed', 'maxOrderQtyRq': '2', 'minOrderValueRv': '10',
                          'minPriceRp': '5', 'maxPriceRp': '100000', 'qtyStepSize': '0.001',
                          'tickSize': '1', 'maxLeverage': '50'}
        client, _ = make_client(markets=[market])
        fields = client.instrument(SYMBOL)
        self.assertEqual(fields['max_base_quantity'].value, Decimal('2'))
        self.assertEqual(fields['max_contracts'].value, Decimal('2'))
        self.assertEqual(fields['min_notional'].value, Decimal('10'))
        self.assertEqual(fields['max_leverage'].value, Decimal('50'))
        self.assertEqual(fields['max_price'].value, Decimal('100000'))
        self.assertEqual(fields['quantity_step'].value, Decimal('0.001'))
        self.assertEqual(fields['price_tick'].value, Decimal('1'))
        with self.assertRaises(SafetyError): client.round_amount(SYMBOL, Decimal('2.1'))
        with self.assertRaises(SafetyError): client.notional(SYMBOL, Decimal('0.001'), Decimal('100'))
        with self.assertRaises(SafetyError): client.round_price(SYMBOL, Decimal('2'))

    def test_missing_or_invalid_native_fields_are_unknown(self):
        for value in ('NaN', '-1', True, 'SECRET_SENTINEL'):
            market = deepcopy(MARKET)
            market['info'] = {'maxOrderQtyRq': value}
            client, _ = make_client(markets=[market])
            with self.subTest(value=value):
                fields = client.instrument(SYMBOL)
                self.assertIsNone(fields['max_base_quantity'].value)
                self.assertIsNone(fields['max_contracts'].value)
                self.assertNotIn('SECRET_SENTINEL', repr(fields))
        self.assertIsNone(fields['leverage_tiers'].value)

    def test_legacy_max_order_quantity_is_not_a_notional_limit(self):
        market = deepcopy(MARKET)
        market['info'] = {'maxOrderQty': 1000}
        market['limits']['cost']['max'] = 1000
        client, _ = make_client(markets=[market])
        self.assertIsNone(client.instrument(SYMBOL)['max_notional'].value)
        self.assertIsNone(client.market_info(SYMBOL)['max_cost'])

    def test_delisted_instrument_can_be_inspected_but_not_used_for_entry(self):
        market = {**deepcopy(MARKET), 'active': False, 'info': {'status': 'Delisted'}}
        client, _ = make_client(markets=[market])
        self.assertIs(client.instrument('BTCUSDT')['active'].value, False)
        with self.assertRaises(SafetyError): client.resolve('BTCUSDT')

    def test_non_tick_precision_is_not_mislabeled_as_step_size(self):
        client, ex = make_client()
        ex.precisionMode = ccxt.DECIMAL_PLACES
        self.assertIsNone(client.instrument(SYMBOL)['quantity_step'].value)
        self.assertIsNone(client.instrument(SYMBOL)['price_tick'].value)

    def test_inconsistent_listing_and_bounds_are_unknown(self):
        market = deepcopy(MARKET)
        market['info'] = {'status': 'Delisted', 'minPriceRp': '200', 'maxPriceRp': '100'}
        client, _ = make_client(markets=[market])
        fields = client.instrument(SYMBOL)
        self.assertIsNone(fields['active'].value)
        self.assertIsNone(fields['min_price'].value)
        self.assertIsNone(fields['max_price'].value)
        with self.assertRaises(SafetyError): client.resolve(SYMBOL)

    def test_ccxt_contract_convention_is_identified_as_derived(self):
        client, _ = make_client()
        self.assertEqual(client.instrument(SYMBOL)['contract_size'].origin, 'derived')
        market = deepcopy(MARKET)
        market['info'] = {'contractSize': '0.1'}
        client, _ = make_client(markets=[market])
        self.assertIsNone(client.instrument(SYMBOL)['contract_size'].value)

    def test_native_step_and_tick_mismatch_refuses_misleading_normalization(self):
        market = deepcopy(MARKET)
        market['info'] = {'qtyStepSize': '0.01', 'tickSize': '2'}
        client, _ = make_client(markets=[market])
        with self.assertRaises(SafetyError): client.round_amount(SYMBOL, Decimal('0.005'))
        with self.assertRaises(SafetyError): client.round_price(SYMBOL, Decimal('101'))

    def test_normalized_quantity_tick_is_converted_to_base_units(self):
        market = {**deepcopy(MARKET), 'contractSize': 2}
        client, _ = make_client(markets=[market])
        self.assertEqual(client.instrument(SYMBOL)['quantity_step'].value, Decimal('0.002'))

    def test_invalid_explicit_native_limits_block_dry_preview_helpers(self):
        for info, action in (
            ({'maxOrderQtyRq': 'NaN'}, lambda c: c.round_amount(SYMBOL, Decimal('1'))),
            ({'minOrderValueRv': 'NaN'}, lambda c: c.notional(SYMBOL, Decimal('1'), Decimal('100'))),
            ({'minPriceRp': '200', 'maxPriceRp': '100'}, lambda c: c.round_price(SYMBOL, Decimal('150'))),
        ):
            market = {**deepcopy(MARKET), 'info': info}
            client, _ = make_client(markets=[market])
            with self.subTest(info=info), self.assertRaises(SafetyError): action(client)
