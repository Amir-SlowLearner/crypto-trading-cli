from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import hmac
import io
import json
import logging
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import ccxt
import requests

import testnet_readonly
from account_discovery import AccountDiscovery
from config import Settings
from diagnostics import ResponseEvidence, diagnose, exchange_code, response_reason
from exchange_client import PhemexClient
from readonly_exchange import ReadOnlyPhemex
from safety import SafetyError
from tests.account_fixtures import CLASSIC_ACCOUNT, UNIFIED_RISK, envelope
from tests.helpers import MARKET, OfflineTestCase
from trade_cli import print_report_header


SECRET = 'SYNTHETIC_PRIVATE_SENTINEL'
SESSION_REQUEST = requests.Session.request


def http_response(status, payload):
    response = requests.Response()
    response.status_code = status
    response.reason = SECRET
    response.url = 'https://testnet-api.phemex.com/'
    response.headers.update({'Content-Type': 'application/json', 'private-header': SECRET})
    response._content = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
    return response


class DiagnosticTests(OfflineTestCase):
    def exchange(self):
        ex = ReadOnlyPhemex({'apiKey': SECRET, 'secret': SECRET, 'enableRateLimit': False,
                             'verbose': True})
        ex.set_sandbox_mode(True)
        ex.set_markets([deepcopy(MARKET)])
        return ex

    def read_failure(self, status, payload):
        ex = self.exchange()
        ex.session.request = Mock(return_value=http_response(status, payload))
        discovery = AccountDiscovery(ex)
        result = discovery.read('risk_mode', ex.privateGetUtaApiRiskRiskMode, {}, dict)
        self.assertIsNone(result)
        self.assertEqual(ex.session.request.call_count, 1)
        self.assertNotIn(SECRET, repr(discovery.report))
        self.assertNotIn(SECRET, repr(ex.last_read_diagnostic))
        self.assertIsNone(ex.last_http_response)
        self.assertIsNone(ex.last_json_response)
        self.assertIsNone(ex.last_response_headers)
        return discovery

    def test_unmapped_exchange_error_preserves_numeric_code_without_raw_message(self):
        # Synthetic unmapped code, not a claim about the actual testnet failure.
        discovery = self.read_failure(200, {'code': 98765, 'msg': SECRET, 'data': {'userId': SECRET}})
        self.assertIn('category=exchange_error; http=200; code=98765; type=ExchangeError',
                      discovery.report.issues[0])
        self.assertFalse(discovery.stopped)

    def test_http_failures_remain_distinct_even_when_ccxt_raises_generic_exchange_error(self):
        for status, category, stopped in ((400, 'bad_request', False), (401, 'authentication', True),
                (403, 'permission', False), (404, 'http_not_found', False),
                (405, 'method_not_allowed', False), (429, 'rate_limit', True),
                (500, 'server_error', True), (503, 'server_error', True)):
            with self.subTest(status=status):
                discovery = self.read_failure(status, {'code': 98765, 'msg': SECRET})
                self.assertIn(f'category={category}; http={status}; code=98765', discovery.report.issues[0])
                self.assertEqual(discovery.stopped, stopped)
                self.assertEqual(discovery.authentication_failed, status == 401)

    def test_non_json_http_errors_keep_status_without_claiming_unsupported(self):
        discovery = self.read_failure(404, '<html>' + SECRET + '</html>')
        self.assertIn('http=404; code=unknown', discovery.report.issues[0])
        self.assertNotIn('endpoint unsupported', discovery.report.issues[0])
        self.assertFalse(discovery.stopped)

    def test_nested_error_code_is_preserved(self):
        discovery = self.read_failure(200, {'error': {'code': 6001, 'message': SECRET}})
        self.assertIn('category=bad_request; http=200; code=6001', discovery.report.issues[0])

    def test_api_authentication_code_with_http_200_stops(self):
        discovery = self.read_failure(200, {'code': '401', 'msg': SECRET})
        self.assertTrue(discovery.authentication_failed)
        self.assertTrue(discovery.stopped)
        self.assertIn('category=authentication; http=200; code=401', discovery.report.issues[0])

    def test_malformed_success_response_is_distinct_from_exchange_rejection(self):
        discovery = self.read_failure(200, {'code': 0, 'data': [], 'msg': SECRET})
        self.assertIn('category=invalid_response; http=200; code=0', discovery.report.issues[0])

    def test_exception_categories_never_read_exception_text_or_untrusted_class_name(self):
        class SecretException(Exception):
            def __str__(self):
                raise AssertionError('Exception text must not be inspected')
        for error, category in ((ccxt.PermissionDenied(SECRET), 'permission'),
                (ccxt.NotSupported(SECRET), 'unsupported'), (ccxt.RequestTimeout(SECRET), 'timeout'),
                (ccxt.NetworkError(SECRET), 'network'), (SecretException(), 'local_error')):
            with self.subTest(category=category):
                failure = diagnose(error)
                self.assertEqual(failure.category, category)
                self.assertNotIn(SECRET, str(failure))
                self.assertNotIn('SecretException', str(failure))

    def test_only_bounded_numeric_error_codes_are_reportable(self):
        for code in (True, False, 1.5, SECRET, '401\n' + SECRET, '1' * 11, {}, []):
            with self.subTest(code=code):
                self.assertIsNone(exchange_code({'code': code}))
        self.assertEqual(exchange_code({'code': '-123'}), '-123')
        self.assertIsNone(exchange_code({'userId': 12345, 'data': {'code': 401}}))

    def test_timeout_after_previous_response_does_not_reuse_http_or_code(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[http_response(403, {'code': 98765}), requests.Timeout(SECRET)])
        discovery = AccountDiscovery(ex)
        discovery.read('risk_mode', ex.privateGetUtaApiRiskRiskMode, {}, dict)
        discovery.read('risk_units', ex.privateGetUtaApiRiskRiskUnits, {'currency': 'USDT'}, list)
        self.assertIn('http=403; code=98765', discovery.report.issues[0])
        self.assertIn('category=timeout; http=unknown; code=unknown', discovery.report.issues[1])
        self.assertEqual(ex.session.request.call_count, 2)
        self.assertTrue(discovery.stopped)

    def test_signing_failure_cannot_reuse_previous_response(self):
        ex = self.exchange()
        ex.last_read_diagnostic = ResponseEvidence(403, '98765')
        ex.apiKey = ''
        ex.session.request = Mock()
        discovery = AccountDiscovery(ex)
        discovery.read('risk_mode', ex.privateGetUtaApiRiskRiskMode, {}, dict)
        self.assertIn('category=authentication; http=unknown; code=unknown', discovery.report.issues[0])
        ex.session.request.assert_not_called()

    def test_transport_connection_tls_and_redirect_errors_are_not_api_rejections(self):
        for error, category in ((requests.ConnectionError(SECRET), 'network'),
                (requests.exceptions.SSLError(SECRET), 'tls'),
                (requests.exceptions.TooManyRedirects(SECRET), 'redirect')):
            with self.subTest(category=category):
                ex = self.exchange()
                ex.session.request = Mock(side_effect=error)
                discovery = AccountDiscovery(ex)
                discovery.read('risk_mode', ex.privateGetUtaApiRiskRiskMode, {}, dict)
                self.assertIn(f'category={category}; http=unknown; code=unknown', discovery.report.issues[0])
                self.assertTrue(discovery.stopped)
                self.assertNotIn(SECRET, repr(discovery.report))
                ex.session.request.assert_called_once()

    def test_local_error_reports_safe_class_without_fabricating_http_failure(self):
        failure = diagnose(TypeError(SECRET))
        self.assertEqual(failure.category, 'local_error')
        self.assertEqual(failure.error_type, 'TypeError')
        self.assertIsNone(failure.http_status)
        self.assertNotIn(SECRET, str(failure))

    def test_known_10500_authentication_errors_survive_http_500(self):
        for message in ('Failed to find api-key ' + SECRET, 'Api key not found ' + SECRET,
                        'API Signature verification failed. ' + SECRET):
            with self.subTest(message=message):
                discovery = self.read_failure(500, {'code': 10500, 'msg': message})
                self.assertTrue(discovery.authentication_failed)
                self.assertTrue(discovery.stopped)
                self.assertIn('category=authentication; http=500; code=10500', discovery.report.issues[0])
                self.assertIn('type=AuthenticationError', discovery.report.issues[0])

    def test_http_500_does_not_override_security_or_network_exception_classes(self):
        for cls, category in ((ccxt.AuthenticationError, 'authentication'),
                (ccxt.PermissionDenied, 'permission'), (ccxt.RateLimitExceeded, 'rate_limit'),
                (ccxt.RequestTimeout, 'timeout'), (ccxt.NetworkError, 'network')):
            with self.subTest(category=category):
                failure = diagnose(cls(SECRET), ResponseEvidence(500, '10500'))
                self.assertEqual(failure.category, category)
                self.assertTrue(failure.stop)
                self.assertFalse(failure.generic_exchange_error)
                self.assertNotIn(SECRET, str(failure))

    def test_http_authentication_and_rate_limits_remain_stops_with_conflicting_api_evidence(self):
        for status, category in ((401, 'authentication'), (403, 'permission'), (429, 'rate_limit')):
            with self.subTest(status=status):
                discovery = self.read_failure(status, {'code': 10500, 'msg': 'Missing required parameter'})
                self.assertIn('category=' + category, discovery.report.issues[0])
                self.assertEqual(discovery.stopped, status != 403)

    def test_response_reasons_are_fixed_labels_from_error_envelopes_only(self):
        cases = (
            ({'code': 10500, 'msg': 'Failed to find api-key ' + SECRET}, 'api_key_unrecognized'),
            ({'code': 10500, 'msg': 'API Signature verification failed. ' + SECRET}, 'signature_rejected'),
            ({'code': 10500, 'msg': '401 Request IP mismatch ' + SECRET}, 'ip_restricted'),
            ({'code': 10500, 'msg': '401 Insufficient privilege ' + SECRET}, 'permission_rejected'),
            ({'code': 10500, 'msg': 'Too many requests. ' + SECRET}, 'rate_limited'),
            ({'code': 10500, 'msg': 'Missing required parameter ' + SECRET}, 'missing_parameter'),
            ({'code': 10500, 'msg': 'Please try again later. ' + SECRET}, 'retry_later'),
            ({'error': {'code': 10500, 'message': 'Api key not found ' + SECRET}}, 'api_key_unrecognized'),
            ({'code': 0, 'data': {'msg': 'API Signature verification failed'}}, 'unclassified'),
            ({'code': 10500, 'msg': SECRET}, 'unclassified'),
            ({'code': 10500, 'msg': [], 'message': {}}, 'unclassified'),
            ({'code': 10500, 'msg': 'API Signature verification failed',
              'message': 'Please try again later'}, 'conflicting_error_evidence'),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(response_reason(payload), expected)
                self.assertNotIn(SECRET, response_reason(payload))

    def test_read_helper_and_shared_policy_still_stop_on_generic_http_500(self):
        discovery = self.read_failure(500, {'code': 10500, 'msg': 'Please try again later'})
        self.assertTrue(discovery.stopped)
        failure = diagnose(ccxt.ExchangeError(SECRET), ResponseEvidence(500, '10500', 'retry_later'))
        self.assertTrue(failure.stop)

    def test_risk_mode_http_500_continues_only_fixed_gets_without_accounting_fallback(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[
            http_response(500, {'code': 10500, 'msg': 'Please try again later. ' + SECRET}),
            http_response(200, envelope(CLASSIC_ACCOUNT)),
            http_response(500, {'code': 10500, 'msg': SECRET}),
        ])
        report = AccountDiscovery(ex).discover()
        self.assertTrue(report.authenticated.value)
        self.assertTrue(report.positions_available)
        self.assertIsNone(report.account_type.value)
        for key in ('balance', 'equity', 'available_margin'):
            self.assertIsNone(report.wallet[key].value)
        self.assertFalse(report.complete)
        self.assertTrue(any('continuing independent discovery GETs' in issue for issue in report.issues))
        self.assertTrue(any('risk_mode:' in issue and 'http=500; code=10500' in issue for issue in report.issues))
        self.assertTrue(any('risk_units:' in issue and 'http=500; code=10500' in issue for issue in report.issues))
        self.assertEqual([urlsplit(call.args[1]).path for call in ex.session.request.call_args_list],
                         ['/uta-api/risk/risk-mode', '/g-accounts/positions', '/uta-api/risk/risk-units'])
        self.assertTrue(all(call.args[0] == 'GET' for call in ex.session.request.call_args_list))
        self.assertNotIn(SECRET, repr(report))
        # Partial success must not change transport or application guards.
        client = PhemexClient(SECRET, SECRET, exchange=ex)
        for call in (lambda: client.place_order('BTCUSDT', 'buy', 'Long', 'market', 1),
                     lambda: client.cancel_order({}), lambda: client.set_leverage('BTCUSDT', 2)):
            with self.assertRaises(SafetyError):
                call()
        for path, method in (('g-orders', 'POST'), ('g-orders/cancel', 'DELETE'),
                             ('g-positions/leverage', 'PUT'), ('uta-account/switch-mode', 'POST')):
            with self.subTest(path=path), self.assertRaises(SafetyError):
                ex.request(path, 'private', method, {})
        self.assertEqual(ex.session.request.call_count, 3)

    def test_optional_testnet_check_exits_two_for_http_500_partial_discovery(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[http_response(500, {'code': 10500, 'msg': SECRET}),
            http_response(200, envelope(CLASSIC_ACCOUNT)), http_response(200, envelope(UNIFIED_RISK))])
        client = PhemexClient(SECRET, SECRET, exchange=ex)
        output = io.StringIO()
        with patch('testnet_readonly.load_settings', return_value=Settings(SECRET, SECRET)), redirect_stdout(output):
            result = testnet_readonly.main(['--authorize-testnet-read'], client_factory=lambda *a, **k: client)
        self.assertEqual(result, 2)
        self.assertIn('accounting: unknown', output.getvalue())
        self.assertIn('Minimum discovery checks passed: False', output.getvalue())
        self.assertIn('http=500; code=10500', output.getvalue())
        self.assertNotIn(SECRET, output.getvalue())
        self.assertEqual(ex.session.request.call_count, 3)

    def test_reused_discovery_does_not_carry_partial_success_past_new_authentication_failure(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[http_response(500, {'code': 10500, 'msg': SECRET}),
            http_response(200, envelope(CLASSIC_ACCOUNT)), http_response(200, envelope(UNIFIED_RISK)),
            http_response(500, {'code': 10500, 'msg': 'API Signature verification failed. ' + SECRET})])
        reader = AccountDiscovery(ex)
        self.assertTrue(reader.discover().authenticated.value)
        report = reader.discover()
        self.assertIs(report.authenticated.value, False)
        self.assertFalse(report.positions_available)
        self.assertFalse(report.complete)
        self.assertNotIn(SECRET, repr(report))
        self.assertEqual(ex.session.request.call_count, 4)

    def test_unknown_10500_reason_can_continue_but_never_establishes_account_type(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[http_response(500, {'code': 10500, 'msg': SECRET}),
            http_response(200, envelope(CLASSIC_ACCOUNT)), http_response(200, envelope(UNIFIED_RISK))])
        report = AccountDiscovery(ex).discover()
        self.assertTrue(report.authenticated.value)
        self.assertTrue(report.risk_units)
        self.assertIsNone(report.account_type.value)
        self.assertFalse(report.complete)
        self.assertEqual(ex.session.request.call_count, 3)

    def test_recognized_or_other_server_failures_do_not_get_the_continuation_exception(self):
        cases = (
            (500, {'code': 10500, 'msg': 'API Signature verification failed. ' + SECRET}, 'authentication'),
            (500, {'code': 10500, 'msg': 'Failed to find api-key ' + SECRET}, 'authentication'),
            (500, {'code': 10500, 'msg': '401 Request IP mismatch ' + SECRET}, 'permission'),
            (500, {'code': 34003, 'msg': SECRET}, 'permission'),
            (500, {'code': 39995, 'msg': SECRET}, 'rate_limit'),
            (500, {'code': 10500, 'msg': 'Missing required parameter ' + SECRET}, 'bad_request'),
            (500, {'code': 10500, 'msg': 'Please try again later',
                   'message': 'API Signature verification failed'}, 'server_error'),
            (503, {'code': 10500, 'msg': 'Please try again later'}, 'server_error'),
            (500, '<html>' + SECRET + '</html>', 'server_error'),
        )
        for status, payload, category in cases:
            with self.subTest(status=status, category=category):
                ex = self.exchange()
                ex.session.request = Mock(return_value=http_response(status, payload))
                report = AccountDiscovery(ex).discover()
                self.assertTrue(any('category=' + category in issue for issue in report.issues))
                self.assertFalse(report.positions_available)
                self.assertFalse(report.complete)
                self.assertNotIn(SECRET, repr(report))
                ex.session.request.assert_called_once()

    def test_security_and_network_failures_after_continuation_still_stop(self):
        for reply in (requests.Timeout(SECRET), requests.ConnectionError(SECRET),
                http_response(401, {'code': 401, 'msg': SECRET}),
                http_response(429, {'code': 98765, 'msg': SECRET}),
                http_response(500, {'code': 10500, 'msg': SECRET})):
            with self.subTest(reply_type=type(reply).__name__):
                ex = self.exchange()
                ex.session.request = Mock(side_effect=[http_response(500, {'code': 10500, 'msg': SECRET}), reply])
                report = AccountDiscovery(ex).discover()
                self.assertFalse(report.positions_available)
                self.assertFalse(report.complete)
                self.assertIsNone(report.account_type.value)
                self.assertNotIn(SECRET, repr(report))
                self.assertEqual(ex.session.request.call_count, 2)

    def test_risk_unit_server_error_still_stops_subsequent_unified_asset_read(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[http_response(200, envelope({'riskMode': 'MultiAsset'})),
            http_response(200, envelope(CLASSIC_ACCOUNT)),
            http_response(500, {'code': 10500, 'msg': 'Please try again later'})])
        report = AccountDiscovery(ex).discover()
        self.assertTrue(report.authenticated.value)
        self.assertFalse(report.complete)
        self.assertIsNone(report.wallet['balance'].value)
        self.assertEqual(ex.session.request.call_count, 3)

    def test_explicit_retry_options_cannot_add_attempts_or_alter_signed_query(self):
        ex = self.exchange()
        ex.options['maxRetriesOnFailure'] = 3
        ex.options['uta-api/risk/risk-mode'] = {'maxRetriesOnFailure': 4}
        for reply in (requests.Timeout(SECRET), http_response(500, {'code': 10500, 'msg': SECRET})):
            with self.subTest(reply_type=type(reply).__name__):
                ex.session.request = Mock(side_effect=reply if isinstance(reply, Exception) else None,
                                          return_value=reply)
                params = {'maxRetriesOnFailure': 5}
                with self.assertRaises(ccxt.BaseError):
                    ex.privateGetUtaApiRiskRiskMode(params)
                self.assertEqual(params, {'maxRetriesOnFailure': 5})
                ex.session.request.assert_called_once()
                self.assertEqual(ex.session.request.call_args.args[1],
                                 'https://testnet-api.phemex.com/uta-api/risk/risk-mode')

    def test_redirect_cannot_add_an_unreviewed_http_request(self):
        ex = self.exchange()
        ex.session.trust_env = False
        response = http_response(302, {})
        response.headers['Location'] = 'https://unapproved.invalid/g-orders/create'
        adapter = ex.session.get_adapter('https://')
        adapter.send = Mock(return_value=response)
        # Exercise requests' redirect handling with a mocked adapter; the socket
        # guard remains in place, and no actual HTTP send is possible.
        ex.session.request = SESSION_REQUEST.__get__(ex.session, requests.Session)
        report = AccountDiscovery(ex).discover()
        adapter.send.assert_called_once()
        self.assertTrue(any('category=redirect' in issue for issue in report.issues))
        self.assertFalse(report.positions_available)
        self.assertFalse(report.complete)

    def test_error_hints_cannot_expose_credentials_in_debug_logs_or_report_output(self):
        ex = self.exchange()
        ex.session.request = Mock(return_value=http_response(500,
            {'code': 10500, 'msg': 'Failed to find api-key ' + SECRET, 'data': {'secret': SECRET}}))
        logs, output = io.StringIO(), io.StringIO()
        root = logging.getLogger()
        handler = logging.StreamHandler(logs)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        with patch.object(root, 'level', logging.DEBUG), redirect_stdout(output):
            report = AccountDiscovery(ex).discover()
            print_report_header(report)
        rendered = logs.getvalue() + output.getvalue() + repr(ex.last_read_diagnostic) + repr(report)
        self.assertIn('reason=api_key_unrecognized', rendered)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn('x-phemex-request-signature', rendered)

    def test_failed_risk_endpoints_keep_account_unknown_and_positions_readable(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[
            http_response(200, {'code': 98765, 'msg': SECRET}),
            http_response(200, envelope(CLASSIC_ACCOUNT)),
            http_response(200, {'code': 98766, 'msg': SECRET}),
        ])
        report = AccountDiscovery(ex).discover()
        self.assertTrue(report.authenticated.value)
        self.assertTrue(report.positions_available)
        self.assertIsNone(report.account_type.value)
        self.assertIsNone(report.wallet['balance'].value)
        self.assertIsNone(report.wallet['equity'].value)
        self.assertIsNone(report.wallet['available_margin'].value)
        self.assertFalse(report.complete)
        self.assertTrue(any('risk_mode:' in issue and 'code=98765' in issue for issue in report.issues))
        self.assertTrue(any('risk_units:' in issue and 'code=98766' in issue for issue in report.issues))
        self.assertEqual(ex.session.request.call_count, 3)
        output = io.StringIO()
        with redirect_stdout(output):
            print_report_header(report)
        self.assertIn('code=98765', output.getvalue())
        self.assertIn('code=98766', output.getvalue())
        self.assertNotIn(SECRET, output.getvalue())

    def test_http_404_does_not_prevent_independent_authenticated_position_reads(self):
        ex = self.exchange()
        ex.session.request = Mock(side_effect=[http_response(404, '<html>' + SECRET + '</html>'),
            http_response(200, envelope(CLASSIC_ACCOUNT)), http_response(200, envelope(UNIFIED_RISK))])
        report = AccountDiscovery(ex).discover()
        self.assertTrue(report.authenticated.value)
        self.assertTrue(report.positions_available)
        self.assertIsNone(report.account_type.value)

    def test_pinned_implicit_routes_parameters_and_hmac_match_documented_get_signing(self):
        ex = self.exchange()
        ex.seconds = Mock(return_value=1700000000)
        ex.session.request = Mock(return_value=http_response(200, envelope({})))
        for method, path, params, query in (
            (ex.privateGetUtaApiRiskRiskMode, '/uta-api/risk/risk-mode', {}, ''),
            (ex.privateGetUtaApiRiskRiskUnits, '/uta-api/risk/risk-units', {'currency': 'USDT'}, 'currency=USDT'),
            (ex.privateGetGAccountsPositions, '/g-accounts/positions', {'currency': 'USDT'}, 'currency=USDT'),
            (ex.privateGetGAccountsRiskUnit, '/g-accounts/risk-unit', {}, ''),
        ):
            with self.subTest(path=path):
                method(params)
                call = ex.session.request.call_args
                self.assertEqual(call.args, ('GET', 'https://testnet-api.phemex.com' + path + ('?' + query if query else '')))
                expected = hmac.new(SECRET.encode(), (path + query + '1700000060').encode(), hashlib.sha256).hexdigest()
                self.assertEqual(call.kwargs['headers']['x-phemex-request-signature'], expected)
                self.assertIsNone(call.kwargs['data'])

    def test_debug_logging_and_verbose_configuration_do_not_expose_private_headers_or_body(self):
        ex = self.exchange()
        ex.session.request = Mock(return_value=http_response(200, envelope({'riskMode': 'Classic', 'userId': SECRET})))
        logs, output = io.StringIO(), io.StringIO()
        root = logging.getLogger()
        handler = logging.StreamHandler(logs)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        with patch.object(root, 'level', logging.DEBUG), redirect_stdout(output):
            ex.privateGetUtaApiRiskRiskMode({})
        self.assertNotIn(SECRET, logs.getvalue() + output.getvalue())
        self.assertNotIn('x-phemex-request-signature', logs.getvalue() + output.getvalue())
        self.assertEqual(output.getvalue(), '')
