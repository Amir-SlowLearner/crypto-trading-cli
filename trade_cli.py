"""Phemex trading CLI (USDT perpetuals, hedged mode).

Run on testnet (default):   python trade_cli.py
Run on mainnet (explicit):  python trade_cli.py --live

Examples:
  leverage symbol=BTCUSDT x=5
  buy  symbol=BTCUSDT usdt=100 sl=60000 tp=70000
  buy  symbol=BTCUSDT qty=0.002 price=64000          (limit)
  sell symbol=ETHUSDT usdt=50 dry                    (dry run, nothing sent)
  positions | balance | orders symbol=BTCUSDT
  close symbol=BTCUSDT [position=Long]
"""
import logging
import os
import sys
import time
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

try:  # command history + arrow keys on Linux
    import readline  # noqa: F401
except ImportError:
    pass

MAX_NOTIONAL_USDT = Decimal(os.getenv("MAX_NOTIONAL_USDT", "200"))
MAX_LEVERAGE = Decimal(os.getenv("MAX_LEVERAGE", "5"))

log = logging.getLogger("trades")


class CommandError(Exception):
    pass


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def parse(parts):
    """'buy symbol=BTCUSDT usdt=100 dry' -> ({'symbol': 'BTCUSDT', 'usdt': '100'}, {'dry'})"""
    args, flags = {}, set()
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            args[k.lower()] = v
        else:
            flags.add(p.lower())
    return args, flags


def dec(name, value):
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError):
        raise CommandError(f"{name} must be a number, got '{value}'")
    if not d.is_finite() or d <= 0:
        raise CommandError(f"{name} must be greater than 0, got '{value}'")
    return d


def confirm(prompt="Send? [y/N] "):
    return input(prompt).strip().lower() == "y"


def is_opening(side, pos_side):
    return (side == "buy" and pos_side == "Long") or (side == "sell" and pos_side == "Short")


def check_sl_tp(side, pos_side, ref_price, sl, tp):
    """Catch inverted stops before they reach the exchange (opening orders only)."""
    if not is_opening(side, pos_side):
        return
    if pos_side == "Long":
        if sl is not None and sl >= ref_price:
            raise CommandError(f"Long stop-loss {sl} must be below price {ref_price}")
        if tp is not None and tp <= ref_price:
            raise CommandError(f"Long take-profit {tp} must be above price {ref_price}")
    else:
        if sl is not None and sl <= ref_price:
            raise CommandError(f"Short stop-loss {sl} must be above price {ref_price}")
        if tp is not None and tp >= ref_price:
            raise CommandError(f"Short take-profit {tp} must be below price {ref_price}")


def print_order(o):
    info = o.get("info") or {}
    print("-" * 40)
    print(f"ID      : {o.get('id')}")
    print(f"ClientID: {o.get('clientOrderId')}")
    print(f"Symbol  : {o.get('symbol')}")
    print(f"Side    : {o.get('side')} ({info.get('posSide', '?')})")
    print(f"Type    : {o.get('type')}")
    print(f"Price   : {o.get('price')}")
    print(f"Amount  : {o.get('amount')}   filled: {o.get('filled')}")
    print(f"Status  : {o.get('status')}")
    print(f"SL / TP : {info.get('stopLossRp')} / {info.get('takeProfitRp')}")
    print("-" * 40)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_order(client, parts, side):
    args, flags = parse(parts)
    symbol = client.resolve(args.get("symbol"))

    qty_raw, usdt_raw, price_raw = args.get("qty"), args.get("usdt"), args.get("price")
    if bool(qty_raw) == bool(usdt_raw):
        raise CommandError("Give exactly one of qty= (base units) or usdt= (position size in USDT)")

    ord_type = (args.get("type") or ("limit" if price_raw else "market")).lower()
    if ord_type not in ("limit", "market"):
        raise CommandError("type must be limit or market")
    if ord_type == "limit" and not price_raw:
        raise CommandError("Limit order requires price=")
    if ord_type == "market" and price_raw:
        raise CommandError("Market order must not include price=")

    pos_side = args.get("position", "Long" if side == "buy" else "Short").capitalize()
    if pos_side not in ("Long", "Short", "Merged"):
        raise CommandError("position must be Long, Short or Merged")

    price = dec("price", price_raw) if price_raw else None
    sl = dec("sl", args["sl"]) if args.get("sl") else None
    tp = dec("tp", args["tp"]) if args.get("tp") else None

    ref_price = price if price is not None else client.best_price(symbol, side)
    amount = dec("qty", qty_raw) if qty_raw else dec("usdt", usdt_raw) / ref_price
    amount = client.round_amount(symbol, amount)
    notional = amount * ref_price

    if notional > MAX_NOTIONAL_USDT:
        raise CommandError(
            f"Notional {notional:.2f} USDT exceeds MAX_NOTIONAL_USDT={MAX_NOTIONAL_USDT}"
        )
    check_sl_tp(side, pos_side, ref_price, sl, tp)

    cl_id = args.get("cl_id") or f"{client.market_id(symbol)}_{side}_{int(time.time())}"
    if len(cl_id) > 40:
        raise CommandError("cl_id too long (max 40)")

    print("\nORDER PREVIEW")
    print(f"  {side.upper()} {pos_side}  {symbol}  ({ord_type})")
    print(f"  amount  : {amount}")
    print(f"  price   : {price if price is not None else f'market (~{ref_price})'}")
    print(f"  notional: ~{notional:.2f} USDT")
    print(f"  SL / TP : {sl} / {tp}")
    print(f"  cl_id   : {cl_id}")
    print(f"  env     : {'LIVE' if client.live else 'TESTNET'}")

    if "dry" in flags:
        print("DRY RUN - nothing sent.")
        return
    if not confirm():
        print("Cancelled.")
        return

    log.info("SEND %s %s %s %s amount=%s price=%s sl=%s tp=%s cl_id=%s",
             "LIVE" if client.live else "TEST", side, pos_side, symbol, amount, price, sl, tp, cl_id)
    result = client.place_order(symbol, side, pos_side, ord_type, amount,
                                price=price, sl=sl, tp=tp, cl_id=cl_id)
    log.info("RESULT %s", result.get("info") or result)
    print_order(result)


