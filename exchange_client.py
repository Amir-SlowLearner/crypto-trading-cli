"""CCXT boundary for USDT linear perpetuals; mutations are intentionally blocked.

CCXT owns signing and transport. The application owns risk and recovery.
No account, position or margin settings are changed during initialization.
"""
from decimal import Decimal

import ccxt

from command_parser import dec
from config import RiskLimits
from safety import SafetyError, reject_mutation


class PhemexClient:
    def __init__(self, api_key, api_secret, live=False, *, limits=None, exchange=None):
        self.live = live
        self.limits = limits if limits is not None else RiskLimits()
        self.ex = exchange if exchange is not None else ccxt.phemex({
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
        self.ex.load_markets()

    @staticmethod
    def _supported(market):
        return (market.get("swap") is True and market.get("linear") is True
                and market.get("settle") == "USDT" and market.get("quote") == "USDT"
                and market.get("active") is True)

    def resolve(self, raw):
        raw = (raw or "").strip().upper()
        if not raw:
            raise SafetyError("Missing symbol")
        candidates = ([self.ex.markets[raw]] if raw in self.ex.markets else
                      [m for m in self.ex.markets.values() if m["id"] == raw])
        matches = [m for m in candidates if self._supported(m)]
        if len(matches) != 1:
            raise SafetyError("Symbol must identify one active USDT-settled linear perpetual")
        dec("contract size", str(matches[0].get("contractSize")))
        return matches[0]["symbol"]

    def market_id(self, symbol):
        return self.ex.market(self.resolve(symbol))["id"]

    def market_info(self, symbol):
        m = self.ex.market(self.resolve(symbol))
        limits = m.get("limits") or {}
        return {
            "id": m["id"], "symbol": m["symbol"],
            "contract_size": m.get("contractSize"),
            "min_amount": (limits.get("amount") or {}).get("min"),
            "max_amount": (limits.get("amount") or {}).get("max"),
            "min_cost": (limits.get("cost") or {}).get("min"),
            "max_cost": (limits.get("cost") or {}).get("max"),
            "amount_precision": (m.get("precision") or {}).get("amount"),
            "price_precision": (m.get("precision") or {}).get("price"),
        }

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
        self._bounds(rounded, info["min_amount"], info["max_amount"], "amount")
        if rounded > amount:
            raise SafetyError("Amount normalization must not increase requested size")
        return rounded

    def round_price(self, symbol, price):
        symbol = self.resolve(symbol)
        price = dec("price", str(price))
        rounded = dec("normalized price", self.ex.price_to_precision(symbol, str(price)))
        limits = (self.ex.market(symbol).get("limits") or {}).get("price") or {}
        self._bounds(rounded, limits.get("min"), limits.get("max"), "price")
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
        self._bounds(result, info["min_cost"], info["max_cost"], "notional")
        return result

    def balance(self):
        # Raw account type/equity verification is a later increment. These are
        # CCXT's basic swap balance fields, not a validated available-margin view.
        bal = self.ex.fetch_balance({"type": "swap", "code": "USDT"})
        usdt = bal.get("USDT", {})
        return {"free": usdt.get("free"), "used": usdt.get("used"), "total": usdt.get("total")}

    def positions(self, symbol=None):
        syms = [self.resolve(symbol)] if symbol else None
        rows = self.ex.fetch_positions(syms, {"code": "USDT"})
        result = []
        for row in rows:
            raw_size = row.get("contracts")
            if raw_size is None:
                raise SafetyError("Position size is unavailable; account state is unverified")
            size = Decimal(str(raw_size))
            if not size.is_finite() or size < 0:
                raise SafetyError("Position size is invalid; account state is unverified")
            if size:
                self.resolve(row.get("symbol"))
                result.append(row)
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
