from copy import deepcopy
from decimal import Decimal
import socket
import unittest
from unittest.mock import Mock, patch

import ccxt
import requests

from exchange_client import PhemexClient

MARKET = {
    'id': 'BTCUSDT', 'symbol': 'BTC/USDT:USDT',
    'base': 'BTC', 'quote': 'USDT', 'settle': 'USDT',
    'baseId': 'BTC', 'quoteId': 'USDT', 'settleId': 'USDT',
    'type': 'swap', 'spot': False, 'margin': False, 'swap': True,
    'future': False, 'option': False, 'contract': True,
    'linear': True, 'inverse': False, 'active': True, 'contractSize': 1,
    'precision': {'amount': 0.001, 'price': 1},
    'limits': {'amount': {'min': 0.001, 'max': 1000},
               'price': {'min': 1}, 'cost': {'min': 1}}, 'info': {},
}
SYMBOL = MARKET['symbol']


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        for target in (socket.socket, requests.sessions.Session):
            method = 'connect' if target is socket.socket else 'request'
            guard = patch.object(target, method, side_effect=AssertionError('Network prohibited in tests'))
            guard.start()
            self.addCleanup(guard.stop)


def make_client(*, live=False, markets=None, limits=None):
    ex = ccxt.phemex({'enableRateLimit': False})
    ex.set_markets(deepcopy(markets if markets is not None else [MARKET]))
    ex.load_markets = Mock(return_value=ex.markets)
    ex.fetch_order_book = Mock(return_value={'asks': [[100, 50]], 'bids': [[99, 50]]})
    ex.fetch_open_orders = Mock(return_value=[])
    ex.fetch_positions = Mock(return_value=[])
    ex.fetch_balance = Mock(return_value={'USDT': {'free': 100, 'used': 0, 'total': 100}})
    # Keep the first-increment normalized position fixtures useful for dry-close
    # regression tests. New discovery tests use documented raw fixtures directly.
    def native_positions(params):
        rows = []
        for position in ex.fetch_positions():
            symbol = position.get('symbol')
            market = ex.markets.get(symbol) or {}
            contracts = position.get('contracts')
            size = (Decimal(str(contracts)) * Decimal(str(market.get('contractSize', 1)))
                    if contracts is not None else None)
            rows.append({'symbol': market.get('id', symbol), 'sizeRq': size,
                         'side': {'long': 'Buy', 'short': 'Sell'}.get(position.get('side'), position.get('side')),
                         **(position.get('info') or {})})
        return {'code': 0, 'data': {'account': {'currency': 'USDT'}, 'positions': rows}}
    ex.privateGetUtaApiRiskRiskMode = Mock(return_value={'code': 0, 'data': {'riskMode': 'Classic'}})
    ex.privateGetGAccountsPositions = Mock(side_effect=native_positions)
    ex.privateGetGAccountsRiskUnit = Mock(return_value={'code': 0, 'data': []})
    for name in ('create_order', 'edit_order', 'cancel_order', 'cancel_all_orders',
                 'set_leverage', 'set_position_mode', 'set_margin_mode'):
        setattr(ex, name, Mock(side_effect=AssertionError('Mutation prohibited in tests')))
    client = PhemexClient('SYNTHETIC_KEY', 'SYNTHETIC_SECRET', live=live, limits=limits, exchange=ex)
    return client, ex


def open_order(**changes):
    result = {'id': 'order-1', 'clientOrderId': 'client-1', 'symbol': SYMBOL,
              'type': 'limit', 'side': 'buy', 'price': 100, 'amount': 1,
              'filled': 0, 'info': {'posSide': 'Long'}}
    result.update(changes)
    return result


def position(*, direction='long', mode='Hedged', amount=2):
    return {'symbol': SYMBOL, 'side': direction, 'contracts': amount,
            'info': {'posSide': 'Merged' if mode == 'OneWay' else ('Long' if direction == 'long' else 'Short'),
                     'posMode': mode}}