def cmd_buy(client, parts):
    cmd_order(client, parts, "buy")


def cmd_sell(client, parts):
    cmd_order(client, parts, "sell")


def cmd_leverage(client, parts):
    args, flags = parse(parts)
    symbol = client.resolve(args.get("symbol"))
    lev = dec("x", args.get("x"))
    if lev > MAX_LEVERAGE:
        raise CommandError(f"Leverage {lev}x exceeds MAX_LEVERAGE={MAX_LEVERAGE}")
    if not confirm(f"Set {symbol} leverage to {lev}x? [y/N] "):
        print("Cancelled.")
        return
    log.info("LEVERAGE %s %s", symbol, lev)
    print(client.set_leverage(symbol, lev))


def cmd_orders(client, parts):
    args, _ = parse(parts)
    symbol = client.resolve(args.get("symbol"))
    rows = client.open_orders(symbol)
    if not rows:
        print("No open orders")
    for o in rows:
        print_order(o)


def cmd_amend(client, parts):
    args, flags = parse(parts)
    symbol = client.resolve(args.get("symbol"))
    if not args.get("cl_id") and not args.get("ord_id"):
        raise CommandError("Need cl_id= or ord_id=")
    if not args.get("price") and not args.get("qty"):
        raise CommandError("Nothing to amend (give price= and/or qty=)")
    order = client.find_open_order(symbol, args.get("cl_id"), args.get("ord_id"))
    if not order:
        raise CommandError("Order not found among open orders")

    price = dec("price", args["price"]) if args.get("price") else None
    qty = client.round_amount(symbol, dec("qty", args["qty"])) if args.get("qty") else None
    if price is not None and price * (qty or Decimal(str(order["amount"]))) > MAX_NOTIONAL_USDT:
        raise CommandError(f"Amended notional exceeds MAX_NOTIONAL_USDT={MAX_NOTIONAL_USDT}")

    print(f"Amend {order['id']}: price {order.get('price')} -> {price or 'same'}, "
          f"amount {order.get('amount')} -> {qty or 'same'}")
    if "dry" in flags:
        print("DRY RUN - nothing sent.")
        return
    if not confirm("Send? [y/N] "):
        print("Cancelled.")
        return
    log.info("AMEND %s price=%s qty=%s", order["id"], price, qty)
    print_order(client.amend_order(order, price=price, amount=qty))


def cmd_cancel(client, parts):
    args, _ = parse(parts)
    symbol = client.resolve(args.get("symbol"))
    if not args.get("cl_id") and not args.get("ord_id"):
        raise CommandError("Need cl_id= or ord_id=")
    order = client.find_open_order(symbol, args.get("cl_id"), args.get("ord_id"))
    if not order:
        raise CommandError("Order not found among open orders")
    print_order(order)
    if not confirm("Cancel this order? [y/N] "):
        print("Cancelled.")
        return
    log.info("CANCEL %s", order["id"])
    client.cancel_order(order)
    print("Canceled.")


def cmd_cancelall(client, parts):
    args, _ = parse(parts)
    symbol = client.resolve(args.get("symbol"))
    if not confirm(f"Cancel ALL open orders on {symbol}? [y/N] "):
        print("Cancelled.")
        return
    log.info("CANCELALL %s", symbol)
    print(client.cancel_all(symbol))


