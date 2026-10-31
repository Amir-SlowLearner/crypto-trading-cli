"""Strict schemas for the existing interactive command vocabulary."""
from decimal import Decimal, InvalidOperation
import re

from safety import SafetyError


class CommandError(SafetyError):
    pass


# allowed arguments, required arguments, allowed flags
SCHEMAS = {
    "buy": ({"symbol", "qty", "usdt", "price", "type", "position", "sl", "tp", "cl_id"}, {"symbol"}, {"dry"}),
    "sell": ({"symbol", "qty", "usdt", "price", "type", "position", "sl", "tp", "cl_id"}, {"symbol"}, {"dry"}),
    "amend": ({"symbol", "cl_id", "ord_id", "price", "qty"}, {"symbol"}, {"dry"}),
    "cancel": ({"symbol", "cl_id", "ord_id"}, {"symbol"}, {"dry"}),
    "cancelall": ({"symbol"}, {"symbol"}, {"dry"}),
    "close": ({"symbol", "position"}, {"symbol"}, {"dry"}),
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


def dec(name, value):
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise CommandError(f"{name} must be a finite positive number") from None
    if not result.is_finite() or result <= 0:
        raise CommandError(f"{name} must be a finite positive number")
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
    for key in ("qty", "usdt", "price", "sl", "tp", "x"):
        if key in args:
            dec(key, args[key])
    if "symbol" in args and not re.fullmatch(r"[A-Za-z0-9:/._-]{1,80}", args["symbol"]):
        raise CommandError("Invalid symbol format")
    for key, maximum in (("cl_id", 40), ("ord_id", 128)):
        if key in args and not re.fullmatch(r"[A-Za-z0-9_-]{1," + str(maximum) + r"}", args[key]):
            raise CommandError(f"Invalid {key} format or length")
    if "position" in args:
        args["position"] = args["position"].capitalize()
        choices = {"Long", "Short"} if cmd == "close" else {"Long", "Short", "Merged"}
        if args["position"] not in choices:
            raise CommandError("Unsupported position selection")
    if cmd in ("buy", "sell"):
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
