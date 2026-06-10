import os
import time
from dotenv import load_dotenv
from phemex_client import PhemexClient
load_dotenv("test.env")


def parse_args(parts):

    args = {}

    for part in parts[1:]:

        if "=" in part:

            param_name, param_value = part.split("=", 1)

            args[param_name] = param_value

    return args



def handle_buy(parts):

    args = parse_args(parts)

    symbol = args.get("symbol", "").upper()
    qty = args.get("qty")
    usdt = args.get("usdt")
    price = args.get("price")
    
    if not symbol:
        print("Missing symbol")
        return

    if not qty and not usdt:
        print("Need qty or usdt")
        return

    if qty and usdt:
        print("Use qty or usdt, not both")
        return
    
    ord_type = args.get("type")

    if ord_type:
        ord_type = ord_type.capitalize()

    # Decide order type safely
    if price and not ord_type:
        ord_type = "Limit"

    elif not ord_type:
        ord_type = "Market"

    # Validation
    if ord_type == "Limit" and not price:
        print("ERROR: Limit order requires price")
        return

    if ord_type == "Market" and price:
        print("ERROR: Market order should not include price")
        return
    
    # -----------------------------
    # SIZE CALCULATION
    # -----------------------------
    
    if usdt:

        try:
            usdt = float(usdt)

            if ord_type == "Limit":
                reference_price = float(price)

            else:
                # use best ask for market buy
                reference_price = client.get_market_price(
                    symbol,
                    side="Buy"
                )

                if not reference_price:
                    print("Price fetch failed")
                    return

            qty = usdt / reference_price

        except Exception as e:
            print("Error calculating qty:", e)
            return

    else:
        qty = float(qty)

    qty = round(qty, 6)

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": "Buy",
        "position": args.get("position", "Long").capitalize(),
        "type": ord_type
        }

    if price:
        order["price"] = float(price)
    
    # optional stop loss
    if args.get("sl"):
        order["sl"] = float(args["sl"])

    # optional take profit
    if args.get("tp"):
        order["tp"] = float(args["tp"])

    # optional client crder id
    if args.get("cl_id"):
        order["cl_id"] = args["cl_id"]
        
        if len(order["cl_id"])>40:
            print("Order ID too long (max 40)")
            return
    
    else:
        order["cl_id"] = f"{symbol}_Buy_{int(time.time())}"


    # DEBUG
    print("REQUEST BODY:", order)
    
    response = client.place_order(order)

    print(response)



def handle_sell(parts):

    args = parse_args(parts)

    symbol = args.get("symbol", "").upper()
    qty = args.get("qty")
    usdt = args.get("usdt")
    price = args.get("price")
    
    if not symbol:
        print("Missing symbol")
        return

    if not qty and not usdt:
        print("Need qty or usdt")
        return

    if qty and usdt:
        print("Use qty or usdt, not both")
        return
    
    ord_type = args.get("type")

    if ord_type:
        ord_type = ord_type.capitalize()

    # Decide order type safely
    if price and not ord_type:
        ord_type = "Limit"

    elif not ord_type:
        ord_type = "Market"

    # Validation
    if ord_type == "Limit" and not price:
        print("ERROR: Limit order requires price")
        return

    if ord_type == "Market" and price:
        print("ERROR: Market order should not include price")
        return
    
    # -----------------------------
    # SIZE CALCULATION
    # -----------------------------
    
    if usdt:

        try:
            usdt = float(usdt)

            if ord_type == "Limit":
                reference_price = float(price)

            else:
                # use best ask for market buy
                reference_price = client.get_market_price(
                    symbol,
                    side="Sell"
                )

                if not reference_price:
                    print("Price fetch failed")
                    return

            qty = usdt / reference_price

        except Exception as e:
            print("Error calculating qty:", e)
            return

    else:
        qty = float(qty)

    qty = round(qty, 6)

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": "Sell",
        "position": args.get("position", "Short").capitalize(),
        "type": ord_type
        }

    if price:
        order["price"] = float(price)
    
    # optional stop loss
    if args.get("sl"):
        order["sl"] = float(args["sl"])

    # optional take profit
    if args.get("tp"):
        order["tp"] = float(args["tp"])

    # optional client crder id
    if args.get("cl_id"):
        order["cl_id"] = args["cl_id"]
        
        if len(order["cl_id"])>40:
            print("Order ID too long (max 40)")
            return
    
    else:
        order["cl_id"] = f"{symbol}_Sell_{int(time.time())}"


    # DEBUG
    print("REQUEST BODY:", order)
    
    response = client.place_order(order)

    print(response)



