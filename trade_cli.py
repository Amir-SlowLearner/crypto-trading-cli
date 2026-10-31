"""Phemex CLI foundation: read-only commands and dry previews.

Run: python trade_cli.py [--env-file PATH]
Live connectivity additionally requires --live, PHEMEX_ALLOW_LIVE=true, and LIVE
confirmation. All exchange mutations are disabled in this increment.

Commands:
  account | balance | modes [symbol=BTCUSDT]
  positions [symbol=BTCUSDT] | info symbol=BTCUSDT
  orders symbol=BTCUSDT
  buy|sell symbol=BTCUSDT qty=0.002|usdt=100 [price=64000] [sl=60000]
           [tp=70000] [position=Long|Short] [type=market|limit] [cl_id=ID] dry
  amend symbol=BTCUSDT cl_id=ID|ord_id=ID [price=64000] [qty=0.002] dry
  cancel symbol=BTCUSDT cl_id=ID|ord_id=ID dry
  cancelall symbol=BTCUSDT dry
  close symbol=BTCUSDT [position=Long|Short] dry
  leverage symbol=BTCUSDT x=5 dry
  help | exit | quit

qty is base-asset quantity; amendment qty is the new total including fills.
Close currently previews a full reduction only; partial-close arguments are rejected.
Account discovery is read-only and scoped; successful discovery does not establish
trading safety. Previews do not verify aggregate exposure or protection coverage.
"""
import argparse
from decimal import Decimal
import logging
from pathlib import Path
import sys
from uuid import uuid4

from command_parser import CommandError, dec, parse
from config import ConfigError, load_settings
from safety import SafetyError, reject_mutation

try:
    import readline  # noqa: F401
except ImportError:
    pass

log = logging.getLogger("trades")


def preview_only(flags):
    if "dry" not in flags:
        reject_mutation()


def check_sl_tp(side, pos_side, ref_price, sl, tp):
    opening = (side, pos_side) in {("buy", "Long"), ("sell", "Short")}
    if not opening:
        raise CommandError("Order previews support opening intent only; use close for reductions")
    if pos_side == "Long":
        if sl is not None and sl >= ref_price:
            raise CommandError("Normalized long stop-loss must be below the reference price")
        if tp is not None and tp <= ref_price:
            raise CommandError("Normalized long take-profit must be above the reference price")
    else:
        if sl is not None and sl <= ref_price:
            raise CommandError("Normalized short stop-loss must be above the reference price")
        if tp is not None and tp >= ref_price:
            raise CommandError("Normalized short take-profit must be below the reference price")


def check_cap(client, notional):
    if notional > client.limits.max_notional_usdt:
        raise CommandError("Notional exceeds MAX_NOTIONAL_USDT")


def print_order(order):
    info = order.get("info") or {}
    for label, value in (
        ("ID", order.get("id")), ("ClientID", order.get("clientOrderId")),
        ("Symbol", order.get("symbol")), ("Side", order.get("side")),
        ("Position", info.get("posSide", "unverified")),
        ("Type", order.get("type")), ("Price", order.get("price")),
        ("Amount", order.get("amount")), ("Filled", order.get("filled")),
        ("Status", order.get("status")),
        ("SL / TP", f"{info.get('stopLossRp')} / {info.get('takeProfitRp')}"),
    ):
        print(f"{label:10}: {value}")
    print("-" * 40)


def cmd_order(client, parts, side):
    args, flags = parse(parts)
    preview_only(flags)
    symbol = client.resolve(args["symbol"])
    pos_side = args.get("position", "Long" if side == "buy" else "Short")
    price = client.round_price(symbol, dec("price", args["price"])) if "price" in args else None
    sl = client.round_price(symbol, dec("sl", args["sl"])) if "sl" in args else None
    tp = client.round_price(symbol, dec("tp", args["tp"])) if "tp" in args else None
    ref = price if price is not None else client.best_price(symbol, side)
    base_amount = dec("qty", args["qty"]) if "qty" in args else dec("usdt", args["usdt"]) / ref
    amount = client.round_amount(symbol, base_amount / client.contract_size(symbol))
    notional = client.notional(symbol, amount, ref)
    check_cap(client, notional)
    check_sl_tp(side, pos_side, ref, sl, tp)
    cl_id = args.get("cl_id") or uuid4().hex
    print(f"ORDER PREVIEW [{'LIVE' if client.live else 'TESTNET'}]")
    print(f"  {side.upper()} opening intent={pos_side} {symbol} ({args['type']})")
    print(f"  contracts: {amount}; base quantity: {amount * client.contract_size(symbol)}")
    print(f"  price: {price if price is not None else f'market estimate (~{ref})'}")
    print(f"  notional: ~{notional} USDT; SL / TP: {sl} / {tp}")
    print(f"  proposed cl_id: {cl_id} (not reserved)")
    print("DRY RUN: no submission. Account mode and aggregate exposure are unverified.")


def cmd_buy(client, parts):
    cmd_order(client, parts, "buy")


def cmd_sell(client, parts):
    cmd_order(client, parts, "sell")


