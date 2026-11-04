"""Small Testnet read-command adapter. No submission or journal authority."""
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import re
from urllib.parse import urlsplit

import ccxt

from account_discovery import AccountDiscovery, number, unknown
from command_parser import parse as parse_existing
from manual_market_catalog import decimal_value, on_grid
from readonly_exchange import ReadOnlyPhemex
from safety import SafetyError
from testnet_transport import decode_response_json


LOOKUP = 'api-data/g-futures/orders/by-order-id'


class TradingReadPhemex(ReadOnlyPhemex):
    """Existing GET guard plus one exact-ID GET; all writes still hit the guard."""
    def __init__(self, config=None):
        if ccxt.__version__ != '4.5.90':
            raise SafetyError('Trading read adapter requires the reviewed CCXT 4.5.90')
        self.public_inventory = self.legacy_inventory = None
        super().__init__(config)
        self.number = Decimal
        self.session.trust_env = False
        self.session.proxies.clear()
        self.session.verify = True
        self.session.auth = None

    def _private_output_off(self):
        super()._private_output_off()
        self.fetchHistoryCacheSize = 0
        self.fetchHistoryCache = deque(maxlen=0)
        self.last_request_headers = self.last_request_body = self.last_request_url = None

    def set_last_request(self, request):
        self.last_request_headers = self.last_request_body = self.last_request_url = None

    def add_fetch_cache(self, data):
        self.fetchHistoryCache = deque(maxlen=0)

    def get_fetch_cache(self):
        return deque(maxlen=0)

    def parse_json(self, text):
        return decode_response_json(text.encode('utf-8'), exact_numbers=True)

    def on_rest_response(self, code, reason, url, method, response_headers, response_body,
                         request_headers, request_body):
        if type(code) is not int:
            raise SafetyError('Read response has no accepted HTTP 200 status; details withheld')
        result = super().on_rest_response(code, reason, url, method, response_headers, response_body,
                                         request_headers, request_body)
        if code != 200:
            # Retain sanitized status/code diagnostics for discovery's existing
            # endpoint-specific 500 handling; never retain or raise the body.
            raise ccxt.ExchangeError('Read response has no accepted HTTP 200 status; details withheld')
        return result

    def request(self, path, api='public', method='GET', params=None, config=None):
        if path == LOOKUP:
            # Do not extend the original shared allowlist or the manual plan.
            testnet(self)
            if (api not in ('private', ['private'], ('private',)) or method != 'GET'
                    or type(params) is not dict or set(params) != {'symbol', 'orderID'}
                    or not identifier(params['orderID'])
                    or not any(m.get('id') == params['symbol'] and m.get('swap') is True
                               and m.get('linear') is True and m.get('settle') == 'USDT'
                               and m.get('active') is True for m in self.markets.values())):
                raise SafetyError('Exact-order read blocked: invalid route parameters or scope')
            self.reset_read_diagnostic()
            self._private_output_off()
            self.session.max_redirects = 0
            return ccxt.phemex.request(self, path, ['private'], 'GET',
                dict(params, maxRetriesOnFailure=0), config or {})
        result = super().request(path, api, method, params, config)
        if path == 'public/products' and isinstance(result, dict):
            self.public_inventory = result.get('data')
        elif path == 'exchange/public/products' and isinstance(result, dict):
            self.legacy_inventory = result.get('data')
        return result


def testnet(exchange):
    if exchange.isSandboxModeEnabled is not True:
        raise SafetyError('Trading read commands require Testnet sandbox mode')
    for url in exchange.urls['api'].values():
        part = urlsplit(url)
        if (part.scheme != 'https' or part.netloc != 'testnet-api.phemex.com'
                or part.query or part.fragment):
            raise SafetyError('Trading read commands rejected a non-Testnet origin')


def identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def parse(parts):
    """Positional conveniences; preserve the existing strict mutation grammar."""
    if not parts:
        return parse_existing(parts)
    parts = list(parts)
    command = parts[0].lower()
    if command in ('price', 'positions', 'orders') and len(parts) > 1 and '=' not in parts[1]:
        parts[1] = 'symbol=' + parts[1]
    if command == 'price':
        return parse_existing(['info', *parts[1:]])
    if command == 'order-status':
        if len(parts) > 1 and '=' not in parts[1]:
            parts[1] = 'ord_id=' + parts[1]
        # Reuse ID/symbol validation without guessing a real default symbol.
        has_symbol = any(p.lower().startswith('symbol=') for p in parts[1:])
        args, flags = parse_existing(['cancel', *parts[1:], *([] if has_symbol else ['symbol=SCOPE_UNKNOWN'])])
        if 'cl_id' in args or flags:
            raise SafetyError('order-status requires an exact exchange order ID')
        if not has_symbol:
            del args['symbol']
        return args, flags
    return parse_existing(parts)


