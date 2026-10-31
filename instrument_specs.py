"""Instrument inspection, including native metadata lost by pinned CCXT parsing."""
import ccxt

from account_discovery import choice, contract_size_fact, derived, number, reported, unknown
from safety import SafetyError


def inspect_instrument(exchange, raw):
    raw = (raw or '').strip().upper()
    matches = [m for m in exchange.markets.values()
               if raw in (m.get('id'), m.get('symbol')) and m.get('swap') is True
               and m.get('linear') is True and m.get('settle') == 'USDT'
               and m.get('quote') == 'USDT']
    if len(matches) != 1:
        raise SafetyError('Instrument must identify one USDT-settled linear perpetual')
    market = matches[0]
    raw_info = market.get('info') or {}
    limits = market.get('limits') or {}
    precision = market.get('precision') or {}
    source = 'CCXT market.info'
    fields = {
        'symbol': reported(market['symbol'], 'CCXT market'),
        'settlement': reported('USDT', 'CCXT market'),
        'contract_size': contract_size_fact(market),
        'status': choice(raw_info, 'status', {'Listed': 'listed', 'Delisted': 'delisted'}, source),
        'active': (reported(market['active'], 'CCXT market.active')
                   if type(market.get('active')) is bool else unknown('market activity not established')),
        'max_leverage': number(raw_info, 'maxLeverage', source, positive=True),
        'leverage_tiers': unknown('USDT tier table is not supplied by the normalized market; do not assume a flat limit'),
    }
    if fields['status'].value and fields['active'].value is not None:
        if (fields['status'].value == 'listed') != fields['active'].value:
            fields['active'] = unknown('inconsistent native and normalized listing status')
    for key, native, fallback, fallback_name in (
        ('quantity_step', 'qtyStepSize', {}, ''),
        ('price_tick', 'tickSize', precision, 'price'),
        ('max_base_quantity', 'maxOrderQtyRq', {}, ''),
        ('min_notional', 'minOrderValueRv', limits.get('cost') or {}, 'min'),
        ('min_price', 'minPriceRp', limits.get('price') or {}, 'min'),
        ('max_price', 'maxPriceRp', limits.get('price') or {}, 'max'),
    ):
        fact = number(raw_info, native, source, positive=True)
        # An invalid explicitly supplied native field must not be replaced by a
        # plausible normalized default. Missing precision fallback needs tick mode.
        fallback_ok = key not in ('quantity_step', 'price_tick') or exchange.precisionMode == ccxt.TICK_SIZE
        if raw_info.get(native) is None and fallback_name and fallback_ok:
            fact = number(fallback, fallback_name, 'CCXT normalized metadata', positive=True)
        fields[key] = fact
    size = fields['contract_size'].value
    maximum = fields['max_base_quantity'].value
    if raw_info.get('maxOrderQtyRq') is not None:
        fields['max_contracts'] = (derived(maximum / size, 'maxOrderQtyRq / contractSize')
                                   if maximum is not None and size is not None else
                                   unknown('native amount limit invalid or contract size unknown'))
    else:
        fields['max_contracts'] = number(limits.get('amount') or {}, 'max', 'CCXT amount limit', positive=True)
    fields['min_contracts'] = number(limits.get('amount') or {}, 'min', 'CCXT amount limit', positive=True)
    minimum = fields['min_contracts'].value
    fields['min_base_quantity'] = (derived(minimum * size, 'CCXT minimum contracts * contractSize')
                                    if minimum is not None and size is not None else unknown())
    if raw_info.get('qtyStepSize') is None and exchange.precisionMode == ccxt.TICK_SIZE:
        step = number(precision, 'amount', 'CCXT amount precision', positive=True)
        fields['quantity_step'] = (derived(step.value * size, 'CCXT amount tick * contractSize')
                                   if step.value is not None and size is not None else unknown())
    fields['max_notional'] = (unknown('pinned CCXT maps legacy maxOrderQty into cost.max; units unverified')
                              if raw_info.get('maxOrderQty') is not None else
                              number(limits.get('cost') or {}, 'max', 'CCXT cost limit', positive=True))
    if fields['min_price'].value is not None and fields['max_price'].value is not None:
        if fields['min_price'].value > fields['max_price'].value:
            fields['min_price'] = fields['max_price'] = unknown('inconsistent price bounds')
    if fields['min_contracts'].value is not None and fields['max_contracts'].value is not None:
        if fields['min_contracts'].value > fields['max_contracts'].value:
            fields['min_contracts'] = fields['max_contracts'] = unknown('inconsistent amount bounds')
    return fields