def cmd_leverage(client, parts):
    args, flags = parse(parts)
    preview_only(flags)
    symbol = client.resolve(args["symbol"])
    leverage = dec("x", args["x"])
    if leverage < 1 or leverage > client.limits.max_leverage:
        raise CommandError("Leverage must be at least 1 and no greater than MAX_LEVERAGE")
    print(f"DRY RUN: requested leverage {leverage}x for {symbol}; nothing changed.")
    print("Current margin/position modes and affected legs are unverified.")


def cmd_orders(client, parts):
    args, _ = parse(parts)
    rows = client.open_orders(client.resolve(args["symbol"]))
    if not rows:
        print("No open orders returned by the exchange.")
    for order in rows:
        print_order(order)


def find_order(client, args):
    symbol = client.resolve(args["symbol"])
    order = client.find_open_order(symbol, args.get("cl_id"), args.get("ord_id"))
    if not order:
        raise CommandError("Not found among open orders; execution status is unresolved by this lookup")
    client.order_pos_side(order)
    return symbol, order


def cmd_amend(client, parts):
    args, flags = parse(parts)
    preview_only(flags)
    symbol, order = find_order(client, args)
    if order.get("type") != "limit":
        raise CommandError("Only ordinary limit-order amendment previews are supported")
    price = client.round_price(symbol, dec("price", args.get("price", str(order.get("price")))))
    requested = (dec("qty", args["qty"]) / client.contract_size(symbol)
                 if "qty" in args else dec("order amount", str(order.get("amount"))))
    amount = client.round_amount(symbol, requested)
    # Check the complete resulting order, even for quantity-only amendments.
    notional = client.notional(symbol, amount, price)
    check_cap(client, notional)
    raw_filled = order.get("filled")
    if raw_filled is None:
        raise CommandError("Filled quantity is missing; amendment state is unverified")
    filled = Decimal(str(raw_filled))
    if not filled.is_finite() or filled < 0 or amount < filled:
        raise CommandError("Amended total quantity must not be below filled quantity")
    protection = []
    for field in ("stopLossRp", "takeProfitRp"):
        value = (order.get("info") or {}).get(field)
        if value is None:
            protection.append(None)
        else:
            trigger = Decimal(str(value))
            if not trigger.is_finite() or trigger < 0:
                raise CommandError("Existing attached protection has an invalid trigger price")
            protection.append(None if trigger == 0 else client.round_price(symbol, trigger))
    if any(value is not None for value in protection):
        check_sl_tp(order.get("side"), client.order_pos_side(order), price, *protection)
    print(f"DRY RUN: amend {order['id']} -> price={price}, total contracts={amount}, notional={notional}")
    print("No submission. Position exposure and attached protection require reconciliation.")


def cmd_cancel(client, parts):
    args, flags = parse(parts)
    preview_only(flags)
    _, order = find_order(client, args)
    print_order(order)
    print("DRY RUN: cancellation preview only; the order remains on the exchange.")


def cmd_cancelall(client, parts):
    args, flags = parse(parts)
    preview_only(flags)
    symbol = client.resolve(args["symbol"])
    rows = client.open_orders(symbol)
    print(f"DRY RUN: {len(rows)} open orders returned for {symbol}; nothing canceled.")
    print("Complete cancellation must handle active AND untriggered conditional orders.")
    print("Canceling protection can leave an open position unprotected.")


def print_report_header(report):
    print(f"Authentication: {report.authenticated}; accounting: {report.account_type}")
    print(f"Read window: {report.started_at} -> {report.completed_at} (sequential, not atomic)")
    print(f"Minimum discovery checks passed: {report.complete}; exchange mutations remain disabled.")
    for issue in report.issues:
        print(f"Issue: {issue}")


def print_balances(report):
    print("USDT wallet (account-wide available margin is not inferred):")
    for key, value in report.wallet.items():
        print(f"  {key}: {value}")
    for index, unit in enumerate(report.risk_units, 1):
        print(f"Risk unit {index}: currency={unit['currency']}; scope={unit['risk_type']}")
        for key, value in unit.items():
            if key not in {'currency', 'risk_type'}:
                print(f"  {key}: {value}")
    print("Risk-unit totals have distinct scopes/currencies and must not be summed.")


def selected_positions(client, report, args):
    if 'symbol' not in args:
        return report.positions
    symbol = client.instrument(args['symbol'])['symbol'].value
    return [p for p in report.positions if p['symbol'].value == symbol]


def print_positions(report, rows, *, modes_only=False):
    if not report.positions_available:
        print("Positions/configuration unavailable; this is not an empty-account result.")
        return
    if not modes_only:
        rows = [p for p in rows if p['contracts'].value is None or p['contracts'].value != 0]
    if not rows:
        if modes_only:
            print("No configuration rows returned for this selection; position/margin modes are unknown.")
        else:
            print("No nonzero positions returned for this selection; rows with unknown size are retained.")
    for row in rows:
        print(f"Position: {row['symbol']}; side={row['position_side']}; contracts={row['contracts']}")
        keys = ('position_mode', 'margin_mode', 'configured_leverage') if modes_only else tuple(row)
        for key in keys:
            if key not in {'symbol', 'position_side', 'contracts'}:
                print(f"  {key}: {row[key]}")
    print("Modes apply only to the returned instrument/leg; no defaults are inferred for other symbols.")


