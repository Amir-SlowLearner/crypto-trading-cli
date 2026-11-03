"""Synthetic envelopes only. All transport, credentials and journal use denied."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import position_inventory_crosscheck as x
from tests.account_fixtures import raw_position


NOW = datetime(2026,11,3,12,tzinfo=timezone.utc)
ACCOUNT, USER = '222222222222', '111111111111'
EXTREME_TIMESTAMPS = (
    datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    datetime.max.replace(tzinfo=timezone(-timedelta(hours=1))),
)


def response(symbol='BTCUSDT', *, long='0.002', short='0', field='sizeRq'):
    rows = [raw_position(symbol,mode='Hedged',pos_side='Long',side='Buy' if long != '0' else 'None',
                         size=long,leverage='-15'),
            raw_position(symbol,mode='Hedged',pos_side='Short',side='Sell' if short != '0' else 'None',
                         size=short,leverage='3')]
    for row in rows:
        row['accountID'] = ACCOUNT
        if field != 'sizeRq':row[field] = row.pop('sizeRq')
    return {'code':0,'data':{'account':{'currency':'USDT','accountId':ACCOUNT,'userID':USER,
                                      'accountBalanceRv':'BALANCE_WITHHELD'},'positions':rows}}


def fixture(symbol='BTCUSDT', **quantities):
    scope = x.PositionScope(symbol,symbol[:-4])
    first = x.PositionObservation(x.Endpoint.POSITIONS,response(symbol,**quantities),
        NOW-timedelta(seconds=3),NOW-timedelta(seconds=2),'invented_window')
    second = x.PositionObservation(x.Endpoint.ACCOUNT_POSITIONS,response(symbol,field='size',**quantities),
        NOW-timedelta(seconds=1.5),NOW-timedelta(seconds=1),'invented_window')
    mappings = tuple(x.QuantityMapping(endpoint,symbol,scope.base_asset,field,x.Encoding.UNSCALED_BASE)
        for endpoint,field in ((x.Endpoint.POSITIONS,'sizeRq'),(x.Endpoint.ACCOUNT_POSITIONS,'size')))
    return scope,first,second,mappings


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.prohibited_calls = {}
        for name in ('socket.socket.connect','socket.create_connection','socket.getaddrinfo',
                     'requests.sessions.Session.request','ccxt.phemex.__init__',
                     'account_discovery.AccountDiscovery.discover','config.load_settings',
                     'manual_testnet_status.load_credentials','manual_testnet_status.collect_manual_status',
                     'operation_journal.OperationJournal.resolve','operation_journal.OperationJournal.__init__',
                     'ccxt.phemex.create_order','ccxt.phemex.cancel_order','ccxt.phemex.set_leverage'):
            guard=patch(name,side_effect=AssertionError('Prohibited capability'))
            self.prohibited_calls[name] = guard.start()
            self.addCleanup(guard.stop)
        # Run before unpatching, even when the engine swallows an assertion or
        # the test invokes the public API directly instead of self.compare().
        self.addCleanup(self.assert_no_prohibited_calls)
        self.scope,self.a,self.b,self.maps=fixture()

    def assert_no_prohibited_calls(self):
        for denied in self.prohibited_calls.values():
            denied.assert_not_called()

    def compare(self, **changes):
        args={'scope':self.scope,'primary':self.a,'secondary':self.b,'mappings':self.maps,'now':NOW}
        args.update(changes)
        report = x.compare_position_inventory(**args)
        self.assert_no_prohibited_calls()
        return report

    def test_prohibited_call_assertions_detect_swallowed_failures(self):
        for category in ('network','credentials','execution'):
            with self.subTest(category=category):
                denied = Mock(side_effect=AssertionError('Prohibited capability'))
                probe = InventoryTests()
                probe.scope,probe.a,probe.b,probe.maps = fixture()
                probe.prohibited_calls = {category:denied}
                # Simulate a regression calling a denied capability inside the
                # response parser. Its broad handler converts that failure to
                # UNKNOWN, but the independent call-count assertion must fail.
                with patch.object(x,'_identity',side_effect=denied):
                    report = x.compare_position_inventory(
                        probe.scope,probe.a,probe.b,mappings=probe.maps,now=NOW)
                    self.unknown(report)
                    with self.assertRaises(AssertionError):
                        probe.compare()
                    with self.assertRaises(AssertionError):
                        probe.assert_no_prohibited_calls()
                self.assertGreater(denied.call_count,0)

    def unknown(self, report):
        self.assertIsNone(report.crosscheck.value,report.render())
        self.assertIsNone(report.corroborated_selected_zero.value,report.render())
        self.assertIsNone(report.account_wide_exposure.value)
        self.assertIsNone(report.inventory_complete.value)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.may_resolve_journal)
        self.assertFalse(report.authenticated_evidence_verified)

    def test_dynamic_btc_eth_xrp_and_new_native_symbols(self):
        for symbol in ('BTCUSDT','ETHUSDT','XRPUSDT','NEWCOINUSDT','1000SHIBUSDT'):
            scope,a,b,maps=fixture(symbol)
            report=x.compare_position_inventory(scope,a,b,mappings=maps,now=NOW)
            self.assertTrue(report.crosscheck.value,report.render())
            self.assertEqual(report.native_symbol,symbol)
            self.assertEqual(report.endpoints[0].legs[0].quantity.value,Decimal('0.002'))
            self.assertFalse(report.corroborated_selected_zero.value)

    def test_explicit_zero_is_observed_and_corroborated_not_account_exposure(self):
        scope,a,b,maps=fixture(long='0',short='0')
        report=x.compare_position_inventory(scope,a,b,mappings=maps,now=NOW)
        self.assertTrue(report.crosscheck.value)
        self.assertTrue(all(s.observed_selected_zero.value for s in report.endpoints))
        self.assertTrue(report.corroborated_selected_zero.value)
        self.assertIsNone(report.account_wide_exposure.value)
        self.assertIsNone(report.stable_account_binding.value)
        self.assertFalse(report.execution_authorized)

    def test_hedged_nonzero_net_zero_is_not_zero_gross_exposure(self):
        scope,a,b,maps=fixture(long='2',short='2')
        report=x.compare_position_inventory(scope,a,b,mappings=maps,now=NOW)
        self.assertTrue(report.crosscheck.value)
        self.assertFalse(report.corroborated_selected_zero.value)

    def test_no_quantity_convention_is_inferred_even_for_zero(self):
        scope,a,b,_=fixture(long='0')
        report=x.compare_position_inventory(scope,a,b,now=NOW)
        self.unknown(report)
        self.assertTrue(all(leg.quantity.value is None for s in report.endpoints for leg in s.legs))

    def test_missing_selected_rows_never_mean_zero(self):
        for target in (self.a,self.b):target.body['data']['positions']=[]
        report=self.compare();self.unknown(report)
        self.assertTrue(all(s.accepted_response.value for s in report.endpoints))
        self.assertTrue(report.snapshot_identity_consistency.value)

    def test_btc_rows_cannot_supply_missing_eth_or_xrp_rows(self):
        for symbol in ('ETHUSDT','XRPUSDT'):
            scope,a,b,maps=fixture(symbol)
            a=replace(a,body=response('BTCUSDT',long='0'))
            b=replace(b,body=response('BTCUSDT',long='0',field='size'))
            report=x.compare_position_inventory(scope,a,b,mappings=maps,now=NOW)
            self.unknown(report)
            self.assertTrue(all(leg.quantity.value is None for s in report.endpoints for leg in s.legs))

    def test_one_endpoint_omission_preserves_other_observation(self):
        self.b.body['data']['positions']=[]
        report=self.compare();self.unknown(report)
        self.assertEqual(report.endpoints[0].legs[0].quantity.value,Decimal('0.002'))
        self.assertIsNone(report.endpoints[1].legs[0].quantity.value)

    def test_each_missing_leg_is_unknown(self):
        for index in (0,1):
            b=deepcopy(self.b);b.body['data']['positions'].pop(index)
            self.unknown(self.compare(secondary=b))

    def test_identical_and_conflicting_duplicate_legs_never_get_summed(self):
        for amount in ('0.002','0.003'):
            b=deepcopy(self.b);extra=deepcopy(b.body['data']['positions'][0]);extra['size']=amount
            b.body['data']['positions'].append(extra)
            report=self.compare(secondary=b);self.unknown(report)
            self.assertIsNone(report.endpoints[1].legs[0].quantity.value)

    def test_row_order_does_not_change_join(self):
        original=self.compare()
        self.b.body['data']['positions'].reverse()
        self.assertEqual(original,self.compare())

    def test_unrelated_rows_are_not_rendered(self):
        for obs,field in ((self.a,'sizeRq'),(self.b,'size')):
            extra=response('PRIVATEUSDT',field=field)['data']['positions']
            obs.body['data']['positions'].extend(extra)
        report=self.compare()
        self.assertTrue(report.crosscheck.value)
        self.assertNotIn('PRIVATEUSDT',report.render()+repr(report))

    def test_exact_native_identity_no_aliases_or_prefix_rewrites(self):
        for symbol in ('BTC/USDT:USDT','btcusdt','BTC','sBTCUSDT'):
            b=deepcopy(self.b)
            for row in b.body['data']['positions']:row['symbol']=symbol
            self.unknown(self.compare(secondary=b))

    def test_wrong_account_currency(self):
        for value in ('BTC','USDC',None,[],True):
            b=deepcopy(self.b);b.body['data']['account']['currency']=value
            report=self.compare(secondary=b);self.unknown(report)
            self.assertIsNone(report.endpoints[1].scope.value)

    def test_conflicting_row_and_envelope_scope(self):
        for location in ('payload','data','account','row'):
            for field,value in (('currency','BTC'),('settleCurrency','USDC'),('quoteCurrency',None)):
                b=deepcopy(self.b)
                objects={'payload':b.body,'data':b.body['data'],'account':b.body['data']['account'],
                         'row':b.body['data']['positions'][0]}
                objects[location][field]=value
                self.unknown(self.compare(secondary=b))

    def test_root_symbol_scope_is_checked(self):
        for location in ('payload','data','account'):
            b=deepcopy(self.b)
            target={'payload':b.body,'data':b.body['data'],'account':b.body['data']['account']}[location]
            target['symbol']='ETHUSDT'
            self.unknown(self.compare(secondary=b))

    def test_missing_identity_is_not_authentication_or_binding(self):
        for name in ('accountId','userID'):
            b=deepcopy(self.b);del b.body['data']['account'][name]
            report=self.compare(secondary=b);self.unknown(report)
            self.assertIsNone(report.snapshot_identity_consistency.value)
            self.assertTrue(report.endpoints[1].accepted_response.value)

    def test_conflicting_identifiers_and_aliases_are_hidden(self):
        variants=(('account','accountId','333333333333'),('account','accountID','333333333333'),
                  ('account','userID','333333333333'),('row','accountID','333333333333'),
                  ('row','userID','333333333333'),('data','accountId','333333333333'),
                  ('payload','userID','333333333333'))
        for place,key,value in variants:
            b=deepcopy(self.b)
            obj={'account':b.body['data']['account'],'row':b.body['data']['positions'][0],
                 'data':b.body['data'],'payload':b.body}[place]
            obj[key]=value
            report=self.compare(secondary=b);self.unknown(report)
            self.assertNotIn(value,report.render()+repr(report))

    def test_identifier_int_string_aliases_may_agree(self):
        self.b.body['data']['account']['accountID']=int(ACCOUNT)
        self.b.body['data']['account']['userId']=int(USER)
        self.assertTrue(self.compare().crosscheck.value)

    def test_invalid_identifier_types_fail_closed(self):
        for value in (True,0,'','0','01',[],{},10**10000):
            b=deepcopy(self.b);b.body['data']['account']['accountId']=value
            self.unknown(self.compare(secondary=b))

    def test_one_way_or_missing_mode_is_not_inferred_from_direction(self):
        for fields in ({'posMode':'OneWay','posSide':'Merged'},{'posMode':None},{'posSide':None},
                       {'posMode':'OneWay'},{'posSide':1}):
            b=deepcopy(self.b);b.body['data']['positions'][0].update(fields)
            self.unknown(self.compare(secondary=b))

    def test_partial_row_cannot_hide_contradictory_mode(self):
        self.b.body['data']['positions']=[{'symbol':'BTCUSDT','accountID':ACCOUNT,
                                          'posMode':'OneWay','posSide':'Merged'}]
        self.unknown(self.compare())

    def test_nonzero_wrong_direction_is_not_accepted(self):
        for value in ('Sell','None',None,False):
            b=deepcopy(self.b);b.body['data']['positions'][0]['side']=value
            self.unknown(self.compare(secondary=b))

    def test_zero_does_not_hide_direction_conflict(self):
        self.b.body['data']['positions'][1]['side']='Sell'
        self.unknown(self.compare())

    def test_flat_without_quantity_is_not_explicit_zero(self):
        for obs,field in ((self.a,'sizeRq'),(self.b,'size')):
            for row in obs.body['data']['positions']:row['side']='None';row.pop(field)
        report=self.compare();self.unknown(report)
        self.assertTrue(all(s.observed_selected_zero.value is None for s in report.endpoints))

    def test_each_overlapping_field_is_compared(self):
        for key,value,name in (('size','0.003','quantity'),('leverageRr','-20','configured_leverage'),
                               ('crossMargin',False,'margin_mode'),('positionStatus','Adl','position_status')):
            b=deepcopy(self.b);b.body['data']['positions'][0][key]=value
            report=self.compare(secondary=b);self.unknown(report)
            self.assertIsNone(next(c for c in report.comparisons if c.side=='Long' and c.field==name).agreement.value)

    def test_missing_field_does_not_erase_independent_comparisons(self):
        del self.b.body['data']['positions'][0]['leverageRr']
        report=self.compare();self.unknown(report)
        quantity=next(c for c in report.comparisons if c.side=='Long' and c.field=='quantity')
        self.assertTrue(quantity.agreement.value)

    def test_conflict_survives_a_different_missing_field(self):
        row=self.b.body['data']['positions'][0]
        row['size']='0.003';del row['leverageRr']
        report=self.compare();self.unknown(report)
        self.assertIsNone(next(c for c in report.comparisons if c.side=='Long' and c.field=='quantity').agreement.value)

    def test_zero_or_malformed_leverage_does_not_get_a_default(self):
        for value in ('0','NaN','Infinity',[],False,15.0):
            b=deepcopy(self.b);b.body['data']['positions'][0]['leverageRr']=value
            report=self.compare(secondary=b);self.unknown(report)
            self.assertIsNone(report.endpoints[1].legs[0].configured_leverage.value)

    def test_margin_flag_and_leverage_sign_must_agree(self):
        for value in (False,'true',None,1):
            b=deepcopy(self.b);b.body['data']['positions'][0]['crossMargin']=value
            self.unknown(self.compare(secondary=b))

    def test_size_field_requires_its_own_endpoint_specific_mapping(self):
        report=self.compare(mappings=self.maps[:1]);self.unknown(report)
        self.assertIsNotNone(report.endpoints[0].legs[0].quantity.value)
        self.assertIsNone(report.endpoints[1].legs[0].quantity.value)

    def test_wrong_or_unknown_mapping_cannot_scale_or_convert(self):
        mapping=self.maps[1]
        for change in ({'native_symbol':'ETHUSDT'},{'base_asset':'ETH'},{'field':'sizeRq'},
                       {'endpoint':x.Endpoint.POSITIONS},{'encoding':x.Encoding.UNKNOWN},
                       {'encoding':'contracts'},{'encoding':'scaled_integer'},{'origin':x.Origin.SUPPLIED_RESPONSE}):
            self.unknown(self.compare(mappings=(self.maps[0],replace(mapping,**change))))

    def test_duplicate_mapping_rejects_ambiguity(self):
        self.unknown(self.compare(mappings=(*self.maps,self.maps[1])))

    def test_both_size_fields_require_mappings_and_exact_equality(self):
        row=self.b.body['data']['positions'][0];row['sizeRq']='0.0020'
        self.unknown(self.compare())
        extra=replace(self.maps[1],field='sizeRq')
        self.assertTrue(self.compare(mappings=(*self.maps,extra)).crosscheck.value)
        row['sizeRq']='0.002000000000000000000000000000000000001'
        self.unknown(self.compare(mappings=(*self.maps,extra)))

    def test_negative_nonfinite_float_bool_or_excessive_quantity_is_unknown(self):
        for value in ('-1','NaN','Infinity',0.002,True,None,{},'1e100000',10**10000):
            b=deepcopy(self.b);b.body['data']['positions'][0]['size']=value
            self.unknown(self.compare(secondary=b))

    def test_decimal_precision_is_preserved_under_small_context(self):
        value='123456789.1234567890123456789012345678'
        self.a.body['data']['positions'][0]['sizeRq']=value
        self.b.body['data']['positions'][0]['size']=value
        with localcontext() as context:
            context.prec=2
            report=self.compare()
        self.assertTrue(report.crosscheck.value)
        self.assertEqual(report.endpoints[0].legs[0].quantity.value,Decimal(value))

    def test_freshness_boundary_is_strict(self):
        a=replace(self.a,started_at=NOW-timedelta(seconds=5))
        self.unknown(self.compare(primary=a))
        a=replace(a,started_at=NOW-timedelta(seconds=4.999999))
        self.assertTrue(self.compare(primary=a).crosscheck.value)

    def test_future_reversed_and_naive_timestamps_fail_closed(self):
        for changes in ({'completed_at':NOW+timedelta(seconds=1)},
                        {'started_at':NOW,'completed_at':NOW-timedelta(seconds=1)},
                        {'started_at':NOW.replace(tzinfo=None)}, {'started_at':'PRIVATE_TIME'}):
            self.unknown(self.compare(primary=replace(self.a,**changes)))

    def test_extreme_start_timestamps_return_unknown(self):
        for slot,observation,index in (('primary',self.a,0),('secondary',self.b,1)):
            for stamp in EXTREME_TIMESTAMPS:
                with self.subTest(slot=slot,stamp=stamp):
                    report=self.compare(**{slot:replace(observation,started_at=stamp)})
                    self.unknown(report)
                    self.assertIsNone(report.endpoints[index].started_at)
                    self.assertIsNone(report.endpoints[index].fresh.value)

    def test_extreme_completion_timestamps_return_unknown(self):
        for slot,observation,index in (('primary',self.a,0),('secondary',self.b,1)):
            for stamp in EXTREME_TIMESTAMPS:
                with self.subTest(slot=slot,stamp=stamp):
                    report=self.compare(**{slot:replace(observation,completed_at=stamp)})
                    self.unknown(report)
                    self.assertIsNone(report.endpoints[index].completed_at)
                    self.assertIsNone(report.endpoints[index].fresh.value)

    def test_extreme_evaluation_timestamps_return_unknown(self):
        for stamp in EXTREME_TIMESTAMPS:
            with self.subTest(stamp=stamp):
                report=self.compare(now=stamp)
                self.unknown(report)
                self.assertTrue(all(item.fresh.value is None for item in report.endpoints))

    def test_invalid_now_and_clock_domain(self):
        for now in (None,NOW.replace(tzinfo=None),True,'PRIVATE_TIME'):
            self.unknown(self.compare(now=now))
        self.unknown(self.compare(secondary=replace(self.b,clock=x.Clock.UNKNOWN)))

    def test_window_mismatch_and_reversed_request_order(self):
        for changes in ({'window_id':'other_window'},{'window_id':False},
                        {'started_at':NOW-timedelta(seconds=2.5)},
                        {'started_at':NOW-timedelta(seconds=4),'completed_at':NOW-timedelta(seconds=3.5)}):
            self.unknown(self.compare(secondary=replace(self.b,**changes)))

    def test_window_token_is_not_printed(self):
        a=replace(self.a,window_id='PRIVATE_WINDOW');b=replace(self.b,window_id='PRIVATE_WINDOW')
        report=self.compare(primary=a,secondary=b)
        self.assertTrue(report.crosscheck.value)
        self.assertNotIn('PRIVATE_WINDOW',report.render()+repr(report)+repr(a))

    def test_provenance_is_independent_and_never_authenticates(self):
        b=replace(self.b,origin=x.Origin.SUPPLIED_RESPONSE)
        report=self.compare(secondary=b);self.unknown(report)
        self.assertEqual([s.origin for s in report.endpoints],[x.Origin.SYNTHETIC,x.Origin.SUPPLIED_RESPONSE])
        self.unknown(self.compare(primary=replace(self.a,origin='authenticated_exchange')))

    def test_supplied_responses_remain_unverified_even_with_matching_assertions(self):
        a=replace(self.a,origin=x.Origin.SUPPLIED_RESPONSE);b=replace(self.b,origin=x.Origin.SUPPLIED_RESPONSE)
        maps=tuple(replace(m,origin=x.Origin.SUPPLIED_RESPONSE) for m in self.maps)
        report=self.compare(primary=a,secondary=b,mappings=maps)
        self.assertTrue(report.crosscheck.value)
        self.assertFalse(report.authenticated_evidence_verified)
        self.assertIn('unverified input assertions',report.render())

    def test_swapped_missing_or_forged_endpoint_is_rejected(self):
        self.unknown(self.compare(primary=self.b,secondary=self.a))
        self.unknown(self.compare(primary=None))
        for endpoint in ('https://api.phemex.com/g-accounts/positions?currency=USDT',
                         'https://testnet-api.phemex.com/g-orders',x.Endpoint.POSITIONS.value):
            self.unknown(self.compare(primary=replace(self.a,endpoint=endpoint)))

    def test_no_status_except_integer_http_200_is_success(self):
        for status in (101,199,204,301,302,400,401,403,429,500,True,'200',None):
            report=self.compare(secondary=replace(self.b,http_status=status));self.unknown(report)
            self.assertIsNone(report.endpoints[1].accepted_response.value)

    def test_response_failure_cannot_erase_successful_primary_observation(self):
        body={'code':10500,'msg':'PRIVATE_SECRET','data':self.b.body['data']}
        report=self.compare(secondary=replace(self.b,body=body,http_status=500));self.unknown(report)
        self.assertTrue(report.endpoints[0].accepted_response.value)
        self.assertIsNone(report.endpoints[1].accepted_response.value)
        self.assertNotIn('PRIVATE_SECRET',report.render())

    def test_duplicate_json_bytes_reject_before_interpretation(self):
        raw=json.dumps(self.b.body).encode()
        variants=(raw.replace(b'"code": 0',b'"code":10500,"code":0'),
                  raw.replace(b'"code": 0',br'"co\u0064e":0,"code":0'),
                  raw.replace(b'"posMode": "Hedged"',b'"posMode":"OneWay","posMode":"Hedged"',1),
                  raw.replace(b'"size": "0.002"',b'"size":"0.003","size":"0.002"',1))
        for body in variants:self.unknown(self.compare(secondary=replace(self.b,body=body)))

    def test_json_bytes_preserve_decimals_and_support_nested_duplicate_rejection(self):
        raw=json.dumps(self.b.body).replace('"size": "0.002"','"size": 0.002').encode()
        self.assertTrue(self.compare(secondary=replace(self.b,body=raw)).crosscheck.value)
        raw=raw[:-1]+b',"extra":[{"nested":{"key":1,"key":1}}]}'
        self.unknown(self.compare(secondary=replace(self.b,body=raw)))

    def test_malformed_bodies_never_become_empty_inventory(self):
        for body in (None,[],b'\xff',b'{',{'code':True,'data':{}},
                     {'code':0,'data':{'account':{},'positions':{}}},
                     {'code':0,'data':{'account':{'currency':'USDT'},'positions':None}}):
            self.unknown(self.compare(secondary=replace(self.b,body=body)))

    def test_unassignable_rows_block_completeness_without_printing_them(self):
        for row in (None,[],{'symbol':None},{'symbol':'BAD\x1bSYMBOL'}):
            b=deepcopy(self.b);b.body['data']['positions'].append(row)
            report=self.compare(secondary=b);self.unknown(report)
            self.assertNotIn('\x1b',report.render())

    def test_pagination_marks_unknown_without_following_or_printing_cursor(self):
        for cursor in ('PRIVATE_CURSOR',False,[],{}):
            b=deepcopy(self.b);b.body['data']['nextPageArg']=cursor
            report=self.compare(secondary=b);self.unknown(report)
            self.assertNotIn('PRIVATE_CURSOR',report.render())

    def test_empty_cursor_still_cannot_prove_complete_inventory(self):
        for cursor in (None,''):
            b=deepcopy(self.b);b.body['data']['nextPageArg']=cursor
            report=self.compare(secondary=b)
            self.assertTrue(report.crosscheck.value)
            self.assertIsNone(report.inventory_complete.value)

    def test_input_size_bounds_fail_closed(self):
        for body in (b' '*(4*1024*1024+1),{**self.b.body,**{str(i):0 for i in range(129)}}):
            self.unknown(self.compare(secondary=replace(self.b,body=body)))
        b=deepcopy(self.b);b.body['data']['positions']=[{}]*10001
        self.unknown(self.compare(secondary=b))

    def test_reports_and_input_repr_never_echo_private_values_or_balances(self):
        for obs in (self.a,self.b):
            obs.body['secret']='PRIVATE_SECRET';obs.body['signedHeaders']={'token':'PRIVATE_TOKEN'}
            obs.body['data']['account']['extra']='PRIVATE_ACCOUNT_EXTRA'
            for row in obs.body['data']['positions']:row['ignored']='PRIVATE_ROW_EXTRA'
        report=self.compare()
        self.assertTrue(report.crosscheck.value)
        text=report.render()+repr(report)+repr(self.a)+repr(self.b)
        for value in (ACCOUNT,USER,'BALANCE_WITHHELD','PRIVATE_SECRET','PRIVATE_TOKEN',
                      'PRIVATE_ACCOUNT_EXTRA','PRIVATE_ROW_EXTRA','signedHeaders'):
            self.assertNotIn(value,text)

    def test_invalid_scope_cannot_leak_arbitrary_labels(self):
        for scope in (None,{},x.PositionScope('SECRET\n','SECRET'),x.PositionScope('BTC/USDT:USDT','BTC'),
                      x.PositionScope(True,'BTC'),x.PositionScope('BTCUSDT',False)):
            report=self.compare(scope=scope);self.unknown(report)
            self.assertEqual(report.native_symbol,'UNKNOWN')
            self.assertNotIn('SECRET',report.render())

    def test_inputs_are_not_modified_and_result_retains_no_mutable_response(self):
        before=deepcopy((self.a,self.b,self.maps))
        report=self.compare()
        self.assertEqual((self.a,self.b,self.maps),before)
        self.a.body['data']['positions'].clear()
        self.assertEqual(report.endpoints[0].legs[0].quantity.value,Decimal('0.002'))

    def test_no_import_time_clients_credentials_or_transport(self):
        script="""
from unittest.mock import patch
with patch('ccxt.phemex.__init__',side_effect=AssertionError('client')), \\
     patch('requests.sessions.Session.request',side_effect=AssertionError('HTTP')), \\
     patch('socket.socket.connect',side_effect=AssertionError('socket')), \\
     patch('socket.getaddrinfo',side_effect=AssertionError('DNS')), \\
     patch('config.load_settings',side_effect=AssertionError('credentials')):
    import position_inventory_crosscheck
"""
        result=subprocess.run([sys.executable,'-B','-c',script],capture_output=True,text=True,
                              cwd=pathlib.Path(__file__).resolve().parents[1],timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
