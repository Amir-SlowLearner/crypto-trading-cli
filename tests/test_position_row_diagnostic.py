"""Synthetic private rows only: collection uses mocked HTTP and denied sockets."""
from copy import deepcopy
from decimal import Decimal
import io
import unittest
from unittest.mock import patch

import manual_testnet_status as m
import testnet_readonly as cli
from position_row_diagnostic import describe_position_rows
from tests import test_manual_testnet_status as manual_tests
from tests import test_manual_market_catalog as catalog_tests
from tests.account_fixtures import raw_position


class PositionDiagnosticTests(unittest.TestCase):
    respond = manual_tests.ManualStatusTests.respond

    def setUp(self):
        manual_tests.ManualStatusTests.setUp(self)
        self.data['/public/products']['data']['perpProductsV2'].append(
            catalog_tests.product('XRP',step='1',tick='0.0001'))
        self.unrelated=deepcopy(self.data['/g-accounts/positions']['data']['positions'])
        self.long=raw_position('XRPUSDT',mode='Hedged',pos_side='Long',size='20',leverage='-4')
        self.short=raw_position('XRPUSDT',mode='Hedged',pos_side='Short',side='None',size='0',leverage='3')
        self.rows([self.long,self.short])

    def rows(self, selected):
        self.data['/g-accounts/positions']['data']['positions']=deepcopy(selected)

    def collect(self, **kwargs):
        return m.collect_manual_status(m.ManualCredentials(manual_tests.KEY,manual_tests.SECRET),
                                       symbol='XRPUSDT',position_schema=True,**kwargs)

    def command(self):
        with patch.object(m,'load_credentials',return_value=m.ManualCredentials(manual_tests.KEY,manual_tests.SECRET)), \
             patch('sys.stdout',io.StringIO()) as out:
            code=cli.main(['--status','--position-schema','--symbol','XRPUSDT',
                           '--credentials-env','--authorize-testnet-read'])
        return code,out.getvalue()

    def test_valid_hedge_rows_keep_quantities_but_diagnostic_shows_only_types(self):
        report=self.collect()
        self.assertTrue(report.complete,report.render())
        self.assertEqual(report.legs[0].quantity.value,Decimal('20'))
        self.assertEqual(report.legs[1].quantity.value,Decimal('0'))
        text=report.render_position_schema()
        self.assertIn('exact_native: rows=2; Long=1; Short=1; Merged=0; UNKNOWN=0',text)
        self.assertIn('sizeRq:string',text)
        self.assertIn('leg=Long; mode=HEDGE',text)
        self.assertNotIn('quantity=',text)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.may_resolve_journal)
        self.assertEqual([r.url for r in self.calls],[e.url for e in m.MANUAL_PLAN])

    def test_explicit_zero_quantity_legs_are_readable_not_assumed_missing(self):
        self.rows([dict(self.long,sizeRq='0',side='None'),self.short])
        report=self.collect()
        self.assertTrue(report.complete,report.render())
        self.assertTrue(all(leg.quantity.value==0 for leg in report.legs))

    def test_empty_inventory_reproduces_observed_message_without_inferring_zero(self):
        self.rows([])
        report=self.collect()
        self.assertTrue(report.authenticated)
        self.assertFalse(report.complete)
        for leg in report.legs:
            self.assertIsNone(leg.quantity.value)
            self.assertEqual(leg.quantity.reason,'missing or duplicate leg')
        self.assertIn('exact_native: rows=0; Long=0; Short=0',report.render_position_schema())

    def test_catalog_coverage_does_not_imply_private_position_coverage(self):
        # Operator-observed row presence/layout: BTC legs present, ETH/XRP absent.
        # All metadata and numeric quantities below are invented. Explicit zero
        # is also supported by Phemex's published /g-accounts/positions example;
        # this fixture makes no claim about why the exchange omits other rows.
        self.data['/public/products']['data']['perpProductsV2'].append(
            catalog_tests.product('ETH',step='0.01'))
        self.rows([dict(row,sizeRq='0',side='None') for row in self.unrelated])
        for selector in ('BTCUSDT','BTC/USDT:USDT','ETHUSDT','ETH/USDT:USDT',
                         'XRPUSDT','XRP/USDT:USDT'):
            with self.subTest(selector=selector):
                report=m.collect_manual_status(m.ManualCredentials(manual_tests.KEY,manual_tests.SECRET),
                                               symbol=selector,position_schema=True)
                self.assertTrue(report.authenticated)
                self.assertTrue(report.market.supported)
                self.assertEqual(report.catalog.issues,())
                self.assertFalse(report.execution_authorized)
                self.assertFalse(report.may_resolve_journal)
                if report.market.native=='BTCUSDT':
                    self.assertTrue(report.complete,report.render())
                    self.assertTrue(all(leg.quantity.value==Decimal('0') for leg in report.legs))
                else:
                    self.assertFalse(report.complete)
                    for leg in report.legs:
                        for field in (leg.quantity,leg.mode,leg.margin,leg.leverage,leg.contracts):
                            self.assertIsNone(field.value)
                    self.assertIn('exact_native: rows=0;',report.render_position_schema())
                    self.assertNotIn('quantity=0 ',report.render())
                # --symbol is a local selector, not a server-side position filter.
                self.assertEqual([r.url for r in self.calls[-3:]],[e.url for e in m.MANUAL_PLAN])
                self.assertEqual([r.method for r in self.calls[-3:]],['GET']*3)
        self.assertEqual(len(self.calls),18)

    def test_absent_or_empty_cursor_cannot_prove_missing_position_is_zero(self):
        self.rows(self.unrelated)  # Only independently scoped BTC rows exist.
        data=self.data['/g-accounts/positions']['data']
        for cursor in ('absent',None,'','PRIVATE_CURSOR'):
            with self.subTest(cursor=cursor):
                data.pop('nextPageArg',None)
                if cursor!='absent':data['nextPageArg']=cursor
                report=self.collect()
                self.assertTrue(report.authenticated)
                self.assertFalse(report.complete)
                self.assertTrue(all(leg.quantity.value is None for leg in report.legs))
                self.assertEqual(report.position_diagnostic.incomplete,cursor=='PRIVATE_CURSOR')
                self.assertNotIn('PRIVATE_CURSOR',report.render_position_schema())
        self.assertEqual(len(self.calls),12)  # Never follow a cursor or invent a page query.

    def test_flat_direction_without_explicit_quantity_cannot_establish_zero(self):
        rows=[dict(row,side='None') for row in self.unrelated]
        for row in rows:del row['sizeRq']
        self.rows(rows)
        report=m.collect_manual_status(m.ManualCredentials(manual_tests.KEY,manual_tests.SECRET),
                                       symbol='BTCUSDT',position_schema=True)
        self.assertTrue(report.authenticated)
        self.assertFalse(report.complete)
        self.assertTrue(all(leg.quantity.value is None for leg in report.legs))
        self.assertIn('direction=FLAT',report.render_position_schema())
        self.assertNotIn('quantity=0 ',report.render())

    def test_unrelated_rows_cannot_establish_selected_positions_or_leak_symbols(self):
        self.rows(self.unrelated)
        report=self.collect()
        text=report.render_position_schema()
        self.assertTrue(all(leg.quantity.value is None for leg in report.legs))
        self.assertIn('exact_native: rows=0;',text)
        self.assertNotIn('BTC',text)
        self.assertNotIn('accountID',text)

    def test_one_returned_leg_does_not_invent_the_other(self):
        for row,index in ((self.long,0),(dict(self.short,side='Sell',sizeRq='10'),1)):
            self.rows([row]);report=self.collect()
            self.assertIsNotNone(report.legs[index].quantity.value)
            self.assertIsNone(report.legs[1-index].quantity.value)
            self.assertFalse(report.complete)

    def test_one_way_merged_remains_unsupported_by_manual_hedge_view(self):
        for quantity in ('0','20'):
            self.rows([dict(self.long,posMode='OneWay',posSide='Merged',sizeRq=quantity)])
            report=self.collect()
            self.assertTrue(all(leg.quantity.value is None for leg in report.legs))
            self.assertNotIn('missing or duplicate leg',report.render())
            self.assertIn('leg=Merged; mode=ONE_WAY',report.render_position_schema())

    def test_duplicates_for_both_legs_also_reproduce_observed_message(self):
        self.rows([self.long,self.short,self.long,self.short])
        report=self.collect()
        self.assertTrue(all(leg.quantity.reason=='missing or duplicate leg' for leg in report.legs))
        self.assertIn('exact_native: rows=4; Long=2; Short=2;',report.render_position_schema())

    def test_duplicate_one_leg_preserves_independent_other_leg(self):
        self.rows([self.long,dict(self.long,sizeRq='21'),self.short])
        report=self.collect()
        self.assertIsNone(report.legs[0].quantity.value)
        self.assertEqual(report.legs[1].quantity.value,Decimal(0))

    def test_unified_or_case_variant_rows_are_diagnostic_candidates_not_parser_aliases(self):
        for spelling,group in (('XRP/USDT:USDT','exact_unified'),('xrpusdt','case_variant'),
                               ('xrp/usdt:usdt','case_variant')):
            self.rows([dict(self.long,symbol=spelling),dict(self.short,symbol=spelling)])
            report=self.collect()
            self.assertTrue(all(leg.quantity.reason=='missing or duplicate leg' for leg in report.legs))
            text=report.render_position_schema()
            self.assertIn('exact_native: rows=0;',text)
            self.assertIn(group+': rows=2; Long=1; Short=1;',text)

    def test_missing_or_numeric_side_and_missing_mode_stay_unknown(self):
        for fields in ({'posSide':None},{'posSide':1},{'posSide':'1'},{'posMode':None}):
            self.rows([dict(self.long,**fields),self.short])
            report=self.collect()
            self.assertTrue(all(leg.quantity.value is None for leg in report.legs))
            self.assertIn('UNKNOWN',report.render_position_schema())

    def test_conflicting_mode_leg_and_direction_do_not_get_repaired(self):
        self.rows([dict(self.long,posMode='OneWay'),self.short])
        report=self.collect()
        self.assertIn('mode_leg=CONFLICT',report.render_position_schema())
        self.assertFalse(report.complete)
        self.rows([dict(self.long,side='Sell'),self.short])
        self.assertFalse(self.collect().complete)

    def test_malformed_rows_are_omitted_with_unknown_coverage(self):
        self.rows([self.long,self.short,{'symbol':None,'accountID':'PRIVATE_ID'},'PRIVATE_BLOB'])
        report=self.collect()
        self.assertTrue(all(leg.quantity.value is None for leg in report.legs))
        self.assertTrue(report.position_diagnostic.incomplete)
        text=report.render_position_schema()
        self.assertIn('Unscopable rows omitted',text)
        self.assertNotIn('PRIVATE_ID',text)
        self.assertNotIn('PRIVATE_BLOB',text)

    def test_only_reviewed_fields_and_known_enum_labels_escape(self):
        selected=dict(self.long,posSide='PRIVATE_SIDE',posMode='PRIVATE_MODE',side='PRIVATE_DIRECTION',
                      sizeRq='123456789',accountID='PRIVATE_ID',userID='PRIVATE_USER',
                      accountBalanceRv='PRIVATE_BALANCE',SECRET_FIELD='PRIVATE_VALUE',
                      positionSide={'secret':'NESTED_PRIVATE'})
        self.rows([selected,*self.unrelated])
        report=self.collect();text=report.render_position_schema()+repr(report.position_diagnostic)
        for secret in ('PRIVATE_SIDE','PRIVATE_MODE','PRIVATE_DIRECTION','123456789','PRIVATE_ID',
                       'PRIVATE_USER','PRIVATE_BALANCE','SECRET_FIELD','PRIVATE_VALUE','NESTED_PRIVATE','BTC',
                       manual_tests.KEY,manual_tests.SECRET,'accountID','accountBalanceRv'):
            self.assertNotIn(secret,text)
        self.assertIn('positionSide:object',text)
        self.assertIn('leg=UNKNOWN; mode=UNKNOWN; direction=UNKNOWN',text)

    def test_paginated_response_never_prints_cursor_and_remains_incomplete(self):
        self.data['/g-accounts/positions']['data']['nextPageArg']='PRIVATE_CURSOR'
        code,text=self.command()
        self.assertEqual(code,2)
        self.assertIn('pagination is present',text)
        self.assertNotIn('PRIVATE_CURSOR',text)

    def test_detail_budget_retains_all_selected_counts_and_signals_incompleteness(self):
        self.rows([self.long]*21)
        report=self.collect();text=report.render_position_schema()
        self.assertIn('exact_native: rows=21; Long=21;',text)
        self.assertIn('Selected row 20:',text)
        self.assertNotIn('Selected row 21:',text)
        self.assertTrue(report.position_diagnostic.incomplete)

    def test_cli_diagnostic_only_success_does_not_claim_position_verification(self):
        self.rows([])
        code,text=self.command()
        self.assertEqual(code,0,text)
        self.assertIn('NOT POSITION VERIFICATION',text)
        self.assertIn('rows=0',text)
        self.assertNotIn('quantity=',text)
        self.assertNotIn('Contract size=',text)
        self.assertIn('does not establish an absent or zero position',text)

    def test_cli_constraints_and_missing_optin_prevent_credential_reads(self):
        with patch.object(m,'load_credentials') as credentials,patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['--status','--position-schema','--credentials-env']),2)
            for args in (['--position-schema'],['--status','--markets','--position-schema'],
                         ['--status','--position-schema','--live']):
                with patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):cli.main(args)
        credentials.assert_not_called()
        self.http.assert_not_called()

    def test_private_http_failure_cannot_produce_schema_or_expose_response(self):
        self.statuses['account_positions']=500
        self.raw['account_positions']=b'{"code":10500,"msg":"PRIVATE_ERROR"}'
        code,text=self.command()
        self.assertEqual(code,2)
        self.assertIn('diagnostic unavailable',text)
        self.assertNotIn('PRIVATE_ERROR',text)
        self.assertNotIn('BTC',text)
        self.assertEqual(len(self.calls),3)

    def test_diagnostic_is_opt_in_and_does_not_change_normal_view(self):
        report=m.collect_manual_status(m.ManualCredentials(manual_tests.KEY,manual_tests.SECRET),symbol='XRPUSDT')
        self.assertIsNone(report.position_diagnostic)
        self.assertTrue(report.complete,report.render())
        self.assertNotIn('field types:',report.render())

    def test_helper_cannot_treat_missing_inventory_as_empty(self):
        market=self.collect().market
        for data in ({},{'positions':None},{'positions':{}}):
            diagnostic=describe_position_rows(data,market)
            self.assertFalse(diagnostic.available)
            self.assertTrue(diagnostic.incomplete)

    def test_pinned_ccxt_normalized_side_and_margin_are_not_authoritative_leg_evidence(self):
        ex=m.ManualTestnetPhemex(m.ManualCredentials(manual_tests.KEY,manual_tests.SECRET))
        try:
            # Pure local market parser: no transport plan begun or request made.
            market=ex.parse_swap_market(catalog_tests.product('XRP'))
            ex.set_markets([market])
            parsed=ex.parse_position(dict(self.long,side='None',sizeRq='0'),market)
            self.assertEqual(parsed['side'],'short')  # CCXT maps the literal "None" this way.
            self.assertEqual(parsed['marginMode'],'isolated')  # Missing crossMargin defaults.
            self.assertEqual(parsed['info']['posSide'],'Long')
        finally:ex.close()
        self.http.assert_not_called()


if __name__=='__main__':unittest.main()