def _market(client, selector):
    testnet(client.ex)
    symbol = client.resolve(selector)
    specs = client.instrument(symbol)
    if specs['active'].value is not True or specs['contract_size'].value is None:
        raise SafetyError('Selected instrument activity or contract sizing is UNKNOWN')
    return client.ex.markets[symbol], specs


def display(fact):
    return str(fact) if fact.value is not None else 'UNKNOWN (' + fact.reason + ')'


def price(client, parts):
    args, _ = parse(parts)
    market, specs = _market(client, args['symbol'])
    book = client.ex.fetch_order_book(market['symbol'], limit=5)
    bid = ask = None
    if type(book) is dict and book.get('symbol') == market['symbol']:
        for key in ('bids', 'asks'):
            levels = book.get(key)
            value = decimal_value(levels[0][0]) if (type(levels) is list and levels
                and type(levels[0]) in (list, tuple) and len(levels[0]) >= 2) else None
            if value is not None and value > 0 and on_grid(value, specs['price_tick'].value):
                if key == 'bids': bid = value
                else: ask = value
    if bid is not None and ask is not None and bid > ask:
        bid = ask = None
    stamp = book.get('timestamp') if type(book) is dict else None
    try:
        observed = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat() if type(stamp) is int else 'UNKNOWN'
    except (ValueError, OverflowError, OSError):
        observed = 'UNKNOWN'
    print(f"{market['id']} bid={bid if bid is not None else 'UNKNOWN'} ask={ask if ask is not None else 'UNKNOWN'} USDT")
    print('Exchange timestamp: ' + observed + '; collected UTC: ' + datetime.now(timezone.utc).isoformat())
    print('Observed order book; freshness and execution price are not guaranteed.')


def positions(client, parts):
    args, _ = parse(parts)
    if 'symbol' not in args:
        return False  # Preserve the existing all-position discovery view.
    market, specs = _market(client, args['symbol'])
    reader = AccountDiscovery(client.ex)
    data = reader.read('positions', client.ex.privateGetGAccountsPositions, {'currency': 'USDT'}, dict)
    valid = (type(data) is dict and type(data.get('account')) is dict
             and data['account'].get('currency') == 'USDT' and type(data.get('positions')) is list)
    rows = data['positions'] if valid else []
    valid = valid and not data.get('nextPageArg') and all(type(r) is dict and type(r.get('symbol')) is str for r in rows)
    selected = [r for r in rows if type(r) is dict and r.get('symbol') == market['id']]
    valid = valid and all(r.get('posMode') == 'Hedged' and r.get('posSide') in ('Long', 'Short')
                         and r.get('currency') in (None, 'USDT') for r in selected)
    account = data['account'] if type(data) is dict and type(data.get('account')) is dict else {}
    for names in (('accountID', 'accountId'), ('userID', 'userId')):
        if any(k in account for k in names):
            expected = _identity(account, names)
            if expected is None or any(any(k in row for k in names) and _identity(row, names) != expected
                                       for row in selected):
                valid = False
    print(market['id'] + ' USDT hedge legs; observed UTC: ' + datetime.now(timezone.utc).isoformat())
    for leg in ('Long', 'Short'):
        matches = [r for r in selected if r.get('posSide') == leg]
        if not valid or len(matches) != 1:
            print(leg + ': quantity=UNKNOWN entry=UNKNOWN leverage=UNKNOWN margin=UNKNOWN (scope, mode, missing or duplicate leg)')
            continue
        p = reader.position(matches[0])
        quantity = p['base_quantity']
        if (not on_grid(quantity.value, specs['quantity_step'].value)
                or quantity.value and p['direction'].value != leg.lower()):
            quantity = unknown('quantity grid or direction unverified')
        entry = p['entry_price'] if quantity.value not in (None, Decimal(0)) else unknown('no verified nonzero position')
        print(f"{leg}: quantity={display(quantity)} {market['base']}; entry={display(entry)}; "
              f"leverage={display(p['configured_leverage'])}; margin={display(p['margin_mode'])}")
    for issue in reader.report.issues:
        print('Issue: ' + issue)
    print('Missing rows are UNKNOWN; account-wide inventory completeness is UNKNOWN. Mutations remain disabled.')
    return True


def _rows(data):
    if type(data) is list:
        return data
    if type(data) is dict and not data.get('nextPageArg') and type(data.get('rows')) is list:
        return data['rows']
    if type(data) is dict and not data.get('nextPageArg') and 'orderID' in data:
        return [data]
    return None


