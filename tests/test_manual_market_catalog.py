"""Dynamic catalog and per-market parsing: invented metadata, denied sockets."""
from copy import deepcopy
from decimal import Decimal, localcontext
import io
import unittest
from unittest.mock import patch

import ccxt

import manual_testnet_status as m
import testnet_readonly as cli
from manual_market_catalog import build_catalog, on_grid, valid_selector, _reviewed_leverage_margins
from tests.compatibility_fixtures import PRODUCT
from tests import test_manual_testnet_status as manual_tests
from safety import SafetyError


KEY, SECRET = manual_tests.KEY, manual_tests.SECRET


def product(base, *, step='1', tick='0.01', **changes):
    result = dict(PRODUCT, symbol=base + 'USDT', baseCurrency=base,
                  qtyStepSize=step, tickSize=tick)
    result.update(changes)
    return result


def spot_product(native='sOLUSDT', base='OL'):
    """Observed ID spelling; other fields are synthetic CCXT spot-schema data.

    The operator supplied counts/IDs, not the original public metadata rows.
    This fixture must not be cited as proof of sOLUSDT's actual base currency.
    """
    return {'symbol':native, 'type':'Spot', 'baseCurrency':base, 'quoteCurrency':'USDT',
            'status':'Listed', 'baseTickSize':'0.01 ' + base, 'quoteTickSize':'0.0001 USDT'}


def risk_configuration(count=205):
    """Operator-observed field layout with invented values and identities.

    The fixture's JSON numeric literal 100.5 is decoded as Decimal by the real
    response parser before schema validation. No float is used by the validator.
    """
    return [{'index_id':i,'items':[{'notionalValueRv':1000,'maxLeverage':100.5,
            'maintenanceMarginRateRr':'0.005','maintenanceAmountRv':'0'}]} for i in range(count)]


