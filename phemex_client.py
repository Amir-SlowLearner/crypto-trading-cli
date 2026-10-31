"""Legacy native reference client. Mutations are disabled; use trade_cli.py."""
import time
import hmac
import hashlib
import json
import requests
from urllib.parse import urlencode

from safety import SafetyError, reject_mutation



class PhemexClient:

    def __init__(self, api_key, api_secret, base_url):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url



    def get_expiry(self):
        return str(int(time.time()) + 60) 



    def _sign(self, endpoint, expiry, query_string="", body=""):
        message = f"{endpoint}{query_string}{expiry}{body}"

        return hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()



    def _request(self, method, endpoint, body=None, params=None):
        if method.upper() != "GET":
            reject_mutation()

        # -----------------------
        # QUERY STRING (SAFE)
        # -----------------------
        query_string = ""

        if params:
            query_string = urlencode(params)

        # -----------------------
        # URL
        # -----------------------
        url = self.base_url + endpoint

        if query_string:
            url += "?" + query_string

        # -----------------------
        # EXPIRY
        # -----------------------
        expiry = self.get_expiry()

        # -----------------------
        # BODY
        # -----------------------
        body_json = json.dumps(body, separators=(",", ":")) if body else ""

        # -----------------------
        # SIGNATURE
        # -----------------------
        signature = self._sign(
            endpoint,
            expiry,
            query_string,
            body_json
        )

        # -----------------------
        # HEADERS
        # -----------------------
        headers = {
            "x-phemex-access-token": self.api_key,
            "x-phemex-request-expiry": expiry,
            "x-phemex-request-signature": signature,
            "Content-Type": "application/json"
        }

        # -----------------------
        # SEND REQUEST
        # -----------------------
        try:
            response = requests.get(url, headers=headers, timeout=15, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise SafetyError("Legacy read returned an unexpected HTTP status")
            result = response.json()
        except (requests.RequestException, ValueError):
            raise SafetyError("Legacy read failed; exchange state is unverified") from None
        if not isinstance(result, dict):
            raise SafetyError("Legacy read returned an invalid response")
        if result.get("code", 0) not in (0, "0") or result.get("error"):
            raise SafetyError("Legacy read returned an exchange error")
        return result

    
    
    def place_order(self, order):

        body = {
            "symbol": order["symbol"],
            "side": order["side"],
            "posSide": order["position"],
            "orderQtyRq": order["qty"],
            "ordType": order["type"],
            "timeInForce": "GoodTillCancel"
            }

        if "price" in order:
            body["priceRp"] = order["price"]

        if "sl" in order:
            body["stopLossRp"] = order["sl"]

        if "tp" in order:
            body["takeProfitRp"] = order["tp"]
        
        if "cl_id" in order:
            body["clOrdID"] = order["cl_id"]

        return self._request(
            "POST",
            "/g-orders",
            body=body
            )
    


    def get_market_price(self, symbol, side="Buy"):
        
        """
        Returns best execution price:
        - Buy  → best ask
        - Sell → best bid
        """

        endpoint = "/md/v2/orderbook"
    
        params = {"symbol": symbol}

        response = self._request(
            "GET",
            endpoint,
            params=params
            )

        try:
            book = response["result"]["orderbook_p"]

            if side == "Buy":
                # best ask (you pay this when buying)
                price = float(book["asks"][0][0])

            elif side == "Sell":
                # best bid (you receive this when selling)
                price = float(book["bids"][0][0])

            else:
                raise ValueError("side must be 'Buy' or 'Sell'")

            return price

        except Exception as e:
            print("Error parsing market price:", e)
            return None


        
    def amend_pending_order(self, amend_order):

       
        params = {
            "symbol": amend_order["symbol"],
            "posSide": amend_order["position"]
        }

        if amend_order.get("ord_id"):
            params["orderID"] = amend_order["ord_id"]

        if amend_order.get("cl_id"):
            params["origClOrdID"] = amend_order["cl_id"]

        if amend_order.get("price") is not None:
            params["priceRp"] = amend_order["price"]

        if amend_order.get("qty") is not None:
            params["orderQtyRq"] = amend_order["qty"]

        return self._request(
            "PUT",
            "/g-orders/replace",
            params=params
        )



    def get_open_orders(self, symbol):

        params = {
            "symbol": symbol.upper()
        }

        return self._request(
            "GET",
            "/g-orders/activeList",
            params=params
        )



    def cancel_order(self, order):
        params = {
            "symbol": order["symbol"],
            "posSide": order["position"]
        }

        if order.get("ord_id"):
            params["orderID"] = order["ord_id"]

        if order.get("cl_id"):
            params["clOrdID"] = order["cl_id"]

        return self._request(
            "DELETE",
            "/g-orders/cancel",
            params=params
        )



    def cancel_all_orders(self, symbol=None):

        params = {}

        if symbol:
            params["symbol"] = symbol.upper()

        return self._request(
            "DELETE",
            "/g-orders/all",
            params=params
        )



    def get_balance(self, currency="USDT"):
        if currency.upper() != "USDT":
            raise SafetyError("Legacy balance support is restricted to USDT")
        return self._request(
            "GET", "/g-accounts/accountPositions", params={"currency": "USDT"}
        )
