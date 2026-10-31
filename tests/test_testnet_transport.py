"""Adversarial HTTP-boundary tests; sockets and Requests transport stay denied."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import hashlib
import hmac
import json
import os
from unittest.mock import Mock, patch

import ccxt
import requests

from diagnostics import ResponseEvidence
from safety import SafetyError
from testnet_compatibility import AUTHORIZATION, CompatibilityVerifier, Status, VerificationPhemex
from testnet_transport import (AUTH_HEADERS, HOST, PLAN, PROXY_OPTIONS, BlockedSession,
                               OfflineGetTransport, decode_response_json)
from tests.compatibility_fixtures import (FAKE_ACCOUNT, FAKE_USER, NOW, collect,
                                         duplicate_json_cases, fixture)
from tests.helpers import OfflineTestCase


def response(url, status=200, payload=None):
    value = requests.Response()
    value.url = url
    value.status_code = status
    value.encoding = 'utf-8'
    value.headers = {'Content-Type': 'application/json'}
    value._content = json.dumps(payload if payload is not None else {'code': 0, 'data': {}}).encode()
    return value


class TransportTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.verifier, self.ex, self.data, self.now = fixture()

    def activate_mock_plan(self):
        self.ex._transport.begin(lambda: self.now[0])
        self.ex._reads_authorized = True
        self.addCleanup(self.ex._transport.end)

    def signed(self, spec=PLAN[0]):
        return self.ex.sign(spec.path, [spec.api], 'GET', dict(spec.query))

    def wire(self, signed):
        return self.ex._transport.dispatch(self.ex, signed['url'], signed['method'],
                                            signed['headers'], signed['body'])

    def test_response_decoder_rejects_all_nested_duplicate_byte_sequences(self):
        for name, _, raw in duplicate_json_cases():
            with self.subTest(case=name), self.assertRaises(SafetyError) as failure:
                decode_response_json(raw)
            self.assertEqual(str(failure.exception),
                             'Response JSON is malformed or ambiguous; details withheld')

    def test_response_decoder_allows_same_field_in_distinct_objects(self):
        raw = b'{"items":[{"value":1},{"value":2}],"nested":{"value":3}}'
        self.assertEqual(decode_response_json(raw),
                         {'items': [{'value': 1}, {'value': 2}], 'nested': {'value': 3}})

    def test_ambiguous_response_is_rejected_before_ccxt_interpretation(self):
        self.activate_mock_plan()
        for entry in PLAN[:2]:
            self.ex.request(entry.path, entry.api)
        raw = next(raw for name, _, raw in duplicate_json_cases() if name == 'contradictory_mode')
        value = response(PLAN[2].url)
        value._content = raw
        self.ex._transport._send_http.side_effect = None
        self.ex._transport._send_http.return_value = value
        with patch.object(self.ex, 'on_rest_response') as diagnostics, \
             patch.object(self.ex, 'parse_json') as ccxt_parser, \
             patch.object(self.ex, 'handle_errors') as ccxt_errors:
            with self.assertRaises(SafetyError):
                self.ex.request(PLAN[2].path, PLAN[2].api, params=dict(PLAN[2].query))
        diagnostics.assert_not_called()
        ccxt_parser.assert_not_called()
        ccxt_errors.assert_not_called()
        self.assertEqual(self.ex.last_read_diagnostic, ResponseEvidence(200))
        self.assertEqual(self.ex._transport._send_http.call_count, 3)

    def test_inherited_fetch2_cannot_accept_or_cache_duplicate_private_json(self):
        self.activate_mock_plan()
        self.ex.fetchHistoryCacheSize = 10
        for entry in PLAN[:2]:
            self.ex.request(entry.path, entry.api)
        raw = next(raw for name, _, raw in duplicate_json_cases() if name == 'private_error_then_success')
        value = response(PLAN[2].url)
        value._content = raw
        self.ex._transport._send_http.side_effect = None
        self.ex._transport._send_http.return_value = value
        with patch.object(self.ex, 'seconds', return_value=1793448000):
            signed = self.signed(PLAN[2])
            with self.ex._transport.signed_request(signed), self.assertRaises(SafetyError):
                ccxt.phemex.fetch2(self.ex, PLAN[2].path, ['private'], 'GET', dict(PLAN[2].query))
        self.assert_history_empty()
        self.assertEqual(self.ex.last_read_diagnostic, ResponseEvidence(200))
        self.assertEqual(self.ex._transport._send_http.call_count, 3)

    def test_exact_four_gets_reach_mock_http_in_order(self):
        report = collect(self.verifier)
        calls = self.ex._transport._send_http.call_args_list
        self.assertEqual([call.args[0].url for call in calls], [spec.url for spec in PLAN])
        self.assertEqual(len(calls), 4)
        for call, spec in zip(calls, PLAN):
            prepared = call.args[0]
            self.assertEqual(prepared.method, 'GET')
            self.assertIsNone(prepared.body)
            self.assertEqual(set(prepared.headers), AUTH_HEADERS if spec.api == 'private' else set())
            self.assertEqual(call.kwargs, {'timeout': 15, 'verify': True, 'allow_redirects': False, 'proxies': {}})
        self.assertFalse(report.execution_authorized)
        self.assertEqual({receipt.provenance for receipt in self.ex._transport.receipts.values()},
                         {'synthetic_http_fixture'})

    def test_unmocked_transport_cannot_send_even_an_approved_signed_request(self):
        ex = VerificationPhemex('FIXTURE_ACCESS_TOKEN', 'FIXTURE_SECRET')
        with patch('ccxt.phemex.fetch', side_effect=AssertionError('CCXT network')) as ccxt_http:
            report = collect(CompatibilityVerifier(ex, clock=lambda: NOW))
        ccxt_http.assert_not_called()
        self.assertTrue(all(row.status is Status.UNKNOWN for row in report.requirements))
        self.assertEqual(len(ex._transport.receipts), 1)
        self.assertFalse(ex._reads_authorized)

    def test_no_plan_no_send_even_if_wrapper_flag_is_manually_enabled(self):
        self.ex._reads_authorized = True
        for invoke in (lambda: self.ex.request('public/products', 'v2'),
                       lambda: self.ex.fetch2('public/products', ['v2']),
                       lambda: self.ex.fetch(PLAN[0].url),
                       lambda: self.ex._transport.dispatch(self.ex, PLAN[0].url)):
            with self.assertRaises(SafetyError): invoke()
        self.ex._transport._send_http.assert_not_called()

    def test_canonical_url_validation_rejects_all_unapproved_serializations(self):
        self.activate_mock_plan()
        bad_urls = [
            'https://api.phemex.com/public/products', 'http://testnet-api.phemex.com/public/products',
            'https://testnet-api.phemex.com.evil/public/products',
            'https://testnet-api.phemex.com:443/public/products',
            'https://testnet-api.phemex.com:444/public/products',
            'https://user:pass@testnet-api.phemex.com/public/products',
            'https://TESTNET-API.PHEMEX.COM/public/products',
            'https://testnet-api.phemex.com./public/products',
            HOST + '//public/products', HOST + '/public/../public/products',
            HOST + '/public/%70roducts', HOST + '/public%2fproducts',
            HOST + '/public/products/', HOST + '/public/products?',
            HOST + '/public/products#', HOST + '/public/products#fragment',
            HOST + '/public/products?currency=USDT', HOST + '/g-orders',
            HOST + '/g-accounts/positions?currency=USDT&currency=USDT',
            HOST + '/g-accounts/positions?currency=%55SDT',
            HOST + '/g-accounts/positions?currency=USDT&',
            HOST + '/g-accounts/positions?currency=USDT;extra=1',
            HOST + '/g-accounts/positions?Currency=USDT',
            HOST + '/g-accounts/positions?currency=usdt',
            HOST + '/g-accounts/positions?currency=USDT&extra=1',
            HOST + '/g-accounts/positions?currency=USDT%00',
            HOST + '/g-accounts/positions', HOST + '/uta-api/risk/risk-mode',
            HOST + '/uta-api/risk/risk-units?currency=USDT',
            '\n' + PLAN[0].url, PLAN[0].url + '\r\n',
        ]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(SafetyError):
                self.ex._transport.dispatch(self.ex, url)
        self.ex._transport._send_http.assert_not_called()

    def test_final_boundary_rejects_mutations_bodies_and_extra_headers(self):
        self.activate_mock_plan()
        signed = self.signed()
        with self.ex._transport.signed_request(signed):
            for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'get'):
                with self.subTest(method=method), self.assertRaises(SafetyError):
                    self.ex._transport.dispatch(self.ex, PLAN[0].url, method)
            for body in ('', b'', '{}', {}, {'orderQtyRq': '1'}):
                with self.subTest(body=body), self.assertRaises(SafetyError):
                    self.ex._transport.dispatch(self.ex, PLAN[0].url, body=body)
            for headers in ({'Host': 'api.phemex.com'}, {'Authorization': 'PRIVATE_SENTINEL'},
                            {'User-Agent': 'unexpected'}, {'Cookie': 'PRIVATE_SENTINEL'},
                            {'x-phemex-access-token': 'PRIVATE_SENTINEL'}, []):
                with self.subTest(headers=headers), self.assertRaises(SafetyError):
                    self.ex._transport.dispatch(self.ex, PLAN[0].url, headers=headers)
        self.ex._transport._send_http.assert_not_called()

    def test_private_signing_matches_pinned_ccxt_and_accepts_only_exact_headers(self):
        self.activate_mock_plan()
        self.ex.request(PLAN[0].path, PLAN[0].api)
        self.ex.request(PLAN[1].path, PLAN[1].api)
        signed = self.signed(PLAN[2])
        headers = signed['headers']
        auth = '/g-accounts/positions' + 'currency=USDT' + headers['x-phemex-request-expiry']
        self.assertEqual(headers['x-phemex-request-signature'],
                         hmac.new(b'FIXTURE_SECRET', auth.encode(), hashlib.sha256).hexdigest())
        with self.ex._transport.signed_request(signed):
            for mutate in ('extra', 'missing', 'uppercase', 'value', 'crlf'):
                changed = dict(headers)
                if mutate == 'extra': changed['Content-Type'] = 'application/json'
                if mutate == 'missing': changed.pop('x-phemex-request-expiry')
                if mutate == 'uppercase': changed['X-Phemex-Access-Token'] = changed.pop('x-phemex-access-token')
                if mutate == 'value': changed['x-phemex-request-signature'] = '0' * 64
                if mutate == 'crlf': changed['x-phemex-access-token'] = 'key\r\nHost: api.phemex.com'
                with self.subTest(mutate=mutate), self.assertRaises(SafetyError):
                    self.ex._transport.dispatch(self.ex, signed['url'], headers=changed)
            self.wire(signed)
        self.assertEqual(self.ex._transport._send_http.call_count, 3)

    def test_signer_payload_cannot_change_before_final_http_send(self):
        self.activate_mock_plan()
        signed = self.signed()
        with self.ex._transport.signed_request(signed):
            changed = dict(signed, url=PLAN[1].url)
            with self.assertRaises(SafetyError): self.wire(changed)
            with self.assertRaises(SafetyError): self.ex.fetch(PLAN[0].url, headers={'extra': 'PRIVATE_SENTINEL'})
        self.ex._transport._send_http.assert_not_called()

    def test_prepared_request_is_revalidated_after_requests_normalization(self):
        self.activate_mock_plan()
        changed = requests.Request('POST', 'https://api.phemex.com/g-orders', data='{}').prepare()
        with patch('requests.Request.prepare', return_value=changed):
            with self.assertRaises(SafetyError): self.ex.request('public/products', 'v2')
        self.ex._transport._send_http.assert_not_called()

    def test_direct_fetch_without_a_matching_signed_request_cannot_send(self):
        self.activate_mock_plan()
        with self.assertRaises(SafetyError): self.ex.fetch(PLAN[0].url)
        with self.assertRaises(SafetyError): self.ex._transport.dispatch(self.ex, PLAN[0].url)
        self.ex._transport._send_http.assert_not_called()

    def test_direct_ccxt_entry_points_cannot_bypass_final_boundary(self):
        self.activate_mock_plan()
        for invoke in (
            lambda: self.ex.fetch2('g-orders', ['private'], 'POST', {}),
            lambda: ccxt.phemex.fetch2(self.ex, 'g-orders', ['private'], 'POST', {}),
            lambda: ccxt.phemex.request(self.ex, 'uta-api/risk/risk-mode', ['private']),
            lambda: ccxt.phemex.fetch(self.ex, 'https://api.phemex.com/g-orders', 'POST'),
            lambda: ccxt.phemex.fetch(self.ex, PLAN[0].url),
            lambda: self.ex.session.request('GET', PLAN[0].url),
            lambda: self.ex.session.send(requests.Request('GET', PLAN[0].url).prepare()),
            lambda: self.ex.session.get(PLAN[0].url),
        ):
            with self.assertRaises(SafetyError): invoke()
        self.ex._transport._send_http.assert_not_called()
        self.assertIs(type(self.ex.session), BlockedSession)
        self.assertFalse(hasattr(self.ex.session, 'adapters'))

    def test_implicit_load_markets_uses_only_two_public_gets(self):
        self.activate_mock_plan()
        self.ex._loading_metadata = True
        self.ex.load_markets(reload=True)
        calls = self.ex._transport._send_http.call_args_list
        self.assertEqual([call.args[0].url for call in calls], [PLAN[0].url, PLAN[1].url])
        self.assertIn('BTC/USDT:USDT', self.ex.markets)
        self.ex._loading_metadata = False
        with self.assertRaises(SafetyError): self.ex.fetch_currencies()
        self.assertEqual(self.ex._transport._send_http.call_count, 2)

    def test_public_products_cache_cannot_cross_collection_or_failed_attempts(self):
        self.activate_mock_plan()
        self.ex._loading_metadata = True
        self.ex._transport._send_http.side_effect = requests.Timeout('PRIVATE_SENTINEL')
        for _ in range(2):
            with self.assertRaises((SafetyError, ccxt.RequestTimeout)):
                self.ex.request('public/products', 'v2')
        self.assertEqual(self.ex._transport._send_http.call_count, 1)
        self.assertIsNone(self.ex._public_products)

    def test_wrong_order_and_four_request_budget_cannot_be_bypassed(self):
        self.activate_mock_plan()
        with self.assertRaises(SafetyError):
            with self.ex._transport.signed_request(self.signed(PLAN[1])):
                self.wire(self.signed(PLAN[1]))
        for spec in PLAN:
            signed = self.signed(spec)
            with self.ex._transport.signed_request(signed): self.wire(signed)
        for spec in PLAN:
            with self.assertRaises(SafetyError):
                with self.ex._transport.signed_request(self.signed(spec)):
                    self.wire(self.signed(spec))
        self.assertEqual(self.ex._transport._send_http.call_count, 4)

    def test_failed_wire_attempt_is_consumed_and_cannot_be_replayed(self):
        self.activate_mock_plan()
        signed = self.signed()
        self.ex._transport._send_http.side_effect = requests.Timeout('PRIVATE_SENTINEL')
        with self.ex._transport.signed_request(signed):
            with self.assertRaises(ccxt.RequestTimeout) as failure: self.wire(signed)
            with self.assertRaises(SafetyError): self.wire(signed)
        with self.assertRaises(SafetyError):
            with self.ex._transport.signed_request(signed): self.wire(signed)
        self.assertNotIn('PRIVATE_SENTINEL', str(failure.exception))
        self.assertEqual(self.ex._transport._send_http.call_count, 1)

    def test_redirects_are_rejected_without_followup_for_any_location(self):
        for location in ('https://api.phemex.com/g-orders', 'https://other.example/', PLAN[0].url):
            verifier, ex, _, _ = fixture()
            value = response(PLAN[0].url, 302)
            value.headers['Location'] = location
            ex._transport._send_http.return_value = value
            ex._transport._send_http.side_effect = None
            report = collect(verifier)
            self.assertEqual(ex._transport._send_http.call_count, 1)
            self.assertTrue(all(row.status is Status.UNKNOWN for row in report.requirements))
            self.assertNotIn(location, '\n'.join(report.diagnostics))

    def test_redirect_history_or_changed_response_url_is_rejected(self):
        for kind in ('history', 'url', 'status'):
            verifier, ex, _, _ = fixture()
            value = response(PLAN[0].url)
            if kind == 'history': value.history = [response(PLAN[0].url, 302)]
            if kind == 'url': value.url = 'https://api.phemex.com/public/products'
            if kind == 'status': value.status_code = 777
            ex._transport._send_http.side_effect = None
            ex._transport._send_http.return_value = value
            report = collect(verifier)
            self.assertTrue(all(row.status is Status.UNKNOWN for row in report.requirements))
            self.assertEqual(ex._transport._send_http.call_count, 1)

    def test_every_explicit_ccxt_proxy_option_is_rejected_before_callbacks(self):
        for name in PROXY_OPTIONS:
            verifier, ex, _, _ = fixture()
            value = Mock(side_effect=AssertionError('Proxy callback invoked')) if 'callback' in name.lower() else 'https://PRIVATE_SENTINEL/'
            setattr(ex, name, value)
            with self.subTest(name=name), self.assertRaises(SafetyError): collect(verifier)
            ex._transport._send_http.assert_not_called()
            if isinstance(value, Mock): value.assert_not_called()

    def test_environment_proxies_and_ca_overrides_are_not_inherited(self):
        values = {name: 'https://PRIVATE_SENTINEL/' for name in
                  ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')}
        values.update({'REQUESTS_CA_BUNDLE': 'PRIVATE_SENTINEL', 'CURL_CA_BUNDLE': 'PRIVATE_SENTINEL'})
        with patch.dict(os.environ, values), patch('requests.utils.get_environ_proxies', side_effect=AssertionError('Environment proxy lookup')):
            report = collect(self.verifier)
        self.assertFalse(self.ex.session.trust_env)
        self.assertNotIn('PRIVATE_SENTINEL', report.render())
        self.assertEqual(self.ex._transport._send_http.call_count, 4)

    def test_tls_session_redirect_headers_and_retry_configuration_fail_closed(self):
        cases = [('exchange', 'verify', False), ('exchange', 'validateServerSsl', False),
                 ('session', 'verify', False), ('session', 'trust_env', True),
                 ('session', 'max_redirects', 1), ('session', 'proxies', {'https': 'PRIVATE_SENTINEL'}),
                 ('session', 'headers', {'Authorization': 'PRIVATE_SENTINEL'}),
                 ('exchange', 'headers', {'Cookie': 'PRIVATE_SENTINEL'})]
        for target, key, value in cases:
            verifier, ex, _, _ = fixture()
            setattr(ex if target == 'exchange' else ex.session, key, value)
            with self.subTest(target=target, key=key), self.assertRaises(SafetyError): collect(verifier)
            ex._transport._send_http.assert_not_called()
        self.ex.options['maxRetriesOnFailure'] = 5
        with self.assertRaises(SafetyError): collect(self.verifier)
        self.ex._transport._send_http.assert_not_called()

    def test_final_boundary_rechecks_configuration_after_high_level_validation(self):
        self.activate_mock_plan()
        signed = self.signed()
        with self.ex._transport.signed_request(signed):
            self.ex.verify = False
            with self.assertRaises(SafetyError): self.wire(signed)
        self.ex._transport._send_http.assert_not_called()

    def test_concurrent_thread_cannot_use_another_threads_plan(self):
        self.activate_mock_plan()
        signed = self.signed()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.wire, signed)
            with self.assertRaises(SafetyError): future.result()
        self.ex._transport._send_http.assert_not_called()

    def test_closed_or_restarted_transport_plan_cannot_resume_or_replay(self):
        self.activate_mock_plan()
        self.ex._transport.end()
        with self.assertRaises(SafetyError): self.ex._transport.begin(lambda: NOW)
        with self.assertRaises(SafetyError): self.ex.fetch(PLAN[0].url)
        fresh = OfflineGetTransport()
        with self.assertRaises(SafetyError): fresh.dispatch(self.ex, PLAN[0].url)
        self.ex._transport._send_http.assert_not_called()

    def test_http_500_10500_stops_without_retries_or_risk_fallback(self):
        original = self.ex._transport._send_http.side_effect
        def reply(prepared, **kwargs):
            if prepared.url == PLAN[2].url:
                return response(prepared.url, 500, {'code': 10500, 'msg': 'Please try again later PRIVATE_SENTINEL'})
            return original(prepared, **kwargs)
        self.ex._transport._send_http.side_effect = reply
        report = collect(self.verifier)
        rows = {row.name: row for row in report.requirements}
        self.assertEqual(rows['authentication'].status, Status.UNKNOWN)
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
        self.assertIn('http=500; code=10500', report.render())
        self.assertIn('reason=retry_later', report.render())
        self.assertNotIn('PRIVATE_SENTINEL', report.render())
        self.assertEqual(self.ex._transport._send_http.call_count, 3)

    def test_receipts_track_actual_mock_http_times_and_never_assert_real_provenance(self):
        original = self.ex._transport._send_http.side_effect
        def reply(prepared, **kwargs):
            self.now[0] += timedelta(milliseconds=100)
            return original(prepared, **kwargs)
        self.ex._transport._send_http.side_effect = reply
        report = collect(self.verifier)
        receipts = list(self.ex._transport.receipts.values())
        self.assertEqual([r.started_at for r in receipts], [NOW + timedelta(milliseconds=100 * i) for i in range(4)])
        self.assertEqual([r.completed_at for r in receipts], [NOW + timedelta(milliseconds=100 * (i + 1)) for i in range(4)])
        rows = {row.name: row for row in report.requirements}
        self.assertEqual(rows['Long.base_quantity'].observed_at, receipts[2].completed_at.isoformat())
        self.assertEqual(rows['instrument.price_tick'].observed_at, receipts[1].completed_at.isoformat())
        self.assertTrue(all(row.provenance.startswith('synthetic_') or row.provenance == 'not_collected'
                            for row in report.requirements))
        for key in ('accounting_type', 'account_restrictions'):
            self.assertEqual(rows[key].provenance, 'not_collected')
            self.assertEqual(rows[key].observed_at, 'unavailable')
        with self.assertRaises(SafetyError):
            self.verifier.collect(authorization=AUTHORIZATION, synthetic=False)

    def test_reports_errors_and_ccxt_caches_do_not_retain_signed_or_private_material(self):
        self.ex.fetchHistoryCacheSize = 10
        self.ex.verbose = True
        with patch('builtins.print') as printed:
            report = collect(self.verifier)
        printed.assert_not_called()
        for value in ('FIXTURE_SECRET', 'FIXTURE_ACCESS_TOKEN', FAKE_USER, FAKE_ACCOUNT,
                      'x-phemex-request-signature', 'x-phemex-access-token'):
            self.assertNotIn(value, repr(report) + report.render() + repr(self.ex._transport.receipts))
        for name in ('last_request_headers', 'last_request_body', 'last_request_url',
                     'last_http_response', 'last_json_response', 'last_response_headers'):
            self.assertIsNone(getattr(self.ex, name, None), name)
        self.assertFalse(self.ex.get_fetch_cache())

    def assert_history_empty(self):
        for cache in (self.ex.get_fetch_cache(), ccxt.phemex.get_fetch_cache(self.ex), self.ex.fetchHistoryCache):
            self.assertFalse(cache)
            for value in ('FIXTURE_ACCESS_TOKEN', 'FIXTURE_SECRET', FAKE_ACCOUNT, FAKE_USER,
                          'x-phemex-request-signature'):
                self.assertNotIn(value, repr(cache))
        for name in ('last_request_headers', 'last_request_body', 'last_request_url',
                     'last_http_response', 'last_json_response', 'last_response_headers'):
            self.assertIsNone(getattr(self.ex, name, None), name)

    def test_inherited_fetch2_rejection_does_not_cache_signed_request_or_exception(self):
        self.ex.fetchHistoryCacheSize = 10
        with self.assertRaises(SafetyError):
            ccxt.phemex.fetch2(self.ex, PLAN[2].path, ['private'], 'GET', dict(PLAN[2].query))
        self.ex._transport._send_http.assert_not_called()
        self.assert_history_empty()

    def test_inherited_fetch2_success_does_not_cache_private_response(self):
        self.activate_mock_plan()
        self.ex.fetchHistoryCacheSize = 10
        for spec in PLAN[:2]: self.ex.request(spec.path, spec.api)
        with patch.object(self.ex, 'seconds', return_value=1793448000):
            signed = self.signed(PLAN[2])
            with self.ex._transport.signed_request(signed):
                result = ccxt.phemex.fetch2(self.ex, PLAN[2].path, ['private'], 'GET', dict(PLAN[2].query))
        self.assertEqual(result['data']['account']['accountId'], FAKE_ACCOUNT)
        self.assertEqual(self.ex._transport._send_http.call_count, 3)
        self.assert_history_empty()

    def test_inherited_fetch2_failed_mocked_private_read_does_not_cache(self):
        self.activate_mock_plan()
        self.ex.fetchHistoryCacheSize = 10
        for spec in PLAN[:2]: self.ex.request(spec.path, spec.api)
        self.ex._transport._send_http.side_effect = requests.Timeout('FIXTURE_SECRET PRIVATE_SENTINEL')
        with patch.object(self.ex, 'seconds', return_value=1793448000):
            signed = self.signed(PLAN[2])
            with self.ex._transport.signed_request(signed), self.assertRaises(ccxt.RequestTimeout):
                ccxt.phemex.fetch2(self.ex, PLAN[2].path, ['private'], 'GET', dict(PLAN[2].query))
        self.assert_history_empty()

    def test_direct_and_inherited_history_writers_cannot_enable_storage(self):
        self.ex.fetchHistoryCacheSize = 10
        entry = {'request': {'headers': {'x-phemex-access-token': 'FIXTURE_ACCESS_TOKEN'}},
                 'response': {'body': {'accountId': FAKE_ACCOUNT}}}
        self.ex.add_fetch_cache(entry)
        ccxt.phemex.add_fetch_cache(self.ex, entry)
        self.ex.fetchHistoryCache.append(entry)
        self.assert_history_empty()

    def test_only_http_200_can_support_successful_observations(self):
        for status in (101, 199, 200, 201, 204, 206, 299, 301, 302, 304, 400, 499, 500, 599):
            for target in PLAN:
                verifier, ex, _, _ = fixture()
                original = ex._transport._send_http.side_effect
                def reply(prepared, **options):
                    value = original(prepared, **options)
                    if prepared.url == target.url: value.status_code = status
                    return value
                ex._transport._send_http.side_effect = reply
                report = collect(verifier)
                rows = {row.name: row for row in report.requirements}
                with self.subTest(status=status, endpoint=target.label):
                    expected = Status.VERIFIED if status == 200 else Status.UNKNOWN
                    self.assertEqual(rows['Long.base_quantity'].status, expected)
                    if target == PLAN[2]: self.assertEqual(rows['authentication'].status, expected)
                    if target in PLAN[:2] and status != 200:
                        self.assertTrue(all(row.status is Status.UNKNOWN for row in report.requirements))
                    self.assertLessEqual(ex._transport._send_http.call_count, 4)

    def test_malformed_http_status_cannot_verify_authentication(self):
        for status in (None, '200', 200.0, True, 0, 99, 600):
            verifier, ex, _, _ = fixture()
            original = ex._transport._send_http.side_effect
            def reply(prepared, **options):
                value = original(prepared, **options)
                if prepared.url == PLAN[2].url: value.status_code = status
                return value
            ex._transport._send_http.side_effect = reply
            report = collect(verifier)
            rows = {row.name: row for row in report.requirements}
            with self.subTest(status=status):
                self.assertEqual(rows['authentication'].status, Status.UNKNOWN)
                self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
                self.assertEqual(ex._transport._send_http.call_count, 3)

    def test_http_200_still_requires_complete_success_envelope_for_authentication(self):
        for payload in (None, {}, {'code': 0, 'data': None}, {'code': 0, 'data': []},
                        {'code': 10500, 'data': {'accountId': FAKE_ACCOUNT}}):
            verifier, ex, data, _ = fixture()
            data['/g-accounts/positions'] = payload
            report = collect(verifier)
            rows = {row.name: row for row in report.requirements}
            self.assertEqual(rows['authentication'].status, Status.UNKNOWN)
            self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
            self.assertEqual(ex._transport._send_http.call_count, 3)

    def test_unexpected_backend_errors_are_sanitized_without_retry(self):
        self.ex._transport._send_http.side_effect = RuntimeError('FIXTURE_SECRET PRIVATE_SENTINEL signed headers')
        report = collect(self.verifier)
        self.assertNotIn('FIXTURE_SECRET', report.render())
        self.assertNotIn('PRIVATE_SENTINEL', report.render())
        self.assertEqual(self.ex._transport._send_http.call_count, 1)

    def test_report_cannot_be_relabelled_as_actual_exchange_verification(self):
        report = collect(self.verifier)
        with self.assertRaises(SafetyError): replace(report, synthetic=False)

    def test_fabricated_receipt_provenance_cannot_verify_private_fields(self):
        original = self.ex._transport.dispatch
        def dispatch(exchange, url, *args):
            value = original(exchange, url, *args)
            if url == PLAN[2].url:
                receipt = self.ex._transport.receipts['account_positions']
                self.ex._transport.receipts['account_positions'] = replace(receipt, provenance='authenticated_testnet_observation')
            return value
        with patch.object(self.ex._transport, 'dispatch', side_effect=dispatch):
            report = collect(self.verifier)
        rows = {row.name: row for row in report.requirements}
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
        self.assertTrue(report.synthetic)

    def test_inherited_verbose_fetch_does_not_print_signing_material(self):
        self.ex.verbose = True
        with patch('builtins.print') as printed, self.assertRaises(SafetyError):
            ccxt.phemex.fetch(self.ex, PLAN[2].url, headers={
                'x-phemex-access-token': 'FIXTURE_ACCESS_TOKEN',
                'x-phemex-request-signature': 'FIXTURE_SECRET'})
        printed.assert_not_called()
        self.ex._transport._send_http.assert_not_called()

    def test_replaced_session_is_rejected_by_supported_entry_points(self):
        self.activate_mock_plan()
        self.ex.session = Mock()
        with self.assertRaises(SafetyError): self.ex.request('public/products', 'v2')
        with self.assertRaises(SafetyError): self.ex.fetch(PLAN[0].url)
        self.ex.session.request.assert_not_called()
        self.ex._transport._send_http.assert_not_called()
