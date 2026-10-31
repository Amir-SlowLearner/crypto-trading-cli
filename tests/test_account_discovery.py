from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import ccxt

from account_discovery import AccountDiscovery
from tests.account_fixtures import CLASSIC_ACCOUNT, CLASSIC_RISK, UNIFIED_RISK, discovery_client, envelope, raw_position
from tests.helpers import OfflineTestCase


class DiscoveryTests(OfflineTestCase):
    def test_classic_account_and_scope_preserving_values(self):
        client, ex = discovery_client()
        report = client.account()
        self.assertTrue(report.authenticated.value)
        self.assertEqual(report.account_type.value, 'classic')
        self.assertEqual(report.wallet['balance'].value, Decimal('1000'))
        self.assertEqual(report.wallet['balance'].origin, 'reported')
        self.assertEqual(report.wallet['equity'].value, Decimal('1020'))
        self.assertEqual(report.wallet['equity'].origin, 'derived')
        self.assertEqual(report.wallet['unallocated_wallet_balance'].value, Decimal('900'))
        self.assertIsNone(report.wallet['available_margin'].value)
        self.assertEqual(report.risk_units[0]['available_margin_estimate'].value, Decimal('500'))
        self.assertEqual(report.risk_units[0]['equity'].value, Decimal('1010'))
        self.assertTrue(report.complete)
        ex.privateGetUtaBizAssets.assert_not_called()
        ex.privateGetUtaApiRiskRiskUnits.assert_not_called()
        ex.privateGetGAccountsPositions.assert_called_once_with({'currency': 'USDT'})

    def test_unified_uses_asset_and_risk_unit_scopes_without_classic_fallback(self):
        client, ex = discovery_client('MultiAsset')
        report = client.account()
        self.assertEqual(report.account_type.value, 'unified_multi_asset')
        self.assertEqual(report.wallet['balance'].value, Decimal('500'))
        self.assertEqual(report.wallet['borrowed'].value, Decimal('40'))
        self.assertEqual(report.wallet['interest'].value, Decimal('10'))
        self.assertEqual(report.wallet['frozen_as_collateral'].value, Decimal('100'))
        self.assertEqual(report.risk_units[0]['equity'].value, Decimal('1210'))
        self.assertIsNone(report.wallet['equity'].value)
        self.assertIsNone(report.wallet['available_margin'].value)
        self.assertIsNone(report.risk_units[0]['available_margin_estimate'].value)
        self.assertTrue(report.complete)
        ex.privateGetGAccountsRiskUnit.assert_not_called()

    def test_modes_and_leverage_are_per_symbol_and_leg(self):
        client, _ = discovery_client()
        rows = client.account().positions
        self.assertEqual(rows[0]['position_mode'].value, 'one_way')
        self.assertEqual(rows[0]['position_side'].value, 'Merged')
        self.assertEqual(rows[0]['margin_mode'].value, 'cross')
        self.assertEqual(rows[0]['configured_leverage'].value, Decimal('5'))
        self.assertEqual(rows[0]['margin_mode'].origin, 'derived')
        self.assertEqual(rows[1]['position_mode'].value, 'hedge')
        self.assertEqual(rows[1]['margin_mode'].value, 'isolated')
        self.assertEqual(rows[2]['contracts'].value, 0)
        self.assertEqual(rows[2]['position_side'].value, 'Short')

    def test_hedge_legs_need_not_have_equal_leverage_or_margin(self):
        client, ex = discovery_client()
        account = deepcopy(CLASSIC_ACCOUNT)
        account['positions'][2]['leverageRr'] = '-7'
        ex.privateGetGAccountsPositions.return_value = envelope(account)
        rows = client.account().positions
        self.assertEqual(rows[1]['configured_leverage'].value, 3)
        self.assertEqual(rows[2]['configured_leverage'].value, 7)
        self.assertEqual(rows[2]['margin_mode'].value, 'cross')

    def test_missing_mode_is_not_inferred_from_side_or_other_symbols(self):
        client, ex = discovery_client()
        account = deepcopy(CLASSIC_ACCOUNT)
        del account['positions'][0]['posMode']
        del account['positions'][0]['leverageRr']
        ex.privateGetGAccountsPositions.return_value = envelope(account)
        report = client.account()
        self.assertIsNone(report.positions[0]['position_mode'].value)
        self.assertIsNone(report.positions[0]['margin_mode'].value)
        self.assertFalse(report.complete)

    def test_conflicting_margin_evidence_is_unknown_not_isolated_default(self):
        client, ex = discovery_client()
        for flag in (False, 'true', 1):
            account = deepcopy(CLASSIC_ACCOUNT)
            account['positions'][0]['crossMargin'] = flag
            ex.privateGetGAccountsPositions.return_value = envelope(account)
            with self.subTest(flag=flag):
                report = client.account()
                self.assertIsNone(report.positions[0]['margin_mode'].value)
                self.assertFalse(report.complete)

    def test_explicit_margin_flag_and_zero_leverage(self):
        client, ex = discovery_client()
        account = deepcopy(CLASSIC_ACCOUNT)
        account['positions'][0].update(leverageRr='0', crossMargin=True)
        ex.privateGetGAccountsPositions.return_value = envelope(account)
        row = client.account().positions[0]
        self.assertEqual(row['margin_mode'].value, 'cross')
        self.assertEqual(row['margin_mode'].origin, 'reported')
        self.assertIsNone(row['configured_leverage'].value)

    def test_unknown_account_modes_never_default_to_classic(self):
        for mode in (None, '', 'Portfolio', 'SECRET_SENTINEL', 'CrossAsset'):
            client, ex = discovery_client(mode)
            with self.subTest(mode=mode):
                report = client.account()
                self.assertIsNone(report.account_type.value)
                self.assertIsNone(report.wallet['balance'].value)
                self.assertNotIn('SECRET_SENTINEL', repr(report))
                ex.privateGetGAccountsRiskUnit.assert_not_called()

    def test_unavailable_mode_endpoint_can_leave_other_reads_authenticated(self):
        client, ex = discovery_client()
        ex.privateGetUtaApiRiskRiskMode.side_effect = ccxt.NotSupported('SECRET_SENTINEL')
        report = client.account()
        self.assertTrue(report.authenticated.value)
        self.assertIsNone(report.account_type.value)
        self.assertFalse(report.complete)
        self.assertNotIn('SECRET_SENTINEL', repr(report))

    def test_authentication_failure_stops_without_retries_or_fallbacks(self):
        client, ex = discovery_client()
        ex.privateGetUtaApiRiskRiskMode.side_effect = ccxt.AuthenticationError('SECRET_SENTINEL')
        report = client.account()
        self.assertIs(report.authenticated.value, False)
        self.assertEqual(ex.privateGetUtaApiRiskRiskMode.call_count, 1)
        ex.privateGetGAccountsPositions.assert_not_called()
        ex.privateGetGAccountsRiskUnit.assert_not_called()
        ex.privateGetUtaApiRiskRiskUnits.assert_not_called()
        self.assertNotIn('SECRET_SENTINEL', repr(report))

    def test_network_rate_limit_and_timeout_stop_subsequent_reads(self):
        for error in (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.RateLimitExceeded):
            client, ex = discovery_client()
            ex.privateGetUtaApiRiskRiskMode.side_effect = error('SECRET_SENTINEL')
            with self.subTest(error=error):
                report = client.account()
                self.assertIsNone(report.authenticated.value)
                self.assertNotIn('SECRET_SENTINEL', repr(report))
                self.assertEqual(ex.privateGetUtaApiRiskRiskMode.call_count, 1)
                ex.privateGetGAccountsPositions.assert_not_called()

    def test_permission_error_does_not_claim_account_type(self):
        client, ex = discovery_client()
        ex.privateGetUtaApiRiskRiskMode.side_effect = ccxt.PermissionDenied('SECRET_SENTINEL')
        report = client.account()
        self.assertIsNone(report.account_type.value)
        self.assertTrue(report.authenticated.value)
        self.assertNotIn('SECRET_SENTINEL', repr(report))

    def test_bad_success_envelopes_do_not_become_valid_account_mode(self):
        values = [None, [], {'code': False, 'data': {}}, {'code': 0},
                  {'code': 401, 'data': {}, 'msg': 'SECRET_SENTINEL'},
                  {'code': 0, 'data': [], 'msg': 'SECRET_SENTINEL'}]
        for value in values:
            client, ex = discovery_client()
            ex.privateGetUtaApiRiskRiskMode.return_value = value
            with self.subTest(value=value):
                report = client.account()
                self.assertIsNone(report.account_type.value)
                self.assertNotIn('SECRET_SENTINEL', repr(report))

    def test_unified_asset_currency_mismatch_is_not_labeled_usdt(self):
        client, ex = discovery_client('MultiAsset')
        ex.privateGetUtaBizAssets.return_value = envelope({'currency': 'USDC', 'balanceRv': '2000'})
        report = client.account()
        self.assertIsNone(report.wallet['balance'].value)
        self.assertFalse(report.complete)

    def test_account_currency_mismatch_prevents_wallet_derivations(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_ACCOUNT)
        value['account']['currency'] = 'BTC'
        ex.privateGetGAccountsPositions.return_value = envelope(value)
        report = client.account()
        self.assertIsNone(report.wallet['balance'].value)
        self.assertIsNone(report.wallet['equity'].value)

    def test_missing_positions_is_not_empty_and_missing_pnl_blocks_equity(self):
        client, ex = discovery_client()
        for replacement in (None, 'missing', [raw_position() | {'unRealisedPnlRv': None}], [raw_position() | {'sizeRq': None}]):
            value = deepcopy(CLASSIC_ACCOUNT)
            value['positions'] = replacement
            ex.privateGetGAccountsPositions.return_value = envelope(value)
            with self.subTest(replacement=replacement):
                report = client.account()
                self.assertIsNone(report.wallet['equity'].value)
        self.assertIsNone(report.positions[0]['contracts'].value)

    def test_confirmed_empty_positions_have_zero_pnl_but_unknown_modes(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_ACCOUNT)
        value['positions'] = []
        ex.privateGetGAccountsPositions.return_value = envelope(value)
        report = client.account()
        self.assertEqual(report.positions, [])
        self.assertEqual(report.wallet['equity'].value, 1000)
        self.assertTrue(report.positions_available)

    def test_zero_and_missing_financial_values_are_distinct(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_ACCOUNT)
        value['account'] = {'currency': 'USDT', 'accountBalanceRv': '0'}
        ex.privateGetGAccountsPositions.return_value = envelope(value)
        report = client.account()
        self.assertEqual(report.wallet['balance'].value, 0)
        self.assertIsNone(report.wallet['used'].value)
        self.assertIsNone(report.wallet['unallocated_wallet_balance'].value)

    def test_nonfinite_negative_and_boolean_response_numbers_are_unknown(self):
        for value in ('NaN', 'sNaN', 'Infinity', True, [], 'SECRET_SENTINEL'):
            client, ex = discovery_client()
            data = deepcopy(CLASSIC_ACCOUNT)
            data['account']['accountBalanceRv'] = value
            data['positions'][0]['sizeRq'] = value
            ex.privateGetGAccountsPositions.return_value = envelope(data)
            with self.subTest(value=value):
                report = client.account()
                self.assertIsNone(report.wallet['balance'].value)
                self.assertIsNone(report.positions[0]['contracts'].value)
                self.assertNotIn('SECRET_SENTINEL', repr(report))
        data['positions'][0]['sizeRq'] = '-1'
        ex.privateGetGAccountsPositions.return_value = envelope(data)
        self.assertIsNone(client.account().positions[0]['contracts'].value)

    def test_conflicting_size_aliases_and_duplicate_rows_block_equity(self):
        client, ex = discovery_client()
        for rows in ([raw_position() | {'size': '10'}], [raw_position(), raw_position()]):
            value = deepcopy(CLASSIC_ACCOUNT)
            value['positions'] = rows
            ex.privateGetGAccountsPositions.return_value = envelope(value)
            with self.subTest(rows=rows):
                report = client.account()
                self.assertFalse(report.positions_complete)
                self.assertIsNone(report.wallet['equity'].value)

    def test_inconsistent_modes_for_one_symbol_are_unknown(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_ACCOUNT)
        value['positions'] = [raw_position(), raw_position(mode='Hedged', pos_side='Long')]
        ex.privateGetGAccountsPositions.return_value = envelope(value)
        report = client.account()
        self.assertTrue(all(p['position_mode'].value is None for p in report.positions))
        self.assertFalse(report.positions_complete)

    def test_account_mode_conflict_does_not_keep_classic_wallet_interpretation(self):
        client, ex = discovery_client()
        ex.privateGetGAccountsRiskUnit.return_value = envelope(UNIFIED_RISK)
        report = client.account()
        self.assertIsNone(report.account_type.value)
        self.assertIsNone(report.wallet['balance'].value)
        self.assertIsNone(report.wallet['equity'].value)

    def test_unmapped_numeric_currency_is_unknown_not_assumed_usdt(self):
        client, ex = discovery_client()
        ex.currencies['USDT']['info'] = {}
        report = client.account()
        self.assertIsNone(report.risk_units[0]['currency'].value)
        self.assertFalse(report.complete)

    def test_conflicting_currency_aliases_are_unknown(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_RISK)
        value[0]['currency'] = 'BTC'
        ex.privateGetGAccountsRiskUnit.return_value = envelope(value)
        self.assertIsNone(client.account().risk_units[0]['currency'].value)

    def test_undocumented_unified_available_margin_is_not_trusted(self):
        client, ex = discovery_client('MultiAsset')
        value = deepcopy(UNIFIED_RISK)
        value[0]['estAvailableBalanceRv'] = '777'
        ex.privateGetUtaApiRiskRiskUnits.return_value = envelope(value)
        self.assertIsNone(client.account().risk_units[0]['available_margin_estimate'].value)

    def test_report_has_no_raw_private_response_or_identifier_fields(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_ACCOUNT)
        value['account']['apiSecret'] = 'SECRET_SENTINEL'
        value['account']['userID'] = 'PRIVATE_USER_SENTINEL'
        value['positions'][0]['debug'] = 'SECRET_SENTINEL'
        ex.privateGetGAccountsPositions.return_value = envelope(value)
        result = repr(client.account())
        self.assertNotIn('SECRET_SENTINEL', result)
        self.assertNotIn('PRIVATE_USER_SENTINEL', result)

    def test_reusing_reader_does_not_keep_previous_authenticated_snapshot(self):
        client, ex = discovery_client()
        reader = AccountDiscovery(ex)
        self.assertTrue(reader.discover().authenticated.value)
        ex.privateGetUtaApiRiskRiskMode.side_effect = ccxt.AuthenticationError('SECRET_SENTINEL')
        report = reader.discover()
        self.assertFalse(report.authenticated.value)
        self.assertIsNone(report.wallet['balance'].value)
        self.assertEqual(report.positions, [])

    def test_no_mutation_method_is_called_during_discovery(self):
        for mode in ('Classic', 'MultiAsset'):
            client, ex = discovery_client(mode)
            client.account()
            for method in ('create_order', 'edit_order', 'cancel_order', 'cancel_all_orders',
                           'set_leverage', 'set_margin_mode', 'set_position_mode'):
                getattr(ex, method).assert_not_called()

    def test_missing_reported_prices_and_pnl_are_not_filled_from_market_data(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_ACCOUNT)
        for key in ('markPriceRp', 'avgEntryPriceRp', 'unRealisedPnlRv'):
            value['positions'][0].pop(key)
        ex.privateGetGAccountsPositions.return_value = envelope(value)
        report = client.account()
        self.assertIsNone(report.positions[0]['mark_price'].value)
        self.assertIsNone(report.positions[0]['entry_price'].value)
        self.assertIsNone(report.positions[0]['unrealized_pnl'].value)
        self.assertIsNone(report.wallet['equity'].value)
        ex.fetch_order_book.assert_not_called()

    def test_unsupported_position_is_retained_as_unknown_and_blocks_aggregate(self):
        client, ex = discovery_client()
        value = deepcopy(CLASSIC_ACCOUNT)
        value['positions'] += [raw_position('UNLISTEDUSDT')]
        ex.privateGetGAccountsPositions.return_value = envelope(value)
        report = client.account()
        self.assertEqual(len(report.positions), 4)
        self.assertIsNone(report.positions[-1]['symbol'].value)
        self.assertIsNone(report.positions[-1]['contracts'].value)
        self.assertFalse(report.positions_complete)
        self.assertIsNone(report.wallet['equity'].value)

    def test_duplicate_risk_scopes_are_flagged_and_not_summed(self):
        client, ex = discovery_client('MultiAsset')
        ex.privateGetUtaApiRiskRiskUnits.return_value = envelope([UNIFIED_RISK[0], UNIFIED_RISK[0]])
        report = client.account()
        self.assertFalse(report.complete)
        self.assertTrue(any('duplicate scope' in issue for issue in report.issues))
        self.assertIsNone(report.wallet['equity'].value)

    def test_paginated_or_malformed_position_sets_do_not_produce_equity(self):
        client, ex = discovery_client()
        for extra, positions in (({'nextPageArg': 'cursor'}, []), ({}, [None, raw_position()])):
            value = {**deepcopy(CLASSIC_ACCOUNT), **extra, 'positions': positions}
            ex.privateGetGAccountsPositions.return_value = envelope(value)
            with self.subTest(extra=extra, positions=positions):
                report = client.account()
                self.assertFalse(report.positions_complete)
                self.assertIsNone(report.wallet['equity'].value)