def _identity(row, names):
    present = [row[k] for k in names if k in row]
    if any(type(v) not in (str, int) for v in present):
        return None
    values = [str(v) for v in present]
    return values[0] if values and len(set(values)) == 1 and re.fullmatch(r'[0-9]{1,30}', values[0]) else None


def orders(client, parts):
    args, _ = parse(parts)
    exact = parts[0].lower() == 'order-status'
    bindings = getattr(client, '_read_order_symbols', {})
    selector = args.get('symbol') or bindings.get(args.get('ord_id'))
    if selector is None:
        raise SafetyError('Order scope UNKNOWN; supply symbol=BTCUSDT or first inspect orders for that symbol')
    market, specs = _market(client, selector)
    reader = AccountDiscovery(client.ex)
    frame = reader.read('order_scope', client.ex.privateGetGAccountsPositions, {'currency': 'USDT'}, dict)
    for issue in reader.report.issues:
        print('Issue: ' + issue)
    account = frame.get('account') if type(frame) is dict else None
    if type(account) is not dict or account.get('currency') != 'USDT':
        print('Orders: UNKNOWN (authenticated USDT scope unavailable); no order lookup attempted.')
        return
    account_id = _identity(account, ('accountID', 'accountId'))
    user_id = _identity(account, ('userID', 'userId'))
    if account_id is None or user_id is None:
        print('Orders: UNKNOWN (account identity evidence unavailable); no order lookup attempted.')
        return
    if exact:
        method = client.ex.privateGetApiDataGFuturesOrdersByOrderId
        params = {'symbol': market['id'], 'orderID': args['ord_id']}
    else:
        method = client.ex.privateGetGOrdersActiveList
        params = {'symbol': market['id']}
    data = reader.read('order_status' if exact else 'open_orders', method, params, (list, dict))
    for issue in reader.report.issues:
        print('Issue: ' + issue)
    rows = _rows(data)
    if rows is None or any(type(r) is not dict for r in rows):
        print('Orders: UNKNOWN (missing, malformed or incomplete order response).')
        return
    ids = [r.get('orderID') for r in rows]
    if (any(not identifier(i) for i in ids) or len(set(ids)) != len(ids)
            or any(r.get('symbol') != market['id'] for r in rows)
            or exact and (len(rows) != 1 or ids[0] != args['ord_id'])):
        print('Orders: UNKNOWN (order ID, duplicate rows or symbol binding mismatch).')
        return
    # Request authentication alone cannot prove an order belongs to this account.
    for row in rows:
        aid, uid = _identity(row, ('accountID', 'accountId')), _identity(row, ('userID', 'userId'))
        if (aid != account_id
                or uid is not None and uid != user_id
                or any(k in row for k in ('accountID', 'accountId')) and aid is None
                or any(k in row for k in ('userID', 'userId')) and uid is None
                or row.get('currency') not in (None, 'USDT')):
            print('Orders: UNKNOWN (order account binding unavailable or conflicting).')
            return
    print(market['id'] + ' USDT account-bound order observations; UTC: ' + datetime.now(timezone.utc).isoformat())
    if not rows:
        print('No order rows returned; absence is not proof of cancellation or no conditional orders.')
    for row in rows:
        qty = number(row, 'orderQtyRq', 'order.orderQtyRq', nonnegative=True)
        if not on_grid(qty.value, specs['quantity_step'].value): qty = unknown('order quantity units/grid unverified')
        price_value = number(row, 'priceRp', 'order.priceRp', positive=True)
        if not on_grid(price_value.value, specs['price_tick'].value): price_value = unknown('price grid unavailable or invalid')
        side = row.get('side') if row.get('side') in ('Buy', 'Sell') else 'UNKNOWN'
        leg = row.get('posSide') if row.get('posSide') in ('Long', 'Short') else 'UNKNOWN'
        status = row.get('ordStatus') if row.get('ordStatus') in (
            'Created', 'New', 'PartiallyFilled', 'Filled', 'Canceled', 'Rejected', 'Untriggered', 'Triggered', 'Deactivated') else 'UNKNOWN'
        reduce = row['reduceOnly'] if type(row.get('reduceOnly')) is bool else 'UNKNOWN'
        print(f"ID={row['orderID']} side={side} leg={leg} quantity={display(qty)} {market['base']} "
              f"price={display(price_value)} status={status} reduce_only={reduce}")
        key = row['orderID']
        bindings[key] = market['symbol'] if key not in bindings or bindings[key] == market['symbol'] else None
    client._read_order_symbols = bindings
    print('Conditional-order coverage and complete order history: UNKNOWN. No execution or journal authority.')
