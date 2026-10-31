from copy import deepcopy
from unittest.mock import Mock

from readonly_exchange import READ_ROUTES, ReadOnlyPhemex
from safety import SafetyError
from tests.helpers import MARKET, SYMBOL, OfflineTestCase


class ReadOnlyTransportTests(OfflineTestCase):
    def exchange(self):
        ex = ReadOnlyPhemex({'apiKey': 'SYNTHETIC', 'secret': 'SYNTHETIC', 'enableRateLimit': False})
        ex.set_sandbox_mode(True)
        ex.set_markets([deepcopy(MARKET)])
        ex.fetch = Mock(return_value={'code': 0, 'data': {'riskMode': 'Classic'}})
        return ex

    def test_allowlisted_get_uses_ccxt_signing_and_testnet(self):
        ex = self.exchange()
        self.assertEqual(ex.privateGetUtaApiRiskRiskMode({})['data']['riskMode'], 'Classic')
        call = ex.fetch.call_args
        self.assertTrue(call.args[0].startswith('https://testnet-api.phemex.com/'))
        self.assertEqual(call.args[1], 'GET')
        self.assertIn('x-phemex-request-signature', call.args[2])

    def test_native_and_normalized_mutators_cannot_reach_transport(self):
        ex = self.exchange()
        calls = [
            lambda: ex.create_order(SYMBOL, 'limit', 'buy', 1, 100, {'posSide': 'Long'}),
            lambda: ex.edit_order('id', SYMBOL, 'limit', 'buy', 1, 100),
            lambda: ex.cancel_order('id', SYMBOL),
            lambda: ex.cancel_all_orders(SYMBOL),
            lambda: ex.set_leverage(2, SYMBOL),
            lambda: ex.set_margin_mode('isolated', SYMBOL, {'leverage': 2}),
            lambda: ex.set_position_mode(True, SYMBOL),
            lambda: ex.privatePostGOrders({'symbol': 'BTCUSDT'}),
            lambda: ex.request('uta-account/switch-mode', 'private', 'POST', {}),
            lambda: ex.request('phemex-withdraw/wallets/api/createWithdraw', 'private', 'POST', {}),
            lambda: ex.request('assets/transfer', 'private', 'POST', {}),
        ]
        for index, call in enumerate(calls):
            with self.subTest(index=index), self.assertRaises(SafetyError): call()
        ex.fetch.assert_not_called()

    def test_get_alone_is_not_sufficient_to_authorize_an_endpoint(self):
        ex = self.exchange()
        for api, path, method in (
            ('private', 'g-orders/create', 'GET'), ('private', 'uta-funds/contract/borrow', 'GET'),
            ('private', 'g-orders', 'POST'), ('private', 'g-accounts/positions', 'DELETE'),
            ('public', 'uta-api/risk/risk-mode', 'GET'),
        ):
            with self.subTest(path=path, method=method), self.assertRaises(SafetyError):
                ex.request(path, api, method, {})
        ex.fetch.assert_not_called()
        self.assertIsInstance(READ_ROUTES, frozenset)

    def test_actual_market_loading_routes_are_allowlisted(self):
        ex = ReadOnlyPhemex({'enableRateLimit': False})
        ex.set_sandbox_mode(True)
        product = {
            'symbol': 'BTCUSDT', 'type': 'PerpetualV2', 'baseCurrency': 'BTC',
            'quoteCurrency': 'USDT', 'settleCurrency': 'USDT', 'status': 'Listed',
            'qtyStepSize': '0.001', 'tickSize': '0.1', 'maxOrderQtyRq': '1000',
            'minOrderValueRv': '1', 'maxLeverage': 100,
        }
        def reply(url, *args, **kwargs):
            if 'exchange/public/products' in url:
                return {'code': 0, 'data': []}
            if '/public/products' in url:
                return {'code': 0, 'data': {'perpProductsV2': [product], 'products': [],
                        'currencies': [{'currency': 'USDT', 'code': 3, 'valueScale': 8}]}}
            raise AssertionError('Unexpected metadata endpoint')
        ex.fetch = Mock(side_effect=reply)
        markets = ex.load_markets()
        self.assertIn(SYMBOL, markets)
        self.assertTrue(all(c.args[1] == 'GET' for c in ex.fetch.call_args_list))
        self.assertTrue(all('testnet-api.phemex.com' in c.args[0] for c in ex.fetch.call_args_list))
