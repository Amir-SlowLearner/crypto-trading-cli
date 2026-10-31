from copy import deepcopy
from decimal import Decimal
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import ccxt

from account_discovery import reported, unknown
from config import RiskLimits
from market_cache import load_market_cache
from order_preparation import prepare_order, render_preparation
from safety import SafetyError
from tests.helpers import MARKET, OfflineTestCase, make_client
import trade_cli


CACHE = Path(__file__).resolve().parents[1] / 'examples/order-markets.synthetic.json'


class PreparationTests(OfflineTestCase):
    def specs(self, market=None):
        client, _ = make_client(markets=[market or deepcopy(MARKET)])
        return client.instrument('BTCUSDT')

    def prepare(self, *, side='buy', action='open', leg='long', current='0', qty='1',
                price='100', kind='limit', extra='', specs=None):
        pricing = f'price={price}' if kind == 'limit' else f'reference_price={price}'
        reduction = 'reduce_only=true' if action in {'reduce', 'close'} else ''
        line = (f'{side} symbol=BTCUSDT action={action} position_side={leg} current_qty={current} '
                f'qty={qty} type={kind} {pricing} {reduction} {extra} dry')
        return prepare_order(specs if specs is not None else self.specs(), line.split(), RiskLimits())

    def test_valid_market_limit_action_leg_matrix(self):
        for action, current in (('open', '0'), ('increase', '1'), ('reduce', '2'), ('close', '1')):
            for leg in ('long', 'short'):
                for kind in ('market', 'limit'):
                    side = 'buy' if ((leg == 'long') != (action in {'reduce', 'close'})) else 'sell'
                    with self.subTest(action=action, leg=leg, kind=kind):
                        order = self.prepare(side=side, action=action, current=current, leg=leg, kind=kind)
                        self.assertEqual(order.side, side)
                        self.assertEqual(order.position_side, leg.capitalize())
                        self.assertEqual(order.base_qty, 1)
                        self.assertEqual(order.reduce_only, action in {'reduce', 'close'})
                        self.assertEqual(order.resulting_qty, Decimal(current) + (-1 if order.reduce_only else 1))

    def test_incorrect_direction_rejected_for_every_action_and_leg(self):
        for action, current in (('open', '0'), ('increase', '1'), ('reduce', '2'), ('close', '1')):
            for leg in ('long', 'short'):
                wrong = 'sell' if ((leg == 'long') != (action in {'reduce', 'close'})) else 'buy'
                with self.subTest(action=action, leg=leg), self.assertRaisesRegex(SafetyError, 'direction'):
                    self.prepare(side=wrong, action=action, leg=leg, current=current)

    def test_position_context_is_required_and_not_guessed(self):
        tokens = 'buy symbol=BTCUSDT action=open position_side=long current_qty=0 qty=1 price=100 dry'.split()
        for name in ('action', 'position_side', 'current_qty'):
            with self.subTest(name=name), self.assertRaisesRegex(SafetyError, 'Missing parameters'):
                prepare_order(self.specs(), [t for t in tokens if not t.startswith(name + '=')], RiskLimits())

    def test_zero_positive_and_invalid_context_rules(self):
        for action, current in (('open', '1'), ('increase', '0'), ('reduce', '0'), ('close', '0'),
                                ('open', '-1'), ('open', 'NaN'), ('open', 'Infinity')):
            side = 'sell' if action in {'reduce', 'close'} else 'buy'
            with self.subTest(action=action, current=current), self.assertRaises(SafetyError):
                self.prepare(side=side, action=action, current=current)

    def test_reduce_is_strictly_smaller_before_normalization(self):
        for qty in ('2', '2.0001', '3'):
            with self.subTest(qty=qty), self.assertRaisesRegex(SafetyError, 'strictly smaller'):
                self.prepare(side='sell', action='reduce', current='2', qty=qty)

    def test_close_requires_exact_quantity_not_rounded_equality(self):
        for qty in ('0.5', '1.0001', '2'):
            with self.subTest(qty=qty), self.assertRaisesRegex(SafetyError, 'equal current_qty'):
                self.prepare(side='sell', action='close', current='1', qty=qty)

    def test_reduce_only_cannot_be_missing_false_or_applied_to_open(self):
        for flag in ('', 'reduce_only=false', 'reduce_only=yes', 'reduce_only=1'):
            line = f'sell symbol=BTCUSDT action=reduce position_side=long current_qty=2 qty=1 price=100 {flag} dry'
            with self.subTest(flag=flag), self.assertRaises(SafetyError):
                prepare_order(self.specs(), line.split(), RiskLimits())
        with self.assertRaises(SafetyError):
            self.prepare(extra='reduce_only=true')

    def test_quantity_rounds_down_visibly_and_never_increases(self):
        order = self.prepare(qty='1.00199999999999999999')
        self.assertEqual(order.base_qty, Decimal('1.001'))
        self.assertLessEqual(order.base_qty, Decimal(order.requested_qty))
        output = render_preparation(order)
        self.assertIn('Requested quantity: 1.00199999999999999999', output)
        self.assertIn('Normalized quantity: 1.001', output)
        self.assertIn('normalized DOWN', output)

    def test_current_position_context_cannot_be_rounded(self):
        for action in ('increase', 'reduce', 'close'):
            with self.subTest(action=action), self.assertRaisesRegex(SafetyError, 'current_qty is off-grid'):
                self.prepare(action=action, current='1.0001',
                             side='buy' if action == 'increase' else 'sell')

    def test_quantity_minimum_maximum_and_zero_after_normalization(self):
        for qty in ('0.0001', '0.001', '1001'):
            with self.subTest(qty=qty), self.assertRaises(SafetyError):
                self.prepare(qty=qty)
        specs = self.specs()
        self.assertEqual(self.prepare(qty='0.01', specs=specs).notional, 1)

    def test_high_precision_native_maximum_boundaries_for_open_reduce_and_close(self):
        market = deepcopy(MARKET)
        maximum = '0.99999999999999999999999999999'
        market['precision']['amount'] = Decimal('1e-29')
        market['limits']['amount'] = {'min': Decimal('1e-29'), 'max': Decimal(maximum)}
        market['info'] = {'qtyStepSize': '1e-29', 'maxOrderQtyRq': maximum}
        specs = self.specs(market)
        for action in ('open', 'reduce', 'close'):
            for leg in ('long', 'short'):
                side = 'buy' if ((leg == 'long') != (action != 'open')) else 'sell'
                for qty, accepted in (('0.99999999999999999999999999998', True),
                                      (maximum, True), ('1', False),
                                      ('1.00000000000000000000000000001', False)):
                    current = '0' if action == 'open' else qty if action == 'close' else '2'
                    with self.subTest(action=action, leg=leg, qty=qty):
                        if accepted:
                            order = self.prepare(side=side, action=action, leg=leg, current=current,
                                                 qty=qty, specs=specs)
                            self.assertEqual(order.base_qty, Decimal(qty))
                            self.assertLessEqual(order.base_qty, Decimal(maximum))
                            if action == 'close':
                                self.assertEqual(order.resulting_qty, 0)
                        else:
                            with self.assertRaisesRegex(SafetyError, 'maximum'):
                                self.prepare(side=side, action=action, leg=leg, current=current,
                                             qty=qty, specs=specs)

    def test_final_base_quantity_is_independently_checked_against_native_maximum(self):
        specs = self.specs()
        # A conflicting derived/normalized contract limit must not override the
        # exact native base maximum, even if another conversion regresses.
        specs['max_base_quantity'] = reported(Decimal('0.99999999999999999999999999999'), 'synthetic native maximum')
        for action, current, side in (('open', '0', 'buy'), ('reduce', '2', 'sell'), ('close', '1', 'sell')):
            with self.subTest(action=action), self.assertRaisesRegex(SafetyError, 'native maximum'):
                self.prepare(side=side, action=action, current=current, qty='1', specs=specs)

    def test_contract_units_and_usdt_conversion_are_exact(self):
        market = {**deepcopy(MARKET), 'contractSize': 2}
        order = self.prepare(specs=self.specs(market))
        self.assertEqual(order.base_qty, 1)
        self.assertEqual(order.contracts, Decimal('0.5'))
        line = 'buy symbol=BTCUSDT action=open position_side=long current_qty=0 usdt=100 price=3 dry'
        order = prepare_order(self.specs(), line.split(), RiskLimits())
        self.assertEqual(order.base_qty, Decimal('33.333'))
        self.assertEqual(order.notional, Decimal('99.999'))
        self.assertLessEqual(order.notional, 100)

    def test_off_grid_entry_and_protection_prices_are_rejected(self):
        for extra, price in (('', '100.5'), ('sl=99.5 trigger=mark trigger_price=100', '100'),
                             ('tp=110.5 trigger=last trigger_price=100', '100')):
            with self.subTest(extra=extra), self.assertRaisesRegex(SafetyError, 'off-grid'):
                self.prepare(price=price, extra=extra)

    def test_price_and_notional_bounds_and_local_cap(self):
        market = deepcopy(MARKET)
        market['info'] = {'minPriceRp': '50', 'maxPriceRp': '150'}
        for price in ('49', '151'):
            with self.subTest(price=price), self.assertRaisesRegex(SafetyError, 'price bounds'):
                self.prepare(price=price, specs=self.specs(market))
        with self.assertRaisesRegex(SafetyError, 'MAX_NOTIONAL'):
            self.prepare(qty='2.001')
        with self.assertRaisesRegex(SafetyError, 'minimum'):
            self.prepare(qty='0.001')

    def test_missing_required_or_ambiguous_instrument_specs_reject(self):
        for key in ('contract_size', 'quantity_step', 'contract_step', 'min_contracts',
                    'max_contracts', 'min_notional', 'price_tick', 'price_precision_tick'):
            specs = self.specs()
            specs[key] = unknown('missing or ambiguous')
            with self.subTest(key=key), self.assertRaisesRegex(SafetyError, key):
                self.prepare(specs=specs)

    def test_invalid_explicit_native_specs_are_not_silently_skipped(self):
        for key in ('qtyStepSize', 'tickSize', 'maxOrderQtyRq', 'minOrderValueRv', 'minPriceRp', 'maxPriceRp'):
            market = deepcopy(MARKET)
            market['info'] = {key: 'NaN'}
            with self.subTest(key=key), self.assertRaises(SafetyError):
                self.prepare(specs=self.specs(market))

    def test_conflicting_native_and_normalized_price_grids_reject(self):
        market = deepcopy(MARKET)
        market['info'] = {'tickSize': '0.1'}
        with self.assertRaisesRegex(SafetyError, 'off-grid'):
            self.prepare(price='100.1', specs=self.specs(market))

    def test_inactive_wrong_type_and_mismatched_symbol_reject(self):
        with self.assertRaises(SafetyError):
            self.prepare(specs=self.specs({**deepcopy(MARKET), 'active': False}))
        with self.assertRaises(SafetyError):
            self.prepare(specs=self.specs({**deepcopy(MARKET), 'info': {'status': 'Paused'}}))
        for market in ({**deepcopy(MARKET), 'swap': False}, {**deepcopy(MARKET), 'linear': False},
                       {**deepcopy(MARKET), 'settle': 'BTC'}):
            with self.subTest(market=market), self.assertRaises(SafetyError):
                self.specs(market)
        specs = self.specs()
        line = 'buy symbol=ETHUSDT action=open position_side=long current_qty=0 qty=1 price=100 dry'
        with self.assertRaisesRegex(SafetyError, 'match'):
            prepare_order(specs, line.split(), RiskLimits())

    def test_valid_long_short_tp_sl_and_trigger_directions(self):
        for leg, side, sl, tp in (('long', 'buy', '90', '110'), ('short', 'sell', '110', '90')):
            for trigger in ('mark', 'last'):
                with self.subTest(leg=leg, trigger=trigger):
                    order = self.prepare(side=side, leg=leg, extra=f'sl={sl} tp={tp} trigger={trigger} trigger_price=100')
                    text = render_preparation(order)
                    self.assertIn('ByMarkPrice' if trigger == 'mark' else 'ByLastPrice', text)
                    self.assertIn('SL falling / TP rising' if leg == 'long' else 'SL rising / TP falling', text)
                    self.assertIn('lifecycle are UNVERIFIED', text)

    def test_bad_tp_sl_directions_or_already_crossed_trigger_reference_reject(self):
        for extra in ('sl=100', 'sl=110', 'tp=100', 'tp=90', 'sl=90 tp=110 trigger_price=80'):
            if 'trigger_price' not in extra:
                extra += ' trigger_price=100'
            with self.subTest(extra=extra), self.assertRaises(SafetyError):
                self.prepare(extra=extra + ' trigger=mark')

    def test_unsupported_protection_and_order_combinations_reject(self):
        for extra in ('sl=90', 'tp=110 trigger=last', 'trigger=mark trigger_price=100',
                      'sl=90 trigger=index trigger_price=100', 'tp_limit=120',
                      'type=stop', 'position_side=merged', 'reduce_only=TRUE reduce_only=false'):
            with self.subTest(extra=extra), self.assertRaises(SafetyError):
                self.prepare(extra=extra)
        for action, side, current in (('increase', 'buy', '1'), ('reduce', 'sell', '2'), ('close', 'sell', '1')):
            with self.subTest(action=action), self.assertRaisesRegex(SafetyError, 'open only'):
                self.prepare(action=action, side=side, current=current, extra='sl=90 trigger=mark trigger_price=100')

    def test_market_requires_supplied_reference_and_limit_rejects_extra_reference(self):
        line = 'buy symbol=BTCUSDT action=open position_side=long current_qty=0 qty=1 type=market dry'
        with self.assertRaisesRegex(SafetyError, 'reference_price'):
            prepare_order(self.specs(), line.split(), RiskLimits())
        with self.assertRaisesRegex(SafetyError, 'market-only'):
            self.prepare(extra='reference_price=100')

    def test_reduce_and_close_do_not_accept_estimated_usdt_quantity(self):
        for action in ('reduce', 'close'):
            line = f'sell symbol=BTCUSDT action={action} position_side=long current_qty=2 usdt=100 price=100 reduce_only=true dry'
            with self.subTest(action=action), self.assertRaisesRegex(SafetyError, 'exact base units'):
                prepare_order(self.specs(), line.split(), RiskLimits())

    def test_large_or_excessively_precise_inputs_fail_without_echoing_them(self):
        for qty in ('1e100000', '1e-100000', '1.' + '1' * 50):
            with self.subTest(qty=qty), self.assertRaises(SafetyError) as caught:
                self.prepare(qty=qty)
            self.assertNotIn(qty, str(caught.exception))

    def test_no_dry_flag_can_bypass_mutation_barrier(self):
        line = 'buy symbol=BTCUSDT action=open position_side=long current_qty=0 qty=1 price=100'
        with self.assertRaisesRegex(SafetyError, 'mutations are disabled'):
            prepare_order(self.specs(), line.split(), RiskLimits())

    def test_cli_preparation_never_calls_account_price_or_mutation_methods(self):
        client, ex = make_client()
        for method in ('fetch_order_book', 'fetch_positions', 'fetch_balance', 'fetch_open_orders',
                       'privateGetUtaApiRiskRiskMode', 'privateGetGAccountsPositions',
                       'privateGetGAccountsRiskUnit', 'create_order', 'edit_order', 'cancel_order'):
            getattr(ex, method).side_effect = AssertionError('Unexpected exchange access')
        line = 'sell symbol=BTCUSDT action=close position_side=long current_qty=1 qty=1 reference_price=100 reduce_only=true dry'
        output = io.StringIO()
        with redirect_stdout(output):
            trade_cli.cmd_sell(client, line.split())
        self.assertIn('USER-SUPPLIED, UNVERIFIED', output.getvalue())
        self.assertIn('Executable: NO', output.getvalue())
        self.assertIn('execution compatibility UNVERIFIED', output.getvalue())
        for method in ('fetch_order_book', 'fetch_positions', 'fetch_balance', 'fetch_open_orders',
                       'privateGetUtaApiRiskRiskMode', 'privateGetGAccountsPositions',
                       'privateGetGAccountsRiskUnit', 'create_order', 'edit_order', 'cancel_order'):
            getattr(ex, method).assert_not_called()

    def test_pinned_ccxt_preserves_explicit_hedge_leg_and_reduce_only_with_mocked_transport(self):
        # Isolated adapter-mapping test only. The production read-only subclass
        # remains untouched, and socket/HTTP guards block every actual request.
        ex = ccxt.phemex({'enableRateLimit': False})
        ex.set_markets([deepcopy(MARKET)])
        ex.request = Mock(return_value={'code': 0, 'data': {}})
        for leg, side in (('Long', 'sell'), ('Short', 'buy')):
            with self.subTest(leg=leg):
                ex.create_order(MARKET['symbol'], 'market', side, 1,
                                params={'posSide': leg, 'reduceOnly': True, 'clOrdID': 'SYNTHETIC'})
                payload = ex.request.call_args.args[3]
                self.assertEqual(payload['posSide'], leg)
                self.assertIs(payload['reduceOnly'], True)
                self.assertEqual(payload['side'].lower(), side)


