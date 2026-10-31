from copy import deepcopy
import contextlib
from decimal import Decimal
import importlib
import io
import re
from unittest.mock import patch

import ccxt

import trade_cli as cli
from safety import SafetyError
from tests.helpers import MARKET, SYMBOL, OfflineTestCase, make_client, open_order, position


class CliRegressionTests(OfflineTestCase):
    def run_command(self, client, line):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            parts = line.split()
            cli.COMMANDS[parts[0]](client, parts)
        return output.getvalue()

    def test_quantity_only_amendment_cannot_bypass_cap(self):
        client, ex = make_client()
        ex.fetch_open_orders.return_value = [open_order()]
        with self.assertRaisesRegex(SafetyError, 'MAX_NOTIONAL'):
            self.run_command(client, 'amend symbol=BTCUSDT ord_id=order-1 qty=1000 dry')
        ex.edit_order.assert_not_called()

    def test_price_only_and_combined_amendment_caps_use_final_values(self):
        client, ex = make_client()
        ex.fetch_open_orders.return_value = [open_order(amount=2)]
        for suffix in ('price=100.6', 'price=100 qty=2.001'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(SafetyError, 'MAX_NOTIONAL'):
                self.run_command(client, 'amend symbol=BTCUSDT ord_id=order-1 ' + suffix + ' dry')

    def test_amendment_preview_uses_existing_effective_fields(self):
        client, ex = make_client()
        ex.fetch_open_orders.return_value = [open_order()]
        result = self.run_command(client, 'amend symbol=BTCUSDT ord_id=order-1 qty=2 dry')
        self.assertIn('price=100', result)
        self.assertIn('notional=200', result)
        ex.edit_order.assert_not_called()

    def test_amendment_below_filled_or_without_fill_state_is_rejected(self):
        client, ex = make_client()
        for filled in (1, None, 'NaN', '-1'):
            ex.fetch_open_orders.return_value = [open_order(filled=filled)]
            with self.subTest(filled=filled), self.assertRaises(SafetyError):
                self.run_command(client, 'amend symbol=BTCUSDT ord_id=order-1 qty=0.5 dry')

    def test_amendment_uses_base_units_with_nonunit_contract_size(self):
        market = {**deepcopy(MARKET), 'contractSize': 2}
        client, ex = make_client(markets=[market])
        ex.fetch_open_orders.return_value = [open_order()]
        result = self.run_command(client, 'amend symbol=BTCUSDT ord_id=order-1 qty=1 dry')
        self.assertIn('total contracts=0.5', result)
        self.assertIn('notional=100', result)

    def test_amendment_cannot_invert_existing_attached_protection(self):
        client, ex = make_client()
        ex.fetch_open_orders.return_value = [open_order(info={'posSide': 'Long', 'stopLossRp': '90', 'takeProfitRp': '110'})]
        for price in ('80', '120'):
            with self.subTest(price=price), self.assertRaises(SafetyError):
                self.run_command(client, f'amend symbol=BTCUSDT ord_id=order-1 price={price} dry')

    def test_stop_rounding_to_entry_is_rejected_for_both_directions(self):
        client, _ = make_client()
        for line in (
            'buy symbol=BTCUSDT qty=1 price=100.1 sl=99.9 dry',
            'buy symbol=BTCUSDT qty=1 price=100.1 tp=100.2 dry',
            'sell symbol=BTCUSDT qty=1 price=100.1 sl=100.2 dry',
            'sell symbol=BTCUSDT qty=1 price=100.1 tp=99.9 dry',
        ):
            with self.subTest(line=line), self.assertRaises(SafetyError): self.run_command(client, line)

    def test_preview_uses_final_normalized_prices_and_quantity(self):
        client, ex = make_client()
        result = self.run_command(client, 'buy symbol=BTCUSDT qty=1.0019 price=100.4 sl=98.6 tp=110.2 dry')
        self.assertIn('contracts: 1.001', result)
        self.assertIn('price: 100', result)
        self.assertIn('SL / TP: 99 / 110', result)
        self.assertNotIn('100.4', result)
        self.assertNotIn('1.0019', result)
        ex.create_order.assert_not_called()

    def test_requested_base_quantity_and_usdt_convert_via_contract_size(self):
        market = {**deepcopy(MARKET), 'contractSize': 2}
        client, _ = make_client(markets=[market])
        for size in ('qty=1', 'usdt=100'):
            result = self.run_command(client, f'buy symbol=BTCUSDT {size} price=100 dry')
            self.assertIn('contracts: 0.5', result)
            self.assertIn('notional: ~100', result)

    def test_reduction_or_unknown_mode_cannot_bypass_stop_validation(self):
        client, _ = make_client()
        for line in (
            'buy symbol=BTCUSDT qty=1 position=Short dry',
            'sell symbol=BTCUSDT qty=1 position=Long dry',
            'buy symbol=BTCUSDT qty=1 position=Merged sl=200 dry',
        ):
            with self.subTest(line=line), self.assertRaises(SafetyError): self.run_command(client, line)

    def test_proposed_ids_are_not_second_based_and_fit_exchange_limit(self):
        client, _ = make_client()
        values = []
        for _ in range(20):
            output = self.run_command(client, 'buy symbol=BTCUSDT qty=1 dry')
            value = re.search(r'proposed cl_id: ([0-9a-f]+)', output).group(1)
            self.assertLessEqual(len(value), 40)
            values.append(value)
        self.assertEqual(len(set(values)), len(values))

    def test_partial_close_arguments_are_rejected_before_account_read(self):
        client, ex = make_client()
        with self.assertRaises(SafetyError):
            self.run_command(client, 'close symbol=BTCUSDT qty=0.1 dry')
        ex.fetch_positions.assert_not_called()
        ex.create_order.assert_not_called()

    def test_close_preview_preserves_both_modes_and_directions(self):
        client, ex = make_client()
        for mode in ('OneWay', 'Hedged'):
            for direction in ('long', 'short'):
                p = position(mode=mode, direction=direction)
                ex.fetch_positions.return_value = [p]
                result = self.run_command(client, 'close symbol=BTCUSDT dry')
                self.assertIn('posSide=' + p['info']['posSide'], result)
                self.assertIn('reduceOnly=true', result)
                self.assertIn(('sell' if direction == 'long' else 'buy') + ' 2 contracts', result)
        ex.create_order.assert_not_called()

    def test_close_rejects_missing_inconsistent_or_ambiguous_state(self):
        client, ex = make_client()
        for rows in (
            [], [position(), position(direction='short')],
            [{**position(), 'info': {}}],
            [{**position(), 'side': 'unknown'}],
            [{**position(), 'info': {'posMode': 'OneWay', 'posSide': 'Long'}}],
        ):
            ex.fetch_positions.return_value = rows
            with self.subTest(rows=rows), self.assertRaises(SafetyError):
                self.run_command(client, 'close symbol=BTCUSDT dry')

    def test_leverage_dry_never_changes_either_leg_or_margin_mode(self):
        client, ex = make_client()
        output = self.run_command(client, 'leverage symbol=BTCUSDT x=2 dry')
        self.assertIn('nothing changed', output)
        ex.set_leverage.assert_not_called()
        ex.set_margin_mode.assert_not_called()
        ex.set_position_mode.assert_not_called()
        for value in ('0.5', '6'):
            with self.assertRaises(SafetyError):
                self.run_command(client, f'leverage symbol=BTCUSDT x={value} dry')

    def test_cancel_previews_cannot_claim_complete_cancellation(self):
        client, ex = make_client()
        ex.fetch_open_orders.return_value = [open_order()]
        result = self.run_command(client, 'cancel symbol=BTCUSDT ord_id=order-1 dry')
        self.assertIn('remains on the exchange', result)
        result = self.run_command(client, 'cancelall symbol=BTCUSDT dry')
        self.assertIn('active AND untriggered', result)
        ex.cancel_order.assert_not_called()
        ex.cancel_all_orders.assert_not_called()

    def test_non_dry_commands_block_before_exchange_reads_or_confirmation(self):
        client, ex = make_client()
        commands = (
            'buy symbol=BTCUSDT qty=1', 'sell symbol=BTCUSDT qty=1',
            'amend symbol=BTCUSDT ord_id=id qty=2', 'cancel symbol=BTCUSDT ord_id=id',
            'cancelall symbol=BTCUSDT', 'close symbol=BTCUSDT', 'leverage symbol=BTCUSDT x=2',
        )
        with patch('builtins.input', side_effect=AssertionError('No confirmation should bypass barrier')):
            for line in commands:
                with self.subTest(line=line), self.assertRaisesRegex(SafetyError, 'disabled'):
                    self.run_command(client, line)
        ex.fetch_positions.assert_not_called()
        ex.fetch_open_orders.assert_not_called()
        ex.fetch_order_book.assert_not_called()

    def test_api_error_is_not_displayed_as_no_orders_or_as_secret(self):
        client, ex = make_client()
        ex.fetch_open_orders.side_effect = ccxt.AuthenticationError('SECRET_SENTINEL signed-url api-key')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['orders symbol=BTCUSDT', 'exit']), \
             contextlib.redirect_stdout(output), self.assertLogs('trades') as logs:
            cli.repl(client)
        combined = output.getvalue() + '\n'.join(logs.output)
        self.assertNotIn('SECRET_SENTINEL', combined)
        self.assertNotIn('signed-url', combined)
        self.assertNotIn('No open orders', combined)
        self.assertNotIn('Connected', combined)
        self.assertIn('unverified', combined)
        self.assertIn('No automatic retry', combined)
        self.assertEqual(ex.fetch_open_orders.call_count, 1)

    def test_timeout_is_sanitized_and_never_automatically_retried(self):
        client, ex = make_client()
        ex.fetch_order_book.side_effect = ccxt.RequestTimeout('SECRET_SENTINEL')
        with patch('builtins.input', side_effect=['buy symbol=BTCUSDT qty=1 dry', 'exit']), \
             contextlib.redirect_stdout(io.StringIO()) as output, self.assertLogs('trades') as logs:
            cli.repl(client)
        self.assertEqual(ex.fetch_order_book.call_count, 1)
        self.assertNotIn('SECRET_SENTINEL', output.getvalue() + str(logs.output))
        ex.create_order.assert_not_called()

    def test_legacy_entrypoint_import_has_no_side_effects(self):
        with patch('builtins.input', side_effect=AssertionError('Import started terminal')), \
             patch('exchange_client.PhemexClient', side_effect=AssertionError('Import created client')):
            import terminal_v2
            importlib.reload(terminal_v2)

    def test_read_commands_keep_unverified_fields_explicit(self):
        from tests.account_fixtures import discovery_client
        client, ex = discovery_client()
        self.assertIn('available_margin: unknown', self.run_command(client, 'balance'))
        self.assertIn('contract_size', self.run_command(client, 'info symbol=BTCUSDT'))
        self.assertIn('one_way', self.run_command(client, 'positions symbol=BTCUSDT'))
        self.assertIn('hedge', self.run_command(client, 'modes symbol=ETHUSDT'))
        self.assertIn('accounting: classic', self.run_command(client, 'account'))
        ex.fetch_open_orders.return_value = [open_order()]
        self.assertIn('order-1', self.run_command(client, 'orders symbol=BTCUSDT'))
        self.assertIn('mutations are disabled', self.run_command(client, 'help'))