def handle_amend(parts):

    args = parse_args(parts)

    symbol = args.get("symbol", "").upper()
    cl_id = args.get("cl_id")

    price = args.get("price")
    qty = args.get("qty")

    if not symbol or not cl_id:
        print("Missing symbol or cl_id")
        return

    if not price and not qty:
        print("Nothing to amend")
        return

    # 1. FETCH live orders from API
    response = client.get_open_orders(symbol)

    if response.get("code") != 0:
        print("API error:", response)
        return

    rows = response["data"]["rows"]

    # 2. FIND order by clOrdID
    target = None

    for o in rows:
        if o["clOrdID"] == cl_id:
            target = o
            break

    if not target:
        print("Order not found on exchange")
        return

    # 3. BUILD amend request
    amend_order = {
        "symbol": symbol,
        "ord_id": target["orderID"],
        "cl_id": cl_id,
        "position": target.get("posSide", "Long" if target["side"] == "Buy" else "Short")
    }

    if price:
        amend_order["price"] = float(price)

    if qty:
        amend_order["qty"] = float(qty)


    response = client.amend_pending_order(amend_order)

    print(response)


def handle_orders(parts):

    args = parse_args(parts)

    symbol = args.get("symbol", "").upper()
    
    if not symbol:
        print("Usage: orders symbol=BTCUSDT")
        return

    response = client.get_open_orders(symbol)
    
    data = response.get("data")

    if not data or not data.get("rows"):
        print("No open orders")
        return

    rows = data["rows"]
    
    for order in rows:
        print_order(order)



def handle_cancel(parts):

    args = parse_args(parts)

    symbol = args.get("symbol", "").upper()
    cl_id = args.get("cl_id")
    ord_id = args.get("ord_id")

    if not symbol:
        print("Missing symbol")
        return

    if not cl_id and not ord_id:
        print("Need cl_id or ord_id")
        return
    
    # 1. FETCH live orders from API
    response = client.get_open_orders(symbol)

    if response.get("code") != 0:
        print("API error:", response)
        return

    rows = response["data"]["rows"]

    # 2. FIND order by clOrdID
    target = None

    for o in rows:

        if cl_id and o["clOrdID"] == cl_id:
            target = o
            break

        if ord_id and o["orderID"] == ord_id:
            target = o
            break

    if not target:
        print("Order not found on exchange")
        return

    # 3. BUILD cancel request
    order = {
        "symbol": target["symbol"],
        "ord_id": target["orderID"],
        "cl_id": target["clOrdID"],
        "position": target.get("posSide", "Long" if target["side"] == "Buy" else "Short")
    }

    response = client.cancel_order(order)

    print(response)



def handle_cancel_all(parts):

    args = parse_args(parts)

    symbol = args.get("symbol")

    response = client.cancel_all_orders(symbol if symbol else None)

    print(response)



def handle_balance():
    print("BALANCE → (coming from API later)")



def handle_positions():
    print("POSITIONS → (coming from API later)")



def print_order(order):
    
    print("\n" + "=" * 40)
    print(f"ID      : {order.get('clOrdID')}")
    print(f"Symbol  : {order.get('symbol')}")
    print(f"Side    : {order.get('side')}")
    print(f"Type    : {order.get('orderType')}")
    print(f"Price   : {order.get('priceRp')}")
    print(f"Qty     : {order.get('orderQtyRq')}")
    print(f"Status  : {order.get('ordStatus')}")
    print(f"TP      : {order.get('takeProfitRp')}")
    print(f"SL      : {order.get('stopLossRp')}")
    print("=" * 40)



def router(cmd, parts):
    if cmd == "buy":
        handle_buy(parts)

    elif cmd == "sell":
        handle_sell(parts)

    elif cmd == "amend":
        handle_amend(parts)
    
    elif cmd == "orders":
        handle_orders(parts)

    elif cmd == "cancel":
        handle_cancel(parts)

    elif cmd == "cancelall":
        handle_cancel_all(parts)

    elif cmd == "balance":
        handle_balance()

    elif cmd == "positions":
        handle_positions()

    elif cmd == "exit":
        print("bye 👋")
        return False

    else:
        print("Unknown command")

    return True



def terminal():
    running = True

    while running:
        user_input = input("> ")
        parts = user_input.split()

        if not parts:
            continue

        cmd = parts[0].lower()

        running = router(cmd, parts)



API_KEY = os.getenv("PHEMEX_API_KEY")
API_SECRET = os.getenv("PHEMEX_API_SECRET")
BASE_URL = "https://testnet-api.phemex.com"

client = PhemexClient(
    api_key=API_KEY,
    api_secret=API_SECRET,
    base_url=BASE_URL
)

terminal()




    





