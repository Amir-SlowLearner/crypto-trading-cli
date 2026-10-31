import contextlib
from dataclasses import replace
from decimal import Decimal
import io
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import config
import trade_cli
from tests.helpers import OfflineTestCase


class ConfigurationTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profile.env'
        self.write('PHEMEX_API_KEY=FILE_KEY\nPHEMEX_API_SECRET=FILE_SECRET\nMAX_NOTIONAL_USDT=123\nMAX_LEVERAGE=3\n')

    def write(self, content):
        self.path.write_text(content)
        self.path.chmod(0o600)

    def load(self, **kwargs):
        return config.load_settings(env_file=self.path, environ=kwargs.pop('environ', {}), **kwargs)

    def test_profile_limits_loaded_before_validation(self):
        settings = self.load()
        self.assertEqual(settings.limits.max_notional_usdt, Decimal('123'))
        self.assertEqual(settings.limits.max_leverage, Decimal('3'))
        self.assertFalse(settings.live)

    def test_environment_precedence_and_no_global_mutation(self):
        before = dict(os.environ)
        settings = self.load(environ={'PHEMEX_API_KEY': 'EXPORTED_KEY', 'PHEMEX_API_SECRET': 'EXPORTED_SECRET', 'MAX_NOTIONAL_USDT': '42'})
        self.assertEqual(settings.api_key, 'EXPORTED_KEY')
        self.assertEqual(settings.limits.max_notional_usdt, Decimal('42'))
        self.assertEqual(dict(os.environ), before)

    def test_no_credential_pair_mixing(self):
        for name in config.CREDENTIAL_KEYS:
            with self.subTest(name=name), self.assertRaises(config.ConfigError):
                self.load(environ={name: 'ONE_HALF'})

    def test_missing_empty_and_whitespace_credentials_rejected(self):
        for value in ('', ' ', None):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                self.load(environ={'PHEMEX_API_KEY': value, 'PHEMEX_API_SECRET': 'SECRET'})

    def test_invalid_limits_are_rejected_without_echoing_values(self):
        for name in ('MAX_NOTIONAL_USDT', 'MAX_LEVERAGE'):
            for value in ('0', '-1', 'NaN', 'sNaN', 'Infinity', '-Infinity', 'SECRET_SENTINEL', ''):
                with self.subTest(name=name, value=value), self.assertRaises(config.ConfigError) as caught:
                    self.load(environ={name: value})
                self.assertNotIn('SECRET_SENTINEL', str(caught.exception))

    def test_live_requires_explicit_configuration(self):
        with self.assertRaises(config.ConfigError):
            self.load(live=True)
        self.assertTrue(self.load(live=True, environ={'PHEMEX_ALLOW_LIVE': 'true'}).live)
        self.assertFalse(self.load(environ={'PHEMEX_ALLOW_LIVE': 'true'}).live)

    def test_invalid_live_opt_in(self):
        for value in ('yes', '1', '', 'SECRETS'):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                self.load(environ={'PHEMEX_ALLOW_LIVE': value})

    def test_credentials_are_not_in_settings_repr(self):
        result = repr(self.load())
        self.assertNotIn('FILE_KEY', result)
        self.assertNotIn('FILE_SECRET', result)

    def test_no_secret_interpolation(self):
        self.write('PHEMEX_API_KEY=KEY\nPHEMEX_API_SECRET=${UNRELATED_SECRET}\n')
        with patch.dict(os.environ, {'UNRELATED_SECRET': 'SHOULD_NOT_LOAD'}):
            self.assertEqual(self.load().api_secret, '${UNRELATED_SECRET}')

    def test_missing_explicit_profile_is_an_error(self):
        with self.assertRaises(config.ConfigError):
            config.load_settings(env_file=Path(self.temp.name) / 'missing', environ={})

    def test_environment_only_configuration(self):
        with patch.object(config, 'PROJECT_DIR', Path(self.temp.name)):
            settings = config.load_settings(environ={'PHEMEX_API_KEY': 'KEY', 'PHEMEX_API_SECRET': 'SECRET'})
        self.assertEqual(settings.api_key, 'KEY')

    def test_default_profiles_are_project_relative_and_separate(self):
        for name, live in [('test.env', False), ('live.env', True)]:
            target = Path(self.temp.name) / name
            target.write_text('PHEMEX_API_KEY=' + name + '\nPHEMEX_API_SECRET=SECRET\nPHEMEX_ALLOW_LIVE=true\n')
            target.chmod(0o600)
            with patch.object(config, 'PROJECT_DIR', Path(self.temp.name)):
                result = config.load_settings(live=live, environ={})
            self.assertEqual(result.api_key, name)
            self.assertEqual(result.live, live)

    def test_insecure_credential_file_rejected(self):
        if os.name != 'posix':
            self.skipTest('POSIX permissions only')
        for mode in (0o644, 0o664, 0o640, 0o606):
            self.path.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(config.ConfigError):
                self.load()

    def test_live_confirmation_before_client_creation(self):
        settings = replace(self.load(), live=True)
        with patch.object(trade_cli, 'load_settings', return_value=settings), \
             patch('builtins.input', return_value='no'), \
             patch('exchange_client.PhemexClient') as factory, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trade_cli.main(['--live']), 1)
            factory.assert_not_called()

    def test_confirmed_live_connectivity_is_still_mutation_gated(self):
        settings = replace(self.load(), live=True)
        with patch.object(trade_cli, 'load_settings', return_value=settings), \
             patch('builtins.input', return_value='LIVE'), \
             patch('exchange_client.PhemexClient') as factory, \
             patch.object(trade_cli, 'repl'):
            self.assertEqual(trade_cli.main(['--live']), 0)
            self.assertTrue(factory.call_args.kwargs['live'])

    def test_help_and_invalid_startup_options_do_not_load_credentials(self):
        with patch.object(trade_cli, 'load_settings') as load:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                trade_cli.main(['--help'])
            self.assertEqual(caught.exception.code, 0)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                trade_cli.main(['--lve'])
            self.assertEqual(caught.exception.code, 2)
            load.assert_not_called()