class CatalogCollectionTests(unittest.TestCase):
    # Reuse the same denied-network/HTTP-adapter fixture, without rerunning its
    # inherited test methods under a second TestCase class.
    setUp = manual_tests.ManualStatusTests.setUp
    respond = manual_tests.ManualStatusTests.respond
    exchange = manual_tests.ManualStatusTests.exchange

    def products(self):
        return self.data['/public/products']['data']['perpProductsV2']

    def collect(self, **options):
        return m.collect_manual_status(m.ManualCredentials(KEY, SECRET), **options)

    def add(self, base, *, qty='2', **metadata):
        native = product(base, **metadata)
        self.products().append(native)
        rows = deepcopy(self.data['/g-accounts/positions']['data']['positions'][:2])
        for row in rows:
            row['symbol'] = native['symbol']
        rows[0]['sizeRq'] = qty
        self.data['/g-accounts/positions']['data']['positions'].extend(rows)
        return native

    def test_dynamic_native_and_unified_selection_and_per_market_units(self):
        self.add('ETH', step='0.01', tick='0.05', qty='2.37')
        self.add('SOL', step='0.1', qty='14.2')
        self.add('1000SHIB', step='1', tick='0.000001', qty='152')
        for symbol, base, expected in [('ETHUSDT','ETH','2.37'), ('SOL/USDT:USDT','SOL','14.2'),
                                      ('1000SHIBUSDT','1000SHIB','152')]:
            report = self.collect(symbol=symbol)
            self.assertTrue(report.complete, report.render())
            self.assertEqual(report.market.base, base)
            self.assertEqual(report.legs[0].quantity.value, Decimal(expected))
            self.assertEqual(report.legs[0].contracts.value, Decimal(expected))
            self.assertIn('quantities in ' + base + ' base units', report.render())
        self.assertEqual([r.url for r in self.calls], [e.url for e in m.MANUAL_PLAN] * 3)

    def test_observed_spot_prefix_case_does_not_collide_with_sol_perpetual(self):
        self.add('SOL',step='0.1',qty='1.2')
        self.data['/public/products']['data']['products'].append(spot_product())
        report=self.collect(list_markets=True)
        spot=next(r for r in report.catalog.markets if r.native=='sOLUSDT')
        swap=next(r for r in report.catalog.markets if r.native=='SOLUSDT')
        self.assertEqual((spot.product_type,spot.status),('spot','excluded'))
        self.assertEqual((swap.product_type,swap.status),('perpetual','supported'))
        for selector in ('SOLUSDT','SOL/USDT:USDT','solusdt','sol/usdt:usdt'):
            self.assertEqual(report.catalog.resolve(selector),swap)
        with self.assertRaises(SafetyError):report.catalog.resolve('sOLUSDT')
        before=len(self.calls)
        position_report=self.collect(symbol='SOLUSDT')
        self.assertTrue(position_report.complete,position_report.render())
        self.assertEqual(position_report.legs[0].quantity.value,Decimal('1.2'))
        self.assertEqual(len(self.calls)-before,3)

    def test_exact_spot_selection_cannot_fall_back_to_case_folded_swap(self):
        self.add('SOL',step='0.1')
        self.data['/public/products']['data']['products'].append(spot_product())
        report=self.collect(symbol='sOLUSDT')
        self.assertFalse(report.authenticated)
        self.assertFalse(report.complete)
        self.assertEqual(len(self.calls),2)

    def test_product_type_not_lowercase_s_prefix_establishes_identity(self):
        self.add('SOL')
        self.add('SECOND',symbol='sOLUSDT')  # Explicit perpetual, not a prefix inference.
        report=self.collect(list_markets=True)
        upper=report.catalog.resolve('SOLUSDT')
        mixed=report.catalog.resolve('sOLUSDT')
        self.assertEqual(upper.product_type,'perpetual')
        self.assertEqual(mixed.product_type,'perpetual')
        self.assertNotEqual(upper.native,mixed.native)
        with self.assertRaises(SafetyError):report.catalog.resolve('Solusdt')
        self.assertFalse(self.collect(symbol='Solusdt').authenticated)

    def test_exact_native_id_shared_between_distinct_typed_products(self):
        self.add('SOL')
        self.data['/public/products']['data']['products'].append(spot_product(native='SOLUSDT'))
        original=m.ManualTestnetPhemex.set_markets
        with patch.object(m.ManualTestnetPhemex,'set_markets',autospec=True,side_effect=original) as register:
            report=self.collect(symbol='SOLUSDT')
        self.assertTrue(report.complete,report.render())
        self.assertEqual(report.market.product_type,'perpetual')
        self.assertEqual(sum(r.native=='SOLUSDT' for r in report.catalog.markets),2)
        registered=register.call_args.args[1]
        self.assertTrue(all(row['type']=='swap' for row in registered))

    def test_distinct_perpetual_ids_with_same_ccxt_unified_identity_still_reject(self):
        self.add('SOL')
        self.add('sOL')  # Pinned CCXT canonicalizes this base currency to SOL.
        report=self.collect(list_markets=True)
        collisions=[r for r in report.catalog.markets if r.native in ('SOLUSDT','sOLUSDT')]
        self.assertTrue(all(r.product_type=='perpetual' for r in collisions))
        self.assertTrue(all(r.status=='unsupported' for r in collisions))
        with self.assertRaises(SafetyError):report.catalog.resolve('SOLUSDT')

    def test_unverified_folded_candidate_is_not_discarded_to_select_supported_one(self):
        self.add('SOL')
        self.products().append(product('sol',type='UnfamiliarPerpetual'))
        report=self.collect(list_markets=True)
        self.assertTrue(report.catalog.resolve('SOLUSDT').supported)
        with self.assertRaises(SafetyError):report.catalog.resolve('Solusdt')

    def test_new_listing_discovered_after_refresh_without_symbol_code_changes(self):
        first = self.collect(list_markets=True)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(first.authenticated)
        self.assertTrue(first.complete)
        self.add('NEWCOIN')
        second = self.collect(list_markets=True)
        self.assertEqual(len(self.calls), 4)
        self.assertIn('NEWCOINUSDT', second.render())
        with self.assertRaises(SafetyError):first.catalog.resolve('NEWCOINUSDT')
        self.assertTrue(second.catalog.resolve('NEWCOIN/USDT:USDT').supported)
        self.assertTrue(self.collect(symbol='NEWCOINUSDT').complete)

    def test_exclude_spot_inverse_dated_inactive_and_other_settlement(self):
        for base, changes in [('SPOT', {'type':'Spot'}), ('INVERSE', {'quoteCurrency':'USD','settleCurrency':'BTC'}),
                ('DATED', {'type':'Future'}), ('OFF', {'status':'Delisted'}),
                ('USDC', {'settleCurrency':'USDC'})]:
            self.products().append(product(base, **changes))
        report = self.collect(list_markets=True)
        for native in ('SPOTUSDT','INVERSEUSDT','DATEDUSDT','OFFUSDT','USDCUSDT'):
            row = next(r for r in report.catalog.markets if r.native == native)
            self.assertEqual(row.status, 'excluded')
            with self.assertRaises(SafetyError):report.catalog.resolve(native)
        self.assertTrue(report.catalog.resolve('BTCUSDT').supported)

    def test_unfamiliar_product_types_and_missing_classification_are_visible(self):
        for base, changes in [('ALIEN', {'type':'PerpetualAlien'}), ('PILOT', {'type':'PerpetualPilot'}),
                              ('OLD', {'type':'Perpetual'}), ('STATUS', {'status':None}),
                              ('SETTLE', {'settleCurrency':None})]:
            self.products().append(product(base, **changes))
        report = self.collect(list_markets=True)
        for native in ('ALIENUSDT','PILOTUSDT','OLDUSDT','STATUSUSDT','SETTLEUSDT'):
            self.assertIn(native, report.render())
            self.assertEqual(next(r for r in report.catalog.markets if r.native == native).status,'unsupported')
        self.assertEqual(len(self.calls), 2)

    def test_missing_base_mapping_and_parser_failure_are_not_silently_dropped(self):
        self.products().append(product('BROKEN', baseCurrency=None))
        self.products().append(product('BADPARSE', baseCurrency={'invalid':'shape'}))
        report = self.collect(list_markets=True)
        for native in ('BROKENUSDT','BADPARSEUSDT'):
            self.assertEqual(next(r for r in report.catalog.markets if r.native==native).status,'unsupported')
        self.assertTrue(report.catalog.resolve('BTCUSDT').supported)

    def test_unknown_or_unsupported_selected_market_stops_before_private_get(self):
        self.products().append(product('ODD', contractSize='2'))
        for symbol in ('MISSINGUSDT','ODDUSDT'):
            before=len(self.calls)
            report = self.collect(symbol=symbol)
            self.assertFalse(report.complete)
            self.assertFalse(report.authenticated)
            self.assertEqual(len(self.calls)-before,2)
            self.assertIn('ODDUSDT',report.render())

    def test_contract_size_unit_and_precision_failures_are_unsupported(self):
        changes = [{'contractSize':'2'}, {'contractSize':'1 BTC'}, {'contractSize':'NaN'},
                   {'contractSize':True}, {'qtyStepSize':None}, {'qtyStepSize':'0'},
                   {'qtyStepSize':'NaN'}, {'tickSize':'Infinity'}, {'tickSize':'-1'},
                   {'qtyPrecision':2,'qtyStepSize':'0.001'}, {'pricePrecision':1,'tickSize':'0.01'},
                   {'qtyStepSize':'1e-41'}, {'qtyPrecision':True}, {'quoteCurrency':{'unknown':'shape'}}]
        for i, change in enumerate(changes):self.products().append(product('BAD'+str(i), **change))
        report = self.collect(list_markets=True)
        for i in range(len(changes)):
            self.assertEqual(next(r for r in report.catalog.markets if r.native=='BAD'+str(i)+'USDT').status,'unsupported',i)

    def test_missing_native_size_uses_explicit_pinned_ccxt_convention(self):
        row = self.add('CONVENTION')
        row.pop('contractSize')
        report=self.collect(symbol='CONVENTIONUSDT')
        self.assertTrue(report.complete,report.render())
        self.assertEqual(report.market.contract_size.origin,'derived')
        self.assertIsNone(report.market.minimum_quantity.value)

    def test_duplicate_native_and_unified_ids_cannot_be_coalesced_into_support(self):
        self.products().append(deepcopy(PRODUCT))
        report=self.collect()
        self.assertFalse(report.authenticated)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(all(r.status=='unsupported' for r in report.catalog.markets))
        self.products().pop()
        self.products().append(product('ALIAS',baseCurrency='BTC'))
        report=self.collect(list_markets=True)
        self.assertTrue(all(r.status=='unsupported' for r in report.catalog.markets))

    def test_legacy_merge_conflicts_and_unmatched_rows_are_not_hidden(self):
        self.data['/v1/exchange/public/products']['data']=[dict(PRODUCT,tickSize='0.5'),product('ORPHAN')]
        report=self.collect(list_markets=True)
        self.assertFalse(report.catalog.markets[0].supported)
        self.assertIn('ORPHANUSDT',report.render())
        self.assertIn('contradictory merged/native',report.render())

    def test_contradictory_legacy_duplicate_cannot_hide_behind_matching_last_row(self):
        self.data['/v1/exchange/public/products']['data']=[dict(PRODUCT,contractSize='2'),deepcopy(PRODUCT)]
        report=self.collect()
        self.assertFalse(report.complete)
        self.assertFalse(report.authenticated)
        self.assertEqual(len(self.calls),2)
        self.assertIn('duplicate legacy product identity',report.render())

    def test_native_expiry_cannot_masquerade_as_perpetual(self):
        self.products().append(product('EXPIRY',expiryTime=1800000000000))
        report=self.collect(list_markets=True)
        self.assertFalse(next(m for m in report.catalog.markets if m.native=='EXPIRYUSDT').supported)

    def test_conflicting_underlying_unit_is_not_guessed_from_symbol_text(self):
        self.products().append(product('1000ODD',contractUnderlyingAssets='ODD'))
        self.products().append(product('1000SAME',contractUnderlyingAssets='1000 SAME'))
        report=self.collect(list_markets=True)
        odd=next(m for m in report.catalog.markets if m.native=='1000ODDUSDT')
        same=next(m for m in report.catalog.markets if m.native=='1000SAMEUSDT')
        self.assertFalse(odd.supported)
        self.assertTrue(same.supported)

    def test_partial_or_malformed_catalog_is_not_complete(self):
        del self.data['/public/products']['data']['products']
        report=self.collect()
        self.assertFalse(report.complete)
        self.assertFalse(report.authenticated)
        self.assertTrue(report.catalog.issues)

    def test_unreviewed_product_bucket_is_visible_not_silently_skipped(self):
        self.data['/public/products']['data']['perpProductsV3']=[product('FUTURESCHEMA')]
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertIn('FUTURESCHEMAUSDT',report.render())
        self.assertIn('unreviewed product inventory schema',report.render())
        before=len(self.calls)
        self.assertFalse(self.collect().authenticated)
        self.assertEqual(len(self.calls)-before,2)

    def test_observed_optional_pilot_literal_null_has_no_disclosed_inventory_rows(self):
        self.data['/public/products']['data']['perpProductsPilot']=None
        report=self.collect(list_markets=True)
        self.assertTrue(report.complete,report.render())
        self.assertEqual(report.catalog.issues,())
        self.assertEqual(len(report.catalog.markets),1)
        self.assertTrue(self.collect().complete)
        self.assertEqual(len(self.calls),5)  # Two public reads, then a fresh three-GET status.

    def test_pilot_nonempty_inventory_stays_unsupported_and_prevents_private_reads(self):
        self.data['/public/products']['data']['perpProductsPilot']=[product('PILOT',type='PerpetualPilot')]
        report=self.collect(list_markets=True)
        pilot=next(row for row in report.catalog.markets if row.native=='PILOTUSDT')
        self.assertFalse(pilot.supported)
        self.assertFalse(report.complete)
        self.assertIn('field=perpProductsPilot; shape=array(1)',report.render())
        self.assertFalse(self.collect().authenticated)
        self.assertEqual(len(self.calls),4)

    def test_only_exact_pilot_null_is_recognized_not_other_null_or_malformed_values(self):
        data=self.data['/public/products']['data']
        for value in ('null',False,0,{},[],{'nested':[product('NEWPILOT')]}):
            data['perpProductsPilot']=value
            report=self.collect(list_markets=True)
            self.assertFalse(report.complete,repr(value))
            self.assertTrue(any('field=perpProductsPilot;' in issue for issue in report.catalog.issues))
        del data['perpProductsPilot']
        data['perpProductsUnreviewed']=None
        self.assertFalse(self.collect(list_markets=True).complete)

    def test_required_catalog_null_is_still_a_failure_with_optional_pilot_null(self):
        data=self.data['/public/products']['data']
        data['perpProductsPilot']=None
        data['perpProductsV2']=None
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertIn('Missing or malformed perpProductsV2 inventory',report.render())

    def test_pilot_null_does_not_hide_json_or_exchange_response_failures(self):
        self.data['/public/products']['data']['perpProductsPilot']=None
        for raw in (b'{"code":0,"data":{"perpProductsPilot":null}',
                    b'{"code":10500,"data":{"perpProductsPilot":null}}',
                    b'{"code":0,"data":{"perpProductsPilot":null,"perpProductsPilot":[]}}'):
            self.raw['products']=raw
            before=len(self.calls)
            report=self.collect()
            self.assertFalse(report.complete)
            self.assertFalse(report.authenticated)
            self.assertEqual(len(self.calls)-before,1)

    def test_observed_leverage_margins_array_size_is_not_sufficient_to_classify_schema(self):
        # Only the length/zero-detection result was provided. Element fields are
        # invented placeholders, not sanitized exchange data or a reviewed schema.
        data=self.data['/public/products']['data']
        data['perpProductsPilot']=None
        data['leverageMargins']=[{'unreviewedConfigValue':'1'} for _ in range(205)]
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.catalog.issues),1)
        self.assertIn('field=leverageMargins; shape=array(205); product_rows=0; coverage=UNKNOWN',report.render())
        self.assertFalse(self.collect().authenticated)
        self.assertEqual(len(self.calls),4)

    def test_leverage_margins_cannot_hide_nested_product_definitions(self):
        self.data['/public/products']['data']['leverageMargins']=[{'unreviewedWrapper':[product('MISSING')]}]
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertIn('field=leverageMargins; shape=array(1); product_rows=1; coverage=UNKNOWN',report.render())
        self.assertFalse(next(row for row in report.catalog.markets if row.native=='MISSINGUSDT').supported)

    def test_leverage_margins_incompatible_outer_shapes_cannot_silently_pass(self):
        for value in ('[]',None,False,{},[None],['tier'],[[]]):
            self.data['/public/products']['data']['leverageMargins']=value
            report=self.collect(list_markets=True)
            self.assertFalse(report.complete,repr(value))
            self.assertTrue(any('field=leverageMargins;' in issue for issue in report.catalog.issues))

    def test_reviewed_all_205_risk_entries_are_ancillary_not_additional_markets(self):
        data=self.data['/public/products']['data']
        data['perpProductsPilot']=None
        data['leverageMargins']=risk_configuration()
        report=self.collect(list_markets=True)
        self.assertTrue(report.complete,report.render())
        self.assertEqual(report.catalog.issues,())
        self.assertEqual(len(report.catalog.markets),1)
        self.assertEqual(report.catalog.markets[0].native,'BTCUSDT')
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.may_resolve_journal)
        status=self.collect()
        self.assertTrue(status.complete,status.render())
        self.assertEqual(status.legs[0].leverage.value,Decimal('15'))
        self.assertEqual(len(self.calls),5)

    def test_late_invalid_entries_cannot_hide_after_three_valid_examples(self):
        data=self.data['/public/products']['data']
        for index in (3,100,204):
            data['leverageMargins']=risk_configuration()
            data['leverageMargins'][index]['unexpected']='RAW_SENTINEL'
            report=self.collect(list_markets=True)
            self.assertFalse(report.complete,index)
            self.assertIn('field=leverageMargins;',report.render())
            self.assertNotIn('RAW_SENTINEL',report.render())
        before=len(self.calls)
        self.assertFalse(self.collect().authenticated)
        self.assertEqual(len(self.calls)-before,2)

    def test_every_nested_tier_is_checked_including_late_records(self):
        data=self.data['/public/products']['data']
        data['leverageMargins']=risk_configuration()
        row=data['leverageMargins'][-1]
        row['items']*=5
        row['items'][-1]=dict(row['items'][-1],maintenanceAmountRv={'unreviewed':'nested'})
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertTrue(report.catalog.issues)

    def test_missing_entry_fields_and_incorrect_entry_types_remain_unknown(self):
        data=self.data['/public/products']['data']
        variants=[{'items':[]},{'index_id':1},{'index_id':True,'items':[]},
                  {'index_id':'1','items':[]},{'index_id':1.0,'items':[]},
                  {'index_id':1,'items':{}},{'index_id':1,'items':None},
                  {'index_id':1,'items':[],'extra':[]},None]
        for value in variants:
            data['leverageMargins']=risk_configuration(3)+[value]
            report=self.collect(list_markets=True)
            self.assertFalse(report.complete,repr(value))
            self.assertTrue(report.catalog.issues)

    def test_missing_or_extra_tier_fields_never_get_discarded(self):
        data=self.data['/public/products']['data']
        example=risk_configuration(1)[0]['items'][0]
        variants=[]
        for key in example:
            missing=dict(example);del missing[key];variants.append(missing)
        variants.extend([dict(example,extra='UNREVIEWED'),dict(example,symbol='HIDDENUSDT')])
        for value in variants:
            data['leverageMargins']=[{'index_id':1,'items':[deepcopy(example),value]}]
            report=self.collect(list_markets=True)
            self.assertFalse(report.complete,repr(value))
            self.assertTrue(report.catalog.issues)

    def test_incompatible_scalar_types_in_each_tier_field_are_rejected(self):
        data=self.data['/public/products']['data']
        example=risk_configuration(1)[0]['items'][0]
        for key,bad_values in (
                ('notionalValueRv',[True,'1000',1000.0,None,{},[]]),
                ('maxLeverage',[True,'100.5',100,None,{},[]]),
                ('maintenanceMarginRateRr',[True,1,0.5,None,{},[]]),
                ('maintenanceAmountRv',[False,0,0.0,None,{},[]])):
            for bad in bad_values:
                data['leverageMargins']=[{'index_id':1,'items':[dict(example,**{key:bad})]}]
                report=self.collect(list_markets=True)
                self.assertFalse(report.complete,(key,repr(bad)))
                self.assertTrue(report.catalog.issues)

    def test_malformed_nested_tier_records_remain_unknown(self):
        data=self.data['/public/products']['data']
        for malformed in (None,False,'tier',1,[],[risk_configuration(1)[0]['items'][0]]):
            data['leverageMargins']=[{'index_id':1,'items':[malformed]}]
            self.assertFalse(self.collect(list_markets=True).complete)

    def test_reviewed_empty_arrays_are_structural_only_not_assumed_financial_limits(self):
        for value in ([],[{'index_id':0,'items':[]}]):
            self.data['/public/products']['data']['leverageMargins']=value
            report=self.collect(list_markets=True)
            self.assertTrue(report.complete,report.render())
            self.assertEqual(len(report.catalog.markets),1)
            self.assertIn('Accounting, account-wide funds, stable account identity and order coverage: UNKNOWN',report.render())

    def test_reviewed_risk_array_does_not_suppress_other_unknown_inventories(self):
        data=self.data['/public/products']['data']
        data['leverageMargins']=risk_configuration()
        data['perpProductsPilot']=[product('PILOT',type='PerpetualPilot')]
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertTrue(all('field=perpProductsPilot;' in issue for issue in report.catalog.issues))

    def test_duplicate_tier_keys_reject_before_schema_classification(self):
        raw=b'[{"index_id":1,"items":[{"notionalValueRv":1000,"maxLeverage":100.5,"maxLeverage":100.5,"maintenanceMarginRateRr":"0.005","maintenanceAmountRv":"0"}]}]'
        self.data['/public/products']['data']['leverageMargins']='RAW_TIER_REPLACEMENT'
        import json
        self.raw['products']=json.dumps(self.data['/public/products']).encode().replace(b'"RAW_TIER_REPLACEMENT"',raw)
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertFalse(report.authenticated)
        self.assertEqual(len(self.calls),1)

    def test_scalar_product_field_reports_name_shape_not_value_and_stays_blocking(self):
        # The actual field name behind the operator's warning is not yet known.
        self.data['/public/products']['data']['productVersion']='RAW_VALUE_SENTINEL'
        report=self.collect(list_markets=True)
        self.assertIn('field=productVersion; shape=string; product_rows=0; coverage=UNKNOWN',report.render())
        self.assertNotIn('RAW_VALUE_SENTINEL',report.render())
        self.assertFalse(report.complete)
        self.assertFalse(self.collect().authenticated)
        self.assertEqual(len(self.calls),4)

    def test_nested_and_keyed_unreviewed_inventory_cannot_hide_eligible_looking_rows(self):
        self.data['/public/products']['data']['productMap']={'nested':{'EXTRA':product('EXTRA')}}
        report=self.collect(list_markets=True)
        extra=next(m for m in report.catalog.markets if m.native=='EXTRAUSDT')
        self.assertFalse(extra.supported)
        self.assertIn('field=productMap; shape=object(1); product_rows=1; coverage=UNKNOWN',report.render())
        self.assertFalse(report.complete)

    def test_unexpected_container_without_product_name_also_blocks_coverage(self):
        self.data['/public/products']['data']['newCatalog']=[product('HIDDEN')]
        report=self.collect(list_markets=True)
        self.assertFalse(report.complete)
        self.assertIn('HIDDENUSDT',report.render())
        self.assertIn('field=newCatalog; shape=array(1)',report.render())

    def test_reviewed_auxiliary_risk_metadata_is_not_an_independent_product_inventory(self):
        data=self.data['/public/products']['data']
        data['riskLimitsV2']=[{'symbol':'BTCUSDT','riskLimits':[]}]
        data['leveragesV2']=[]
        data['md5Checksum']='RAW_CHECKSUM_SENTINEL'
        report=self.collect(list_markets=True)
        self.assertTrue(report.complete,report.render())
        self.assertEqual(report.catalog.issues,())
        self.assertEqual(len(report.catalog.markets),1)
        self.assertNotIn('RAW_CHECKSUM_SENTINEL',report.render())

    def test_schema_field_label_rejects_terminal_control_characters(self):
        self.data['/public/products']['data']['product\x1bRAW_SENTINEL']={'arbitrary':'PRIVATE_VALUE'}
        report=self.collect(list_markets=True)
        self.assertIn('field=UNKNOWN_FIELD; shape=object(1)',report.render())
        self.assertNotIn('\x1b',report.render())
        self.assertNotIn('RAW_SENTINEL',report.render())
        self.assertNotIn('PRIVATE_VALUE',report.render())

    def test_missing_selected_positions_stay_unknown_not_btc_or_flat(self):
        self.products().append(product('NOLEGS'))
        report=self.collect(symbol='NOLEGSUSDT')
        self.assertTrue(report.authenticated)
        self.assertFalse(report.complete)
        self.assertTrue(all(l.quantity.value is None for l in report.legs))
        self.assertNotIn('quantity=0 ',report.render())

    def test_distinct_position_grids_do_not_round_or_reuse_btc_grid(self):
        self.add('ETH', step='0.01', qty='0.002')
        report=self.collect(symbol='ETHUSDT')
        self.assertFalse(report.complete)
        self.assertEqual(report.legs[0].quantity.value,Decimal('0.002'))
        self.assertIsNone(report.legs[0].grid_compatible.value)
        self.assertIsNone(report.legs[0].contracts.value)
        self.assertTrue(self.collect().complete)

    def test_non_btc_quantity_aliases_currency_and_mode_contradictions(self):
        self.add('ETH')
        rows=self.data['/g-accounts/positions']['data']['positions']
        rows[-2]['size']='10'
        self.assertFalse(self.collect(symbol='ETHUSDT').complete)
        del rows[-2]['size']
        rows[-1]['currency']='BTC'
        report=self.collect(symbol='ETHUSDT')
        self.assertTrue(all(l.quantity.value is None for l in report.legs))
        del rows[-1]['currency']
        rows[-1]['posMode']='OneWay'
        self.assertFalse(self.collect(symbol='ETHUSDT').complete)

    def test_cli_accepts_native_and_unified_and_catalog_flags(self):
        self.add('ETH',step='0.01')
        with patch.object(m,'load_credentials',return_value=m.ManualCredentials(KEY,SECRET)):
            for args in (['--symbol','ETHUSDT'],['--symbol','eth/usdt:usdt'],['--markets']):
                with patch('sys.stdout',io.StringIO()) as out:
                    code=cli.main(['--status','--credentials-env','--authorize-testnet-read']+args)
                self.assertEqual(code,0,out.getvalue())
            for args in (['--markets'],['--status','--symbol','https://evil'],
                         ['--status','--markets','--symbol','ETHUSDT']):
                with patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):cli.main(args)

    def test_public_labels_are_sanitized_and_never_copy_descriptions(self):
        self.products().append(product('UNSAFE',symbol='\x1b[31mPRIVATE_SENTINEL',description=SECRET))
        report=self.collect(list_markets=True)
        for value in ('\x1b','PRIVATE_SENTINEL',SECRET):self.assertNotIn(value,report.render()+repr(report))

    def test_failure_after_metadata_keeps_catalog_without_false_authentication(self):
        self.add('ETH')
        self.statuses['account_positions']=500
        report=self.collect(symbol='ETHUSDT')
        self.assertFalse(report.authenticated)
        self.assertFalse(report.complete)
        self.assertEqual(report.market.native,'ETHUSDT')
        self.assertEqual(len(self.calls),3)


class CatalogPureTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','socket.create_connection','socket.getaddrinfo'):
            guard=patch(target,side_effect=AssertionError('Offline only'))
            guard.start();self.addCleanup(guard.stop)

    def test_grid_arithmetic_is_exact_under_small_decimal_context(self):
        with localcontext() as context:
            context.prec=2
            self.assertTrue(on_grid(Decimal('123456789.12345678'),Decimal('0.00000001')))
            self.assertFalse(on_grid(Decimal('123456789.123456789'),Decimal('0.00000001')))
        for invalid in (True,0.1,'NaN','Infinity','1e999'):
            self.assertFalse(on_grid(invalid,'0.001'))

    def test_risk_decimal_type_is_exact_and_nonfinite_values_fail_closed(self):
        tier={'notionalValueRv':1000,'maxLeverage':Decimal('100.5'),
              'maintenanceMarginRateRr':'0.005','maintenanceAmountRv':'0'}
        self.assertTrue(_reviewed_leverage_margins([{'index_id':1,'items':[tier]}]))
        for value in (100.5,100,True,Decimal('NaN'),Decimal('Infinity'),Decimal('-Infinity')):
            invalid=dict(tier,maxLeverage=value)
            self.assertFalse(_reviewed_leverage_margins([{'index_id':1,'items':[invalid]}]))

    def test_selector_is_syntax_only_not_a_hardcoded_catalog(self):
        for value in ('BTCUSDT','NEW123USDT','kBONK/USDT:USDT','1000SHIBUSDT'):
            self.assertTrue(valid_selector(value))
        for value in (' BTCUSDT','BTCUSDT\n','https://api.phemex.com',None,True,'A'*81):
            self.assertFalse(valid_selector(value))

    def test_normalized_flags_must_each_match_exact_eligible_contract(self):
        ex=m.ManualTestnetPhemex(m.ManualCredentials(KEY,SECRET))
        self.addCleanup(ex.close)
        raw=deepcopy(PRODUCT)
        market=ex.parse_swap_market(raw)
        for key,value in [('swap',False),('linear',False),('settle','BTC'),('active',None),
                          ('future',True),('expiry',123),('swap',1)]:
            changed=dict(market,**{key:value})
            result=build_catalog({'products':[],'perpProductsV2':[raw]},[],[changed],ccxt.TICK_SIZE)
            self.assertFalse(result.markets[0].supported,key)


if __name__=='__main__':unittest.main()
