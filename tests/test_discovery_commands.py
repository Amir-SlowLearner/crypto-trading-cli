import contextlib
import io
from unittest.mock import Mock, patch

import ccxt

import testnet_readonly
import trade_cli
from config import Settings
from safety import SafetyError
from tests.account_fixtures import discovery_client, envelope
from tests.helpers import OfflineTestCase


class DiscoveryCommandTests(OfflineTestCase):
    def command(self, client, text):
        output = io.StringIO()
        parts = text.split()
        with contextlib.redirect_stdout(output):
            trade_cli.COMMANDS[parts[0]](client, parts)
        return output.getvalue()

    def test_account_balance_positions_and_modes_show_scoped_sources(self):
        client, ex = discovery_client()
        for text in ('account', 'balance', 'positions', 'modes', 'modes symbol=ETHUSDT'):
            with self.subTest(text=text):
                output = self.command(client, text)
                self.assertIn('accounting: classic [reported:', output)
                self.assertIn('exchange mutations remain disabled', output)
                self.assertIn('sequential, not atomic', output)
        output = self.command(client, 'modes symbol=ETHUSDT')
        self.assertIn('hedge', output)
        self.assertIn('Short', output)  # zero-size configuration is retained
        self.assertNotIn('BTC/USDT:USDT', output)
        output = self.command(client, 'balance')
        self.assertIn('available_margin: unknown', output)
        self.assertIn('equity: 1020 [derived:', output)
        self.assertIn('available_margin_estimate: 500 [reported:', output)
        for method in ('create_order', 'edit_order', 'cancel_order', 'cancel_all_orders', 'set_leverage', 'set_margin_mode', 'set_position_mode'):
            getattr(ex, method).assert_not_called()

    def test_unknown_position_sizes_are_displayed_not_hidden(self):
        client, ex = discovery_client()
        ex.privateGetGAccountsPositions.return_value = envelope({'account': {'currency': 'USDT'},
            'positions': [{'symbol': 'BTCUSDT', 'sizeRq': 'NaN'}]})
        result = self.command(client, 'positions')
        self.assertIn('contracts=unknown', result)
        self.assertNotIn('No nonzero positions', result)

    def test_unavailable_positions_do_not_claim_no_positions(self):
        client, ex = discovery_client()
        ex.privateGetGAccountsPositions.side_effect = ccxt.RequestTimeout('SECRET_SENTINEL')
        result = self.command(client, 'positions')
        self.assertIn('Positions/configuration unavailable', result)
        self.assertNotIn('No nonzero positions', result)
        self.assertNotIn('SECRET_SENTINEL', result)

    def test_unsupported_symbol_rejected_before_private_reads(self):
        client, ex = discovery_client()
        with self.assertRaises(SafetyError):
            self.command(client, 'modes symbol=BTC/USD:BTC')
        ex.privateGetUtaApiRiskRiskMode.assert_not_called()

    def test_modes_do_not_infer_defaults_when_no_rows_returned(self):
        client, ex = discovery_client()
        ex.privateGetGAccountsPositions.return_value = envelope({'account': {'currency': 'USDT'}, 'positions': []})
        result = self.command(client, 'modes symbol=BTCUSDT')
        self.assertIn('modes are unknown', result)
        self.assertNotIn('one_way [reported', result)

    def test_error_responses_are_sanitized_in_repl(self):
        client, ex = discovery_client()
        ex.privateGetUtaApiRiskRiskMode.side_effect = ccxt.AuthenticationError('API_KEY_SECRET_SENTINEL')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['account', 'exit']), contextlib.redirect_stdout(output):
            trade_cli.repl(client)
        self.assertNotIn('API_KEY_SECRET_SENTINEL', output.getvalue())
        self.assertIn('authentication failed', output.getvalue())


class OptionalIntegrationTests(OfflineTestCase):
    def test_no_opt_in_means_no_credentials_loaded_and_no_client_created(self):
        with patch('testnet_readonly.load_settings') as load, \
             patch('testnet_readonly.PhemexClient') as factory, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(testnet_readonly.main([]), 2)
            load.assert_not_called()
            factory.assert_not_called()

    def test_live_and_abbreviated_flags_are_rejected_before_requests(self):
        with patch('testnet_readonly.load_settings') as load, contextlib.redirect_stderr(io.StringIO()):
            for flag in ('--live', '--authorize-testnet'):
                with self.subTest(flag=flag), self.assertRaises(SystemExit):
                    testnet_readonly.main([flag])
            load.assert_not_called()

    def test_explicit_opt_in_constructs_only_sandbox_and_runs_get_inspection(self):
        client, _ = discovery_client()
        factory = Mock(return_value=client)
        with patch('testnet_readonly.load_settings', return_value=Settings('SYNTHETIC_KEY', 'SYNTHETIC_SECRET')) as load, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(testnet_readonly.main(['--authorize-testnet-read'], client_factory=factory), 0)
        self.assertFalse(load.call_args.kwargs['live'])
        self.assertFalse(factory.call_args.kwargs['live'])
        self.assertIn('no raw responses were saved', output.getvalue())
        self.assertNotIn('SYNTHETIC_SECRET', output.getvalue())

    def test_partial_discovery_is_non_success_without_retry(self):
        client, ex = discovery_client()
        ex.privateGetUtaApiRiskRiskMode.side_effect = ccxt.NotSupported('SECRET_SENTINEL')
        with patch('testnet_readonly.load_settings', return_value=Settings('KEY', 'SECRET')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(testnet_readonly.main(['--authorize-testnet-read'], client_factory=lambda *a, **k: client), 2)
        self.assertNotIn('SECRET_SENTINEL', output.getvalue())
        self.assertEqual(ex.privateGetUtaApiRiskRiskMode.call_count, 1)

    def test_live_endpoint_is_rejected_before_authenticated_inspection(self):
        client, ex = discovery_client()
        ex.urls['api']['private'] = 'https://api.phemex.com'
        with patch('testnet_readonly.load_settings', return_value=Settings('KEY', 'SECRET')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(testnet_readonly.main(['--authorize-testnet-read'], client_factory=lambda *a, **k: client), 2)
        ex.privateGetUtaApiRiskRiskMode.assert_not_called()