def cmd_balance(client, parts):
    b = client.balance()
    print(f"USDT  free={b['free']}  used={b['used']}  total={b['total']}")


def cmd_positions(client, parts):
    args, _ = parse(parts)
    symbol = client.resolve(args["symbol"]) if args.get("symbol") else None
    rows = client.positions(symbol)
    if not rows:
        print("No open positions")
    for p in rows:
        print("-" * 40)
        print(f"{p.get('symbol')}  {str(p.get('side')).upper()}  contracts={p.get('contracts')}")
        print(f"  entry={p.get('entryPrice')}  mark={p.get('markPrice')}  "
              f"liq={p.get('liquidationPrice')}")
        print(f"  uPnL={p.get('unrealizedPnl')}  leverage={p.get('leverage')}")
    if rows:
        print("-" * 40)


def cmd_close(client, parts):
    """Market-close a position with a reduce-only order."""
    args, flags = parse(parts)
    symbol = client.resolve(args.get("symbol"))
    rows = client.positions(symbol)
    want = args.get("position")
    if want:
        rows = [p for p in rows if str(p.get("side")).lower() == want.lower()]
    if not rows:
        raise CommandError("No matching open position")
    if len(rows) > 1:
        raise CommandError("Multiple positions found; specify position=Long or position=Short")
    p = rows[0]
    pos_side = "Long" if str(p.get("side")).lower() == "long" else "Short"
    close_side = "sell" if pos_side == "Long" else "buy"
    amount = Decimal(str(p["contracts"]))

    print(f"CLOSE {pos_side} {symbol}: {close_side.upper()} {amount} at market (reduce-only)")
    if "dry" in flags:
        print("DRY RUN - nothing sent.")
        return
    if not confirm():
        print("Cancelled.")
        return
    cl_id = f"{client.market_id(symbol)}_close_{int(time.time())}"
    log.info("CLOSE %s %s amount=%s", symbol, pos_side, amount)
    print_order(client.place_order(symbol, close_side, pos_side, "market", amount,
                                   cl_id=cl_id, reduce_only=True))


def cmd_info(client, parts):
    args, _ = parse(parts)
    symbol = client.resolve(args.get("symbol"))
    for k, v in client.market_info(symbol).items():
        print(f"{k:17}: {v}")


def cmd_help(client, parts):
    print(__doc__)
    print(f"Limits: MAX_NOTIONAL_USDT={MAX_NOTIONAL_USDT}  MAX_LEVERAGE={MAX_LEVERAGE}")
    print("Commands: buy sell close leverage orders amend cancel cancelall "
          "balance positions info help exit")
    print("Add the word 'dry' to buy/sell/amend/close to preview without sending.")


COMMANDS = {
    "buy": cmd_buy, "sell": cmd_sell, "close": cmd_close, "leverage": cmd_leverage,
    "orders": cmd_orders, "amend": cmd_amend, "cancel": cmd_cancel,
    "cancelall": cmd_cancelall, "balance": cmd_balance, "positions": cmd_positions,
    "info": cmd_info, "help": cmd_help,
}


# ----------------------------------------------------------------------
# main loop
# ----------------------------------------------------------------------
def repl(client):
    tag = "LIVE" if client.live else "TESTNET"
    print(f"Connected [{tag}]. Type 'help' for commands.")
    while True:
        try:
            line = input(f"[{tag}]> ")
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            continue

        parts = line.split()
        if not parts:
            continue
        cmd = parts[0].lower()
        if cmd in ("exit", "quit"):
            return
        fn = COMMANDS.get(cmd)
        if fn is None:
            print("Unknown command. Type 'help'.")
            continue
        try:
            fn(client, parts)
        except (CommandError, ValueError) as e:
            print("ERROR:", e)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        except Exception as e:  # exchange/network errors: report, keep the session alive
            log.exception("command failed: %s", line)
            print(f"FAILED: {type(e).__name__}: {e}")


def main():
    live = "--live" in sys.argv
    logging.basicConfig(
        filename="trades.log", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_dotenv("live.env" if live else "test.env")
    key, secret = os.getenv("PHEMEX_API_KEY"), os.getenv("PHEMEX_API_SECRET")
    if not key or not secret:
        sys.exit("Missing PHEMEX_API_KEY / PHEMEX_API_SECRET in "
                 f"{'live.env' if live else 'test.env'}")
    if live and input("LIVE trading with real funds. Type LIVE to continue: ") != "LIVE":
        sys.exit("Aborted.")

    from exchange_client import PhemexClient  # imported late so tests can stub it
    client = PhemexClient(key, secret, live=live)
    repl(client)


if __name__ == "__main__":
    main()
