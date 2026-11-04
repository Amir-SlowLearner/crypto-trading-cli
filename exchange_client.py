"""CCXT boundary for USDT linear perpetuals; mutations are intentionally blocked.

CCXT owns signing and transport. The application owns risk and recovery.
No account, position or margin settings are changed during initialization.
"""
from collections import Counter
from decimal import Decimal

from account_discovery import AccountDiscovery
from command_parser import dec
from config import RiskLimits
from safety import SafetyError, reject_mutation
from trading_reads import TradingReadPhemex, TradingReadPhemex as ReadOnlyPhemex
from instrument_specs import inspect_instrument


class PhemexClient:
    def __init__(self, api_key, api_secret, live=False, *, limits=None, exchange=None):
        self.live = live
        self.limits = limits if limits is not None else RiskLimits()
        self.ex = exchange if exchange is not None else ReadOnlyPhemex({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 15000,
            "verbose": False,
            "options": {
                "defaultType": "swap", "defaultSubType": "linear",
                "maxRetriesOnFailure": 0,
            },
        })
        if not live:
            self.ex.set_sandbox_mode(True)
        self.catalog = None
        if exchange is None and isinstance(self.ex, TradingReadPhemex):
            # Two explicit public catalog reads, once per session. load_markets
            # would implicitly fetch currencies through an additional route.
            from manual_market_catalog import build_catalog
            parsed = self.ex.fetch_markets()
            self.catalog = build_catalog(self.ex.public_inventory, self.ex.legacy_inventory,
                                         parsed, self.ex.precisionMode)
            if self.catalog.issues:
                raise SafetyError('Public market inventory is incomplete; interpretation unavailable')
            supported = {(m.native, m.symbol) for m in self.catalog.markets if m.supported}
            self.ex.set_markets([m for m in parsed if (m.get('id'), m.get('symbol')) in supported
                                 and m.get('swap') is True])
            currencies = self.ex.public_inventory.get('currencies')
            # Numeric risk-unit currencies must have an unambiguous native map;
            # never let duplicate public rows silently overwrite that binding.
            if (type(currencies) is list and all(type(r) is dict and type(r.get('currency')) is str
                    and type(r.get('code')) is int and r['code'] >= 0 for r in currencies)):
                names = Counter(r['currency'] for r in currencies)
                codes = Counter(r['code'] for r in currencies)
                for raw in currencies:
                    if (raw['currency'] in self.ex.currencies and names[raw['currency']] == 1
                            and codes[raw['code']] == 1):
                        self.ex.currencies[raw['currency']]['info'] = raw
        else:
            self.ex.load_markets()

    @staticmethod
    def _supported(market):
        native_status = (market.get('info') or {}).get('status')
        return (market.get("swap") is True and market.get("linear") is True
                and market.get("settle") == "USDT" and market.get("quote") == "USDT"
                and market.get("active") is True and native_status in (None, 'Listed'))

    def resolve(self, raw):
        raw = (raw or "").strip()
        if not raw:
            raise SafetyError("Missing symbol")
        if self.catalog is not None:
            return self.catalog.resolve(raw).symbol
        # Preserve exact identities: a spot sOLUSDT is not the SOLUSDT swap.
        exact = [m for m in self.ex.markets.values() if raw in (m.get('id'), m.get('symbol'))]
        candidates = exact or [m for m in self.ex.markets.values() if raw.casefold()
                              in (m['id'].casefold(), m['symbol'].casefold())]
        matches = [m for m in candidates if self._supported(m)]
        if len(matches) != 1:
            raise SafetyError("Symbol must identify one active USDT-settled linear perpetual")
        dec("contract size", str(matches[0].get("contractSize")))
        return matches[0]["symbol"]

    def market_id(self, symbol):
        return self.ex.market(self.resolve(symbol))["id"]

    def market_info(self, symbol):
        m = self.ex.market(self.resolve(symbol))
        specs = self.instrument(symbol)
        return {
            "id": m["id"], "symbol": m["symbol"],
            "contract_size": specs['contract_size'].value,
            "min_amount": specs['min_contracts'].value,
            "max_amount": specs['max_contracts'].value,
            "min_cost": specs['min_notional'].value,
            "max_cost": specs['max_notional'].value,
            "amount_precision": (m.get("precision") or {}).get("amount"),
            "price_precision": (m.get("precision") or {}).get("price"),
        }

    def instrument(self, symbol):
        return inspect_instrument(self.ex, symbol)

    def contract_size(self, symbol):
        return dec("contract size", str(self.market_info(symbol)["contract_size"]))

    def best_price(self, symbol, side):
        if side not in {"buy", "sell"}:
            raise SafetyError("Invalid order side")
        book = self.ex.fetch_order_book(self.resolve(symbol), limit=5)
        levels = book["asks"] if side == "buy" else book["bids"]
        if not levels:
            raise SafetyError("Order book is empty")
        return dec("market price", str(levels[0][0]))

    def round_amount(self, symbol, amount):
        symbol = self.resolve(symbol)
        amount = dec("amount", str(amount))
        # CCXT precision helpers accept decimal strings. Avoid a float round-trip.
        rounded = dec("normalized amount", self.ex.amount_to_precision(symbol, str(amount)))
        info = self.market_info(symbol)
        native = self.ex.market(symbol).get('info') or {}
        if native.get('maxOrderQtyRq') is not None and info['max_amount'] is None:
            raise SafetyError('Native quantity limit is invalid or unverified')
        self._bounds(rounded, info["min_amount"], info["max_amount"], "amount")
        specs = self.instrument(symbol)
        step = specs['quantity_step'].value
        if (self.ex.market(symbol).get('info') or {}).get('qtyStepSize') is not None:
            if step is None or (rounded * self.contract_size(symbol)) % step:
                raise SafetyError('Normalized amount does not satisfy the native quantity step')
        if rounded > amount:
            raise SafetyError("Amount normalization must not increase requested size")
        return rounded

    def round_price(self, symbol, price):
        symbol = self.resolve(symbol)
        price = dec("price", str(price))
        rounded = dec("normalized price", self.ex.price_to_precision(symbol, str(price)))
        if rounded != price:
            raise SafetyError('Requested price is off-grid; prices are not rounded')
        specs = self.instrument(symbol)
        native = self.ex.market(symbol).get('info') or {}
        for field, key in (('minPriceRp', 'min_price'), ('maxPriceRp', 'max_price')):
            if native.get(field) is not None and specs[key].value is None:
                raise SafetyError('Native price bounds are invalid or inconsistent')
        self._bounds(rounded, specs['min_price'].value, specs['max_price'].value, "price")
        tick = specs['price_tick'].value
        if (self.ex.market(symbol).get('info') or {}).get('tickSize') is not None:
            if tick is None or rounded % tick:
                raise SafetyError('Normalized price does not satisfy the native price tick')
        return rounded

    @staticmethod
    def _bounds(value, minimum, maximum, name):
        if minimum is not None and value < Decimal(str(minimum)):
            raise SafetyError(f"Normalized {name} is below the exchange minimum")
        if maximum is not None and value > Decimal(str(maximum)):
            raise SafetyError(f"Normalized {name} exceeds the exchange maximum")

    def notional(self, symbol, amount, price):
        result = amount * self.contract_size(symbol) * price
        info = self.market_info(symbol)
        if (self.ex.market(symbol).get('info') or {}).get('minOrderValueRv') is not None and info['min_cost'] is None:
            raise SafetyError('Native minimum order value is invalid or unverified')
        self._bounds(result, info["min_cost"], info["max_cost"], "notional")
        return result

    def balance(self):
        return self.account().wallet

    def account(self):
        """Fresh authenticated, scoped discovery; never changes exchange settings."""
        return AccountDiscovery(self.ex).discover()

    def positions(self, symbol=None):
        # Compatibility shape for existing dry close previews. It uses the same
        # conservative discovery facts as account/positions/modes inspection.
        selected = self.instrument(symbol)['symbol'].value if symbol else None
        report = self.account()
        if not report.positions_available or not report.positions_complete:
            raise SafetyError('Position state is unavailable or incomplete; refusing to treat it as empty')
        result = []
        for row in report.positions:
            if selected and row['symbol'].value != selected:
                continue
            size = row['contracts'].value
            if size is None:
                raise SafetyError('Position size is unknown')
            if size == 0:
                continue
            mode = row['position_mode'].value
            result.append({
                'symbol': row['symbol'].value, 'contracts': size, 'side': row['direction'].value,
                'entryPrice': row['entry_price'].value, 'markPrice': row['mark_price'].value,
                'liquidationPrice': row['liquidation_price'].value,
                'unrealizedPnl': row['unrealized_pnl'].value, 'leverage': row['configured_leverage'].value,
                'marginMode': row['margin_mode'].value,
                'info': {'posSide': row['position_side'].value,
                         'posMode': {'one_way': 'OneWay', 'hedge': 'Hedged'}.get(mode)},
            })
        return result

    def open_orders(self, symbol):
        return self.ex.fetch_open_orders(self.resolve(symbol))

    def find_open_order(self, symbol, cl_id=None, ord_id=None):
        if bool(cl_id) == bool(ord_id):
            raise SafetyError("Give exactly one order identifier")
        matches = [o for o in self.open_orders(symbol)
                   if (cl_id and o.get("clientOrderId") == cl_id)
                   or (ord_id and o.get("id") == ord_id)]
        if len(matches) > 1:
            raise SafetyError("Order identifier is ambiguous")
        return matches[0] if matches else None

    @staticmethod
    def order_pos_side(order):
        side = (order.get("info") or {}).get("posSide")
        if side not in {"Long", "Short", "Merged"}:
            raise SafetyError("Exchange position side is missing; refusing to guess")
        return side

    def set_leverage(self, symbol, leverage):
        reject_mutation()

    def place_order(self, symbol, side, pos_side, order_type, amount,
                    price=None, sl=None, tp=None, cl_id=None, reduce_only=False):
        reject_mutation()

    def amend_order(self, order, price=None, amount=None):
        reject_mutation()

    def cancel_order(self, order):
        reject_mutation()

    def cancel_all(self, symbol):
        reject_mutation()
