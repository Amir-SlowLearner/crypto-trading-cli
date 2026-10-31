"""Invented responses only. Numeric identity sentinels are not real accounts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.parse import urlparse
from unittest.mock import Mock

import requests

from testnet_compatibility import VerificationPhemex, CompatibilityVerifier, AUTHORIZATION
from tests.account_fixtures import raw_position, envelope


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
FAKE_USER = '111111111111'
FAKE_ACCOUNT = '222222222222'
PRODUCT = {
    'symbol': 'BTCUSDT', 'type': 'PerpetualV2', 'baseCurrency': 'BTC',
    'quoteCurrency': 'USDT', 'settleCurrency': 'USDT', 'status': 'Listed',
    'contractSize': '1', 'qtyStepSize': '0.001', 'tickSize': '0.1',
    'maxOrderQtyRq': '100', 'minOrderValueRv': '1', 'maxLeverage': '100',
}


def account_data():
    rows = [raw_position(mode='Hedged', pos_side='Long', size='0.002', leverage='-15'),
            raw_position(mode='Hedged', pos_side='Short', side='None', size='0', leverage='3')]
    for row in rows:
        row['accountID'] = FAKE_ACCOUNT
    return {'account': {'currency': 'USDT', 'userID': FAKE_USER, 'accountId': FAKE_ACCOUNT,
                        'accountBalanceRv': '100', 'totalUsedBalanceRv': '0'}, 'positions': rows}


def responses():
    return {
        '/public/products': envelope({'currencies': [{'currency': 'USDT', 'code': 3, 'valueScale': 8}],
                                     'perpProductsV2': [deepcopy(PRODUCT)], 'products': []}),
        '/v1/exchange/public/products': envelope([]),
        '/uta-api/risk/risk-mode': envelope({'riskMode': 'Classic'}),
        '/g-accounts/positions': envelope(account_data()),
        '/g-accounts/accountPositions': envelope(account_data()),
        '/g-accounts/risk-unit': envelope([{'currency': 'USDT', 'riskMode': 'CrossAsset',
                                          'totalEquityRv': '100'}]),
        '/uta-api/risk/risk-units': envelope([]),
        '/uta-biz/assets': envelope({'currency': 'USDT'}),
    }


def duplicate_json_cases():
    """Raw ambiguous bytes; a dict cannot retain duplicate object members."""
    cases = (
        ('public_error_then_success', '/public/products', b'"code": 0',
         b'"code": 10500, "code": 0'),
        ('legacy_identical_codes', '/v1/exchange/public/products', b'"code": 0',
         b'"code": 0, "code": 0'),
        ('private_error_then_success', '/g-accounts/positions', b'"code": 0',
         b'"code": 10500, "code": 0'),
        ('crosscheck_error_then_success', '/g-accounts/accountPositions', b'"code": 0',
         b'"code": 10500, "code": 0'),
        ('private_success_then_error', '/g-accounts/positions', b'"code": 0',
         b'"code": 0, "code": 10500'),
        ('contradictory_mode', '/g-accounts/positions', b'"posMode": "Hedged"',
         b'"posMode": "OneWay", "posMode": "Hedged"'),
        ('identical_mode', '/g-accounts/positions', b'"posMode": "Hedged"',
         b'"posMode": "Hedged", "posMode": "Hedged"'),
        ('crosscheck_reversed_mode', '/g-accounts/accountPositions', b'"posMode": "Hedged"',
         b'"posMode": "Hedged", "posMode": "OneWay"'),
        ('contradictory_quantity', '/g-accounts/positions', b'"sizeRq": "0.002"',
         b'"sizeRq": "0.5", "sizeRq": "0.002"'),
        ('identical_quantity', '/g-accounts/accountPositions', b'"sizeRq": "0.002"',
         b'"sizeRq": "0.002", "sizeRq": "0.002"'),
        ('contradictory_step', '/public/products', b'"qtyStepSize": "0.001"',
         b'"qtyStepSize": "0.1", "qtyStepSize": "0.001"'),
        ('identical_step', '/public/products', b'"qtyStepSize": "0.001"',
         b'"qtyStepSize": "0.001", "qtyStepSize": "0.001"'),
        ('contradictory_tick', '/public/products', b'"tickSize": "0.1"',
         b'"tickSize": "1", "tickSize": "0.1"'),
        ('identical_tick', '/public/products', b'"tickSize": "0.1"',
         b'"tickSize": "0.1", "tickSize": "0.1"'),
        ('escaped_code', '/g-accounts/positions', b'"code": 0',
         br'"co\u0064e": 10500, "code": 0'),
        ('escaped_step', '/public/products', b'"qtyStepSize": "0.001"',
         br'"qtyStep\u0053ize": "0.1", "qtyStepSize": "0.001"'),
    )
    for name, path, old, new in cases:
        raw = json.dumps(responses()[path]).encode()
        assert old in raw
        yield name, path, raw.replace(old, new, 1)
    for name, path, extra in (
            ('nested_precision_in_array', '/public/products',
             b'"extra":[{"nested":{"precision":{"amount":1,"amount":2}}}]'),
            ('legacy_nested_array', '/v1/exchange/public/products',
             b'"extra":[[{"field":"same","field":"same"}]]'),
            ('private_sensitive_duplicate', '/g-accounts/positions',
             b'"extra":[{"FIXTURE_SECRET":"PRIVATE_SENTINEL","FIXTURE_SECRET":"PRIVATE_SENTINEL"}]')):
        raw = json.dumps(responses()[path]).encode()
        yield name, path, raw[:-1] + b',' + extra + b'}'


def fixture():
    exchange = VerificationPhemex('FIXTURE_ACCESS_TOKEN', 'FIXTURE_SECRET')
    exchange.enableRateLimit = False  # Mocked transport needs no wall-clock throttling.
    data = responses()
    now = [NOW]

    def reply(prepared, **options):
        assert prepared.method == 'GET'
        assert urlparse(prepared.url).hostname == 'testnet-api.phemex.com'
        assert options == {'timeout': 15, 'verify': True, 'allow_redirects': False, 'proxies': {}}
        path = urlparse(prepared.url).path
        value = data[path]
        if callable(value):
            value = value()
        response = requests.Response()
        response.status_code = 200
        response.url = prepared.url
        response.encoding = 'utf-8'
        response.headers = {'Content-Type': 'application/json'}
        response._content = json.dumps(value).encode()
        return response

    exchange._transport._send_http = Mock(side_effect=reply)
    exchange.fetch = Mock(wraps=exchange.fetch)  # Observe calls without bypassing the guard.
    verifier = CompatibilityVerifier(exchange, clock=lambda: now[0])
    return verifier, exchange, data, now


def collect(verifier):
    return verifier.collect(authorization=AUTHORIZATION)
