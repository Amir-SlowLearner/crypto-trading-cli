"""Pure, non-executable scenario validation. No exchange or filesystem access."""
from dataclasses import dataclass
from decimal import Decimal, DecimalException, Inexact, localcontext
from fractions import Fraction
from uuid import uuid4

from command_parser import CommandError, dec, parse
from safety import reject_mutation


def check_sl_tp(side, pos_side, ref_price, sl, tp):
    if (side, pos_side) not in {('buy', 'Long'), ('sell', 'Short')}:
        raise CommandError('Attached TP/SL requires opening direction and an explicit Hedge Mode leg')
    if pos_side == 'Long':
        if sl is not None and sl >= ref_price:
            raise CommandError('Long stop-loss must be below the reference price')
        if tp is not None and tp <= ref_price:
            raise CommandError('Long take-profit must be above the reference price')
    else:
        if sl is not None and sl <= ref_price:
            raise CommandError('Short stop-loss must be above the reference price')
        if tp is not None and tp >= ref_price:
            raise CommandError('Short take-profit must be below the reference price')


@dataclass(frozen=True)
class PreparedOrder:
    symbol: str
    side: str
    action: str
    position_side: str
    current_qty: Decimal
    contract_size: Decimal
    requested_qty: str
    base_qty: Decimal
    contracts: Decimal
    resulting_qty: Decimal
    order_type: str
    price: object
    reference_price: Decimal
    notional: Decimal
    reduce_only: bool
    sl: object
    tp: object
    trigger: object
    trigger_price: object
    client_id: str
    warnings: tuple


def required(specs, key):
    fact = specs.get(key)
    if fact is None or fact.value is None:
        raise CommandError(f'Required instrument specification unavailable: {key}')
    value = dec(key, str(fact.value))
    if len(value.as_tuple().digits) > 40 or abs(value.adjusted()) > 40:
        raise CommandError(f'Instrument specification exceeds supported arithmetic precision: {key}')
    return value


def optional_bound(specs, key):
    fact = specs.get(key)
    if fact is None:
        return None
    if fact.value is None:
        if 'invalid' in fact.reason or 'inconsistent' in fact.reason:
            raise CommandError(f'Invalid instrument specification: {key}')
        return None
    return required(specs, key)


def exact_price(specs, name, value):
    price = dec(name, value)
    for key in ('price_tick', 'price_precision_tick'):
        if price % required(specs, key):
            raise CommandError(f'{name} is off-grid; prices are never rounded')
    for key, lower in (('min_price', True), ('max_price', False)):
        bound = optional_bound(specs, key)
        if bound is not None and (price < bound if lower else price > bound):
            raise CommandError(f'{name} violates the instrument price bounds')
    return price


def prepare_order(specs, parts, limits):
    """Return a scenario report, never an exchange request or executable payload."""
    args, flags = parse(parts)
    if 'dry' not in flags:
        reject_mutation()
    command = parts[0].lower()
    if command not in {'buy', 'sell', 'close'}:
        raise CommandError('Only buy, sell and explicit close scenarios can be prepared')
    # Bound arithmetic complexity; excess input precision is rejected, not rounded.
    for key in ('qty', 'usdt', 'current_qty', 'price', 'reference_price', 'sl', 'tp', 'trigger_price'):
        if key in args:
            value = Decimal(args[key])
            if len(value.as_tuple().digits) > 40 or abs(value.adjusted()) > 40:
                raise CommandError('Scenario numbers exceed supported offline arithmetic precision')
    try:
        with localcontext() as context:
            context.prec = 100
            context.traps[Inexact] = True
            return _prepare(specs, command, args, limits)
    except DecimalException:
        raise CommandError('Scenario arithmetic is not exactly representable; no preparation produced') from None


