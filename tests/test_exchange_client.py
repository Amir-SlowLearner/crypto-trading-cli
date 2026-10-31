from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock, patch

import ccxt

from exchange_client import PhemexClient
from safety import SafetyError
from tests.helpers import MARKET, SYMBOL, OfflineTestCase, make_client, open_order


class AdapterTests(OfflineTestCase):
    def test_raw_and_unified_symbols_resolve_to_same_market(self):
        client, _ = make_client()
        self.assertEqual(client.resolve('btcusdt'), SYMBOL)
        self.assertEqual(client.resolve(SYMBOL), SYMBOL)

    def test_target_markets_resolve_when_instrument_metadata_is_present(self):
        markets = [{**deepcopy(MARKET), 'base': base, 'baseId': base,
                    'id': base + 'USDT', 'symbol': base + '/USDT:USDT'}
                   for base in ('BTC', 'ETH', 'SOL', 'XRP')]
        client, _ = make_client(markets=markets)
        for base in ('BTC', 'ETH', 'SOL', 'XRP'):
            with self.subTest(base=base):
                self.assertEqual(client.resolve(base + 'USDT'), base + '/USDT:USDT')

    def test_spot_inverse_inactive_and_other_settlement_rejected(self):
        variants = [
            {'id': 'BTCUSDT', 'symbol': 'BTC/USDT', 'swap': False, 'spot': True, 'settle': None},
            {'id': 'BTCUSD', 'symbol': 'BTC/USD:BTC', 'linear': False, 'inverse': True, 'settle': 'BTC', 'quote': 'USD'},
            {'active': False}, {'active': None}, {'settle': 'USDC'},
            {'quote': 'USD'}, {'contractSize': None}, {'contractSize': 0},
        ]
        for changes in variants:
            market = {**deepcopy(MARKET), **changes}
            client, _ = make_client(markets=[market])
            for value in (market['symbol'], market['id']):
                with self.subTest(changes=changes, value=value), self.assertRaises(SafetyError):
                    client.resolve(value)

    def test_ambiguous_raw_market_is_rejected(self):
        second = {**deepcopy(MARKET), 'symbol': 'BTC/USDT:USDT-duplicate'}
        client, _ = make_client(markets=[MARKET, second])
        with self.assertRaises(SafetyError): client.resolve('BTCUSDT')

    def test_real_ccxt_precision_helpers_and_contract_notional(self):
        market = {**deepcopy(MARKET), 'contractSize': 2}
        client, _ = make_client(markets=[market])
        self.assertEqual(client.round_amount(SYMBOL, Decimal('0.1239')), Decimal('0.123'))
        self.assertEqual(client.round_price(SYMBOL, Decimal('100.4')), Decimal('100'))
        self.assertEqual(client.notional(SYMBOL, Decimal('0.5'), Decimal('100')), Decimal('100'))

    def test_precision_receives_decimal_strings(self):
        client, ex = make_client()
        ex.amount_to_precision = Mock(return_value='0.123')
        ex.price_to_precision = Mock(return_value='100')
        client.round_amount(SYMBOL, Decimal('0.123456789012345678901234567'))
        client.round_price(SYMBOL, Decimal('100.123456789012345678901234567'))
        self.assertIsInstance(ex.amount_to_precision.call_args.args[1], str)
        self.assertEqual(ex.amount_to_precision.call_args.args[1], '0.123456789012345678901234567')
        self.assertEqual(ex.price_to_precision.call_args.args[1], '100.123456789012345678901234567')

    def test_normalization_rejects_zero_bad_numbers_and_exchange_limits(self):
        client, _ = make_client()
        for amount in ('0.0001', '1001', '0', '-1', 'NaN', 'Infinity'):
            with self.subTest(amount=amount), self.assertRaises((SafetyError, ccxt.InvalidOrder)):
                client.round_amount(SYMBOL, Decimal(amount))
        for price in ('0.1', '0', '-1', 'NaN', 'Infinity'):
            with self.subTest(price=price), self.assertRaises((SafetyError, ccxt.InvalidOrder)):
                client.round_price(SYMBOL, Decimal(price))
        with self.assertRaises(SafetyError):
            client.notional(SYMBOL, Decimal('0.001'), Decimal('100'))

    def test_normalization_never_increases_amount(self):
        client, ex = make_client()
        ex.amount_to_precision = Mock(return_value='2')
        with self.assertRaises(SafetyError): client.round_amount(SYMBOL, Decimal('1.9'))

    def test_missing_position_side_never_inferred_from_trade_direction(self):
        for side in ('buy', 'sell', None):
            with self.subTest(side=side), self.assertRaises(SafetyError):
                PhemexClient.order_pos_side({'side': side, 'info': {}})
        for side in ('Long', 'Short', 'Merged'):
            self.assertEqual(PhemexClient.order_pos_side({'info': {'posSide': side}}), side)

    def test_duplicate_lookup_does_not_select_arbitrary_order(self):
        client, ex = make_client()
        ex.fetch_open_orders.return_value = [open_order(), open_order(id='other')]
        with self.assertRaises(SafetyError): client.find_open_order(SYMBOL, cl_id='client-1')
        for args in ({}, {'cl_id': 'client-1', 'ord_id': 'order-1'}):
            with self.assertRaises(SafetyError): client.find_open_order(SYMBOL, **args)

    def test_missing_position_size_is_not_silently_treated_as_zero(self):
        client, ex = make_client()
        for size in (None, 'NaN', '-1', 'Infinity'):
            ex.fetch_positions.return_value = [{'symbol': SYMBOL, 'contracts': size}]
            with self.subTest(size=size), self.assertRaises(SafetyError): client.positions()

    def test_empty_position_is_filtered_but_errors_propagate(self):
        client, ex = make_client()
        ex.fetch_positions.return_value = [{'symbol': SYMBOL, 'contracts': 0}]
        self.assertEqual(client.positions(), [])
        ex.fetch_positions.side_effect = ccxt.AuthenticationError('synthetic')
        with self.assertRaises(ccxt.AuthenticationError): client.positions()

    def test_testnet_is_default_and_no_settings_are_changed(self):
        client, ex = make_client()
        self.assertFalse(client.live)
        self.assertTrue(ex.isSandboxModeEnabled)
        self.assertTrue(all('testnet-api.phemex.com' in url for url in ex.urls['api'].values()))
        for name in ('set_position_mode', 'set_margin_mode', 'set_leverage'):
            getattr(ex, name).assert_not_called()

    def test_transport_has_timeout_rate_limit_and_no_retries(self):
        ex = Mock()
        with patch('exchange_client.ccxt.phemex', return_value=ex) as factory:
            PhemexClient('KEY', 'SECRET')
        options = factory.call_args.args[0]
        self.assertEqual(options['timeout'], 15000)
        self.assertTrue(options['enableRateLimit'])
        self.assertFalse(options['verbose'])
        self.assertEqual(options['options']['maxRetriesOnFailure'], 0)
        ex.set_sandbox_mode.assert_called_once_with(True)

    def test_all_mutation_methods_block_before_transport_for_both_environments(self):
        for live in (False, True):
            client, ex = make_client(live=live)
            actions = [
                lambda: client.place_order(SYMBOL, 'buy', 'Long', 'market', Decimal('1')),
                lambda: client.amend_order(open_order(), amount=Decimal('1000')),
                lambda: client.cancel_order(open_order()),
                lambda: client.cancel_all(SYMBOL),
                lambda: client.set_leverage(SYMBOL, Decimal('2')),
            ]
            for index, action in enumerate(actions):
                with self.subTest(live=live, action=index), self.assertRaises(SafetyError): action()
            for name in ('create_order', 'edit_order', 'cancel_order', 'cancel_all_orders', 'set_leverage'):
                getattr(ex, name).assert_not_called()
