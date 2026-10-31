import contextlib
from importlib.metadata import version
import io
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from phemex_client import PhemexClient
from safety import SafetyError
from tests.helpers import OfflineTestCase


class LegacySafetyTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.client = PhemexClient('SYNTHETIC_KEY', 'SYNTHETIC_SECRET', 'https://example.invalid')

    def test_native_mutations_block_even_with_invalid_quantities(self):
        for quantity in (-1, 0, 1, float('nan')):
            order = {'symbol': 'BTCUSDT', 'side': 'Buy', 'position': 'Long', 'qty': quantity, 'type': 'Market'}
            with self.subTest(quantity=quantity), self.assertRaisesRegex(SafetyError, 'disabled'):
                self.client.place_order(order)
        for method in ('POST', 'PUT', 'DELETE', 'post'):
            with self.subTest(method=method), self.assertRaises(SafetyError):
                self.client._request(method, '/g-orders')

    def test_native_balance_uses_usdt_endpoint_and_currency(self):
        with patch.object(self.client, '_request', return_value={}) as transport:
            self.client.get_balance('usdt')
        transport.assert_called_once_with('GET', '/g-accounts/accountPositions', params={'currency': 'USDT'})
        with self.assertRaises(SafetyError): self.client.get_balance('BTC')

    def test_native_read_timeout_redirect_and_error_handling(self):
        response = Mock(status_code=200)
        response.json.return_value = {'code': 0, 'data': {}}
        with patch('phemex_client.requests.get', return_value=response) as transport:
            self.assertEqual(self.client._request('GET', '/g-orders/activeList'), response.json.return_value)
            self.assertEqual(transport.call_args.kwargs['timeout'], 15)
            self.assertFalse(transport.call_args.kwargs['allow_redirects'])
            response.raise_for_status.assert_called_once()
            for payload in ({'code': 401, 'msg': 'SECRET_SENTINEL'}, {'error': 'SECRET_SENTINEL'}, []):
                response.json.return_value = payload
                with self.subTest(payload=payload), self.assertRaises(SafetyError) as caught:
                    self.client._request('GET', '/g-orders/activeList')
                self.assertNotIn('SECRET_SENTINEL', str(caught.exception))

    def test_native_read_network_and_json_errors_are_sanitized(self):
        for error in (requests.Timeout('SECRET_SENTINEL'), requests.HTTPError('SECRET_SENTINEL')):
            with patch('phemex_client.requests.get', side_effect=error) as transport:
                with self.assertRaises(SafetyError) as caught:
                    self.client._request('GET', '/g-orders/activeList')
                self.assertNotIn('SECRET_SENTINEL', str(caught.exception))
                self.assertEqual(transport.call_count, 1)
        response = Mock(status_code=200)
        response.json.side_effect = ValueError('SECRET_SENTINEL')
        with patch('phemex_client.requests.get', return_value=response), self.assertRaises(SafetyError):
            self.client._request('GET', '/g-orders/activeList')


class DependencyPinTests(OfflineTestCase):
    def test_locked_runtime_versions_match_verified_environment(self):
        path = Path(__file__).resolve().parents[1] / 'requirements.txt'
        for line in path.read_text().splitlines():
            if not line or line.startswith('#'): continue
            name, expected = line.split('==')
            with self.subTest(package=name): self.assertEqual(version(name), expected)
