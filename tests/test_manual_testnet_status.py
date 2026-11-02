"""M1: invented credentials and HTTP adapter responses; all sockets denied."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ccxt
import requests

import manual_testnet_status as m
import testnet_readonly as cli
from safety import SafetyError
from tests.compatibility_fixtures import responses, FAKE_ACCOUNT, FAKE_USER


ROOT = Path(__file__).resolve().parents[1]
KEY, SECRET = 'M1_INVENTED_TESTNET_KEY', 'M1_INVENTED_TESTNET_SECRET'


class ManualStatusTests(unittest.TestCase):
    def setUp(self):
        self.guards = []
        for target in ('socket.socket.connect','socket.create_connection','socket.getaddrinfo',
                       'config.load_settings','ccxt.phemex.create_order','ccxt.phemex.cancel_order',
                       'account_discovery.AccountDiscovery.discover',
                       'operation_journal.OperationJournal.resolve'):
            guard = patch(target,side_effect=AssertionError('Prohibited capability'))
            guard.start(); self.addCleanup(guard.stop)
        self.data = responses()
        self.raw, self.statuses, self.calls, self.options = {}, {}, [], []
        guard = patch('requests.adapters.HTTPAdapter.send',autospec=True,side_effect=self.respond)
        self.http = guard.start(); self.addCleanup(guard.stop)
        guard = patch('ccxt.Exchange.seconds',return_value=1793448000)
        guard.start(); self.addCleanup(guard.stop)
        # Exercise rate-cost selection without sleeping for fake HTTP traffic.
        guard = patch('ccxt.Exchange.throttle',return_value=None)
        guard.start(); self.addCleanup(guard.stop)

    def respond(self, adapter, request, **options):
        self.calls.append(request)
        self.options.append(options)
        entry = next(e for e in m.MANUAL_PLAN if e.url == request.url)
        path = '/' + ('v1/' if entry.api == 'v1' else '') + entry.path
        response = requests.Response()
        response.url, response.status_code = request.url, self.statuses.get(entry.label,200)
        response.raw = io.BytesIO(self.raw.get(entry.label,json.dumps(self.data[path]).encode()))
        response.headers = {'Content-Type':'application/json','Content-Encoding':'identity'}
        response.request = request
        return response

    def collect(self):
        return m.collect_manual_status(m.ManualCredentials(KEY,SECRET))

    def exchange(self):
        ex = m.ManualTestnetPhemex(m.ManualCredentials(KEY,SECRET))
        self.addCleanup(ex.close)
        ex.begin()
        return ex

    def test_three_fixed_requests_and_actual_origin_display(self):
        report = self.collect()
        self.assertTrue(report.complete,report.render())
        self.assertTrue(report.authenticated)
        self.assertEqual([r.url for r in self.calls],[e.url for e in m.MANUAL_PLAN])
        self.assertEqual([r.method for r in self.calls],['GET']*3)
        long,short = report.legs
        self.assertEqual(str(long.quantity.value),'0.002')
        self.assertEqual(long.leverage.value,15)
        self.assertEqual(long.margin.value,'cross')
        self.assertEqual(short.leverage.value,3)
        self.assertEqual(short.margin.value,'isolated')
        self.assertIn('Accounting, account-wide funds, stable account identity and order coverage: UNKNOWN',report.render())
        self.assertIn('Observation window (UTC)',report.render())

    def test_ccxt_signature_and_public_credential_separation(self):
        self.assertTrue(self.collect().complete)
        for request in self.calls[:2]:self.assertEqual(dict(request.headers),{})
        request = self.calls[2]
        expiry = request.headers['x-phemex-request-expiry']
        expected = hmac.new(SECRET.encode(),('/g-accounts/positionscurrency=USDT'+expiry).encode(),hashlib.sha256).hexdigest()
        self.assertEqual(request.headers['x-phemex-access-token'],KEY)
        self.assertEqual(request.headers['x-phemex-request-signature'],expected)

    def test_ccxt_reviewed_rate_limit_weights_are_preserved(self):
        ex=self.exchange()
        with patch.object(ex,'throttle') as throttle:
            ex.set_markets(ex.fetch_markets())
            ex.privateGetGAccountsPositions({'currency':'USDT'})
        self.assertEqual([c.args[0] for c in throttle.call_args_list],[5,5,25])
        self.assertEqual(len(self.calls),3)

    def test_initialization_and_inactive_calls_cannot_dispatch(self):
        ex = m.ManualTestnetPhemex(m.ManualCredentials(KEY,SECRET))
        self.addCleanup(ex.close)
        self.http.assert_not_called()
        with self.assertRaises(SafetyError):ex.fetch_markets()
        self.http.assert_not_called()

    def test_requests_during_ccxt_initialization_are_rejected(self):
        original = ccxt.phemex.after_construct
        def unexpected(ex, config):
            ex.fetch(m.MANUAL_PLAN[0].url)
        with patch.object(ccxt.phemex,'after_construct',unexpected),self.assertRaises(SafetyError):
            m.ManualTestnetPhemex(m.ManualCredentials(KEY,SECRET))
        self.http.assert_not_called()

    def test_route_methods_queries_and_budget_reject(self):
        ex = self.exchange()
        cases = [('g-orders','private','POST',{}),('public/products','v2','POST',{}),
                 ('uta-api/risk/risk-mode','private','GET',{}),
                 ('g-accounts/accountPositions','private','GET',{'currency':'USDT'}),
                 ('public/products','v2','GET',{'extra':'x'}),
                 ('public/products','v2','GET',{'maxRetriesOnFailure':3})]
        for args in cases:
            with self.subTest(args=args),self.assertRaises(SafetyError):ex.fetch2(*args)
        self.http.assert_not_called()

    def test_duplicate_query_pairs_and_extra_private_parameters_reject(self):
        ex=self.exchange()
        with self.assertRaises(SafetyError):ex.request('public/products','v2','GET',[('extra','x'),('extra','y')])
        ex.set_markets(ex.fetch_markets())
        for values in ({'currency':'USDT','extra':'x'},{'currency':'BTC'},
                       [('currency','BTC'),('currency','USDT')]):
            with self.assertRaises(SafetyError):ex.privateGetGAccountsPositions(values)
        self.assertEqual(len(self.calls),2)

    def test_direct_ccxt_fetch_cannot_bypass_final_session_guard(self):
        ex = self.exchange()
        for method,url in [('POST',m.MANUAL_PLAN[0].url),('GET','https://api.phemex.com/public/products'),
                           ('GET',m.MANUAL_PLAN[0].url)]:
            with self.subTest(url=url,method=method),self.assertRaises(SafetyError):
                ccxt.Exchange.fetch(ex,url,method)
        self.http.assert_not_called()

    def test_exact_url_encodings_and_unexpected_bodies_headers_reject(self):
        for url in ['http://testnet-api.phemex.com/public/products',
                    'https://testnet-api.phemex.com:443/public/products',
                    'https://testnet-api.phemex.com/public/%70roducts',
                    m.MANUAL_PLAN[0].url+'?x=1&x=2',m.MANUAL_PLAN[0].url+'#fragment']:
            with self.subTest(url=url),self.assertRaises(SafetyError):self.exchange().fetch(url)
        for headers,body in [({'Cookie':'PRIVATE_SENTINEL'},None),({},b'')]:
            with self.assertRaises(SafetyError):self.exchange().fetch(m.MANUAL_PLAN[0].url,headers=headers,body=body)
        self.http.assert_not_called()

    def test_proxy_tls_redirect_and_retry_configuration(self):
        self.assertTrue(self.collect().complete)
        for options in self.options:
            self.assertTrue(options['verify'])
            self.assertEqual(options['proxies'],{})
            self.assertTrue(options['stream'])
            self.assertEqual(options['timeout'],15)
        for field,value in [('verify',False),('validateServerSsl',False),('httpsProxy','PRIVATE_SENTINEL')]:
            ex = self.exchange();setattr(ex,field,value)
            count=len(self.calls)
            with self.assertRaises(SafetyError):ex.fetch_markets()
            self.assertEqual(len(self.calls),count)

    def test_environment_credentials_and_proxies_not_inherited(self):
        with patch.dict(os.environ,{'PHEMEX_API_KEY':'MAINNET_SENTINEL','PHEMEX_API_SECRET':'MAINNET_SENTINEL',
              'HTTPS_PROXY':'http://invalid','ALL_PROXY':'http://invalid','REQUESTS_CA_BUNDLE':'/invalid',
              'NETRC':'/invalid'}):
            report=self.collect()
        self.assertTrue(report.complete,report.render())
        self.assertNotIn('MAINNET_SENTINEL',report.render())

    def test_redirect_and_all_non200_success_categories_fail(self):
        for status in (101,199,204,301,302,400,401,403,429,500):
            self.statuses['account_positions']=status
            report=self.collect()
            self.assertFalse(report.complete)
            self.assertFalse(report.authenticated)
        self.assertEqual(len(self.calls),30)

    def test_redirect_never_follows_or_prepares_second_host(self):
        original=self.respond
        def redirected(*args,**kwargs):
            r=original(*args,**kwargs);r.status_code=302
            r.headers['Location']='https://api.phemex.com/PRIVATE_SENTINEL'
            return r
        self.http.side_effect=redirected
        report=self.collect()
        self.assertFalse(report.complete)
        self.assertEqual(len(self.calls),1)

    def test_prepared_request_mutation_and_direct_session_send_reject(self):
        original=requests.Session.prepare_request
        def changed(session,request):
            result=original(session,request);result.url='https://api.phemex.com/public/products'
            return result
        with patch.object(requests.Session,'prepare_request',changed):
            self.assertFalse(self.collect().complete)
        ex=self.exchange()
        prepared=requests.Request('GET',m.MANUAL_PLAN[0].url).prepare()
        with self.assertRaises(SafetyError):
            ex.session.send(prepared,verify=True,allow_redirects=False,proxies={},stream=True,timeout=15,cert=None)
        self.http.assert_not_called()

    def test_oversize_compression_and_response_url_mismatch_reject(self):
        original=self.respond
        for change in ('size','encoding','url','hooks'):
            def changed(adapter,request,**options):
                result=original(adapter,request,**options)
                if change=='size':result.raw=io.BytesIO(b'x'*(m.MAX_BYTES+1))
                if change=='encoding':result.headers['Content-Encoding']='gzip'
                if change=='url':result.url='https://api.phemex.com/public/products'
                return result
            self.http.side_effect=changed
            if change=='hooks':
                prepare=requests.Session.prepare_request
                def hooks(session,request):
                    result=prepare(session,request);result.hooks['response']=[lambda response,**kwargs:response]
                    return result
                with patch.object(requests.Session,'prepare_request',hooks):self.assertFalse(self.collect().complete)
            else:self.assertFalse(self.collect().complete)

    def test_http_debug_output_rejects_before_any_send(self):
        with patch.object(m.http.client.HTTPConnection,'debuglevel',1):
            self.assertFalse(self.collect().complete)
        self.http.assert_not_called()

    def test_timeout_and_500_no_retries_or_risk_fallback(self):
        self.http.side_effect=requests.Timeout('PRIVATE_SENTINEL')
        report=self.collect()
        self.assertFalse(report.complete)
        self.assertEqual(self.http.call_count,1)
        self.assertNotIn('PRIVATE_SENTINEL',report.render())

    def test_private_error_codes_and_raw_messages_are_sanitized(self):
        self.raw['account_positions']=b'{"code":10500,"msg":"PRIVATE_SENTINEL"}'
        self.statuses['account_positions']=500
        report=self.collect()
        self.assertFalse(report.authenticated)
        self.assertIn('10500',report.render())
        self.assertNotIn('PRIVATE_SENTINEL',report.render())
        self.assertEqual(len(self.calls),3)

    def test_duplicate_keys_invalid_encoding_and_malformed_envelopes_reject(self):
        for raw in [b'{"code":10500,"code":0,"data":{}}',b'\xff',b'[]',b'{"code":true,"data":{}}',
                    b'{"code":0,"data":{}}',
                    b'{"code":0,"data":{"nested":[{"size":1,"size":2}]}}',b'{']:
            self.raw['account_positions']=raw
            report=self.collect()
            self.assertFalse(report.authenticated)
            self.assertFalse(report.complete)

    def test_missing_short_leg_is_unknown_not_zero(self):
        self.data['/g-accounts/positions']['data']['positions'].pop()
        report=self.collect()
        self.assertTrue(report.authenticated)
        self.assertIsNone(report.legs[1].quantity.value)
        self.assertEqual(report.legs[0].quantity.value,m.Decimal('0.002'))
        self.assertFalse(report.complete)

    def test_empty_positions_are_not_fabricated_flat_legs(self):
        self.data['/g-accounts/positions']['data']['positions']=[]
        report=self.collect()
        self.assertTrue(report.authenticated)
        self.assertTrue(all(l.quantity.value is None for l in report.legs))

    def test_contradictory_modes_duplicate_legs_scope_and_directions(self):
        original=deepcopy(self.data['/g-accounts/positions'])
        for change in ('mode','duplicate','scope','direction','envelope'):
            self.data['/g-accounts/positions']=deepcopy(original)
            data=self.data['/g-accounts/positions']['data'];rows=data['positions']
            if change=='mode':rows[1]['posMode']='OneWay'
            if change=='duplicate':rows.append(deepcopy(rows[0]))
            if change=='scope':data['account']['currency']='BTC'
            if change=='direction':rows[0]['side']='Sell'
            if change=='envelope':data['positions']={}
            report=self.collect()
            self.assertFalse(report.complete,change)
            self.assertIsNone(report.legs[0].quantity.value,change)

    def test_missing_leverage_and_conflicting_margin_are_unknown(self):
        row=self.data['/g-accounts/positions']['data']['positions'][0]
        row['crossMargin']=False
        report=self.collect()
        self.assertIsNone(report.legs[0].margin.value)
        del row['leverageRr']
        report=self.collect()
        self.assertIsNone(report.legs[0].leverage.value)

    def test_quantity_alias_conflict_and_pagination_are_incomplete(self):
        data=self.data['/g-accounts/positions']['data']
        data['positions'][0]['size']='0.5'
        report=self.collect()
        self.assertIsNone(report.legs[0].quantity.value)
        del data['positions'][0]['size']
        data['nextPageArg']='PRIVATE_CURSOR_SENTINEL'
        report=self.collect()
        self.assertFalse(report.complete)
        self.assertNotIn('PRIVATE_CURSOR_SENTINEL',report.render())

    def test_inactive_or_invalid_btc_metadata_prevents_private_request(self):
        self.data['/public/products']['data']['perpProductsV2'][0]['status']='Delisted'
        report=self.collect()
        self.assertFalse(report.authenticated)
        self.assertEqual(len(self.calls),2)

    def test_exact_decimal_private_quantity_preserved(self):
        raw=json.dumps(self.data['/g-accounts/positions']).encode()
        value=b'0.002000000000000000000000000000000000001'
        self.raw['account_positions']=raw.replace(b'"sizeRq": "0.002"',b'"sizeRq": '+value)
        report=self.collect()
        self.assertEqual(report.legs[0].quantity.value,m.Decimal(value.decode()))

    def test_history_caches_never_retain_secrets_even_when_requested(self):
        ex=self.exchange();ex.fetchHistoryCacheSize=10
        ex.set_markets(ex.fetch_markets())
        ex.fetchHistoryCacheSize=10
        ex.privateGetGAccountsPositions({'currency':'USDT'})
        self.assertEqual(list(ex.fetchHistoryCache),[])
        self.assertEqual(list(ex.get_fetch_cache()),[])
        self.assertIsNone(ex.last_request_headers)
        self.assertIsNone(ex.last_json_response)
        self.assertIsNone(ex.last_http_response)

    def test_direct_base_fetch2_history_disabled_on_success_and_private_error(self):
        for failure in (False,True):
            ex=self.exchange();ex.set_markets(ex.fetch_markets())
            ex.fetchHistoryCacheSize=10
            if failure:
                self.statuses['account_positions']=500
                with self.assertRaises(Exception):
                    ccxt.Exchange.fetch2(ex,'g-accounts/positions',['private'],'GET',{'currency':'USDT'})
            else:
                ccxt.Exchange.fetch2(ex,'g-accounts/positions',['private'],'GET',{'currency':'USDT'})
            self.assertEqual(list(ex.fetchHistoryCache),[])
            self.assertEqual(list(ccxt.Exchange.get_fetch_cache(ex)),[])
            self.assertIsNone(ex.last_request_headers)

    def test_mutations_reject_at_request_boundary_without_dispatch(self):
        ex=self.exchange()
        for api,path,method in [('private','g-orders','POST'),('private','g-orders/cancel','DELETE'),
                                ('private','g-positions/leverage','PUT'),('private','wallets/withdraw','POST')]:
            with self.assertRaises(SafetyError):ex.request(path,api,method,{})
        self.http.assert_not_called()

    def test_redacted_report_repr_and_closed_credentials(self):
        report=self.collect()
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.may_resolve_journal)
        for value in (KEY,SECRET,FAKE_ACCOUNT,FAKE_USER,'x-phemex-request-signature'):
            self.assertNotIn(value,report.render()+repr(report))
        self.assertNotIn(KEY,repr(m.ManualCredentials(KEY,SECRET)))

    def test_direct_ccxt_errors_and_timeouts_never_expose_private_text(self):
        for error in (requests.Timeout(SECRET),ccxt.ExchangeError(SECRET),ValueError(SECRET)):
            ex=self.exchange();self.http.side_effect=error
            with self.assertRaises(Exception) as raised:ex.fetch_markets()
            self.assertNotIn(SECRET,str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_no_second_collection_or_fourth_request(self):
        ex=self.exchange();ex.set_markets(ex.fetch_markets())
        ex.privateGetGAccountsPositions({'currency':'USDT'})
        with self.assertRaises(SafetyError):ex.begin()
        with self.assertRaises(SafetyError):ex.privateGetGAccountsPositions({'currency':'USDT'})
        self.assertEqual(len(self.calls),3)

    def test_no_session_or_credential_access_on_help_or_without_optin(self):
        for args in [['--help'],['--status','--env-file','not-read.env']]:
            with patch.object(m,'load_credentials') as load,patch('sys.stdout',io.StringIO()):
                if args==['--help']:
                    with self.assertRaises(SystemExit) as end:cli.main(args)
                    self.assertEqual(end.exception.code,0)
                else:self.assertEqual(cli.main(args),2)
            load.assert_not_called()
        self.http.assert_not_called()

    def test_cli_strict_args_and_nonzero_partial_result(self):
        for args in [['--status'],['--symbol','ETHUSDT'],['--status','--env-file','x.env','--credentials-env'],
                     ['--credentials-env'],['--status','--live']]:
            with patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):cli.main(args)
        with patch.object(m,'load_credentials',return_value=m.ManualCredentials(KEY,SECRET)),patch('sys.stdout',io.StringIO()) as out:
            code=cli.main(['--status','--env-file','fake.env','--authorize-testnet-read'])
        self.assertEqual(code,0,out.getvalue())
        self.data['/g-accounts/positions']['data']['positions']=[]
        with patch.object(m,'load_credentials',return_value=m.ManualCredentials(KEY,SECRET)),patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['--status','--credentials-env','--authorize-testnet-read']),2)

    def test_import_in_fresh_process_has_no_http_or_configuration_side_effect(self):
        code='''
from unittest.mock import patch
with patch('socket.socket.connect',side_effect=AssertionError()), patch('requests.Session.__init__',side_effect=AssertionError()), patch('os.getenv',side_effect=AssertionError()):
 import manual_testnet_status
'''
        p=subprocess.run([sys.executable,'-B','-c',code],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)

    def test_cli_credential_error_is_sanitized(self):
        with patch.object(m,'load_credentials',side_effect=ValueError('PRIVATE_SENTINEL')),patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(cli.main(['--status','--env-file','fake.env','--authorize-testnet-read']),2)
        self.assertNotIn('PRIVATE_SENTINEL',out.getvalue())
        self.http.assert_not_called()


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'manual.env'
        self.path.write_text('PHEMEX_API_KEY='+KEY+'\nPHEMEX_API_SECRET='+SECRET+'\n')
        self.path.chmod(0o600)

    def test_explicit_file_ignores_all_environment_credentials(self):
        result=m.load_credentials(self.path,environ={'PHEMEX_API_KEY':'MAINNET','PHEMEX_TESTNET_API_KEY':'OTHER'})
        self.assertEqual(result.key,KEY)

    def test_environment_requires_complete_explicit_testnet_pair(self):
        env={'PHEMEX_TESTNET_API_KEY':KEY,'PHEMEX_TESTNET_API_SECRET':SECRET}
        self.assertEqual(m.load_credentials(use_environment=True,environ=env).secret,SECRET)
        for bad in ({},{'PHEMEX_API_KEY':KEY,'PHEMEX_API_SECRET':SECRET},{'PHEMEX_TESTNET_API_KEY':KEY}):
            with self.assertRaises(SafetyError):m.load_credentials(use_environment=True,environ=bad)

    def test_ambiguous_sources_and_live_profiles_reject(self):
        with self.assertRaises(SafetyError):m.load_credentials(self.path,use_environment=True)
        with self.assertRaises(SafetyError):m.load_credentials()
        with self.path.open('a') as f:f.write('PHEMEX_ALLOW_LIVE=true\n')
        with self.assertRaises(SafetyError):m.load_credentials(self.path)

    def test_unsafe_files_and_duplicate_keys_reject_without_sensitive_errors(self):
        for text in ['PHEMEX_API_KEY=ONE\nPHEMEX_API_KEY=TWO\nPHEMEX_API_SECRET='+SECRET,
                     'PHEMEX_API_KEY='+KEY,'SECRET_UNKNOWN='+SECRET,'x'*8193]:
            self.path.write_text(text)
            with self.assertRaises(SafetyError) as error:m.load_credentials(self.path)
            self.assertNotIn(SECRET,str(error.exception))
        self.path.chmod(0o644)
        with self.assertRaises(SafetyError):m.load_credentials(self.path)

    def test_symlinks_hardlinks_missing_and_wrong_owner_reject(self):
        alias=self.path.parent/'alias.env';alias.symlink_to(self.path)
        with self.assertRaises(SafetyError):m.load_credentials(alias)
        alias.unlink();os.link(self.path,alias)
        with self.assertRaises(SafetyError):m.load_credentials(self.path)
        alias.unlink()
        with patch('os.geteuid',return_value=os.geteuid()+1),self.assertRaises(SafetyError):m.load_credentials(self.path)
        with self.assertRaises(SafetyError):m.load_credentials(self.path.parent/'missing.env')


if __name__=='__main__':unittest.main()
