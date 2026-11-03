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
