"""Synthetic fixtures shaped from Phemex's published REST examples, not captures.

Sources: https://phemex-docs.github.io/#query-risk-mode
https://phemex-docs.github.io/#query-account-positions-with-unrealized-pnl
https://phemex-docs.github.io/#query-risk-unit
https://phemex-docs.github.io/#query-risk-unit-2
https://phemex-docs.github.io/#query-asset
Private identifiers/credentials are deliberately absent. Balances are invented.
"""
from copy import deepcopy
from unittest.mock import Mock

from tests.helpers import MARKET, make_client


def envelope(data):
    return {'code': 0, 'msg': '', 'data': deepcopy(data)}


def raw_position(symbol='BTCUSDT', *, mode='OneWay', pos_side='Merged', side='Buy', size='0.01', leverage='-5'):
    return {
        'symbol': symbol, 'currency': 'USDT', 'posMode': mode, 'posSide': pos_side,
        'side': side, 'sizeRq': size, 'leverageRr': leverage, 'positionStatus': 'Normal',
        'avgEntryPriceRp': '60000', 'markPriceRp': '61000', 'liquidationPriceRp': '50000',
        'unRealisedPnlRv': '10', 'curTermRealisedPnlRv': '-0.5',
        'positionMarginRv': '100', 'assignedPosBalanceRv': '100', 'usedBalanceRv': '100',
        'valueRv': '610', 'maintMarginReqRr': '0.005', 'riskLimitRv': '200000',
    }


POSITIONS = [
    raw_position(),
    raw_position('ETHUSDT', mode='Hedged', pos_side='Long', size='0.2', leverage='3'),
    raw_position('ETHUSDT', mode='Hedged', pos_side='Short', side='None', size='0', leverage='3'),
]
CLASSIC_ACCOUNT = {
    'account': {'currency': 'USDT', 'accountBalanceRv': '1000', 'totalUsedBalanceRv': '100', 'bonusBalanceRv': '0'},
    'positions': POSITIONS,
}
CLASSIC_RISK = [
    {'valuationCcy': 3, 'riskMode': 'CrossAsset', 'symbol': '', 'posSide': '',
     'totalBalanceRv': '1000', 'totalEquityRv': '1010', 'estAvailableBalanceRv': '500',
     'totalPosUnpnlRv': '10', 'totalPosCostRv': '100', 'totalPosMMRv': '5',
     'totalOrdUsedBalanceRv': '0', 'fixedUsedRv': '0', 'marginRatioRr': '0.005', 'userStatus': 'Normal'},
    {'valuationCcy': 3, 'riskMode': 'Isolated', 'symbol': 'ETHUSDT', 'posSide': 'Long',
     'totalBalanceRv': '100', 'totalEquityRv': '110', 'totalPosUnpnlRv': '10'},
]
UNIFIED_RISK = [
    {'currency': 'USDT', 'riskMode': 'MultiAsset', 'riskType': 'MultiAssetContractCross',
     'symbol': 'BTCUSDT', 'posSide': '', 'totalBalanceRv': '1200', 'totalEquityRv': '1210',
     'totalPosUnpnlRv': '10', 'totalDebtRv': '50', 'totalPosCostRv': '100',
     'totalPosMMRv': '5', 'totalOrdUsedBalanceRv': '0', 'fixedUsedRv': '0',
     'marginRatioRr': '0.005', 'userStatus': 'Normal'},
    {'currency': 'USDT', 'riskMode': 'Isolated', 'riskType': 'Isolated',
     'symbol': 'ETHUSDT', 'posSide': 'Long', 'totalBalanceRv': '100', 'totalEquityRv': '110'},
]
ASSET = {'currency': 'USDT', 'balanceRv': '500', 'totalBonusRv': '0', 'borrowedRv': '40',
         'interestRv': '10', 'frozenBalanceAsCollateralRv': '100'}


def discovery_client(mode='Classic'):
    eth = {**deepcopy(MARKET), 'id': 'ETHUSDT', 'symbol': 'ETH/USDT:USDT', 'base': 'ETH', 'baseId': 'ETH'}
    client, ex = make_client(markets=[MARKET, eth])
    ex.currencies['USDT']['info'] = {'code': 3}
    ex.privateGetUtaApiRiskRiskMode = Mock(return_value=envelope({'riskMode': mode}))
    account = deepcopy(CLASSIC_ACCOUNT)
    if mode == 'MultiAsset':
        account['account']['accountBalanceRv'] = '999999'  # must not substitute for UTA wallet
    ex.privateGetGAccountsPositions = Mock(return_value=envelope(account))
    ex.privateGetGAccountsRiskUnit = Mock(return_value=envelope(CLASSIC_RISK))
    ex.privateGetUtaApiRiskRiskUnits = Mock(return_value=envelope(UNIFIED_RISK))
    ex.privateGetUtaBizAssets = Mock(return_value=envelope(ASSET))
    return client, ex
