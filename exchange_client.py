"""Thin wrapper around CCXT for Phemex USDT-margined perpetuals (hedged mode).

The CLI only talks to this class, so CCXT details stay in one place.
All prices/amounts coming in are Decimal; they are rounded to the symbol's
precision here and converted to float only at the CCXT boundary.
"""
from decimal import Decimal

import ccxt


class PhemexClient:
    def __init__(self, api_key, api_secret, live=False, hedged=True):
        self.live = live
        self.hedged = hedged
        self.ex = ccxt.phemex({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "swap", "defaultSubType": "linear"},
        })
        if not live:
            self.ex.set_sandbox_mode(True)
        self.ex.load_markets()

    # ------------------------------------------------------------------
    # symbols / market data
    # ------------------------------------------------------------------
    def resolve(self, raw):
        """'BTCUSDT' -> 'BTC/USDT:USDT' (linear swap only)."""
        raw = (raw or "").strip().upper()
        if not raw:
            raise ValueError("Missing symbol")
        if raw in self.ex.markets:
            return raw
        for m in self.ex.markets.values():
            if m.get("swap") and m.get("linear") and m["id"] == raw:
                return m["symbol"]
        raise ValueError(f"Unknown USDT perpetual symbol: {raw}")

    def market_id(self, symbol):
        return self.ex.market(symbol)["id"]

    def market_info(self, symbol):
        m = self.ex.market(symbol)
        return {
            "id": m["id"],
            "symbol": m["symbol"],
            "contract_size": m.get("contractSize"),
            "min_amount": (m.get("limits", {}).get("amount") or {}).get("min"),
            "amount_precision": m.get("precision", {}).get("amount"),
            "price_precision": m.get("precision", {}).get("price"),
        }

    def best_price(self, symbol, side):
        """Buy -> best ask, Sell -> best bid."""
        book = self.ex.fetch_order_book(symbol, limit=5)
        levels = book["asks"] if side == "buy" else book["bids"]
        if not levels:
            raise RuntimeError("Order book is empty")
        return Decimal(str(levels[0][0]))

    def round_amount(self, symbol, amount):
        rounded = Decimal(str(self.ex.amount_to_precision(symbol, float(amount))))
        if rounded <= 0:
            raise ValueError("Amount rounds to zero at this symbol's lot size")
        min_amt = self.market_info(symbol)["min_amount"]
        if min_amt is not None and rounded < Decimal(str(min_amt)):
            raise ValueError(f"Amount {rounded} is below the minimum {min_amt}")
        return rounded

    def round_price(self, symbol, price):
        return Decimal(str(self.ex.price_to_precision(symbol, float(price))))

    # ------------------------------------------------------------------
    # account
    # ------------------------------------------------------------------
    def balance(self):
        bal = self.ex.fetch_balance({"type": "swap", "code": "USDT"})
        usdt = bal.get("USDT", {})
        return {"free": usdt.get("free"), "used": usdt.get("used"), "total": usdt.get("total")}

    def positions(self, symbol=None):
        syms = [symbol] if symbol else None
        rows = self.ex.fetch_positions(syms, {"code": "USDT"})
        return [p for p in rows if float(p.get("contracts") or 0) != 0]

    def set_leverage(self, symbol, leverage):
        return self.ex.set_leverage(float(leverage), symbol, {"hedged": self.hedged})

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def place_order(self, symbol, side, pos_side, order_type, amount,
                    price=None, sl=None, tp=None, cl_id=None, reduce_only=False):
        params = {"posSide": pos_side}
        if cl_id:
            params["clientOrderId"] = cl_id
        if reduce_only:
            params["reduceOnly"] = True
        if sl is not None:
            params["stopLoss"] = {"triggerPrice": float(self.round_price(symbol, sl))}
        if tp is not None:
            params["takeProfit"] = {"triggerPrice": float(self.round_price(symbol, tp))}
        px = float(self.round_price(symbol, price)) if price is not None else None
        return self.ex.create_order(symbol, order_type, side, float(amount), px, params)

    def open_orders(self, symbol):
        return self.ex.fetch_open_orders(symbol)

    def find_open_order(self, symbol, cl_id=None, ord_id=None):
        for o in self.open_orders(symbol):
            if cl_id and o.get("clientOrderId") == cl_id:
                return o
            if ord_id and o.get("id") == ord_id:
                return o
        return None

    @staticmethod
    def order_pos_side(order):
        ps = (order.get("info") or {}).get("posSide")
        if ps:
            return ps
        return "Long" if order.get("side") == "buy" else "Short"

    def amend_order(self, order, price=None, amount=None):
        symbol = order["symbol"]
        new_price = float(self.round_price(symbol, price)) if price is not None else None
        new_amount = float(amount) if amount is not None else None
        return self.ex.edit_order(
            order["id"], symbol, order["type"], order["side"],
            new_amount, new_price, {"posSide": self.order_pos_side(order)},
        )

    def cancel_order(self, order):
        return self.ex.cancel_order(
            order["id"], order["symbol"], {"posSide": self.order_pos_side(order)}
        )

    def cancel_all(self, symbol):
        return self.ex.cancel_all_orders(symbol)
