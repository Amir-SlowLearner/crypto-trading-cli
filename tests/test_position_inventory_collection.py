"""Offline transcript adapter tests. No credentials, clients or transport."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import position_inventory_collection as c
import position_inventory_crosscheck as x
from testnet_status_collection import MockFailure, MockHttpResponse, MockResponseProvider
from testnet_transport import PLAN
from safety import SafetyError
from tests import test_position_inventory_crosscheck as fixture_source
from tests.compatibility_fixtures import responses


NOW = fixture_source.NOW


def transcript(symbol='BTCUSDT', *, long='0.002', short='0'):
    scope,a,b,mappings=fixture_source.fixture(symbol,long=long,short=short)
    public=responses()
    product=public['/public/products']['data']['perpProductsV2'][0]
    product.update(symbol=symbol,baseCurrency=scope.base_asset)
    bodies=(public['/public/products'],public['/v1/exchange/public/products'],a.body,b.body)
    times=((NOW-timedelta(seconds=4.8),NOW-timedelta(seconds=4.5)),
           (NOW-timedelta(seconds=4.4),NOW-timedelta(seconds=4.1)),
           (a.started_at,a.completed_at),(b.started_at,b.completed_at))
    records=tuple(c.CollectedGetResponse(
        MockHttpResponse(entry.label,json.dumps(body).encode(),entry.url),start,end,a.window_id)
        for entry,body,(start,end) in zip(PLAN,bodies,times))
    return scope,records,mappings


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.denied={}
        for target in ('socket.socket.connect','socket.create_connection','socket.getaddrinfo',
                       'requests.sessions.Session.__init__','requests.sessions.Session.request',
                       'ccxt.phemex.__init__','ccxt.phemex.create_order','ccxt.phemex.cancel_order',
                       'ccxt.phemex.set_leverage','config.load_settings','dotenv.load_dotenv',
                       'manual_testnet_status.load_credentials','manual_testnet_status.collect_manual_status',
                       'testnet_status_collection.collect_status','testnet_status_collection.collect_runtime_status',
                       'testnet_transport.OfflineGetTransport.begin','testnet_transport.OfflineGetTransport._send_http',
                       'operation_journal.OperationJournal.__init__','operation_journal.OperationJournal.resolve'):
            guard=patch(target,side_effect=AssertionError('Prohibited capability'))
            self.denied[target]=guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(self.assert_no_prohibited_calls)
        self.scope,self.records,self.mappings=transcript()

    def assert_no_prohibited_calls(self):
        for denied in self.denied.values():
            denied.assert_not_called()

    def collect(self, **changes):
        args={'records':self.records,'scope':self.scope,'mappings':self.mappings,'now':NOW}
        args.update(changes)
        report=c.crosscheck_offline_collection(**args)
        self.assert_no_prohibited_calls()
        return report

    def unknown(self, report):
        self.assertFalse(report.crosscheck_complete,report.render())
        self.assertIsNone(report.comparison.crosscheck.value)
        self.assertIsNone(report.comparison.corroborated_selected_zero.value)
        self.assertFalse(report.authenticated_evidence_verified)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.may_resolve_journal)

    def collection_failed(self, report):
        self.assertFalse(report.collection_complete,report.render())
        self.assertIsNone(report.collection_contract.value)
        self.assertTrue(any(not slot.accepted for slot in report.slots))
        self.unknown(report)

    def complete_collection_unknown(self, report):
        self.assertTrue(report.collection_complete,report.render())
        self.assertIs(report.collection_contract.value,True)
        self.assertTrue(all(slot.consumed and slot.accepted for slot in report.slots))
        self.unknown(report)

    def test_failed_collection_assertion_detects_incorrect_positive_flag(self):
        report=self.collect(records=self.records[:3])
        self.collection_failed(report)
        incorrect=replace(report,collection_contract=replace(report.collection_contract,value=True))
        self.assertTrue(incorrect.collection_complete)
        self.unknown(incorrect)  # UNKNOWN corroboration alone misses the regression.
        with self.assertRaises(AssertionError):
            self.collection_failed(incorrect)

    def alter_response(self,index,**changes):
        records=list(self.records)
        records[index]=replace(records[index],response=replace(records[index].response,**changes))
        return tuple(records)

    def alter_record(self,index,**changes):
        records=list(self.records);records[index]=replace(records[index],**changes)
        return tuple(records)

    def alter_body(self,index,mutate):
        body=json.loads(self.records[index].response.body)
        mutate(body)
        return self.alter_response(index,body=json.dumps(body).encode())

    def test_valid_hedge_legs_use_same_fixed_four_plan_and_engine(self):
        original=MockResponseProvider.get
        with patch.object(MockResponseProvider,'get',autospec=True,side_effect=original) as get:
            report=self.collect()
        self.assertEqual([call.args[1] for call in get.call_args_list],list(PLAN))
        self.assertTrue(report.collection_complete)
        self.assertTrue(report.crosscheck_complete,report.render())
        self.assertEqual(tuple(slot.expected_source for slot in report.slots),tuple(p.url for p in PLAN))
        self.assertEqual(tuple(s.endpoint.value for s in report.comparison.endpoints),tuple(p.url for p in PLAN[2:]))
        self.assertEqual(report.comparison.endpoints[0].legs[0].quantity.value,Decimal('0.002'))
        with self.assertRaises(FrozenInstanceError):report.slots=()

    def test_btc_eth_xrp_preserve_exact_native_identity(self):
        for symbol in ('BTCUSDT','ETHUSDT','XRPUSDT','NEWCOINUSDT'):
            scope,records,mappings=transcript(symbol)
            report=self.collect(scope=scope,records=records,mappings=mappings)
            self.assertTrue(report.crosscheck_complete,report.render())
            self.assertEqual(report.comparison.native_symbol,symbol)

    def test_explicit_zero_is_selected_only_never_account_exposure(self):
        scope,records,mappings=transcript(long='0',short='0')
        report=self.collect(scope=scope,records=records,mappings=mappings)
        self.assertTrue(report.comparison.corroborated_selected_zero.value)
        self.assertIsNone(report.comparison.account_wide_exposure.value)
        self.assertIsNone(report.comparison.stable_account_binding.value)
        self.assertIsNone(report.comparison.inventory_complete.value)
        self.assertFalse(report.execution_authorized)

    def test_missing_or_empty_position_rows_never_mean_zero(self):
        for index in (2,3):
            for side in ('Long','Short','both'):
                records=self.alter_body(index,lambda body:body['data'].update(positions=[
                    row for row in body['data']['positions'] if side!='both' and row['posSide']!=side]))
                report=self.collect(records=records)
                self.complete_collection_unknown(report)  # Envelopes, not position completeness.

    def test_duplicate_legs_reject_identical_and_conflicting_rows(self):
        for index in (2,3):
            for quantity in ('0.002','0.003'):
                def mutate(body):
                    row=deepcopy(body['data']['positions'][0])
                    row['sizeRq' if index==2 else 'size']=quantity
                    body['data']['positions'].append(row)
                self.complete_collection_unknown(self.collect(records=self.alter_body(index,mutate)))

    def test_position_field_conflicts_and_missing_values_stay_unknown(self):
        for key,value in (('size','0.003'),('leverageRr','-20'),('crossMargin',False),
                          ('posMode','OneWay'),('posSide','Merged'),('positionStatus','Adl'),('side','Sell')):
            records=self.alter_body(3,lambda body:body['data']['positions'][0].update({key:value}))
            self.complete_collection_unknown(self.collect(records=records))
        records=self.alter_body(3,lambda body:body['data']['positions'][0].pop('leverageRr'))
        self.complete_collection_unknown(self.collect(records=records))

    def test_account_currency_identity_and_symbol_mismatches(self):
        for place,key,value in (('account','currency','BTC'),('account','accountId','333333333333'),
                                ('account','userID','333333333333'),('row','accountID','333333333333'),
                                ('row','currency','USDC'),('row','symbol','ETHUSDT'),
                                ('row','symbol','BTC/USDT:USDT'),('row','symbol','btcusdt')):
            def mutate(body):
                obj=body['data']['account'] if place=='account' else body['data']['positions'][0]
                obj[key]=value
            self.complete_collection_unknown(self.collect(records=self.alter_body(3,mutate)))

    def test_missing_identity_does_not_become_authentication(self):
        records=self.alter_body(3,lambda body:body['data']['account'].pop('userID'))
        report=self.collect(records=records)
        self.complete_collection_unknown(report)
        self.assertIsNone(report.comparison.snapshot_identity_consistency.value)

    def test_quantity_units_require_explicit_endpoint_bound_mappings(self):
        self.complete_collection_unknown(self.collect(mappings=()))
        self.complete_collection_unknown(self.collect(mappings=self.mappings[:1]))
        for change in ({'field':'sizeRq'},{'base_asset':'ETH'}, {'encoding':x.Encoding.UNKNOWN},
                       {'native_symbol':'ETHUSDT'},{'origin':x.Origin.SUPPLIED_RESPONSE}):
            self.complete_collection_unknown(self.collect(mappings=(self.mappings[0],replace(self.mappings[1],**change))))

    def test_unmapped_size_alias_cannot_be_silently_compared(self):
        records=self.alter_body(3,lambda body:body['data']['positions'][0].update(sizeRq='0.002'))
        self.complete_collection_unknown(self.collect(records=records))
        records=self.alter_body(3,lambda body:body['data']['positions'][0].update(sizeRq='0.003'))
        extra=replace(self.mappings[1],field='sizeRq')
        self.complete_collection_unknown(self.collect(records=records,mappings=(*self.mappings,extra)))

    def test_exact_json_decimals_survive_adapter_without_floats(self):
        value='0.002000000000000000000000000000000000001'
        records=list(self.records)
        for index,field in ((2,'sizeRq'),(3,'size')):
            raw=records[index].response.body.replace(('"'+field+'": "0.002"').encode(),
                                                   ('"'+field+'": '+value).encode())
            records[index]=replace(records[index],response=replace(records[index].response,body=raw))
        report=self.collect(records=tuple(records))
        self.assertTrue(report.crosscheck_complete)
        self.assertEqual(report.comparison.endpoints[0].legs[0].quantity.value,Decimal(value))

    def test_closed_input_rejects_callbacks_clients_generators_and_subclasses(self):
        callback=Mock(side_effect=AssertionError('Must not invoke caller'))
        class Subclass(c.CollectedGetResponse):pass
        invalid=(callback,MockResponseProvider(()),list(self.records),iter(self.records),None,
                 (Subclass(self.records[0].response,NOW,NOW,'window'),),({'body':'PRIVATE'},))
        for records in invalid:
            report=self.collect(records=records);self.collection_failed(report)
            self.assertTrue(all(not row.consumed for row in report.slots))
        callback.assert_not_called()

    def test_additional_records_rejected_before_any_provider_consumption(self):
        with patch.object(MockResponseProvider,'get',side_effect=AssertionError('No fifth slot')) as get:
            report=self.collect(records=(*self.records,self.records[0]))
        self.collection_failed(report)
        get.assert_not_called()

    def test_incomplete_transcript_consumes_missing_slot_once_then_stops(self):
        original=MockResponseProvider.get
        for length in range(4):
            with patch.object(MockResponseProvider,'get',autospec=True,side_effect=original) as get:
                report=self.collect(records=self.records[:length])
            self.collection_failed(report)
            self.assertEqual([call.args[1] for call in get.call_args_list],list(PLAN[:length+1]))
            self.assertEqual(sum(slot.consumed for slot in report.slots),length+1)

    def test_reordered_or_repeated_records_cannot_fill_other_slots(self):
        for first,second in ((0,1),(1,2),(2,3)):
            records=list(self.records);records[first],records[second]=records[second],records[first]
            report=self.collect(records=tuple(records));self.collection_failed(report)
            self.assertEqual(sum(slot.consumed for slot in report.slots),first+1)
        records=(*self.records[:3],self.records[2])
        self.collection_failed(self.collect(records=records))

    def test_position_row_order_does_not_change_endpoint_or_leg_binding(self):
        original=self.collect()
        records=list(self.records)
        for index in (2,3):
            body=json.loads(records[index].response.body)
            body['data']['positions'].reverse()
            records[index]=replace(records[index],response=replace(records[index].response,body=json.dumps(body).encode()))
        self.assertEqual(original,self.collect(records=tuple(records)))

    def test_provider_is_closed_and_cannot_restart_after_partial_failure(self):
        providers=[]
        def construct(responses):
            provider=MockResponseProvider(responses)
            providers.append(provider)
            return provider
        with patch.object(c,'MockResponseProvider',side_effect=construct):
            report=self.collect(records=self.alter_response(0,status=500))
        self.collection_failed(report)
        self.assertEqual(len(providers),1)
        self.assertEqual(providers[0].attempts,(PLAN[0].label,))
        with self.assertRaises(SafetyError):providers[0].get(PLAN[1])
        with self.assertRaises(SafetyError):providers[0].begin()

    def test_invalid_scope_or_mapping_container_cannot_gain_default_context(self):
        for scope in (None,{},x.PositionScope('PRIVATE\n','BTC'),x.PositionScope('BTC/USDT:USDT','BTC')):
            report=self.collect(scope=scope);self.complete_collection_unknown(report)
            self.assertEqual(report.comparison.native_symbol,'UNKNOWN')
            self.assertNotIn('PRIVATE',report.render())
        self.complete_collection_unknown(self.collect(mappings=list(self.mappings)))

    def test_response_route_label_query_and_redirect_are_exact(self):
        urls=('https://api.phemex.com/g-accounts/accountPositions?currency=USDT',
              PLAN[3].url+'&currency=USDT',PLAN[3].url+'&symbol=BTCUSDT',
              PLAN[3].url.replace('USDT','BTC'),PLAN[3].url.replace('https://','http://'),
              PLAN[3].url.replace('testnet-api.phemex.com','testnet-api.phemex.com:443'),
              PLAN[3].url+'#fragment',PLAN[3].url.replace('accountPositions','%61ccountPositions'))
        for url in urls:self.collection_failed(self.collect(records=self.alter_response(3,url=url)))
        for changes in ({'label':'account_positions'},{'label':'PRIVATE_ID'},
                        {'redirected':True},{'redirected':0}):
            report=self.collect(records=self.alter_response(3,**changes));self.collection_failed(report)
            self.assertNotIn('PRIVATE_ID',report.render()+repr(report))

    def test_all_failure_slots_stop_without_retry_or_implicit_fallback(self):
        original=MockResponseProvider.get
        for index in range(4):
            for failure in (MockFailure.TIMEOUT,MockFailure.NETWORK,'PRIVATE_ERROR'):
                with patch.object(MockResponseProvider,'get',autospec=True,side_effect=original) as get:
                    report=self.collect(records=self.alter_response(index,failure=failure))
                self.collection_failed(report)
                self.assertEqual([call.args[1] for call in get.call_args_list],list(PLAN[:index+1]))
                self.assertNotIn('PRIVATE_ERROR',report.render())

    def test_failed_http_and_error_envelopes_never_mean_accepted_positions(self):
        for index in range(4):
            for status in (101,199,204,301,302,400,401,403,429,500,True,'200',None):
                report=self.collect(records=self.alter_response(index,status=status));self.collection_failed(report)
                self.assertFalse(report.slots[index].accepted)
            records=self.alter_body(index,lambda body:body.update(code=10500,msg='PRIVATE_FAILURE'))
            report=self.collect(records=records);self.collection_failed(report)
            self.assertNotIn('PRIVATE_FAILURE',report.render())

    def test_private_failure_preserves_independent_primary_observation(self):
        report=self.collect(records=self.alter_response(3,status=500))
        self.collection_failed(report)
        self.assertEqual(report.comparison.endpoints[0].legs[0].quantity.value,Decimal('0.002'))
        self.assertIsNone(report.comparison.endpoints[1].legs[0].quantity.value)

    def test_strict_json_duplicate_keys_at_every_source(self):
        for index in range(4):
            raw=self.records[index].response.body.replace(b'"code": 0',br'"co\u0064e":10500,"code":0')
            self.collection_failed(self.collect(records=self.alter_response(index,body=raw)))
            raw=self.records[index].response.body[:-1]+b',"extra":[{"x":0,"x":0}]}'
            self.collection_failed(self.collect(records=self.alter_response(index,body=raw)))

    def test_malformed_envelopes_and_oversized_bodies_fail_conservatively(self):
        for index in range(4):
            for body in (None,'PRIVATE_RESPONSE',b'{',b'\xff',b'[]',
                         b'{"code":true,"data":{}}',b'{"code":0,"data":null}',
                         b' '*(c.MAX_RESPONSE_BYTES+1)):
                self.collection_failed(self.collect(records=self.alter_response(index,body=body)))
        for index in (2,3):
            for value in ({},{'account':{},'positions':{}},{'positions':[]}):
                body=json.dumps({'code':0,'data':value}).encode()
                self.collection_failed(self.collect(records=self.alter_response(index,body=body)))

    def test_private_paginated_or_unassignable_inventory_remains_unknown(self):
        for index in (2,3):
            records=self.alter_body(index,lambda body:body['data'].update(nextPageArg='PRIVATE_CURSOR'))
            report=self.collect(records=records);self.complete_collection_unknown(report)
            self.assertNotIn('PRIVATE_CURSOR',report.render())
            records=self.alter_body(index,lambda body:body['data']['positions'].append({'symbol':None}))
            self.complete_collection_unknown(self.collect(records=records))

    def test_freshness_boundary_applies_to_all_four_observations(self):
        for index in range(4):
            records=self.alter_record(index,started_at=NOW-timedelta(seconds=5))
            self.collection_failed(self.collect(records=records))
        records=self.alter_record(0,started_at=NOW-timedelta(seconds=4.999999))
        self.assertTrue(self.collect(records=records).crosscheck_complete)

    def test_collection_interval_skew_overlap_reversal_and_future_times(self):
        for index in range(4):
            for changes in ({'started_at':NOW},{'completed_at':NOW+timedelta(seconds=1)},
                            {'started_at':NOW.replace(tzinfo=None)},
                            {'started_at':datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))},
                            {'completed_at':datetime.max.replace(tzinfo=timezone(-timedelta(hours=1)))},
                            {'clock':x.Clock.UNKNOWN}):
                self.collection_failed(self.collect(records=self.alter_record(index,**changes)))
        for index in (1,2,3):
            records=self.alter_record(index,started_at=self.records[index-1].started_at)
            self.collection_failed(self.collect(records=records))
        self.collection_failed(self.collect(now=None))

    def test_window_and_origin_assertions_cannot_be_mixed_or_forged(self):
        for index in range(4):
            for change in ({'window_id':'other'}, {'window_id':False}, {'window_id':'PRIVATE\n'},
                           {'origin':x.Origin.UNKNOWN}, {'origin':'authenticated_exchange'}):
                self.collection_failed(self.collect(records=self.alter_record(index,**change)))
        self.collection_failed(self.collect(records=self.alter_record(3,origin=x.Origin.SUPPLIED_RESPONSE)))

    def test_supplied_responses_and_reanalysis_never_authenticate_or_send(self):
        records=tuple(replace(row,origin=x.Origin.SUPPLIED_RESPONSE) for row in self.records)
        mappings=tuple(replace(m,origin=x.Origin.SUPPLIED_RESPONSE) for m in self.mappings)
        first=self.collect(records=records,mappings=mappings)
        self.assertTrue(first.crosscheck_complete)
        self.assertEqual(first,self.collect(records=records,mappings=mappings))
        self.assertTrue(all(row.origin is x.Origin.SUPPLIED_RESPONSE for row in first.slots))
        self.assertFalse(first.authenticated_evidence_verified)
        self.assertFalse(first.execution_authorized)
        self.assertFalse(first.may_resolve_journal)

    def test_public_envelopes_do_not_verify_instrument_or_quantity_conventions(self):
        records=self.alter_body(0,lambda body:body.update(data={}))
        report=self.collect(records=records,mappings=())
        self.complete_collection_unknown(report)
        self.assertIn('instrument specifications and quantity units NOT verified',report.render())

    def test_input_bodies_and_private_diagnostics_do_not_escape_reports(self):
        records=list(self.records)
        for index in range(4):
            body=json.loads(records[index].response.body)
            body['secret']='PRIVATE_SECRET';body['signedHeaders']={'signature':'PRIVATE_SIGNATURE'}
            if index>=2:
                body['data']['account']['ignored']='PRIVATE_ACCOUNT'
                unrelated=deepcopy(body['data']['positions'][0]);unrelated['symbol']='PRIVATEUSDT'
                body['data']['positions'].append(unrelated)
            records[index]=replace(records[index],response=replace(records[index].response,body=json.dumps(body).encode()))
        report=self.collect(records=tuple(records))
        self.assertTrue(report.crosscheck_complete)
        text=report.render()+repr(report)+repr(records)
        for secret in ('PRIVATE_SECRET','PRIVATE_SIGNATURE','PRIVATE_ACCOUNT','PRIVATEUSDT',
                       fixture_source.ACCOUNT,fixture_source.USER,'BALANCE_WITHHELD',self.records[0].window_id):
            self.assertNotIn(secret,text)

    def test_unexpected_provider_errors_are_sanitized_and_stop(self):
        with patch.object(MockResponseProvider,'get',side_effect=ValueError('PRIVATE_SECRET')) as get:
            report=self.collect()
        self.collection_failed(report)
        self.assertEqual(get.call_count,1)
        self.assertNotIn('PRIVATE_SECRET',report.render()+repr(report))

    def test_input_records_are_not_mutated_or_retained_by_report(self):
        original=deepcopy(self.records)
        report=self.collect()
        self.assertEqual(self.records,original)
        self.assertFalse(any(isinstance(value,c.CollectedGetResponse) for value in report.__dict__.values()))

    def test_import_has_no_credential_client_session_or_network_side_effects(self):
        script='''
from unittest.mock import patch
from contextlib import ExitStack
with ExitStack() as guards:
    for target in ('socket.socket.connect','socket.getaddrinfo',
                   'requests.sessions.Session.__init__','ccxt.phemex.__init__','config.load_settings'):
        guards.enter_context(patch(target,side_effect=AssertionError('Prohibited capability')))
    import position_inventory_collection
'''
        result=subprocess.run([sys.executable,'-B','-c',script],capture_output=True,text=True,
            cwd=pathlib.Path(__file__).resolve().parents[1],timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
