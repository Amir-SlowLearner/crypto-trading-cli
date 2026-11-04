"""Read-command regressions: synthetic responses only, no network or credentials."""
from copy import deepcopy
from decimal import Decimal
import contextlib
import io
import json
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import ccxt
import requests

import trade_cli
import trading_reads as reads
from account_discovery import unknown
from exchange_client import PhemexClient
from safety import SafetyError
from tests.account_fixtures import discovery_client, envelope, raw_position
from tests.compatibility_fixtures import PRODUCT
from tests.compatibility_fixtures import responses
from tests.helpers import MARKET, SYMBOL, OfflineTestCase


class TradingReadTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.denied = []
        for target in ('socket.getaddrinfo', 'socket.create_connection',
                       'requests.sessions.Session.send', 'requests.adapters.HTTPAdapter.send',
                       'config.load_settings', 'manual_testnet_status.load_credentials',
                       'testnet_status_runtime.load_testnet_profile',
                       'testnet_transport.OfflineGetTransport._send_http'):
            guard = patch(target, side_effect=AssertionError('Prohibited capability'))
            self.denied.append(guard.start())
            self.addCleanup(guard.stop)
        self.addCleanup(self.no_prohibited_calls)
        self.client, self.ex = discovery_client()
        self.ex.privateGetGAccountsPositions.return_value['data']['account'].update(
            accountId='222', userID='111')
        self.ex.privateGetGOrdersActiveList = Mock(return_value=envelope({'rows': [self.row()]}))
        self.ex.privateGetApiDataGFuturesOrdersByOrderId = Mock(return_value=envelope([self.row()]))

    def no_prohibited_calls(self):
        for guard in self.denied:
            guard.assert_not_called()
        for name in ('create_order', 'edit_order', 'cancel_order', 'cancel_all_orders',
                     'set_leverage', 'set_margin_mode', 'set_position_mode'):
            getattr(self.ex, name).assert_not_called()

    @staticmethod
    def row(**changes):
        row = dict(orderID='order-1', symbol='BTCUSDT', accountID='222', userID='111',
                   side='Sell', posSide='Long', orderQtyRq='0.002', priceRp='60000',
                   ordStatus='New', reduceOnly=True)
        row.update(changes)
        return row

    def run_command(self, text):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            trade_cli.COMMANDS[text.split()[0]](self.client, text.split())
        return output.getvalue()

    def test_positional_and_existing_keyword_syntax(self):
        for command in ('price', 'positions', 'orders'):
            self.assertEqual(reads.parse([command, 'BTCUSDT'])[0], {'symbol': 'BTCUSDT'})
            self.assertEqual(reads.parse([command, 'symbol=BTCUSDT'])[0], {'symbol': 'BTCUSDT'})
        self.assertEqual(reads.parse(['order-status', 'order-1'])[0], {'ord_id': 'order-1'})

    def test_strict_read_grammar(self):
        for text in ('price', 'price BTCUSDT extra', 'price BTCUSDT symbol=ETHUSDT',
                     'order-status', 'order-status order-1 dry', 'order-status cl_id=x',
                     'order-status order-1 ord_id=other', 'order-status bad/id',
                     'orders BTCUSDT retry=true', 'positions BTCUSDT dry'):
            with self.subTest(text=text), self.assertRaises(SafetyError):
                reads.parse(text.split())

    def test_bid_ask_with_exchange_and_collection_timestamps(self):
        self.ex.fetch_order_book.return_value.update(symbol=SYMBOL, timestamp=1793448000000)
        output = self.run_command('price BTCUSDT')
        self.assertIn('bid=99 ask=100', output)
        self.assertIn('2026-10-31T12:00:00+00:00', output)
        self.ex.fetch_order_book.assert_called_once_with(SYMBOL, limit=5)
        self.ex.load_markets.assert_called_once()  # Fixture setup only; no reload.
        self.ex.privateGetGAccountsPositions.assert_not_called()

    def test_invalid_or_incomplete_prices_are_unknown(self):
        for book in ({}, {'symbol': SYMBOL, 'bids': [], 'asks': []},
                     {'symbol': 'ETH/USDT:USDT', 'bids': [[99, 1]], 'asks': [[100, 1]]},
                     {'symbol': SYMBOL, 'bids': [[101, 1]], 'asks': [[100, 1]]},
                     {'symbol': SYMBOL, 'bids': [['99.1', 1]], 'asks': [['NaN', 1]]}):
            self.ex.fetch_order_book.return_value = book
            self.assertIn('bid=UNKNOWN ask=UNKNOWN', self.run_command('price BTCUSDT'))

    def test_missing_exchange_time_is_not_replaced_by_collection_time(self):
        self.ex.fetch_order_book.return_value.update(symbol=SYMBOL)
        self.assertIn('Exchange timestamp: UNKNOWN; collected UTC:', self.run_command('price BTCUSDT'))

    def test_balance_reuses_classic_and_unified_scoped_discovery(self):
        output = self.run_command('balance')
        self.assertIn('balance: 1000 [reported:', output)
        self.assertIn('available_margin: unknown', output)
        self.assertIn('available_margin_estimate: 500', output)
        self.ex.privateGetUtaApiRiskRiskMode.assert_called_once()
        self.client, self.ex = discovery_client('MultiAsset')
        output = self.run_command('balance')
        self.assertIn('balance: 500 [reported:', output)
        self.assertNotIn('999999', output)
        self.assertIn('available_margin: unknown', output)

    def test_unknown_accounting_does_not_default_wallet_or_margin(self):
        self.ex.privateGetUtaApiRiskRiskMode.side_effect = ccxt.ExchangeError('PRIVATE_SECRET')
        output = self.run_command('balance')
        self.assertIn('balance: unknown', output)
        self.assertIn('available_margin: unknown', output)
        self.assertNotIn('PRIVATE_SECRET', output)

    def hedge_rows(self):
        return [raw_position(mode='Hedged', pos_side='Long', size='0.002'),
                raw_position(mode='Hedged', pos_side='Short', side='None', size='0')]

    def test_explicit_zero_hedge_legs_and_entry_are_retained(self):
        self.ex.privateGetGAccountsPositions.return_value['data']['positions'] = self.hedge_rows()
        output = self.run_command('positions BTCUSDT')
        self.assertIn('Long: quantity=0.002', output)
        self.assertIn('entry=60000', output)
        self.assertIn('Short: quantity=0', output)
        self.assertIn('entry=UNKNOWN (no verified nonzero position)', output)
        self.assertIn('leverage=5', output)
        self.ex.privateGetUtaApiRiskRiskMode.assert_not_called()

    def test_missing_duplicate_oneway_wrong_scope_and_conflicting_position_fields(self):
        for rows in ([], self.hedge_rows()[:1], self.hedge_rows() + self.hedge_rows()[:1],
                     [raw_position()], [dict(self.hedge_rows()[0], currency='BTC')],
                     [dict(self.hedge_rows()[0], symbol='ETHUSDT')]):
            self.ex.privateGetGAccountsPositions.return_value['data']['positions'] = rows
            self.assertIn('UNKNOWN', self.run_command('positions BTCUSDT'))
        self.ex.privateGetGAccountsPositions.return_value['data']['positions'] = [
            dict(self.hedge_rows()[0], size='9', crossMargin=False)]
        output = self.run_command('positions BTCUSDT')
        self.assertIn('quantity=UNKNOWN', output)
        self.assertIn('margin=UNKNOWN', output)

    def test_positions_scope_failure_pagination_and_timeouts_do_not_claim_flat(self):
        frame = self.ex.privateGetGAccountsPositions.return_value['data']
        for change in ({'account': {'currency': 'BTC'}}, {'nextPageArg': 'next'}, {'positions': {}}):
            with patch.dict(frame, change):
                self.assertIn('Long: quantity=UNKNOWN', self.run_command('positions BTCUSDT'))
        self.ex.privateGetGAccountsPositions.side_effect = ccxt.RequestTimeout('PRIVATE_SECRET')
        output = self.run_command('positions BTCUSDT')
        self.assertIn('UNKNOWN', output)
        self.assertNotIn('PRIVATE_SECRET', output)

    def test_open_orders_bind_scope_and_show_only_requested_details(self):
        output = self.run_command('orders BTCUSDT')
        for value in ('ID=order-1', 'side=Sell', 'leg=Long', 'quantity=0.002',
                      'price=60000', 'status=New', 'reduce_only=True', 'coverage'):
            self.assertIn(value, output)
        self.assertNotIn('accountID', output)
        self.ex.privateGetGOrdersActiveList.assert_called_once_with({'symbol': 'BTCUSDT'})
        self.ex.fetch_open_orders.assert_not_called()  # Avoid normalized defaults/filtering.

    def test_exact_order_lookup_and_cached_symbol_binding(self):
        self.run_command('orders BTCUSDT')
        output = self.run_command('order-status order-1')
        self.assertIn('ID=order-1', output)
        self.ex.privateGetApiDataGFuturesOrdersByOrderId.assert_called_once_with(
            {'symbol': 'BTCUSDT', 'orderID': 'order-1'})

    def test_unknown_id_requires_symbol_before_private_requests(self):
        with self.assertRaisesRegex(SafetyError, 'scope UNKNOWN'):
            self.run_command('order-status never-seen')
        self.ex.privateGetGAccountsPositions.assert_not_called()

    def test_terminal_single_object_and_missing_order_are_not_confused(self):
        self.ex.privateGetApiDataGFuturesOrdersByOrderId.return_value = envelope(self.row(ordStatus='Canceled'))
        self.assertIn('status=Canceled', self.run_command('order-status order-1 symbol=BTCUSDT'))
        self.ex.privateGetApiDataGFuturesOrdersByOrderId.return_value = envelope([])
        output = self.run_command('order-status order-1 symbol=BTCUSDT')
        self.assertIn('UNKNOWN', output)
        self.assertNotIn('status=Canceled', output)

    def test_bad_order_ids_duplicates_symbols_and_account_bindings_are_unknown(self):
        cases = [[self.row(orderID='different')], [self.row(), self.row()],
                 [self.row(symbol='ETHUSDT')], [self.row(accountID='999')],
                 [self.row(userID='999')], [self.row(accountID=True)],
                 [self.row(accountId='999')], [self.row(currency='BTC')],
                 [self.row(orderID='PRIVATE\nVALUE')]]
        only_user = self.row()
        del only_user['accountID']
        cases.append([only_user])
        for rows in cases:
            self.ex.privateGetApiDataGFuturesOrdersByOrderId.return_value = envelope(rows)
            output = self.run_command('order-status order-1 symbol=BTCUSDT')
            self.assertIn('UNKNOWN', output)
            self.assertNotIn('ID=', output)
            self.assertNotIn('PRIVATE', output)

    def test_missing_account_identity_prevents_order_request(self):
        self.ex.privateGetGAccountsPositions.return_value['data']['account'].pop('userID')
        self.assertIn('UNKNOWN', self.run_command('orders BTCUSDT'))
        self.ex.privateGetGOrdersActiveList.assert_not_called()

    def test_conflicting_cached_symbol_binding_cannot_be_rehabilitated(self):
        self.run_command('orders BTCUSDT')
        self.ex.privateGetGOrdersActiveList.return_value = envelope({'rows': [self.row(symbol='ETHUSDT')]})
        self.run_command('orders ETHUSDT')
        self.ex.privateGetGOrdersActiveList.return_value = envelope({'rows': [self.row()]})
        self.run_command('orders BTCUSDT')
        with self.assertRaisesRegex(SafetyError, 'scope UNKNOWN'):
            self.run_command('order-status order-1')

    def test_position_account_identity_conflict_is_unknown(self):
        self.ex.privateGetGAccountsPositions.return_value['data']['positions'] = [
            dict(r, accountID='999') for r in self.hedge_rows()]
        self.assertIn('Long: quantity=UNKNOWN', self.run_command('positions BTCUSDT'))

    def test_missing_order_fields_are_unknown_not_fabricated(self):
        self.ex.privateGetGOrdersActiveList.return_value = envelope({'rows': [dict(
            orderID='order-1', symbol='BTCUSDT', accountID='222')]})
        output = self.run_command('orders BTCUSDT')
        for text in ('side=UNKNOWN', 'leg=UNKNOWN', 'quantity=UNKNOWN', 'price=UNKNOWN',
                     'status=UNKNOWN', 'reduce_only=UNKNOWN'):
            self.assertIn(text, output)

    def test_malformed_paginated_failed_and_empty_order_responses(self):
        for data in ({}, {'rows': None}, {'rows': [None]}, {'rows': [], 'nextPageArg': 'next'}):
            self.ex.privateGetGOrdersActiveList.return_value = envelope(data)
            self.assertIn('UNKNOWN', self.run_command('orders BTCUSDT'))
        self.ex.privateGetGOrdersActiveList.side_effect = ccxt.RequestTimeout('PRIVATE_SECRET')
        output = self.run_command('orders BTCUSDT')
        self.assertIn('UNKNOWN', output)
        self.assertNotIn('PRIVATE_SECRET', output)
        self.assertIn('Issue: open_orders:', output)
        self.assertEqual(self.ex.privateGetGOrdersActiveList.call_count, 5)

    def test_new_commands_reject_mainnet_and_unsupported_markets_before_io(self):
        for command in ('price BTC/USD:BTC', 'positions BTC/USD:BTC', 'orders BTC/USD:BTC'):
            with self.assertRaises(SafetyError): self.run_command(command)
        self.ex.urls['api']['private'] = 'https://api.phemex.com'
        for command in ('price BTCUSDT', 'positions BTCUSDT', 'orders BTCUSDT',
                        'order-status order-1 symbol=BTCUSDT'):
            with self.assertRaises(SafetyError): self.run_command(command)
        self.ex.fetch_order_book.assert_not_called()
        self.ex.privateGetGAccountsPositions.assert_not_called()

    def test_read_errors_are_sanitized_by_repl(self):
        self.ex.fetch_order_book.side_effect = ccxt.RequestTimeout('PRIVATE_SECRET')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['price BTCUSDT', 'exit']), contextlib.redirect_stdout(output):
            trade_cli.repl(self.client)
        self.assertNotIn('PRIVATE_SECRET', output.getvalue())
        self.assertIn('No automatic retry', output.getvalue())

    def test_exact_symbol_identity_does_not_coalesce_spot_and_perpetual(self):
        spot = dict(deepcopy(MARKET), id='sOLUSDT', symbol='OL/USDT', swap=False, linear=None)
        sol = dict(deepcopy(MARKET), id='SOLUSDT', symbol='SOL/USDT:USDT')
        self.ex.markets.update({spot['symbol']: spot, sol['symbol']: sol})
        self.assertEqual(self.client.resolve('SOLUSDT'), sol['symbol'])
        with self.assertRaises(SafetyError): self.client.resolve('sOLUSDT')