class OfflinePreviewTests(OfflineTestCase):
    def test_cli_cache_mode_never_loads_credentials_or_constructs_exchange_client(self):
        command = 'buy symbol=BTCUSDT action=open position_side=long current_qty=0 qty=0.002 price=60000 dry'
        with patch('trade_cli.load_settings', side_effect=AssertionError('Credential access')) as settings, \
             patch('exchange_client.PhemexClient', side_effect=AssertionError('Transport construction')) as factory, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(trade_cli.main(['--preview', command, '--market-cache', str(CACHE)]), 0)
        settings.assert_not_called()
        factory.assert_not_called()
        self.assertIn('SYNTHETIC EXAMPLE', output.getvalue())
        self.assertIn('NO EXCHANGE ORDER SUBMITTED', output.getvalue())

    def test_live_env_private_commands_and_missing_cache_are_rejected_before_config(self):
        with patch('trade_cli.load_settings', side_effect=AssertionError('Credential access')) as settings, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for args in (['--preview', 'account', '--market-cache', str(CACHE)],
                         ['--preview', 'buy symbol=BTCUSDT qty=1 dry', '--market-cache', str(CACHE)]):
                self.assertEqual(trade_cli.main(args), 2)
            for args in (['--preview', 'buy'], ['--market-cache', str(CACHE)],
                         ['--preview', 'buy', '--market-cache', str(CACHE), '--live'],
                         ['--preview', 'buy', '--market-cache', str(CACHE), '--env-file', 'DO_NOT_READ.env']):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    trade_cli.main(args)
            settings.assert_not_called()

    def test_bad_or_ambiguous_cache_is_rejected_without_echoing_private_contents(self):
        data = json.loads(CACHE.read_text())
        duplicate = deepcopy(data)
        duplicate['markets'] *= 2
        for content in ('PRIVATE_SENTINEL', json.dumps({'apiKey': 'PRIVATE_SENTINEL'}), json.dumps(duplicate)):
            with TemporaryDirectory() as directory:
                path = Path(directory) / 'cache.json'
                path.write_text(content)
                with self.assertRaises(SafetyError) as caught:
                    load_market_cache(path)
                self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))

    def test_no_default_tick_mode_or_source_for_unversioned_cache(self):
        for field in ('version', 'precision_mode', 'provenance'):
            data = json.loads(CACHE.read_text())
            del data[field]
            with TemporaryDirectory() as directory:
                path = Path(directory) / 'cache.json'
                path.write_text(json.dumps(data))
                with self.assertRaises(SafetyError):
                    load_market_cache(path)

    def test_cache_decimal_values_are_not_rounded_through_float(self):
        content = CACHE.read_text().replace('"contractSize": 1,', '"contractSize": 1.0000000000000001,')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.json'
            path.write_text(content)
            client = load_market_cache(path)
            self.assertEqual(client.instrument('BTCUSDT')['contract_size'].value,
                             Decimal('1.0000000000000001'))
