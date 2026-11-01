"""T1B mock HTTP contract, with network/client/credential construction denied."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from safety import SafetyError
from testnet_compatibility import Status
from testnet_status import main
from testnet_status_collection import (MAX_RESPONSE_BYTES, MockFailure, MockHttpResponse,
    MockResponseProvider, collect_status, mock_demo)
from testnet_transport import OfflineGetTransport, PLAN, PlannedGet, decode_response_json
from tests.compatibility_fixtures import FAKE_ACCOUNT, FAKE_USER, NOW, duplicate_json_cases, responses
from tests.helpers import OfflineTestCase


ROOT = Path(__file__).resolve().parent.parent
CHILD_GUARDS = '''
from unittest.mock import patch
from contextlib import ExitStack
import sys
with ExitStack() as guards:
    for target in ('socket.socket.connect', 'requests.sessions.Session.__init__',
                   'ccxt.phemex.__init__', 'config.load_settings'):
        guards.enter_context(patch(target, side_effect=AssertionError('Sensitive path prohibited')))
    BODY
'''


def transcript(data=None, *, raw=None, statuses=None):
    data = responses() if data is None else data
    raw, statuses = raw or {}, statuses or {}
    entries = []
    for entry in PLAN:
        path = '/' + ('v1/' if entry.api == 'v1' else '') + entry.path
        body = raw.get(entry.label, json.dumps(data[path]).encode())
        entries.append(MockHttpResponse(entry.label, body, entry.url, statuses.get(entry.label, 200)))
    return MockResponseProvider(tuple(entries))


class StatusTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        # Neither fake credentials nor a real CCXT/Requests object are needed.
        for target in ('config.load_settings', 'ccxt.phemex.__init__',
                       'requests.sessions.Session.__init__', 'operation_journal.OperationJournal.__init__',
                       'operation_journal.OperationJournal.resolve', 'testnet_readonly_worker.serve_once',
                       'readonly_ipc.OfflineIPC.handle'):
            guard = patch(target, side_effect=AssertionError('Sensitive path prohibited'))
            guard.start()
            self.addCleanup(guard.stop)

    def report(self, provider=None, **options):
        return collect_status(provider if provider is not None else transcript(),
                              clock=options.get('clock', lambda: NOW))

    def rows(self, report):
        return {row.name: row for row in report.requirements}

    def test_complete_mock_status_without_client_session_credentials_or_journal(self):
        provider = transcript()
        report = self.report(provider)
        self.assertTrue(report.collection_complete)
        self.assertTrue(report.complete)
        self.assertEqual(provider.attempts, tuple(entry.label for entry in PLAN))
        self.assertEqual(tuple(item.source for item in report.endpoints), tuple('GET ' + p.url for p in PLAN))
        rows = self.rows(report)
        expected = {'Long.observed_base_quantity': '0.002', 'Short.observed_base_quantity': '0',
                    'Long.margin_mode': 'cross', 'Short.margin_mode': 'isolated',
                    'Long.configured_leverage': '15', 'Short.configured_leverage': '3',
                    'instrument.contract_size': '1', 'instrument.quantity_step': '0.001',
                    'instrument.price_tick': '0.1'}
        for name, value in expected.items():
            self.assertEqual(rows[name].status, Status.VERIFIED)
            self.assertEqual(rows[name].value, value)

    def test_complete_status_does_not_claim_full_account_verification(self):
        report = self.report()
        rows = self.rows(report)
        for name in ('account.balance', 'account.equity', 'account.available_margin', 'accounting_type',
                     'stable_account_binding', 'open_and_conditional_order_coverage',
                     'instrument.minimum_order_size'):
            self.assertEqual(rows[name].status, Status.UNKNOWN)
        self.assertIn('Full account verification: NOT ESTABLISHED', report.render())

    def test_all_results_remain_synthetic_and_non_authoritative(self):
        report = self.report()
        self.assertTrue(report.synthetic)
        self.assertFalse(report.authenticated_evidence_verified)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.may_resolve_journal)
        with self.assertRaises(FrozenInstanceError):
            report.requirements = ()
        with self.assertRaises(AttributeError):
            report.synthetic = False

    def test_native_minimum_is_not_invented_even_if_fixture_claims_accounting(self):
        data = responses()
        data['/public/products']['data']['perpProductsV2'][0]['minOrderQtyRq'] = '0.001'
        data['/g-accounts/positions']['data']['account']['riskMode'] = 'Classic'
        report = self.report(transcript(data))
        rows = self.rows(report)
        self.assertEqual(rows['instrument.minimum_order_size'].status, Status.UNKNOWN)
        self.assertEqual(rows['accounting_type'].status, Status.UNKNOWN)

    def test_raw_balance_fields_are_never_relabelled_available_funds(self):
        report = self.report()
        text = report.render()
        self.assertEqual(self.rows(report)['account.balance'].value, '-')
        self.assertNotIn('wallet balance - used', text)

    def test_missing_long_or_short_is_unknown_not_flat(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            for leg in ('Long', 'Short'):
                data = responses()
                data[path]['data']['positions'] = [p for p in data[path]['data']['positions'] if p['posSide'] != leg]
                rows = self.rows(self.report(transcript(data)))
                self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
                self.assertEqual(rows[leg + '.observed_base_quantity'].value, '-')

    def test_missing_quantity_does_not_hide_contradictory_mode(self):
        data = responses()
        row = data['/g-accounts/accountPositions']['data']['positions'][0]
        row.pop('sizeRq')
        row['posMode'] = 'OneWay'
        rows = self.rows(self.report(transcript(data)))
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.UNKNOWN)

    def test_overlapping_fields_crosschecked_individually(self):
        for field, value, dependent in (('leverageRr', '-20', 'Long.configured_leverage'),
            ('crossMargin', False, 'Long.margin_mode'), ('positionStatus', 'Liq', 'Long.status'),
            ('sizeRq', '0.003', 'Long.observed_base_quantity')):
            data = responses()
            data['/g-accounts/accountPositions']['data']['positions'][0][field] = value
            rows = self.rows(self.report(transcript(data)))
            self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
            self.assertEqual(rows[dependent].status, Status.UNKNOWN)

    def test_missing_fields_are_not_agreement(self):
        for field, dependent in (('leverageRr', 'Long.configured_leverage'),
                                 ('positionStatus', 'Long.status'), ('sizeRq', 'Long.observed_base_quantity')):
            data = responses()
            for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
                data[path]['data']['positions'][0].pop(field)
            rows = self.rows(self.report(transcript(data)))
            self.assertEqual(rows[dependent].status, Status.UNKNOWN)

    def test_wrong_account_currency_invalidates_position_scope(self):
        data = responses()
        data['/g-accounts/accountPositions']['data']['account']['currency'] = 'BTC'
        rows = self.rows(self.report(transcript(data)))
        self.assertEqual(rows['authentication'].status, Status.VERIFIED)
        self.assertEqual(rows['position_account_scope'].status, Status.INCOMPATIBLE)
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.UNKNOWN)

    def test_missing_or_mismatched_identifiers_do_not_establish_stable_identity(self):
        for value in (None, '999999999999'):
            data = responses()
            data['/g-accounts/accountPositions']['data']['account']['accountId'] = value
            rows = self.rows(self.report(transcript(data)))
            self.assertEqual(rows['snapshot_identity_consistency'].status, Status.UNKNOWN)
            self.assertEqual(rows['stable_account_binding'].status, Status.UNKNOWN)

    def test_one_way_is_incompatible_with_hedge_requirement(self):
        data = responses()
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            row = data[path]['data']['positions'][0]
            row.update(posMode='OneWay', posSide='Merged')
            data[path]['data']['positions'] = [row]
        report = self.report(transcript(data))
        self.assertEqual(self.rows(report)['explicit_hedge_legs'].status, Status.INCOMPATIBLE)
        self.assertFalse(report.complete)

    def test_paginated_positions_are_incomplete(self):
        data = responses()
        data['/g-accounts/positions']['data']['nextPageArg'] = 'MORE_PRIVATE_DATA'
        rows = self.rows(self.report(transcript(data)))
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)

    def test_duplicate_legs_and_malformed_rows_do_not_verify_hedge(self):
        for extra in ({'symbol': 'BTCUSDT', 'posSide': 'Long', 'posMode': 'Hedged'}, None, {}):
            data = responses()
            data['/g-accounts/accountPositions']['data']['positions'].append(extra)
            self.assertEqual(self.rows(self.report(transcript(data)))['explicit_hedge_legs'].status, Status.UNKNOWN)

    def test_invalid_native_numeric_fields_are_unknown_without_default(self):
        for field, output in (('contractSize', 'contract_size'), ('qtyStepSize', 'quantity_step'),
                              ('tickSize', 'price_tick'), ('maxOrderQtyRq', 'max_base_quantity')):
            for value in (None, '-1', 'NaN', True, '1e100'):
                data = responses()
                data['/public/products']['data']['perpProductsV2'][0][field] = value
                rows = self.rows(self.report(transcript(data)))
                self.assertEqual(rows['instrument.' + output].status, Status.UNKNOWN)

    def test_absent_native_contract_size_does_not_invent_ccxt_convention(self):
        data = responses()
        data['/public/products']['data']['perpProductsV2'][0].pop('contractSize')
        report = self.report(transcript(data))
        self.assertEqual(self.rows(report)['instrument.contract_size'].status, Status.UNKNOWN)
        self.assertFalse(report.complete)

    def test_invalid_public_scope_stops_before_private_collection(self):
        for key, value in (('settleCurrency', 'BTC'), ('baseCurrency', 'ETH'), ('type', 'Spot')):
            data = responses()
            data['/public/products']['data']['perpProductsV2'][0][key] = value
            provider = transcript(data)
            report = self.report(provider)
            self.assertEqual(provider.attempts, ('products', 'legacy_products'))
            self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)

    def test_duplicate_btc_instruments_fail_closed(self):
        data = responses()
        rows = data['/public/products']['data']['perpProductsV2']
        rows.append(dict(rows[0]))
        provider = transcript(data)
        report = self.report(provider)
        self.assertFalse(report.complete)
        self.assertEqual(len(provider.attempts), 2)

    def test_companion_metadata_contradiction_fails_closed(self):
        for companion in ([{'symbol': 'BTCUSDT', 'tickSize': '2'}], [None],
                          [{'symbol': 'BTCUSDT'}, {'symbol': 'BTCUSDT'}]):
            data = responses()
            data['/v1/exchange/public/products']['data'] = companion
            provider = transcript(data)
            self.assertFalse(self.report(provider).complete)
            self.assertEqual(len(provider.attempts), 2)

    def test_inactive_instrument_is_incompatible_not_active(self):
        data = responses()
        data['/public/products']['data']['perpProductsV2'][0]['status'] = 'Delisted'
        self.assertEqual(self.rows(self.report(transcript(data)))['instrument.active'].status, Status.INCOMPATIBLE)

    def test_decimal_json_literals_preserved_before_float_rounding(self):
        precise = b'0.00200000000000000000000000000000000001'
        tick = b'0.10000000000000000000000000000000000001'
        raw = {}
        for entry, response in zip(PLAN, transcript()._responses):
            body = response.body
            if entry.label == 'products':
                body = body.replace(b'"tickSize": "0.1"', b'"tickSize": ' + tick)
            if entry.api == 'private':
                body = body.replace(b'"sizeRq": "0.002"', b'"sizeRq": ' + precise)
            raw[entry.label] = body
        rows = self.rows(self.report(transcript(raw=raw)))
        self.assertEqual(rows['Long.observed_base_quantity'].value, precise.decode())
        self.assertEqual(rows['instrument.price_tick'].value, tick.decode())
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.VERIFIED)
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)  # off grid, never rounded

    def test_exact_decoder_handles_nested_exponents_without_changing_integer_codes(self):
        value = decode_response_json(b'{"code":0,"items":[{"x":1.2345678901234567890123456789e-4}]}', exact_numbers=True)
        self.assertIs(type(value['code']), int)
        self.assertEqual(value['items'][0]['x'], Decimal('1.2345678901234567890123456789e-4'))
        self.assertIs(type(value['items'][0]['x']), Decimal)
        self.assertIs(type(decode_response_json(b'{"x":1.2}')['x']), float)  # old API unchanged

    def test_nonstandard_json_numbers_are_rejected(self):
        for literal in (b'NaN', b'Infinity', b'-Infinity'):
            with self.assertRaises(SafetyError):
                decode_response_json(b'{"x":' + literal + b'}', exact_numbers=True)

    def test_all_duplicate_key_byte_fixtures_stop_without_retry_or_sensitive_echo(self):
        for name, path, raw in duplicate_json_cases():
            label = next(entry.label for entry in PLAN if entry.url.split('?')[0].endswith(path))
            provider = transcript(raw={label: raw})
            report = self.report(provider)
            with self.subTest(case=name):
                self.assertFalse(report.complete)
                self.assertEqual(len(provider.attempts), next(i + 1 for i, p in enumerate(PLAN) if p.label == label))
                self.assertNotIn(raw.decode(), report.render())
                self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_malformed_encoding_and_bodies_are_sanitized(self):
        for raw in (b'\xff', b'{"code":0,"data":', b'null', b'[]', b'{} trailing',
                    b'{"code":true,"data":{}}', b'{"code":0.0,"data":{}}'):
            provider = transcript(raw={'account_positions': raw})
            report = self.report(provider)
            self.assertEqual(len(provider.attempts), 3)
            self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)
            self.assertFalse(report.complete)

    def test_private_response_without_account_or_position_structure_is_not_authentication(self):
        for body in ({}, {'account': {}}, {'positions': []}, {'account': {}, 'positions': None}):
            data = responses()
            data['/g-accounts/positions']['data'] = body
            report = self.report(transcript(data))
            self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)
            self.assertFalse(report.endpoints[3].attempted)

    def test_only_http_200_supports_success_even_with_code_zero(self):
        for status in (101, 199, 200, 204, 301, 302, 400, 500):
            report = self.report(transcript(statuses={'account_positions': status}))
            self.assertEqual(self.rows(report)['authentication'].status,
                             Status.VERIFIED if status == 200 else Status.UNKNOWN)
            self.assertEqual(report.endpoints[3].attempted, status == 200)

    def test_authentication_errors_do_not_leak_private_message_or_continue(self):
        data = responses()
        data['/g-accounts/positions'] = {'code': 10500, 'msg': 'Failed to find api-key PRIVATE_SECRET'}
        report = self.report(transcript(data, statuses={'account_positions': 401}))
        self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)
        self.assertIn('category=authentication', report.render())
        self.assertNotIn('PRIVATE_SECRET', report.render())
        self.assertFalse(report.endpoints[3].attempted)

    def test_auth_failure_after_primary_success_invalidates_authentication(self):
        report = self.report(transcript(statuses={'identity_crosscheck': 401}))
        self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)

    def test_crosscheck_server_failure_preserves_independent_modeled_authentication(self):
        data = responses()
        data['/g-accounts/accountPositions'] = {'code': 10500, 'msg': 'Please try again later PRIVATE_SECRET'}
        report = self.report(transcript(data, statuses={'identity_crosscheck': 500}))
        self.assertEqual(self.rows(report)['authentication'].status, Status.VERIFIED)
        self.assertEqual(self.rows(report)['explicit_hedge_legs'].status, Status.UNKNOWN)
        self.assertIn('http=500; code=10500', report.render())
        self.assertNotIn('PRIVATE_SECRET', report.render())
        self.assertFalse(report.collection_complete)

    def test_permission_rate_limit_network_and_server_errors_stop(self):
        for status in (403, 429, 500):
            provider = transcript(statuses={'account_positions': status})
            self.assertFalse(self.report(provider).complete)
            self.assertEqual(len(provider.attempts), 3)
        for failure in MockFailure:
            provider = transcript()
            entries = list(provider._responses)
            entries[2] = replace(entries[2], failure=failure)
            provider = MockResponseProvider(tuple(entries))
            self.assertFalse(self.report(provider).complete)
            self.assertEqual(len(provider.attempts), 3)

    def test_stale_private_observations_do_not_verify_current_state(self):
        times = iter([NOW] * 9 + [NOW + timedelta(seconds=5)])
        report = self.report(clock=lambda: next(times))
        self.assertEqual(self.rows(report)['authentication'].status, Status.UNKNOWN)
        self.assertEqual(self.rows(report)['Long.observed_base_quantity'].status, Status.UNKNOWN)
        self.assertFalse(report.complete)

    def test_stale_public_metadata_does_not_erase_fresh_private_raw_quantities(self):
        times = iter([NOW] * 5 + [NOW + timedelta(seconds=300)] * 5)
        rows = self.rows(self.report(clock=lambda: next(times)))
        self.assertEqual(rows['instrument.quantity_step'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.VERIFIED)

    def test_bad_or_backwards_clocks_fail_closed(self):
        for clock in (lambda: None, lambda: NOW.replace(tzinfo=None)):
            with self.assertRaises(SafetyError):
                self.report(clock=clock)
        times = iter([NOW, NOW, NOW - timedelta(seconds=1), NOW])
        report = self.report(clock=lambda: next(times))
        self.assertFalse(report.complete)
        self.assertIn('clock is inconsistent', report.render())

    def test_reports_show_fixed_sources_and_collection_intervals(self):
        provider, clock = mock_demo()
        report = self.report(provider, clock=clock)
        for endpoint, planned in zip(report.endpoints, PLAN):
            self.assertEqual(endpoint.source, 'GET ' + planned.url)
            self.assertLess(endpoint.started_at, endpoint.completed_at)
            self.assertEqual(endpoint.provenance, 'synthetic_http_fixture')

    def test_report_omits_account_identifiers_raw_payloads_and_false_transport_claims(self):
        text = self.report().render()
        for forbidden in (FAKE_ACCOUNT, FAKE_USER, 'accountBalanceRv', 'x-phemex-access-token',
                          'x-phemex-request-signature', 'Pinned sandbox URLs checked'):
            self.assertNotIn(forbidden, text)
        self.assertIn('T1B MOCK REPORT', text)
        self.assertIn('BTC base units', text)

    def test_wrong_response_url_label_and_redirects_are_rejected(self):
        for changes in ({'url': 'https://api.phemex.com/public/products'},
                        {'url': PLAN[0].url + '?x=1'}, {'label': 'g-orders'}, {'redirected': True}):
            entries = list(transcript()._responses)
            entries[0] = replace(entries[0], **changes)
            provider = MockResponseProvider(tuple(entries))
            self.assertFalse(self.report(provider).complete)
            self.assertEqual(provider.attempts, ('products',))

    def test_provider_rejects_url_method_and_endpoint_substitution(self):
        provider = transcript()
        provider.begin()
        for value in ('https://api.phemex.com', PlannedGet('private', 'g-orders', (), 'order'),
                      replace(PLAN[0], query=(('extra', 'value'),)),
                      replace(PLAN[0], path='public/%70roducts')):
            with self.assertRaises(SafetyError):
                provider.get(value)
        with self.assertRaises(TypeError):
            provider.get(PLAN[0], method='POST')
        self.assertEqual(provider.attempts, ())
        provider.end()

    def test_provider_is_single_use_after_success_failure_and_missing_response(self):
        for provider in (transcript(), transcript(statuses={'products': 500}), MockResponseProvider(())):
            self.report(provider)
            attempts = provider.attempts
            with self.assertRaises(SafetyError):
                self.report(provider)
            self.assertEqual(provider.attempts, attempts)

    def test_budget_and_out_of_order_access_are_rejected(self):
        provider = transcript()
        provider.begin()
        with self.assertRaises(SafetyError):
            provider.get(PLAN[1])
        for entry in PLAN:
            provider.get(entry)
        for entry in PLAN:
            with self.assertRaises(SafetyError):
                provider.get(entry)
        provider.end()

    def test_arbitrary_callable_or_subclass_provider_is_not_accepted(self):
        class Subclass(MockResponseProvider):
            pass
        for provider in (lambda entry: None, {}, Subclass(())):
            with self.assertRaises(SafetyError):
                self.report(provider)
        with self.assertRaises(SafetyError):
            MockResponseProvider(tuple([MockHttpResponse('x', b'{}', 'x')] * 5))

    def test_concurrent_collection_cannot_reuse_same_mock_provider(self):
        provider = transcript()
        def run():
            try:
                return self.report(provider).complete
            except SafetyError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: run(), range(2))), [False, True])
        self.assertEqual(len(provider.attempts), 4)

    def test_response_size_is_bounded(self):
        report = self.report(transcript(raw={'products': b' ' * (MAX_RESPONSE_BYTES + 1)}))
        self.assertFalse(report.complete)
        self.assertFalse(report.endpoints[1].attempted)

    def test_existing_send_boundary_remains_unconditionally_blocked(self):
        with self.assertRaises(SafetyError):
            OfflineGetTransport()._send_http(None, timeout=15, verify=True, allow_redirects=False, proxies={})

    def test_imports_and_help_do_not_construct_sensitive_objects(self):
        code = CHILD_GUARDS.replace('BODY', 'import testnet_status, testnet_status_collection')
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(['--help'])
        self.assertEqual(result.exception.code, 0)

    def test_cli_demo_succeeds_without_network(self):
        out = io.StringIO()
        with redirect_stdout(out):
            result = main(['status', '--mock-demo'])
        self.assertEqual(result, 0)
        self.assertIn('T1B MOCK REPORT', out.getvalue())

    def test_cli_invalid_commands_have_nonzero_exit_codes(self):
        for args in ([], ['status'], ['status', '--mock'], ['status', '--mock-demo', '--live'],
                     ['status', '--mock-demo', '--url', PLAN[0].url], ['account']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                main(args)
            self.assertEqual(result.exception.code, 2)

    def test_cli_partial_report_and_exceptions_are_nonzero_and_sanitized(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('testnet_status_collection.mock_demo', return_value=(MockResponseProvider(()), lambda: NOW)), \
                redirect_stdout(out):
            self.assertEqual(main(['status', '--mock-demo']), 2)
        self.assertIn('PARTIAL/FAILED', out.getvalue())
        with patch('testnet_status_collection.mock_demo', side_effect=RuntimeError('PRIVATE_SECRET')), \
                redirect_stderr(err):
            self.assertEqual(main(['status', '--mock-demo']), 2)
        self.assertNotIn('PRIVATE_SECRET', err.getvalue())

    def test_cli_process_codes_for_help_invalid_and_demo(self):
        for args, expected in ((['--help'], 0), (['status'], 2), (['status', '--mock-demo'], 0)):
            body = 'from testnet_status import main; raise SystemExit(main(' + repr(args) + '))'
            result = subprocess.run([sys.executable, '-B', '-c', CHILD_GUARDS.replace('BODY', body)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected)
