from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import ccxt

from diagnostics import ResponseEvidence
from execution_policy import evaluate
from readonly_exchange import READ_ROUTES, ReadOnlyPhemex
from safety import SafetyError
from testnet_compatibility import (AUTHORIZATION, ROUTES, CompatibilityVerifier, Status,
                                   VerificationPhemex)
from tests.compatibility_fixtures import (FAKE_ACCOUNT, FAKE_USER, PRODUCT, collect,
                                         duplicate_json_cases, fixture)
from tests.execution_fixtures import evidence, NOW
from tests.helpers import MARKET, OfflineTestCase


class CompatibilityTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.verifier, self.ex, self.data, self.now = fixture()

    def report(self):
        return collect(self.verifier)

    def requirements(self, report=None):
        return {row.name: row for row in (report or self.report()).requirements}

    def server_error(self):
        self.ex.last_read_diagnostic = ResponseEvidence(500, '10500', 'retry_later')
        raise ccxt.ExchangeError('FIXTURE_SECRET private signed headers')

    def test_duplicate_response_members_never_verify_affected_evidence_or_retry(self):
        for name, path, raw in duplicate_json_cases():
            verifier, exchange, _, _ = fixture()
            original = exchange._transport._send_http.side_effect
            def reply(prepared, **options):
                result = original(prepared, **options)
                if urlparse(prepared.url).path == path:
                    result._content = raw
                return result
            exchange._transport._send_http.side_effect = reply
            with self.subTest(case=name), \
                 patch('operation_journal.OperationJournal', side_effect=AssertionError('Journal access')) as journal, \
                 patch('ccxt.phemex.create_order', side_effect=AssertionError('Mutation')) as mutation:
                report = collect(verifier)
                journal.assert_not_called()
                mutation.assert_not_called()
                rows = self.requirements(report)
                expected_count = {'/public/products': 1, '/v1/exchange/public/products': 2,
                                  '/g-accounts/positions': 3, '/g-accounts/accountPositions': 4}[path]
                self.assertEqual(exchange._transport._send_http.call_count, expected_count)
                self.assertFalse(report.execution_authorized)
                self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
                self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
                for leg in ('Long', 'Short'):
                    self.assertEqual(rows[leg + '.observed_base_quantity'].status, Status.UNKNOWN)
                    self.assertEqual(rows[leg + '.base_quantity'].status, Status.UNKNOWN)
                if path in ('/public/products', '/v1/exchange/public/products'):
                    self.assertTrue(all(row.status is Status.UNKNOWN for row in report.requirements))
                elif path == '/g-accounts/positions':
                    self.assertEqual(rows['authentication'].status, Status.UNKNOWN)
                else:
                    # An independent successful primary GET can still establish
                    # authentication, never crosschecked position compatibility.
                    self.assertEqual(rows['authentication'].status, Status.VERIFIED)
                for value in ('FIXTURE_SECRET', 'FIXTURE_ACCESS_TOKEN', 'PRIVATE_SENTINEL',
                              FAKE_ACCOUNT, FAKE_USER, raw.decode()):
                    self.assertNotIn(value, report.render())
                exchange._reads_authorized = True
                api, endpoint = ('v1', 'exchange/public/products') if path.startswith('/v1/') else (
                    ('v2', 'public/products') if path == '/public/products' else ('private', path[1:]))
                with self.assertRaises(SafetyError):
                    exchange.request(endpoint, api, params={'currency': 'USDT'} if api == 'private' else {})
                self.assertEqual(exchange._transport._send_http.call_count, expected_count)

    def test_malformed_raw_private_json_remains_unknown_without_authority(self):
        for raw in (b'{"code":0,"data":', b'{"code":0,"data":{}} trailing', b'\xff'):
            verifier, exchange, _, _ = fixture()
            original = exchange._transport._send_http.side_effect
            def reply(prepared, **options):
                result = original(prepared, **options)
                if urlparse(prepared.url).path == '/g-accounts/positions':
                    result._content = raw
                return result
            exchange._transport._send_http.side_effect = reply
            with patch('operation_journal.OperationJournal', side_effect=AssertionError('Journal access')) as journal:
                report = collect(verifier)
                journal.assert_not_called()
            self.assertEqual(self.requirements(report)['authentication'].status, Status.UNKNOWN)
            self.assertFalse(report.execution_authorized)
            self.assertEqual(exchange._transport._send_http.call_count, 3)

    def test_successful_synthetic_snapshot_is_never_execution_authority(self):
        report = self.report()
        rows = self.requirements(report)
        for name in ('authentication', 'testnet_endpoint_profile', 'snapshot_identity_consistency',
                     'explicit_hedge_legs', 'position_crosscheck', 'Long.base_quantity', 'Short.base_quantity',
                     'Long.configured_leverage', 'Short.margin_mode', 'instrument.quantity_step'):
            self.assertEqual(rows[name].status, Status.VERIFIED, name)
        self.assertEqual(rows['Long.margin_mode'].value, 'cross')
        self.assertEqual(rows['Short.margin_mode'].value, 'isolated')
        self.assertEqual(rows['Long.configured_leverage'].value, '15')
        self.assertEqual(rows['Short.reducible_position'].status, Status.INCOMPATIBLE)
        for name in ('stable_account_binding', 'instrument.minimum_order_size',
                     'reduce_only_limit_IOC_acceptance', 'execution_assessment'):
            self.assertEqual(rows[name].status, Status.UNKNOWN, name)
        self.assertFalse(report.execution_authorized)
        self.assertTrue(report.synthetic)
        self.assertIn('NOT EXCHANGE VERIFICATION', report.render())

    def test_report_contains_no_identifiers_credentials_headers_or_private_extras(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            self.data[path]['data']['raw_private_extra'] = 'PRIVATE_SENTINEL'
        report = self.report()
        text = report.render() + repr(report)
        for secret in (FAKE_ACCOUNT, FAKE_USER, 'FIXTURE_ACCESS_TOKEN', 'FIXTURE_SECRET',
                       'x-phemex-request-signature', 'PRIVATE_SENTINEL'):
            self.assertNotIn(secret, text)

    def test_no_authorization_or_invalid_phrase_makes_any_request(self):
        for authorization in (None, 'yes', 'AUTHORIZE', 'FIXTURE_SECRET'):
            with self.subTest(authorization=authorization), self.assertRaises(SafetyError):
                self.verifier.collect(authorization=authorization)
        self.ex.fetch.assert_not_called()

    def test_phase_a_cannot_label_mocked_reads_as_real_observations(self):
        with self.assertRaises(SafetyError):
            self.verifier.collect(authorization=AUTHORIZATION, synthetic=False)
        self.ex.fetch.assert_not_called()

    def test_production_transport_is_unconditionally_blocked_in_phase_a(self):
        ex = VerificationPhemex('FIXTURE_ACCESS_TOKEN', 'FIXTURE_SECRET')
        with patch('ccxt.phemex.fetch', side_effect=AssertionError('Real transport')) as transport:
            report = collect(CompatibilityVerifier(ex, clock=lambda: NOW))
        transport.assert_not_called()
        self.assertTrue(all(row.status is Status.UNKNOWN for row in report.requirements))
        self.assertIn('private GETs were not attempted', report.render())

    def test_live_changed_or_malformed_endpoint_rejected_before_signing(self):
        for changed in ({'private': 'https://api.phemex.com'}, {'v2': 'http://testnet-api.phemex.com'},
                        {'v1': 'https://testnet-api.phemex.com.evil/v1'}, {'public': 'https://user:pass@testnet-api.phemex.com/exchange/public'}):
            verifier, ex, _, _ = fixture()
            ex.urls['api'].update(changed)
            with patch.object(ex, 'sign', side_effect=AssertionError('Signing')) as sign:
                with self.assertRaises(SafetyError): collect(verifier)
            sign.assert_not_called()
            ex.fetch.assert_not_called()

    def test_wrong_ccxt_or_disabled_sandbox_rejected_before_requests(self):
        with patch('ccxt.__version__', 'unknown'), self.assertRaises(SafetyError): self.report()
        self.ex.isSandboxModeEnabled = False
        with self.assertRaises(SafetyError): self.report()
        self.ex.fetch.assert_not_called()

    def test_only_existing_allowlisted_routes_fixed_parameters_and_get_are_accepted(self):
        self.assertTrue(set(ROUTES).issubset(READ_ROUTES))
        self.ex._reads_authorized = True
        cases = [('g-orders', ['private'], 'POST', {}), ('g-orders/create', ['private'], 'GET', {}),
                 ('g-accounts/positions', ['private'], 'GET', {'currency': 'BTC'}),
                 ('g-accounts/positions', ['private'], 'GET', {'currency': 'USDT', 'extra': 'FIXTURE_SECRET'}),
                 ('g-accounts/positions', ['private'], 'DELETE', {'currency': 'USDT'}),
                 ('uta-account/switch-mode', ['private'], 'GET', {}),
                 ('public/products', ['private'], 'GET', {}), ('public/products', [], 'GET', {}),
                 ('public/products', ['v2'], 'GET', [])]
        for path, api, method, params in cases:
            with self.subTest(path=path, method=method), self.assertRaises(SafetyError):
                self.ex.request(path, api, method, params)
        self.ex.fetch.assert_not_called()

    def test_one_attempt_per_endpoint_and_one_collection_per_verifier(self):
        self.report()
        urls = [call.args[0] for call in self.ex.fetch.call_args_list]
        self.assertEqual(len(urls), len(set(urls)))
        before = self.ex.fetch.call_count
        with self.assertRaises(SafetyError): self.report()
        self.ex._reads_authorized = True
        with self.assertRaises(SafetyError): self.ex.privateGetGAccountsPositions({'currency': 'USDT'})
        self.assertEqual(self.ex.fetch.call_count, before)

    def test_successful_authentication_without_identity_is_not_binding(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            self.data[path]['data']['account'].pop('userID')
        rows = self.requirements()
        self.assertEqual(rows['authentication'].status, Status.VERIFIED)
        self.assertEqual(rows['snapshot_identity_consistency'].status, Status.UNKNOWN)
        self.assertEqual(rows['stable_account_binding'].status, Status.UNKNOWN)

    def test_identity_mismatches_or_alias_conflicts_are_unknown_and_hidden(self):
        for kind in ('account', 'user', 'position', 'alias'):
            verifier, _, data, _ = fixture()
            row = data['/g-accounts/accountPositions']['data']
            if kind == 'account': row['account']['accountId'] = '999999999999'
            if kind == 'user': row['account']['userID'] = '999999999999'
            if kind == 'position': row['positions'][0]['accountID'] = '999999999999'
            if kind == 'alias': row['account']['accountID'] = '999999999999'
            report = collect(verifier)
            rows = self.requirements(report)
            self.assertEqual(rows['snapshot_identity_consistency'].status, Status.UNKNOWN)
            self.assertNotIn('999999999999', report.render())

    def test_missing_long_or_short_is_unknown_not_zero(self):
        for index in (0, 1):
            verifier, _, data, _ = fixture()
            for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
                data[path]['data']['positions'].pop(index)
            rows = self.requirements(collect(verifier))
            self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
            self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
            self.assertEqual(rows['Short.base_quantity'].status, Status.UNKNOWN)

    def test_contradictory_hedge_modes_duplicate_legs_and_direction_are_unknown(self):
        for changes in ({'posMode': 'OneWay'}, {'posSide': 'Long'}, {'side': 'Buy', 'sizeRq': '0.001'}):
            verifier, _, data, _ = fixture()
            data['/g-accounts/positions']['data']['positions'][1].update(changes)
            rows = self.requirements(collect(verifier))
            self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)

    def test_disagreement_between_independent_position_reads_invalidates_position_context(self):
        self.data['/g-accounts/accountPositions']['data']['positions'][0]['sizeRq'] = '0.001'
        rows = self.requirements()
        self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)

    def test_known_one_way_is_incompatible_with_hedge_scope(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            row = self.data[path]['data']['positions'][0]
            row.update(posSide='Merged', posMode='OneWay')
            self.data[path]['data']['positions'] = [row]
        self.assertEqual(self.requirements()['explicit_hedge_legs'].status, Status.INCOMPATIBLE)

    def test_partial_crosscheck_cannot_hide_contradictory_mode_or_leg(self):
        for changes in ({'posMode': 'OneWay'}, {'posSide': 'Merged'},
                        {'posMode': 'OneWay', 'posSide': 'Merged'}):
            verifier, _, data, _ = fixture()
            crosscheck = data['/g-accounts/accountPositions']['data']
            crosscheck['positions'][0].update(sizeRq=None, **changes)
            with self.subTest(changes=changes):
                rows = self.requirements(collect(verifier))
                for name in ('position_crosscheck', 'explicit_hedge_legs', 'Long.base_quantity',
                             'Long.reducible_position'):
                    self.assertEqual(rows[name].status, Status.UNKNOWN, name)
                self.assertEqual(rows['instrument.quantity_step'].status, Status.VERIFIED)

    def test_partial_crosscheck_preserves_independently_matching_fields(self):
        self.data['/g-accounts/accountPositions']['data']['positions'][0].pop('sizeRq')
        rows = self.requirements()
        self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.reducible_position'].status, Status.UNKNOWN)
        for name in ('explicit_hedge_legs', 'Long.margin_mode', 'Long.configured_leverage',
                     'Long.status', 'Short.base_quantity'):
            self.assertEqual(rows[name].status, Status.VERIFIED, name)

    def test_crosscheck_compares_leverage_margin_and_status_individually(self):
        for changes, invalid in (({'leverageRr': '-7'}, ('configured_leverage',)),
                                 ({'leverageRr': '15'}, ('margin_mode',)),
                                 ({'positionStatus': 'Liq'}, ('status', 'reducible_position')),
                                 ({'crossMargin': False}, ('margin_mode',))):
            verifier, _, data, _ = fixture()
            data['/g-accounts/accountPositions']['data']['positions'][0].update(changes)
            rows = self.requirements(collect(verifier))
            with self.subTest(changes=changes):
                self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
                for key in invalid:
                    self.assertEqual(rows['Long.' + key].status, Status.UNKNOWN, key)
                self.assertEqual(rows['Short.configured_leverage'].status, Status.VERIFIED)

    def test_wrong_account_currency_cannot_establish_usdt_positions(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            verifier, _, data, _ = fixture()
            data[path]['data']['account']['currency'] = 'BTC'
            rows = self.requirements(collect(verifier))
            with self.subTest(path=path):
                self.assertEqual(rows['position_account_scope'].status, Status.INCOMPATIBLE)
                for name in ('position_crosscheck', 'explicit_hedge_legs', 'Long.base_quantity',
                             'Long.margin_mode', 'Short.configured_leverage'):
                    self.assertEqual(rows[name].status, Status.UNKNOWN, name)
                self.assertEqual(rows['authentication'].status, Status.VERIFIED)
                self.assertEqual(rows['instrument.quantity_step'].status, Status.VERIFIED)

    def test_missing_crosscheck_fields_never_count_as_agreement(self):
        for native, affected in (('leverageRr', ('configured_leverage', 'margin_mode')),
                                 ('crossMargin', ()),
                                 ('positionStatus', ('status', 'reducible_position')),
                                 ('posMode', ('base_quantity', 'margin_mode', 'configured_leverage')),
                                 ('posSide', ('base_quantity', 'margin_mode', 'configured_leverage')),
                                 ('side', ('base_quantity', 'margin_mode', 'configured_leverage'))):
            verifier, _, data, _ = fixture()
            row = data['/g-accounts/accountPositions']['data']['positions'][0]
            row.pop(native, None)
            rows = self.requirements(collect(verifier))
            with self.subTest(native=native):
                for key in affected:
                    self.assertEqual(rows['Long.' + key].status, Status.UNKNOWN, key)
                # crossMargin is optional when a valid leverage sign establishes margin.
                expected = Status.VERIFIED if native == 'crossMargin' else Status.UNKNOWN
                self.assertEqual(rows['position_crosscheck'].status, expected)

    def test_missing_primary_quantity_preserves_matching_mode_leverage_and_margin(self):
        self.data['/g-accounts/positions']['data']['positions'][0].pop('sizeRq')
        rows = self.requirements()
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
        for name in ('explicit_hedge_legs', 'Long.configured_leverage', 'Long.margin_mode', 'Long.status'):
            self.assertEqual(rows[name].status, Status.VERIFIED, name)

    def test_malformed_crosscheck_fields_are_unknown_without_discarding_independent_fields(self):
        for changes, unknown_keys in (({'leverageRr': []}, ('configured_leverage', 'margin_mode')),
                                      ({'leverageRr': 15.0}, ('configured_leverage', 'margin_mode')),
                                      ({'leverageRr': '1e100000'}, ('configured_leverage', 'margin_mode')),
                                      ({'crossMargin': 'true'}, ('margin_mode',)),
                                      ({'positionStatus': {}}, ('status', 'reducible_position')),
                                      ({'sizeRq': 'NaN'}, ('base_quantity', 'reducible_position')),
                                      ({'sizeRq': '0.002', 'size': '0.003'}, ('base_quantity',))):
            verifier, _, data, _ = fixture()
            data['/g-accounts/accountPositions']['data']['positions'][0].update(changes)
            rows = self.requirements(collect(verifier))
            with self.subTest(changes=changes):
                self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
                self.assertEqual(rows['explicit_hedge_legs'].status, Status.VERIFIED)
                self.assertEqual(rows['Short.base_quantity'].status, Status.VERIFIED)
                for key in unknown_keys:
                    self.assertEqual(rows['Long.' + key].status, Status.UNKNOWN, key)

    def test_quantity_aliases_are_exactly_compared_without_rounding(self):
        for value, expected in (('0.0020', Status.VERIFIED), ('0.002000000000000000000000000000000000001', Status.UNKNOWN)):
            verifier, _, data, _ = fixture()
            row = data['/g-accounts/accountPositions']['data']['positions'][0]
            row['size'] = row.pop('sizeRq')
            row['size'] = value
            rows = self.requirements(collect(verifier))
            self.assertEqual(rows['position_crosscheck'].status, expected)

    def test_partial_contradiction_with_missing_other_leg_invalidates_dependents(self):
        self.data['/g-accounts/accountPositions']['data']['positions'] = [
            {'symbol': 'BTCUSDT', 'posSide': 'Merged', 'posMode': 'OneWay'}]
        rows = self.requirements()
        for name in ('position_crosscheck', 'explicit_hedge_legs', 'Long.configured_leverage', 'Short.status'):
            self.assertEqual(rows[name].status, Status.UNKNOWN, name)

    def test_selected_row_currency_mismatch_invalidates_position_scope(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            verifier, _, data, _ = fixture()
            data[path]['data']['positions'][0]['currency'] = 'BTC'
            rows = self.requirements(collect(verifier))
            self.assertEqual(rows['position_account_scope'].status, Status.INCOMPATIBLE)
            self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
            self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)

    def test_missing_or_malformed_account_scope_is_unknown(self):
        for value in (None, [], {}, {'currency': None}, {'currency': []}, {'currency': 'PRIVATE_SENTINEL'}):
            verifier, _, data, _ = fixture()
            data['/g-accounts/accountPositions']['data']['account'] = value
            report = collect(verifier)
            rows = self.requirements(report)
            self.assertEqual(rows['position_account_scope'].status, Status.UNKNOWN)
            self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
            self.assertEqual(rows['authentication'].status, Status.VERIFIED)
            self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_inconsistent_envelope_and_scope_aliases_are_not_ignored(self):
        for target, changes, expected in (('data', {'currency': 'BTC'}, Status.INCOMPATIBLE),
                                          ('data', {'symbol': 'ETHUSDT'}, Status.INCOMPATIBLE),
                                          ('account', {'settleCurrency': 'BTC'}, Status.INCOMPATIBLE),
                                          ('account', {'quoteCurrency': 'BTC'}, Status.INCOMPATIBLE),
                                          ('row', {'settleCurrency': 'BTC'}, Status.INCOMPATIBLE),
                                          ('row', {'currency': None}, Status.UNKNOWN),
                                          ('data', {'currency': {}}, Status.UNKNOWN)):
            verifier, _, data, _ = fixture()
            envelope = data['/g-accounts/accountPositions']['data']
            obj = envelope if target == 'data' else envelope['account'] if target == 'account' else envelope['positions'][0]
            obj.update(changes)
            rows = self.requirements(collect(verifier))
            with self.subTest(target=target, changes=changes):
                self.assertEqual(rows['position_account_scope'].status, expected)
                self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
                self.assertEqual(rows['Long.margin_mode'].status, Status.UNKNOWN)

    def test_absent_row_currency_can_use_validated_account_currency(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            for row in self.data[path]['data']['positions']:
                row.pop('currency')
        rows = self.requirements()
        self.assertEqual(rows['position_account_scope'].status, Status.VERIFIED)
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.VERIFIED)

    def test_paginated_duplicate_or_unassignable_crosscheck_rows_are_not_complete(self):
        for kind in ('pagination', 'duplicate', 'malformed', 'missing_symbol', 'malformed_mode', 'malformed_side'):
            verifier, _, data, _ = fixture()
            crosscheck = data['/g-accounts/accountPositions']['data']
            if kind == 'pagination': crosscheck['nextPageArg'] = 'PRIVATE_SENTINEL'
            if kind == 'duplicate': crosscheck['positions'].append(deepcopy(crosscheck['positions'][0]))
            if kind == 'malformed': crosscheck['positions'].append(None)
            if kind == 'missing_symbol': crosscheck['positions'].append({'posSide': 'Merged', 'posMode': 'OneWay'})
            if kind == 'malformed_mode': crosscheck['positions'][0]['posMode'] = []
            if kind == 'malformed_side': crosscheck['positions'][0]['posSide'] = {}
            report = collect(verifier)
            rows = self.requirements(report)
            with self.subTest(kind=kind):
                for name in ('position_crosscheck', 'explicit_hedge_legs', 'Long.base_quantity'):
                    self.assertEqual(rows[name].status, Status.UNKNOWN, name)
                self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_failed_or_inconsistent_crosscheck_envelope_cannot_verify_dependent_fields(self):
        original = deepcopy(self.data['/g-accounts/accountPositions'])
        for value in ({'code': 0, 'data': []}, {'code': 10500, 'data': original['data']},
                      dict(original, error={'message': 'PRIVATE_SENTINEL'}),
                      {'code': 0, 'data': {'account': original['data']['account'], 'positions': {}}}):
            verifier, _, data, _ = fixture()
            data['/g-accounts/accountPositions'] = value
            report = collect(verifier)
            rows = self.requirements(report)
            self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
            self.assertEqual(rows['Long.configured_leverage'].status, Status.UNKNOWN)
            self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_matching_non_normal_position_status_is_observed_but_not_reducible(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            self.data[path]['data']['positions'][0]['positionStatus'] = 'Adl'
        rows = self.requirements()
        self.assertEqual(rows['Long.status'].status, Status.VERIFIED)
        self.assertEqual(rows['Long.status'].value, 'adl')
        self.assertEqual(rows['Long.reducible_position'].status, Status.INCOMPATIBLE)

    def test_crosscheck_joins_by_explicit_leg_not_list_order(self):
        self.data['/g-accounts/accountPositions']['data']['positions'].reverse()
        rows = self.requirements()
        self.assertEqual(rows['position_crosscheck'].status, Status.VERIFIED)
        self.assertEqual(rows['Long.configured_leverage'].value, '15')
        self.assertEqual(rows['Short.configured_leverage'].value, '3')

    def test_zero_quantity_does_not_hide_overlapping_direction_disagreement(self):
        self.data['/g-accounts/accountPositions']['data']['positions'][1]['side'] = 'Sell'
        rows = self.requirements()
        self.assertEqual(rows['position_crosscheck'].status, Status.UNKNOWN)
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
        self.assertEqual(rows['Short.base_quantity'].status, Status.UNKNOWN)

    def test_bad_unknown_offgrid_or_excess_precision_position_quantity_is_unknown(self):
        for value in (None, 'NaN', '-1', '1e100000', '0.0025', 0.002):
            verifier, _, data, _ = fixture()
            data['/g-accounts/positions']['data']['positions'][0]['sizeRq'] = value
            with self.subTest(value=value):
                rows = self.requirements(collect(verifier))
                self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)

    def test_conflicting_margin_flags_and_unknown_leverage_do_not_infer_defaults(self):
        row = self.data['/g-accounts/positions']['data']['positions'][0]
        row.update(crossMargin=False, leverageRr='-15')
        self.assertEqual(self.requirements()['Long.margin_mode'].status, Status.UNKNOWN)

    def test_zero_leverage_is_unknown_not_a_default(self):
        self.data['/g-accounts/positions']['data']['positions'][0]['leverageRr'] = '0'
        rows = self.requirements()
        self.assertEqual(rows['Long.configured_leverage'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.margin_mode'].status, Status.UNKNOWN)

    def test_stale_private_observations_and_clock_rollback_are_unknown(self):
        original = self.data['/g-accounts/accountPositions']
        def advance():
            self.now[0] += timedelta(seconds=6)
            return original
        self.data['/g-accounts/accountPositions'] = advance
        rows = self.requirements()
        for name in ('Long.base_quantity', 'explicit_hedge_legs', 'snapshot_identity_consistency', 'accounting_type'):
            self.assertEqual(rows[name].status, Status.UNKNOWN)
        self.assertEqual(rows['instrument.quantity_step'].status, Status.VERIFIED)

    def test_stale_public_metadata_is_unknown(self):
        original = self.data['/g-accounts/accountPositions']
        def advance():
            self.now[0] += timedelta(seconds=301)
            return original
        self.data['/g-accounts/accountPositions'] = advance
        self.assertEqual(self.requirements()['instrument.quantity_step'].status, Status.UNKNOWN)

    def test_old_instrument_metadata_cannot_verify_grid_with_fresh_private_reads(self):
        original = self.ex._transport._send_http.side_effect
        def reply(prepared, **options):
            value = original(prepared, **options)
            if '/v1/exchange/public/products' in prepared.url:
                self.now[0] += timedelta(seconds=301)
            return value
        self.ex._transport._send_http.side_effect = reply
        rows = self.requirements()
        self.assertEqual(rows['instrument.quantity_step'].status, Status.UNKNOWN)
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.VERIFIED)
        for leg in ('Long', 'Short'):
            self.assertEqual(rows[leg + '.base_quantity'].status, Status.UNKNOWN)
            self.assertEqual(rows[leg + '.reducible_position'].status, Status.UNKNOWN)
            self.assertEqual(rows[leg + '.observed_base_quantity'].status, Status.VERIFIED)
        self.assertEqual(rows['Long.observed_base_quantity'].value, '0.002')

    def test_grid_freshness_boundary_is_strict_without_rounding_or_assuming_zero(self):
        for age in (299, 300, 301):
            verifier, ex, _, now = fixture()
            original = ex._transport._send_http.side_effect
            def reply(prepared, **options):
                value = original(prepared, **options)
                if '/v1/exchange/public/products' in prepared.url:
                    now[0] += timedelta(seconds=age)
                return value
            ex._transport._send_http.side_effect = reply
            rows = self.requirements(collect(verifier))
            with self.subTest(age=age):
                expected = Status.VERIFIED if age < 300 else Status.UNKNOWN
                self.assertEqual(rows['Long.base_quantity'].status, expected)
                self.assertEqual(rows['Long.reducible_position'].status, expected)
                self.assertEqual(rows['Long.observed_base_quantity'].status, Status.VERIFIED)

    def test_missing_or_contradictory_grid_does_not_erase_raw_position_quantity(self):
        for change in ({'qtyStepSize': None}, {'lotSize': '0.01'}, {'qtyStepSize': 'NaN'}):
            verifier, _, data, _ = fixture()
            data['/public/products']['data']['perpProductsV2'][0].update(change)
            rows = self.requirements(collect(verifier))
            with self.subTest(change=change):
                self.assertEqual(rows['instrument.quantity_step'].status, Status.UNKNOWN)
                for leg in ('Long', 'Short'):
                    self.assertEqual(rows[leg + '.observed_base_quantity'].status, Status.VERIFIED)
                    self.assertEqual(rows[leg + '.base_quantity'].status, Status.UNKNOWN)
                    self.assertEqual(rows[leg + '.reducible_position'].status, Status.UNKNOWN)

    def test_report_distinguishes_observed_off_grid_quantity_from_compatibility(self):
        for path in ('/g-accounts/positions', '/g-accounts/accountPositions'):
            self.data[path]['data']['positions'][0]['sizeRq'] = '0.0025'
        rows = self.requirements()
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.VERIFIED)
        self.assertEqual(rows['Long.observed_base_quantity'].value, '0.0025')
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.reducible_position'].status, Status.UNKNOWN)

    def test_raw_quantity_still_requires_fresh_matching_private_observations(self):
        original = self.data['/g-accounts/accountPositions']
        def reply():
            self.now[0] += timedelta(seconds=6)
            return original
        self.data['/g-accounts/accountPositions'] = reply
        rows = self.requirements()
        self.assertEqual(rows['Long.observed_base_quantity'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.base_quantity'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.reducible_position'].status, Status.UNKNOWN)

    def test_clock_rollback_invalidates_observations(self):
        original = self.data['/g-accounts/accountPositions']
        def backwards():
            self.now[0] -= timedelta(seconds=1)
            return original
        self.data['/g-accounts/accountPositions'] = backwards
        report = self.report()
        self.assertTrue(all(row.status is Status.UNKNOWN for row in report.requirements))

    def test_naive_clock_prevents_requests_and_does_not_leave_reads_authorized(self):
        self.now[0] = self.now[0].replace(tzinfo=None)
        with self.assertRaises(SafetyError): self.report()
        self.ex.fetch.assert_not_called()
        self.assertFalse(self.ex._reads_authorized)

    def test_failing_risk_endpoints_are_deferred_outside_the_four_get_plan(self):
        self.data['/uta-api/risk/risk-mode'] = self.server_error
        self.data['/uta-api/risk/risk-units'] = self.server_error
        report = self.report()
        rows = self.requirements(report)
        self.assertEqual(rows['accounting_type'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.configured_leverage'].status, Status.VERIFIED)
        self.assertNotIn('FIXTURE_SECRET', report.render())
        urls = [call.args[0] for call in self.ex.fetch.call_args_list]
        self.assertEqual(len(urls), 4)
        self.assertFalse(any('/risk-mode' in url or '/risk-units' in url for url in urls))
        self.assertFalse(any('/g-accounts/risk-unit' in url for url in urls))

    def test_auth_permission_rate_limit_and_network_failures_stop_remaining_private_reads(self):
        for error in (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.RateLimitExceeded,
                      ccxt.RequestTimeout, ccxt.NetworkError):
            verifier, ex, data, _ = fixture()
            def fail(error=error): raise error('FIXTURE_SECRET')
            data['/g-accounts/positions'] = fail
            report = collect(verifier)
            self.assertEqual(self.requirements(report)['Long.base_quantity'].status, Status.UNKNOWN)
            self.assertFalse(any('/accountPositions' in call.args[0] for call in ex.fetch.call_args_list))
            self.assertNotIn('FIXTURE_SECRET', report.render())

    def test_non_risk_server_error_stops_and_never_retries(self):
        self.data['/g-accounts/positions'] = self.server_error
        rows = self.requirements()
        self.assertEqual(rows['explicit_hedge_legs'].status, Status.UNKNOWN)
        self.assertFalse(any('/accountPositions' in call.args[0] for call in self.ex.fetch.call_args_list))

    def test_unsupported_identity_endpoint_is_unknown_not_binding(self):
        def unsupported(): raise ccxt.NotSupported('FIXTURE_SECRET')
        self.data['/g-accounts/accountPositions'] = unsupported
        report = self.report()
        self.assertEqual(self.requirements(report)['snapshot_identity_consistency'].status, Status.UNKNOWN)
        self.assertEqual(self.requirements(report)['authentication'].status, Status.VERIFIED)
        self.assertNotIn('FIXTURE_SECRET', report.render())

    def test_500_permission_reason_stops_remaining_planned_private_reads(self):
        def denied():
            self.ex.last_read_diagnostic = ResponseEvidence(500, '10500', 'permission_rejected')
            raise ccxt.ExchangeError('FIXTURE_SECRET')
        self.data['/g-accounts/positions'] = denied
        self.report()
        self.assertFalse(any('/accountPositions' in call.args[0] for call in self.ex.fetch.call_args_list))

    def test_deferred_accounting_mode_does_not_prevent_mixed_margin_observations(self):
        self.data['/uta-api/risk/risk-mode']['data']['riskMode'] = 'MultiAsset'
        rows = self.requirements()
        self.assertEqual(rows['accounting_type'].status, Status.UNKNOWN)
        self.assertEqual(rows['Long.margin_mode'].value, 'cross')
        self.assertEqual(rows['Short.margin_mode'].value, 'isolated')
        paths = [call.args[0] for call in self.ex.fetch.call_args_list]
        self.assertFalse(any('/uta-api/risk/' in path or '/uta-biz/assets' in path for path in paths))
        self.assertFalse(any('/g-accounts/risk-unit' in path for path in paths))

    def test_unsupported_or_malformed_endpoint_never_establishes_accounting(self):
        for value in ({'code': 10500, 'msg': 'PRIVATE_SENTINEL'}, {'code': 0, 'data': []}, [], None):
            verifier, _, data, _ = fixture()
            data['/uta-api/risk/risk-mode'] = value
            report = collect(verifier)
            self.assertEqual(self.requirements(report)['accounting_type'].status, Status.UNKNOWN)
            self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_malformed_or_missing_position_response_never_becomes_empty_position(self):
        for value in (None, [], {'account': {}}, {'positions': []}, {'positions': 'PRIVATE_SENTINEL'}):
            verifier, _, data, _ = fixture()
            data['/g-accounts/positions'] = {'code': 0, 'data': value}
            report = collect(verifier)
            self.assertEqual(self.requirements(report)['explicit_hedge_legs'].status, Status.UNKNOWN)
            self.assertNotIn('PRIVATE_SENTINEL', report.render())

    def test_duplicate_or_malformed_public_product_does_not_reuse_a_cache(self):
        self.ex.set_markets([deepcopy(MARKET)])
        self.data['/public/products']['data']['perpProductsV2'].append(deepcopy(PRODUCT))
        report = self.report()
        self.assertEqual(self.requirements(report)['instrument.quantity_step'].status, Status.UNKNOWN)
        self.assertFalse(any('/g-accounts/' in call.args[0] for call in self.ex.fetch.call_args_list))

    def test_inconsistent_native_and_ccxt_instrument_specs_are_unknown(self):
        for change, key in (({'lotSize': '0.01'}, 'quantity_step'), ({'contractSize': '0.1'}, 'contract_size'),
                            ({'tickSize': 'NaN'}, 'price_tick'), ({'qtyStepSize': None}, 'quantity_step')):
            verifier, _, data, _ = fixture()
            data['/public/products']['data']['perpProductsV2'][0].update(change)
            report = collect(verifier)
            self.assertEqual(self.requirements(report)['instrument.'+key].status, Status.UNKNOWN)

    def test_missing_native_contract_size_does_not_promote_ccxt_convention_to_verified(self):
        self.data['/public/products']['data']['perpProductsV2'][0].pop('contractSize')
        self.assertEqual(self.requirements()['instrument.contract_size'].status, Status.UNKNOWN)

    def test_malformed_public_envelopes_halt_before_private_requests(self):
        for value in ({'code': 10500, 'msg': 'FIXTURE_SECRET'}, [], None, {'code': 0, 'data': []}):
            verifier, ex, data, _ = fixture()
            data['/public/products'] = value
            report = collect(verifier)
            self.assertFalse(any('/g-accounts/' in call.args[0] for call in ex.fetch.call_args_list))
            self.assertEqual(self.requirements(report)['instrument.active'].status, Status.UNKNOWN)
            self.assertNotIn('FIXTURE_SECRET', report.render())

    def test_wrong_instrument_settlement_is_unknown_and_inactive_market_is_incompatible(self):
        self.data['/public/products']['data']['perpProductsV2'][0]['settleCurrency'] = 'BTC'
        self.assertEqual(self.requirements()['instrument.active'].status, Status.UNKNOWN)
        verifier, _, data, _ = fixture()
        data['/public/products']['data']['perpProductsV2'][0]['status'] = 'Delisted'
        self.assertEqual(self.requirements(collect(verifier))['instrument.active'].status, Status.INCOMPATIBLE)

    def test_mutation_verbs_and_dangerous_get_routes_never_reach_mock_transport(self):
        self.ex._reads_authorized = True
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method), self.assertRaises(SafetyError):
                self.ex.request('g-orders', 'private', method, {})
        for path in ('g-orders/cancel', 'g-orders/create', 'assets/transfer', 'uta-funds/contract/borrow'):
            with self.subTest(path=path), self.assertRaises(SafetyError):
                self.ex.request(path, 'private', 'GET', {})
        self.ex.fetch.assert_not_called()

    def test_snapshot_cannot_be_passed_as_execution_policy_evidence(self):
        report = self.report()
        values = list(evidence())
        values[1] = report
        self.assertEqual(evaluate(*values, now=NOW).reasons, ('invalid_evidence_type',))

    def test_collection_does_not_load_credentials_access_journal_or_submit_orders(self):
        with patch('config.load_settings', side_effect=AssertionError('Credential loading')), \
             patch('operation_journal.OperationJournal', side_effect=AssertionError('Journal access')), \
             patch('ccxt.phemex.create_order', side_effect=AssertionError('Mutation')), \
             patch('ccxt.phemex.cancel_order', side_effect=AssertionError('Mutation')):
            self.assertFalse(self.report().execution_authorized)

    def test_unrestricted_exchange_or_user_snapshot_cannot_be_injected_as_adapter(self):
        for value in (Mock(), ReadOnlyPhemex(), {}):
            with self.assertRaises(SafetyError): CompatibilityVerifier(value)