class TradingReadTransportTests(OfflineTestCase):
    def exchange(self):
        ex = reads.TradingReadPhemex({'apiKey': 'SYNTHETIC', 'secret': 'SYNTHETIC', 'enableRateLimit': False})
        ex.set_sandbox_mode(True)
        ex.set_markets([deepcopy(MARKET)])
        self.addCleanup(ex.close)
        return ex

    def test_new_route_is_get_only_exact_params_testnet_only(self):
        ex = self.exchange()
        with patch.object(ccxt.phemex, 'request', return_value=envelope([])) as send:
            ex.privateGetApiDataGFuturesOrdersByOrderId({'symbol': 'BTCUSDT', 'orderID': 'order-1'})
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[3], 'GET')
            self.assertEqual(send.call_args.args[4]['maxRetriesOnFailure'], 0)
            for method in ('POST', 'DELETE', 'PUT', 'PATCH'):
                with self.assertRaises(SafetyError):
                    ex.request(reads.LOOKUP, 'private', method, {'symbol': 'BTCUSDT', 'orderID': 'order-1'})
            for params in ({'orderID': 'order-1'}, {'symbol': 'BTCUSDT', 'orderID': 'order-1', 'extra': 1},
                           {'symbol': 'BTCUSD', 'orderID': 'order-1'}):
                with self.assertRaises(SafetyError): ex.request(reads.LOOKUP, 'private', 'GET', params)
            ex.urls['api']['private'] = 'https://api.phemex.com'
            with self.assertRaises(SafetyError):
                ex.request(reads.LOOKUP, 'private', 'GET', {'symbol': 'BTCUSDT', 'orderID': 'order-1'})
            self.assertEqual(send.call_count, 1)

    def test_new_extension_keeps_shared_mutation_barrier_and_strict_json(self):
        ex = self.exchange()
        with patch.object(ccxt.phemex, 'request', side_effect=AssertionError('No transport')) as send:
            for path, method in (('g-orders', 'POST'), ('g-orders/cancel', 'DELETE'),
                                 ('g-positions/leverage', 'PUT'), ('unreviewed', 'GET')):
                with self.assertRaises(SafetyError): ex.request(path, 'private', method, {})
            send.assert_not_called()
        value = ex.parse_json('{"value":0.123456789012345678901234567890}')
        self.assertEqual(value['value'], Decimal('0.123456789012345678901234567890'))
        with self.assertRaises(SafetyError): ex.parse_json('{"code":10500,"code":0}')

    def test_history_storage_is_disabled(self):
        ex = self.exchange()
        ex.fetchHistoryCacheSize = 5
        ex.set_last_request({'headers': 'PRIVATE_SECRET'})
        ex.add_fetch_cache({'response': 'PRIVATE_SECRET'})
        self.assertEqual(list(ex.get_fetch_cache()), [])
        self.assertIsNone(ex.last_request_headers)
        self.assertFalse(ex.session.trust_env)
        self.assertEqual(ex.session.proxies, {})
        self.assertTrue(ex.session.verify)

    def test_real_ccxt_constructor_and_lookup_path_with_mocked_http(self):
        data = responses()
        row = TradingReadTests.row()
        data['/g-accounts/positions']['data']['account'].update(accountId='222', userID='111')
        data['/' + reads.LOOKUP] = envelope([row])
        calls = []
        def response(method, url, **kwargs):
            parsed = urlsplit(url)
            self.assertEqual(parsed.scheme, 'https')
            self.assertEqual(parsed.netloc, 'testnet-api.phemex.com')
            self.assertEqual(method, 'GET')
            calls.append((parsed.path, parsed.query))
            if parsed.path.startswith('/public/') or parsed.path.startswith('/v1/'):
                self.assertNotIn('x-phemex-access-token', kwargs.get('headers', {}))
            result = requests.Response()
            result.status_code = 200
            result.url = url
            result._content = json.dumps(data[parsed.path]).encode()
            result.headers = {'Content-Type': 'application/json'}
            result.encoding = 'utf-8'
            return result
        with patch('requests.sessions.Session.request', side_effect=response) as http, \
             patch('ccxt.Exchange.throttle'), \
             patch('ccxt.phemex.load_markets', side_effect=AssertionError('Hidden metadata request')) as reload:
            client = PhemexClient('SYNTHETIC', 'SYNTHETIC')
            self.addCleanup(client.ex.close)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                reads.orders(client, ['order-status', 'order-1', 'symbol=BTCUSDT'])
            self.assertIn('ID=order-1', out.getvalue())
            self.assertEqual([p for p, q in calls], ['/public/products', '/v1/exchange/public/products',
                '/g-accounts/positions', '/' + reads.LOOKUP])
            self.assertEqual(calls[-1][1], 'symbol=BTCUSDT&orderID=order-1')
            self.assertEqual(http.call_count, 4)
            reload.assert_not_called()

    def test_only_http_200_can_establish_an_observation(self):
        ex = self.exchange()
        for status in (101, 199, 201, 204, 301, 302, 400, 500, True, '200'):
            with self.assertRaises((SafetyError, ccxt.ExchangeError)):
                ex.on_rest_response(status, '', '', 'GET', {}, 'PRIVATE_SECRET', {}, None)

    def test_catalog_loaded_once_without_implicit_currency_fetch(self):
        native = dict(PRODUCT, tickSize='1')
        market = dict(deepcopy(MARKET), info=native, precision={'amount': Decimal('0.001'), 'price': Decimal('1')})
        def fetch(ex, params=None):
            ex.public_inventory = {'products': [], 'perpProductsV2': [native], 'currencies': [
                {'currency': 'USDT', 'code': 3}]}
            ex.legacy_inventory = []
            return [market]
        with patch.object(reads.TradingReadPhemex, 'fetch_markets', autospec=True, side_effect=fetch) as markets, \
             patch.object(reads.TradingReadPhemex, 'load_markets', side_effect=AssertionError('Hidden metadata request')) as load:
            client = PhemexClient('SYNTHETIC', 'SYNTHETIC')
            self.addCleanup(client.ex.close)
            self.assertEqual(client.resolve('BTCUSDT'), SYMBOL)
            self.assertEqual(client.ex.currencies['USDT']['info']['code'], 3)
            client.instrument(SYMBOL)
            markets.assert_called_once()
            load.assert_not_called()

    def test_ambiguous_public_currency_codes_do_not_establish_risk_unit_scope(self):
        native = dict(PRODUCT, tickSize='1')
        market = dict(deepcopy(MARKET), info=native,
                      precision={'amount': Decimal('0.001'), 'price': Decimal('1')})
        for currencies in ([{'currency': 'USDT', 'code': 3}, {'currency': 'USDT', 'code': 4}],
                           [{'currency': 'USDT', 'code': 3}, {'currency': 'USD', 'code': 3}],
                           [{'currency': 'USDT', 'code': True}],
                           [{'currency': 'USDT', 'code': 3}, None]):
            def fetch(ex, params=None):
                ex.public_inventory = {'products': [], 'perpProductsV2': [native], 'currencies': currencies}
                ex.legacy_inventory = []
                return [market]
            with patch.object(reads.TradingReadPhemex, 'fetch_markets', autospec=True, side_effect=fetch):
                client = PhemexClient('SYNTHETIC', 'SYNTHETIC')
                try:
                    from account_discovery import AccountDiscovery
                    fact = AccountDiscovery(client.ex).risk_currency({'valuationCcy': 3})
                    self.assertIsNone(fact.value)
                finally:
                    client.ex.close()
