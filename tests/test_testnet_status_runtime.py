"""Fake credentials and mocked final HTTP sends only; sockets stay denied."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
import copy
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from unittest.mock import patch

import ccxt
import requests

from safety import SafetyError
from testnet_status_collection import collect_status, collect_runtime_status, MAX_RESPONSE_BYTES
import testnet_status_runtime as module
from testnet_status_runtime import (TestnetCredentialProfile, TestnetReadOnlyRuntime,
    PROFILE_PURPOSE, parse_testnet_profile, load_testnet_profile)
from testnet_transport import AUTH_HEADERS, HOST, PLAN, PROXY_OPTIONS, OfflineGetTransport
from testnet_compatibility import Status
from tests.compatibility_fixtures import NOW, FAKE_ACCOUNT, FAKE_USER, responses
from tests.helpers import OfflineTestCase
from tests.test_testnet_status_once import fixture_run_context


ORIGINAL_BOOTSTRAP = module._require_protected_bootstrap
ORIGINAL_SEND = requests.sessions.Session.send
ROOT = Path(__file__).resolve().parents[1]
KEY, SECRET = 'FAKE_TESTNET_READ_KEY', 'FAKE_TESTNET_READ_SECRET'


def profile_bytes(**changes):
    value = {'version': 1, 'purpose': PROFILE_PURPOSE, 'environment': 'testnet',
        'origin': HOST, 'profile_id': 'a' * 32, 'api_key': KEY, 'api_secret': SECRET}
    value.update(changes)
    return json.dumps(value).encode()


def profile():
    return parse_testnet_profile(profile_bytes())


class RuntimeTests(OfflineTestCase):
    def make_runtime(self):
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='.t1c-2c-',dir=ROOT)))
        context = fixture_run_context(self.stack,directory)
        return TestnetReadOnlyRuntime(profile(),context=context)

    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(module, '_require_protected_bootstrap', return_value=None))
        self.stack.enter_context(patch('ccxt.Exchange.seconds', return_value=1793448000))
        for target in ('config.load_settings', 'ccxt.phemex.__init__',
                       'operation_journal.OperationJournal.__init__',
                       'operation_journal.OperationJournal.resolve', 'testnet_readonly_worker.serve_once',
                       'readonly_ipc.OfflineIPC.handle'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('Prohibited capability')))
        self.data = responses()
        self.raw = {}
        self.statuses = {}
        self.sessions, self.prepared, self.options = [], [], []
        self.http = self.stack.enter_context(patch('requests.sessions.Session.send', autospec=True,
                                                  side_effect=self.respond))
        self.runtime = self.make_runtime()

    def respond(self, session, prepared, **options):
        self.sessions.append(session)
        self.prepared.append(prepared)
        self.options.append(options)
        entry = next(entry for entry in PLAN if prepared.url == entry.url)
        path = prepared.url.removeprefix(HOST).split('?')[0]
        body = self.raw.get(entry.label, json.dumps(self.data[path]).encode())
        result = requests.Response()
        result.status_code = self.statuses.get(entry.label, 200)
        result.url = prepared.url
        result.raw = io.BytesIO(body)
        result.request = prepared  # Must not survive in returned report/receipt.
        result.headers['Content-Encoding'] = 'identity'
        return result

    def collect(self, runtime=None, **options):
        return (runtime or self.runtime).collect(clock=options.get('clock', lambda: NOW))

    def rows(self, report):
        return {row.name: row for row in report.requirements}

    def test_complete_four_request_runtime_with_fake_profile_and_mocked_send(self):
        report = self.collect()
        self.assertTrue(report.complete)
        self.assertEqual([r.url for r in self.prepared], [p.url for p in PLAN])
        self.assertEqual(self.runtime.attempts, tuple(p.label for p in PLAN))
        self.assertEqual(self.http.call_count, 4)
        self.assertEqual(report.collection_mode, 't1c_mock')
        self.assertTrue(report.synthetic)
        self.assertFalse(report.authenticated_evidence_verified)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.may_resolve_journal)

    def test_production_bootstrap_block_is_unconditional_even_with_valid_context(self):
        with patch.object(module, '_require_protected_bootstrap', ORIGINAL_BOOTSTRAP), \
                patch('requests.sessions.Session.__init__', side_effect=AssertionError('Session creation')) as session:
            report = self.collect()
        self.assertFalse(report.complete)
        self.assertEqual(self.runtime.attempts, ('products',))
        session.assert_not_called()
        self.http.assert_not_called()

    def test_missing_context_cannot_send_even_when_only_bootstrap_is_mocked(self):
        bare = TestnetReadOnlyRuntime(profile())
        report = self.collect(bare)
        self.assertFalse(report.collection_complete)
        self.http.assert_not_called()

    def test_copied_budget_cannot_redeem_one_dispatch_twice(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        transport = self.runtime._transport
        original = transport._read_https
        def duplicated(prepared):
            duplicate = copy.copy(transport)
            duplicate._budget = copy.copy(transport._budget)
            result = original(prepared)
            with self.assertRaises(SafetyError):
                module._RuntimeGetTransport._read_https(duplicate,prepared)
            return result
        with patch.object(transport,'_read_https',side_effect=duplicated), self.assertRaises(SafetyError):
            self.runtime.get(PLAN[0])  # Shared context is poisoned by the replay.
        self.assertEqual(self.http.call_count,1)
        context = self.runtime._context
        self.assertEqual(context.store.dispatch_snapshot(context.lease.run_id),((0,True),))

    def test_base_budget_cannot_bypass_durable_dispatch(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        transport = self.runtime._transport
        prepared = requests.Request('GET',PLAN[0].url).prepare()
        transport._budget = module._RunBudget()
        transport._budget.begin()
        transport._budget.reserve(PLAN[0],module._fingerprint(prepared.url,prepared.method,{},None))
        with self.assertRaises(SafetyError):
            transport._read_https(prepared)
        self.http.assert_not_called()

    def test_base_reserve_cannot_mint_a_durable_dispatch_permit(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        transport = self.runtime._transport
        prepared = requests.Request('GET',PLAN[0].url).prepare()
        fingerprint = module._fingerprint(prepared.url,prepared.method,{},None)
        module._RunBudget.reserve(transport._budget,PLAN[0],fingerprint)
        with self.assertRaises(SafetyError):
            transport._read_https(prepared)
        self.http.assert_not_called()

    def test_expiry_during_consumption_commit_prevents_http(self):
        context = self.runtime._context
        original = context.store.consume_dispatch
        def expired(*args):
            original(*args)
            context.store.clock = lambda: NOW + timedelta(seconds=300)
        with patch.object(context.store,'consume_dispatch',side_effect=expired):
            report = self.collect()
        self.assertFalse(report.collection_complete)
        self.http.assert_not_called()
        self.assertEqual(context.store.dispatch_snapshot(context.lease.run_id),((0,True),))

    def test_signing_is_pinned_ccxt_hmac_with_explicit_testnet_credentials(self):
        self.collect()
        for request, entry in zip(self.prepared, PLAN):
            self.assertEqual(request.method, 'GET')
            self.assertIsNone(request.body)
            self.assertEqual(set(request.headers), AUTH_HEADERS if entry.api == 'private' else set())
            if entry.api == 'private':
                expiry = request.headers['x-phemex-request-expiry']
                self.assertEqual(expiry, str(1793448000 + 60))
                message = '/' + entry.path + 'currency=USDT' + expiry
                expected = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
                self.assertEqual(request.headers['x-phemex-request-signature'], expected)
                self.assertEqual(request.headers['x-phemex-access-token'], KEY)

    def test_http_configuration_is_tls_verified_no_proxy_redirect_or_retry(self):
        self.collect()
        for session, options in zip(self.sessions, self.options):
            self.assertFalse(session.trust_env)
            self.assertTrue(session.verify)
            self.assertEqual(session.max_redirects, 0)
            self.assertEqual(dict(session.headers), {})
            self.assertEqual(session.proxies, {})
            self.assertIsNone(session.auth)
            self.assertEqual(list(session.resolve_redirects(None, None)), [])
            self.assertEqual(set(session.adapters), {'https://'})
            self.assertEqual(session.adapters['https://'].max_retries.total, 0)
            self.assertEqual(options, {'timeout': (5, 10), 'verify': True,
                'allow_redirects': False, 'proxies': {}, 'stream': True})

    def test_environment_keys_proxies_and_ca_overrides_are_not_inherited(self):
        values = {key: 'MAINNET_ENV_SENTINEL' for key in (
            'PHEMEX_API_KEY', 'PHEMEX_API_SECRET', 'PHEMEX_ALLOW_LIVE', 'HTTP_PROXY',
            'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
            'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        with patch.dict(os.environ, values):
            report = self.collect()
        self.assertTrue(report.complete)
        self.assertNotIn('MAINNET_ENV_SENTINEL', report.render())
        self.assertEqual(self.prepared[2].headers['x-phemex-access-token'], KEY)
        self.assertTrue(all(not s.trust_env and s.proxies == {} for s in self.sessions))

    def test_no_full_exchange_or_mutation_methods_are_constructed(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        for method in ('create_order', 'cancel_order', 'edit_order', 'set_leverage',
                       'set_position_mode', 'set_margin_mode', 'transfer', 'withdraw',
                       'fetch', 'fetch2', 'request', 'load_markets'):
            self.assertFalse(hasattr(self.runtime, method))
            self.assertFalse(hasattr(self.runtime._signer, method))
        with self.assertRaises(SafetyError):
            self.runtime._signer.session.get(PLAN[0].url)
        self.http.assert_not_called()

    def test_unbound_base_ccxt_transport_cannot_acquire_http_capability(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        for method in (ccxt.phemex.request, ccxt.phemex.fetch2, ccxt.phemex.fetch):
            with self.assertRaises(Exception):
                method(self.runtime._signer, HOST + '/g-orders', 'private', 'POST')
        self.http.assert_not_called()

    def test_unknown_endpoint_or_extra_query_is_rejected_before_send(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        for entry in (replace(PLAN[0], path='g-orders'), replace(PLAN[0], query=(('extra', '1'),)),
                      replace(PLAN[0], path='public/%70roducts'), PLAN[1], 'https://api.phemex.com'):
            with self.assertRaises(SafetyError):
                self.runtime.get(entry)
        self.http.assert_not_called()

    def test_direct_mainnet_method_body_header_and_query_bypasses_reject(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        transport, signer = self.runtime._transport, self.runtime._signer
        for url, method, headers, body in (
            ('https://api.phemex.com/public/products', 'GET', None, None),
            (PLAN[0].url, 'POST', None, None), (PLAN[0].url, 'DELETE', None, None),
            (PLAN[0].url, 'GET', {'Cookie': 'PRIVATE_SENTINEL'}, None),
            (PLAN[0].url, 'GET', None, b''), (PLAN[0].url + '?x=1&x=2', 'GET', None, None)):
            with self.assertRaises(SafetyError):
                transport.dispatch(signer, url, method, headers, body)
        self.http.assert_not_called()

    def test_missing_pending_signed_binding_blocks_direct_fetch(self):
        self.runtime.begin()
        self.addCleanup(self.runtime.end)
        with self.assertRaises(SafetyError):
            self.runtime._transport.dispatch(self.runtime._signer, PLAN[0].url)
        prepared = requests.Request('GET', PLAN[0].url).prepare()
        with self.assertRaises(SafetyError):
            self.runtime._transport._read_https(prepared)
        self.http.assert_not_called()

    def test_preparation_time_request_mutation_is_rejected(self):
        original = requests.Request.prepare
        def changed(request):
            result = original(request)
            result.headers['Cookie'] = 'PRIVATE_SENTINEL'
            return result
        with patch.object(requests.Request, 'prepare', changed):
            report = self.collect()
        self.assertFalse(report.complete)
        self.http.assert_not_called()
        self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_wrong_signer_origin_and_proxy_or_tls_configuration_reject(self):
        cases = [('verify', False), ('validateServerSsl', False)] + [(key, 'PRIVATE_PROXY') for key in PROXY_OPTIONS]
        for key, value in cases:
            runtime = self.make_runtime()
            runtime.begin()
            setattr(runtime._signer, key, value)
            with self.assertRaises(SafetyError):
                runtime.get(PLAN[0])
            runtime.end()
        self.runtime.begin()
        self.runtime._signer.urls['api']['private'] = 'https://api.phemex.com'
        with self.assertRaises(SafetyError):
            self.runtime.get(PLAN[0])
        self.runtime.end()
        self.http.assert_not_called()

    def test_redirects_are_not_followed(self):
        for status in (301, 302, 307, 308):
            self.statuses['products'] = status
            runtime = self.make_runtime()
            report = self.collect(runtime)
            self.assertFalse(report.complete)
            self.assertEqual(runtime.attempts, ('products',))
        self.assertEqual(self.http.call_count, 4)

    def test_real_requests_send_with_mock_adapter_collects_without_hidden_requests(self):
        with patch('requests.sessions.Session.send', ORIGINAL_SEND), \
                patch('requests.adapters.HTTPAdapter.send', autospec=True,
                      side_effect=lambda adapter, request, **options: self.respond(None, request, **options)) as adapter:
            report = self.collect()
        self.assertTrue(report.complete)
        self.assertEqual(adapter.call_count, 4)

    def test_requests_redirect_preparation_cannot_preload_body_or_follow_location(self):
        def redirect(adapter, request, **options):
            result = self.respond(None, request, **options)
            result.status_code = 302
            result.headers['Location'] = 'https://api.phemex.com/PRIVATE_SENTINEL'
            return result
        def premature_content(response):
            raise AssertionError('Unbounded Response.content access before size checks')
        with patch('requests.sessions.Session.send', ORIGINAL_SEND), \
                patch('requests.adapters.HTTPAdapter.send', autospec=True, side_effect=redirect) as adapter, \
                patch.object(requests.Response, 'content', property(premature_content)):
            report = self.collect()
        self.assertFalse(report.complete)
        self.assertEqual(adapter.call_count, 1)
        self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_http_debug_output_cannot_expose_signed_headers(self):
        for connection in (module.http.client.HTTPConnection, module.http.client.HTTPSConnection,
                           module.PoolHTTPConnection, module.PoolHTTPSConnection):
            with patch.object(connection, 'debuglevel', 1):
                self.assertFalse(self.collect(self.make_runtime()).complete)
        self.http.assert_not_called()

    def test_injected_response_hooks_are_rejected_before_session_or_send(self):
        original = requests.Request.prepare
        def hooked(request):
            result = original(request)
            result.hooks['response'].append(lambda response, **kwargs: response)
            return result
        with patch.object(requests.Request, 'prepare', hooked):
            self.assertFalse(self.collect().complete)
        self.http.assert_not_called()

    def test_changed_response_url_or_redirect_history_rejects(self):
        original = self.respond
        def changed(session, request, **options):
            response = original(session, request, **options)
            response.url = 'https://api.phemex.com/PRIVATE_SENTINEL'
            response.history = [requests.Response()]
            return response
        self.http.side_effect = changed
        report = self.collect()
        self.assertFalse(report.complete)
        self.assertEqual(self.http.call_count, 1)
        self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_network_and_timeout_errors_are_sanitized_without_retry(self):
        for error in (requests.Timeout('PRIVATE_SENTINEL'), requests.ConnectionError('PRIVATE_SENTINEL'),
                      requests.exceptions.SSLError('PRIVATE_SENTINEL')):
            self.http.side_effect = error
            runtime = self.make_runtime()
            report = self.collect(runtime)
            self.assertFalse(report.complete)
            self.assertEqual(runtime.attempts, ('products',))
            self.assertNotIn('PRIVATE_SENTINEL', report.render())
            with self.assertRaises(SafetyError):
                self.collect(runtime)
        self.assertEqual(self.http.call_count, 3)

    def test_failed_private_request_preserves_public_metadata_and_stops(self):
        self.statuses['account_positions'] = 500
        self.data['/g-accounts/positions'] = {'code': 10500, 'msg': 'Please try again later PRIVATE_SENTINEL'}
        report = self.collect()
        rows = self.rows(report)
        self.assertEqual(rows['authentication'].status, Status.UNKNOWN)
        self.assertEqual(rows['instrument.quantity_step'].status, Status.VERIFIED)
        self.assertIn('http=500; code=10500', report.render())
        self.assertNotIn('PRIVATE_SENTINEL', report.render())
        self.assertEqual(self.http.call_count, 3)

    def test_failed_crosscheck_retains_primary_authentication_but_not_verified_positions(self):
        self.statuses['identity_crosscheck'] = 500
        report = self.collect()
        rows = self.rows(report)
        self.assertEqual(rows['authentication'].status, Status.VERIFIED)
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.UNKNOWN)
        self.assertFalse(report.complete)
        self.assertEqual(self.http.call_count, 4)

    def test_authentication_permission_rate_limit_and_non200_stop(self):
        for status in (101, 199, 204, 400, 401, 403, 429, 500):
            self.statuses['account_positions'] = status
            runtime = self.make_runtime()
            report = self.collect(runtime)
            self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)
            self.assertFalse(report.endpoints[3].attempted)
            self.assertEqual(len(runtime.attempts), 3)

    def test_decimal_literals_survive_runtime_without_float_conversion(self):
        precise = b'0.00200000000000000000000000000000000001'
        for entry in PLAN[2:]:
            path = entry.url.removeprefix(HOST).split('?')[0]
            self.raw[entry.label] = json.dumps(self.data[path]).encode().replace(
                b'"sizeRq": "0.002"', b'"sizeRq": ' + precise)
        report = self.collect()
        rows = self.rows(report)
        self.assertEqual(rows['Long.observed_base_quantity'].value, precise.decode())
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.VERIFIED)
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)

    def test_duplicate_json_and_invalid_private_envelopes_never_authenticate(self):
        for raw in (b'{"code":10500,"code":0,"data":{}}', b'\xff', b'{"code":0,"data":{}}',
                    b'{"code":0,"data":{"positions":[],"account":{},"extra":[{"x":1,"x":1}]}}'):
            self.raw['account_positions'] = raw
            runtime = self.make_runtime()
            report = self.collect(runtime)
            self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)
            self.assertEqual(len(runtime.attempts), 3)

    def test_oversized_response_and_compressed_encoding_reject(self):
        self.raw['products'] = b'x' * (MAX_RESPONSE_BYTES + 1)
        self.assertFalse(self.collect().complete)
        self.assertEqual(self.http.call_count, 1)
        self.raw.clear()
        original = self.respond
        def compressed(session, request, **options):
            result = original(session, request, **options)
            result.headers['Content-Encoding'] = 'gzip'
            return result
        self.http.side_effect = compressed
        self.assertFalse(self.collect(self.make_runtime()).complete)

    def test_deadline_prevents_new_requests(self):
        self.runtime.begin()
        with patch.object(module.time, 'monotonic', return_value=self.runtime._budget.deadline):
            with self.assertRaises(SafetyError):
                self.runtime.get(PLAN[0])
        self.runtime.end()
        self.http.assert_not_called()

    def test_deadline_during_body_read_stops_without_retry(self):
        original = self.respond
        def expired(session, request, **options):
            response = original(session, request, **options)
            self.runtime._budget.deadline = -1
            return response
        self.http.side_effect = expired
        self.assertFalse(self.collect().complete)
        self.assertEqual(self.http.call_count, 1)

    def test_chunked_response_failure_is_not_retried(self):
        original = self.respond
        def broken(session, request, **options):
            response = original(session, request, **options)
            def chunks(**kwargs):
                yield b'{'
                raise requests.exceptions.ChunkedEncodingError('PRIVATE_SENTINEL')
            response.iter_content = chunks
            return response
        self.http.side_effect = broken
        report = self.collect()
        self.assertFalse(report.complete)
        self.assertEqual(self.http.call_count, 1)
        self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_single_use_after_success_and_no_fifth_request(self):
        self.assertTrue(self.collect().complete)
        with self.assertRaises(SafetyError):
            self.collect()
        with self.assertRaises(SafetyError):
            self.runtime.get(PLAN[0])
        self.assertEqual(self.http.call_count, 4)

    def test_copied_transport_shares_run_quota(self):
        self.runtime.begin()
        duplicate = copy.copy(self.runtime)
        duplicate._transport = copy.copy(self.runtime._transport)
        self.runtime.get(PLAN[0])
        with self.assertRaises(SafetyError):
            duplicate.get(PLAN[0])
        self.assertEqual(self.http.call_count, 1)
        self.runtime.end()

    def test_wrong_thread_cannot_dispatch_on_another_threads_run(self):
        from concurrent.futures import ThreadPoolExecutor
        self.runtime.begin()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.runtime.get, PLAN[0])
            with self.assertRaises(SafetyError):
                future.result()
        self.runtime.end()
        self.http.assert_not_called()

    def test_concurrent_collectors_cannot_claim_same_runtime_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        def collect_once():
            try:
                return self.collect().complete
            except SafetyError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: collect_once(), range(2))), [False, True])
        self.assertEqual(self.http.call_count, 4)

    def test_pin_verification_failure_never_reaches_http(self):
        with patch.object(module, 'verify_installed_ccxt', side_effect=RuntimeError('PRIVATE_SENTINEL')):
            with self.assertRaises(SafetyError) as error:
                self.collect()
        self.assertNotIn('PRIVATE_SENTINEL', str(error.exception))
        self.http.assert_not_called()

    def test_reports_and_receipts_do_not_retain_signing_or_private_identifiers(self):
        report = self.collect()
        text = report.render() + repr(report) + repr(self.runtime._transport.receipts)
        for value in (KEY, SECRET, FAKE_ACCOUNT, FAKE_USER, 'x-phemex-access-token',
                      'x-phemex-request-signature', 'accountBalanceRv'):
            self.assertNotIn(value, text)
        self.assertIsNone(self.runtime._profile)
        self.assertIsNone(self.runtime._signer.apiKey)
        self.assertIsNone(self.runtime._signer.secret)
        self.assertIn('T1C STEP 1 MOCK TRANSPORT REPORT', text)

    def test_existing_t1b_entry_and_runtime_seam_remain_separate(self):
        with self.assertRaises(SafetyError):
            collect_status(self.runtime)
        for runtime in (lambda: None, {}, object()):
            with self.assertRaises(SafetyError):
                collect_runtime_status(runtime)

    def test_original_offline_send_is_still_unconditionally_blocked(self):
        with self.assertRaises(SafetyError):
            OfflineGetTransport()._send_http(None, timeout=15, verify=True, allow_redirects=False, proxies={})
        self.http.assert_not_called()


class ProfileTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def make_context(self, path):
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='.t1c-2c-',dir=ROOT)))
        return fixture_run_context(self.stack,directory,profile_path=path)

    def fixture_ancestor_permissions(self):
        # The development checkout is intentionally not a trusted installation
        # ancestor. Model only that ancestor as protected; change no host mode.
        identity = ROOT.stat()
        original = os.fstat
        def trusted(fd):
            info = original(fd)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                values = list(info)
                values[0] &= ~0o022
                return os.stat_result(values)
            return info
        return patch.object(module.os, 'fstat', side_effect=trusted)

    def test_explicit_testnet_profile_and_hidden_repr(self):
        value = profile()
        self.assertEqual(value.environment, 'testnet')
        self.assertEqual(value.origin, HOST)
        self.assertNotIn(KEY, repr(value))
        self.assertNotIn(SECRET, repr(value))
        self.assertNotIn('a' * 32, repr(value))

    def test_missing_credentials_unknown_fields_wrong_purpose_and_mainnet_reject(self):
        cases = ({'api_key': ''}, {'api_secret': None}, {'api_key': True}, {'version': True},
            {'environment': 'live'}, {'origin': 'https://api.phemex.com'}, {'purpose': 'offline-readonly-worker-v1'},
            {'PHEMEX_API_KEY': 'ENV_SENTINEL'}, {'profile_id': '../secret'}, {'api_key': 'bad\nheader'})
        for changes in cases:
            with self.assertRaises(SafetyError) as error:
                parse_testnet_profile(profile_bytes(**changes))
            self.assertEqual(str(error.exception), 'Invalid dedicated Testnet credential profile; details withheld')
        with self.assertRaises(SafetyError):
            parse_testnet_profile(b'{"version":1}')

    def test_duplicate_profile_keys_encoding_and_size_reject(self):
        for raw in (profile_bytes().replace(b'"version": 1', b'"version": 1, "version": 1'),
                    profile_bytes().replace(b'"version": 1', br'"version": 1, "ver\u0073ion": 1'),
                    b'\xff', b'x' * (module.MAX_PROFILE_BYTES + 1), b'null'):
            with self.assertRaises(SafetyError):
                parse_testnet_profile(raw)

    def test_no_generic_environment_fallback(self):
        with patch.dict(os.environ, {'PHEMEX_API_KEY': 'MAINNET_KEY', 'PHEMEX_API_SECRET': 'MAINNET_SECRET'}):
            with self.assertRaises(SafetyError):
                parse_testnet_profile(b'{}')
            self.assertEqual(profile().api_key, KEY)

    def test_loader_gate_precedes_every_filesystem_read(self):
        with patch.object(module.os, 'open', side_effect=AssertionError('File access')) as opened:
            with self.assertRaises(SafetyError):
                load_testnet_profile('/not/read/testnet-profile.json')
        opened.assert_not_called()

    def test_fake_file_loader_and_permission_failures_in_private_temp_workspace(self):
        # Only an invented profile in a temporary test directory is read. No
        # actual credential path, environment source or OS configuration is used.
        with tempfile.TemporaryDirectory(prefix='.t1c-test-', dir=ROOT) as directory:
            path = Path(directory) / 'profile.json'
            with path.open('xb') as stream:
                stream.write(profile_bytes())
            path.chmod(0o600)
            context = self.make_context(path)
            with patch.object(module, '_require_protected_bootstrap', return_value=None), self.fixture_ancestor_permissions():
                self.assertEqual(load_testnet_profile(path,context=context).api_key, KEY)
                path.chmod(0o644)
                with self.assertRaises(SafetyError):
                    load_testnet_profile(path,context=context)

    def test_profile_id_must_match_the_durable_run(self):
        with tempfile.TemporaryDirectory(prefix='.t1c-test-',dir=ROOT) as directory:
            path = Path(directory)/'profile.json'
            path.write_bytes(profile_bytes(profile_id='c'*32))
            path.chmod(0o600)
            context = self.make_context(path)
            with patch.object(module,'_require_protected_bootstrap',return_value=None), self.fixture_ancestor_permissions():
                with self.assertRaises(SafetyError) as error:
                    load_testnet_profile(path,context=context)
            self.assertNotIn(KEY,str(error.exception))
            self.assertNotIn(SECRET,str(error.exception))

    def test_fake_loader_rejects_symlinks_hardlinks_and_missing_files(self):
        with tempfile.TemporaryDirectory(prefix='.t1c-test-', dir=ROOT) as directory:
            root = Path(directory)
            path = root / 'profile.json'
            path.write_bytes(profile_bytes())
            path.chmod(0o600)
            alias = root / 'alias.json'
            alias.symlink_to(path)
            with patch.object(module, '_require_protected_bootstrap', return_value=None), self.fixture_ancestor_permissions():
                for candidate in (alias, root / 'missing.json'):
                    with self.assertRaises(SafetyError):
                        load_testnet_profile(candidate,context=self.make_context(candidate))
                alias.unlink()
                os.link(path, alias)
                with self.assertRaises(SafetyError):
                    load_testnet_profile(path,context=self.make_context(path))

    def test_runtime_requires_explicit_profile_not_dictionary_or_environment(self):
        for value in (None, {}, KEY):
            with self.assertRaises(SafetyError):
                TestnetReadOnlyRuntime(value)

    def test_import_and_cli_help_do_not_load_profile_or_construct_client_or_session(self):
        code = '''
from contextlib import ExitStack
from unittest.mock import patch
with ExitStack() as guards:
    for target in ('socket.socket.connect', 'requests.sessions.Session.__init__',
                   'ccxt.phemex.__init__', 'config.load_settings'):
        guards.enter_context(patch(target, side_effect=AssertionError('Prohibited side effect')))
    import testnet_status_runtime
    from testnet_status import main
    main(['--help'])
'''
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_loader_rejects_group_writable_ancestor_without_changing_host_permissions(self):
        with tempfile.TemporaryDirectory(prefix='.t1c-test-', dir=ROOT) as directory:
            path = Path(directory) / 'profile.json'
            path.write_bytes(profile_bytes())
            path.chmod(0o600)
            context = self.make_context(path)
            identity = ROOT.stat()
            original = os.fstat
            def writable(fd):
                info = original(fd)
                if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                    values = list(info)
                    values[0] |= 0o020
                    return os.stat_result(values)
                return info
            with patch.object(module, '_require_protected_bootstrap', return_value=None), \
                    patch.object(module.os, 'fstat', side_effect=writable):
                with self.assertRaises(SafetyError):
                    load_testnet_profile(path,context=context)
