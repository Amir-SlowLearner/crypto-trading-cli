"""Strict schemas for the existing interactive command vocabulary."""
from decimal import Decimal, InvalidOperation
import re

from safety import SafetyError


class CommandError(SafetyError):
    pass


# Order intent and scenario context are always explicit. Quantities use base units.
ORDER_ARGS = {"symbol", "qty", "usdt", "price", "type", "action", "position_side",
              "current_qty", "reduce_only", "reference_price", "sl", "tp", "trigger",
              "trigger_price", "cl_id"}
ORDER_REQUIRED = {"symbol", "action", "position_side", "current_qty"}

# allowed arguments, required arguments, allowed flags
SCHEMAS = {
    "buy": (ORDER_ARGS, ORDER_REQUIRED, {"dry"}),
    "sell": (ORDER_ARGS, ORDER_REQUIRED, {"dry"}),
    "amend": ({"symbol", "cl_id", "ord_id", "price", "qty"}, {"symbol"}, {"dry"}),
    "cancel": ({"symbol", "cl_id", "ord_id"}, {"symbol"}, {"dry"}),
    "cancelall": ({"symbol"}, {"symbol"}, {"dry"}),
    "close": (ORDER_ARGS - {"usdt"}, ORDER_REQUIRED | {"qty"}, {"dry"}),
    "leverage": ({"symbol", "x"}, {"symbol", "x"}, {"dry"}),
    "orders": ({"symbol"}, {"symbol"}, set()),
    "info": ({"symbol"}, {"symbol"}, set()),
    "positions": ({"symbol"}, set(), set()),
    "balance": (set(), set(), set()),
    "account": (set(), set(), set()),
    "modes": ({"symbol"}, set(), set()),
    "help": (set(), set(), set()),
    "exit": (set(), set(), set()),
    "quit": (set(), set(), set()),
}


def dec(name, value, *, allow_zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise CommandError(f"{name} must be a finite positive number") from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        rule = 'nonnegative' if allow_zero else 'positive'
        raise CommandError(f"{name} must be a finite {rule} number")
    return result


def parse(parts):
    if not parts or parts[0].lower() not in SCHEMAS:
        raise CommandError("Unknown command. Type help for supported commands")
    cmd = parts[0].lower()
    allowed, required, allowed_flags = SCHEMAS[cmd]
    args, flags = {}, set()
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            key = key.lower()
            if key not in allowed:
                raise CommandError(f"Unsupported parameter for {cmd}; type help")
            if key in args:
                raise CommandError(f"Duplicate parameter: {key}")
            if not value or "=" in value:
                raise CommandError(f"Invalid or empty value for {key}")
            args[key] = value
        else:
            flag = token.lower()
            if flag not in allowed_flags or flag in flags:
                raise CommandError(f"Unsupported or duplicate flag for {cmd}; type help")
            flags.add(flag)
    missing = required - args.keys()
    if missing:
        raise CommandError("Missing parameters: " + ", ".join(sorted(missing)))
    for key in ("qty", "usdt", "price", "sl", "tp", "x", "reference_price", "trigger_price"):
        if key in args:
            dec(key, args[key])
    if "current_qty" in args:
        dec('current_qty', args['current_qty'], allow_zero=True)
    if "symbol" in args and not re.fullmatch(r"[A-Za-z0-9:/._-]{1,80}", args["symbol"]):
        raise CommandError("Invalid symbol format")
    for key, maximum in (("cl_id", 40), ("ord_id", 128)):
        if key in args and not re.fullmatch(r"[A-Za-z0-9_-]{1," + str(maximum) + r"}", args[key]):
            raise CommandError(f"Invalid {key} format or length")
    if cmd in ("buy", "sell", "close"):
        for key, choices in (("action", {"open", "increase", "reduce", "close"}),
                             ("position_side", {"long", "short"}),
                             ("reduce_only", {"true", "false"}), ("trigger", {"mark", "last"})):
            if key in args:
                args[key] = args[key].lower()
                if args[key] not in choices:
                    raise CommandError(f"Unsupported {key}")
        if cmd == 'close' and args['action'] != 'close':
            raise CommandError('The close alias requires action=close')
        if ("qty" in args) == ("usdt" in args):
            raise CommandError("Give exactly one of qty= (base units) or usdt= (notional)")
        kind = args.get("type", "limit" if "price" in args else "market").lower()
        if kind not in {"market", "limit"} or ((kind == "limit") != ("price" in args)):
            raise CommandError("Use type=limit with price=, or type=market without price=")
        args["type"] = kind
    if cmd in ("amend", "cancel") and (("cl_id" in args) == ("ord_id" in args)):
        raise CommandError("Give exactly one of cl_id= or ord_id=")
    if cmd == "amend" and not {"price", "qty"}.intersection(args):
        raise CommandError("Nothing to amend; give price= and/or qty=")
    return args, flags