def cmd_account(client, parts):
    parse(parts)
    report = client.account()
    print_report_header(report)
    print_balances(report)
    print_positions(report, report.positions)


def cmd_balance(client, parts):
    parse(parts)
    report = client.account()
    print_report_header(report)
    print_balances(report)


def cmd_positions(client, parts):
    args, _ = parse(parts)
    if 'symbol' in args:
        client.instrument(args['symbol'])  # reject unsupported input before private requests
    report = client.account()
    print_report_header(report)
    print_positions(report, selected_positions(client, report, args))


def cmd_modes(client, parts):
    args, _ = parse(parts)
    if 'symbol' in args:
        client.instrument(args['symbol'])
    report = client.account()
    print_report_header(report)
    print_positions(report, selected_positions(client, report, args), modes_only=True)


def cmd_close(client, parts):
    args, flags = parse(parts)
    preview_only(flags)
    symbol = client.resolve(args["symbol"])
    rows = client.positions(symbol)
    if "position" in args:
        rows = [p for p in rows if str(p.get("side")).lower() == args["position"].lower()]
    if not rows:
        raise CommandError("No matching nonzero position returned")
    if len(rows) != 1:
        raise CommandError("Multiple positions; specify position=Long or position=Short")
    position = rows[0]
    direction = position.get("side")
    if direction not in {"long", "short"}:
        raise CommandError("Position direction is missing or invalid")
    pos_side = client.order_pos_side(position)
    mode = (position.get("info") or {}).get("posMode")
    expected = "Long" if direction == "long" else "Short"
    if (mode, pos_side) not in {("OneWay", "Merged"), ("Hedged", expected)}:
        raise CommandError("Position mode and side are missing or inconsistent")
    amount = dec("position quantity", str(position.get("contracts")))
    if client.round_amount(symbol, amount) != amount:
        raise CommandError("Position quantity is not exactly representable; manual review required")
    side = "sell" if direction == "long" else "buy"
    print(f"DRY RUN: full CLOSE {direction} {symbol}; {side} {amount} contracts at market")
    print(f"posSide={pos_side}; posMode={mode}; reduceOnly=true; nothing submitted.")
    print("Position freshness and remaining orders/protection require reconciliation.")


def cmd_info(client, parts):
    args, _ = parse(parts)
    for key, value in client.instrument(args["symbol"]).items():
        print(f"{key:17}: {value}")


def cmd_help(client, parts):
    parse(parts)
    print(__doc__)
    print(f"Limits: MAX_NOTIONAL_USDT={client.limits.max_notional_usdt}; MAX_LEVERAGE={client.limits.max_leverage}")


COMMANDS = {
    "buy": cmd_buy, "sell": cmd_sell, "close": cmd_close, "leverage": cmd_leverage,
    "orders": cmd_orders, "amend": cmd_amend, "cancel": cmd_cancel,
    "cancelall": cmd_cancelall, "balance": cmd_balance, "positions": cmd_positions,
    "info": cmd_info, "help": cmd_help, "account": cmd_account, "modes": cmd_modes,
}


def report_exchange_error(error):
    # Never print/log the exception message, request, response, command or traceback:
    # upstream exceptions may contain credentials and signed request material.
    name = type(error).__name__
    log.error("Exchange operation failed (%s); state is unverified", name)
    print(f"Exchange operation failed ({name}); state is unverified. No automatic retry.")


def repl(client):
    tag = "LIVE READ-ONLY" if client.live else "TESTNET"
    print(f"Market metadata loaded [{tag}]. Private authentication/account modes are unverified.")
    print("Exchange mutations are disabled. Type help for commands and dry previews.")
    while True:
        try:
            line = input(f"[{tag}]> ")
            parts = line.split()
            if not parts:
                continue
            parse(parts)  # validate exit/quit and all input before dispatch
            if parts[0].lower() in {"exit", "quit"}:
                return
            COMMANDS[parts[0].lower()](client, parts)
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print("\nInterrupted; no exchange mutation was enabled.")
        except SafetyError as error:
            print(f"ERROR: {error}")
        except Exception as error:
            report_exchange_error(error)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--live", action="store_true", help="connect to live endpoints in read-only mode")
    parser.add_argument("--env-file", type=Path, help="explicit profile file (default: project test.env/live.env)")
    options = parser.parse_args(argv)
    try:
        settings = load_settings(live=options.live, env_file=options.env_file)
        if settings.live:
            if input("LIVE account connectivity (mutations disabled). Type LIVE to continue: ").strip() != "LIVE":
                print("Aborted.")
                return 1
        from exchange_client import PhemexClient
        client = PhemexClient(settings.api_key, settings.api_secret, live=settings.live, limits=settings.limits)
        repl(client)
        return 0
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Aborted.")
        return 1
    except Exception as error:
        report_exchange_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