def _prepare(specs, command, args, limits):
    action, leg = args['action'], args['position_side']
    reducing = action in {'reduce', 'close'}
    expected = 'buy' if ((leg == 'long') != reducing) else 'sell'
    side = expected if command == 'close' else command
    if side != expected:
        raise CommandError('Buy/sell direction conflicts with action and position_side')
    current = dec('current_qty', args['current_qty'], allow_zero=True)
    if (action == 'open') != (current == 0):
        raise CommandError('Open requires current_qty=0; increase/reduce/close require positive current_qty')
    reduce_only = args.get('reduce_only', 'false') == 'true'
    if reduce_only != reducing:
        raise CommandError('Reduce/close require reduce_only=true; open/increase require false')
    active = specs.get('active')
    if active is None or active.value is not True:
        raise CommandError('Instrument is inactive or listing state is unknown')
    symbol = specs.get('symbol')
    if symbol is None or not isinstance(symbol.value, str):
        raise CommandError('Instrument identity is unavailable')
    identity = specs.get('id')
    if identity is None or args['symbol'].upper() not in (identity.value, symbol.value):
        raise CommandError('Instrument specifications do not match the requested symbol')
    if (specs.get('settlement') is None or specs['settlement'].value != 'USDT'
            or specs.get('market_type') is None or specs['market_type'].value != 'linear_perpetual'):
        raise CommandError('Preparation requires a USDT-settled linear perpetual')
    size = required(specs, 'contract_size')
    step = required(specs, 'quantity_step')
    contract_step = required(specs, 'contract_step')
    minimum, maximum = required(specs, 'min_contracts'), required(specs, 'max_contracts')
    min_notional = required(specs, 'min_notional')
    if minimum > maximum:
        raise CommandError('Instrument quantity bounds are inconsistent')
    if current % step or (current / size) % contract_step:
        raise CommandError('current_qty is off-grid; position context cannot be rounded')
    kind = args['type']
    if kind == 'limit':
        if 'reference_price' in args:
            raise CommandError('Limit notional uses price=; reference_price= is market-only')
        price = exact_price(specs, 'price', args['price'])
        reference = price
    else:
        if 'reference_price' not in args:
            raise CommandError('Market preparation requires reference_price=; no price requests are made')
        price = None
        reference = dec('reference_price', args['reference_price'])
    if action in {'reduce', 'close'} and 'usdt' in args:
        raise CommandError('Reduce/close require exact base units via qty=')
    requested = (Fraction(dec('qty', args['qty'])) if 'qty' in args else
                 Fraction(dec('usdt', args['usdt'])) / Fraction(reference))
    if action == 'reduce' and requested >= Fraction(current):
        raise CommandError('Reduce quantity must be strictly smaller than current_qty')
    if action == 'close' and requested != Fraction(current):
        raise CommandError('Close quantity must equal current_qty exactly')
    # Rational division prevents a near-boundary notional conversion rounding up.
    increments = requested / Fraction(step)
    base = step * (increments.numerator // increments.denominator)
    if base <= 0 or Fraction(base) > requested:
        raise CommandError('Normalized quantity is zero or would increase requested quantity')
    if action == 'close' and base != current:
        raise CommandError('Full close cannot leave a rounding remainder')
    native_maximum = optional_bound(specs, 'max_base_quantity')
    if native_maximum is not None and base > native_maximum:
        raise CommandError('Normalized base quantity exceeds the native maximum')
    contracts = base / size
    if contracts % contract_step:
        raise CommandError('Native quantity conflicts with CCXT contract precision')
    if contracts < minimum or contracts > maximum:
        raise CommandError('Normalized quantity violates instrument minimum/maximum')
    notional = base * reference
    if notional < min_notional:
        raise CommandError('Order value is below the instrument minimum (at the supplied price)')
    max_notional = optional_bound(specs, 'max_notional')
    if max_notional is not None and notional > max_notional:
        raise CommandError('Order value exceeds the instrument maximum')
    if notional > limits.max_notional_usdt:
        raise CommandError('Notional exceeds MAX_NOTIONAL_USDT (at the supplied price)')
    sl = exact_price(specs, 'sl', args['sl']) if 'sl' in args else None
    tp = exact_price(specs, 'tp', args['tp']) if 'tp' in args else None
    trigger = args.get('trigger')
    trigger_price = None
    if sl is not None or tp is not None:
        if action != 'open':
            raise CommandError('Attached TP/SL preparation supports open only; existing protection is unverified')
        if trigger is None or 'trigger_price' not in args:
            raise CommandError('TP/SL requires trigger=mark|last and a supplied trigger_price=')
        trigger_price = dec('trigger_price', args['trigger_price'])
        for ref in (reference, trigger_price):
            check_sl_tp(side, leg.capitalize(), ref, sl, tp)
    elif trigger is not None or 'trigger_price' in args:
        raise CommandError('Trigger parameters require sl= and/or tp=')
    warnings = ['current_qty is user-supplied and unverified, not an authenticated position.',
                'Hedge Mode is scenario intent; actual account mode is unverified.',
                'Accounting, equity, available margin, buying power and aggregate exposure are unknown.',
                'Exchange acceptance and financial safety are unverified; this is not executable.']
    if Fraction(base) != requested:
        warnings.append('Quantity was normalized DOWN to the native step; requested and normalized values differ.')
    if kind == 'market':
        warnings.append('reference_price is user-supplied and unverified; notional is an estimate, not a slippage bound.')
    if max_notional is None:
        warnings.append('Instrument maximum notional is unknown; only supplied limits were checked.')
    if price is not None or sl is not None or tp is not None:
        for key in ('min_price', 'max_price'):
            if optional_bound(specs, key) is None:
                warnings.append(f'Instrument {key} is unknown; that price-bound check is unavailable.')
    if reducing:
        warnings.append('posSide=Long/Short plus reduceOnly=true is documented; account-specific Hedge Mode execution is UNVERIFIED.')
    if sl is not None or tp is not None:
        warnings.append('Attached trigger mapping is documented; activation, fills, coverage and lifecycle are UNVERIFIED.')
    result = current - base if reducing else current + base
    return PreparedOrder(
        symbol=symbol.value, side=side, action=action, position_side=leg.capitalize(),
        current_qty=current, contract_size=size,
        requested_qty=args['qty'] if 'qty' in args else f"{args['usdt']} USDT / {reference} (derived base size)",
        base_qty=base, contracts=contracts, resulting_qty=result, order_type=kind,
        price=price, reference_price=reference, notional=notional, reduce_only=reduce_only,
        sl=sl, tp=tp, trigger=trigger, trigger_price=trigger_price,
        client_id=args.get('cl_id') or uuid4().hex, warnings=tuple(warnings))


def render_preparation(order):
    trigger = {'mark': 'ByMarkPrice', 'last': 'ByLastPrice'}.get(order.trigger)
    lines = ['DRY-RUN ORDER PREPARATION — NO EXCHANGE ORDER SUBMITTED',
             f'Symbol: {order.symbol}; market: USDT-settled linear perpetual',
             f'Contract size: {order.contract_size} base units per contract [CACHED CONVENTION]',
             f'Order: {order.order_type}; direction: {order.side}; action: {order.action}; position_side: {order.position_side}',
             f'current_qty: {order.current_qty} base units [USER-SUPPLIED, UNVERIFIED]',
             f'Requested quantity: {order.requested_qty} base units',
             f'Normalized quantity: {order.base_qty} base units; contracts: {order.contracts}',
             f'Resulting leg size: {order.resulting_qty} base units [SCENARIO ONLY]',
             f'Limit price: {order.price if order.price is not None else "not applicable"}',
             f'Notional: {order.notional} USDT [{"ESTIMATE from unverified reference_price" if order.price is None else "calculated from supplied limit price; fill not guaranteed"}]',
             f'reduceOnly={str(order.reduce_only).lower()}; posSide={order.position_side} [execution compatibility UNVERIFIED]',
             f'SL / TP: {order.sl} / {order.tp}; trigger source: {trigger or "not supplied"}; trigger reference: {order.trigger_price}',
             f'Protection directions: {"SL falling / TP rising" if order.position_side == "Long" else "SL rising / TP falling"}' if trigger else 'Protection: none supplied',
             'Validation: local input, intent, quantity grids/bounds, supplied-price notional and any TP/SL direction checks passed.',
             'Account verification: NOT PERFORMED. Executable: NO.',
             f'proposed cl_id: {order.client_id} (not reserved)']
    lines.extend('WARNING: ' + warning for warning in order.warnings)
    return '\n'.join(lines)
