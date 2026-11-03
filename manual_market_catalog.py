"""Pure inspection of the manual Testnet catalog; never execution evidence.

Keep the native inventory: CCXT may omit unfamiliar products or coalesce IDs.
Only the pinned adapter's reviewed PerpetualV2 base-quantity schema is supported.
"""
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import re

import ccxt

from account_discovery import Fact, contract_size_fact, reported, unknown
from safety import SafetyError


_CODE = r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}'
_SELECTOR = re.compile(r'(?:' + _CODE + r'|' + _CODE + r'/' + _CODE + r':USDT)', re.ASCII | re.IGNORECASE)


def valid_selector(value):
    return type(value) is str and _SELECTOR.fullmatch(value) is not None


def _code(value):
    return type(value) is str and re.fullmatch(_CODE, value, re.ASCII) is not None


def decimal_value(value):
    """Bound arithmetic and reject float/bool, nonfinite or unfamiliar encodings."""
    try:
        if type(value) not in (str, int, Decimal):
            return None
        result = Decimal(value)
        if (not result.is_finite() or len(result.as_tuple().digits) > 40
                or abs(result.adjusted()) > 40):
            return None
        return result
    except (ValueError, InvalidOperation):
        return None


def on_grid(value, step):
    """Exact integer arithmetic, independent of the active Decimal context."""
    value, step = decimal_value(value), decimal_value(step)
    if value is None or step is None or step <= 0:
        return False
    vn, vd = value.as_integer_ratio()
    sn, sd = step.as_integer_ratio()
    return (vn * sd) % (vd * sn) == 0


@dataclass(frozen=True)
class CatalogMarket:
    native: str
    symbol: str = ''
    base: str = ''
    status: str = 'unsupported'
    reason: str = 'metadata interpretation is unverified'
    contract_size: Fact = unknown()
    quantity_step: Fact = unknown()
    price_tick: Fact = unknown()
    minimum_quantity: Fact = unknown('native minimum quantity is not established')
    product_type: str = 'unknown'

    @property
    def supported(self):
        return self.status == 'supported'


@dataclass(frozen=True)
class MarketCatalog:
    markets: tuple = ()
    issues: tuple = ()

    def resolve(self, selector):
        if not valid_selector(selector):
            raise SafetyError('Invalid market selector')
        # Native IDs are case-sensitive exchange identities. In particular a
        # lowercase spot prefix is not an uppercase perpetual base letter.
        # An exact spot ID must reject, never fall through to a folded swap ID.
        exact = [m for m in self.markets if selector in (m.native, m.symbol)]
        if exact:
            matches = [m for m in exact if m.product_type not in ('spot', 'dated_future')]
        else:
            # Convenience aliases live only in this command's perpetual scope.
            # Keep unknown/unsupported candidates so they cannot be hidden by
            # selecting a better-looking folded match.
            matches = [m for m in self.markets if m.product_type not in ('spot', 'dated_future')
                       and m.status != 'excluded' and selector.casefold() in
                       (m.native.casefold(), m.symbol.casefold())]
        if len(matches) != 1 or not matches[0].supported:
            raise SafetyError('Selected market is absent, ambiguous, excluded or unsupported; inspect --markets')
        return matches[0]

    def render(self):
        lines = ['PUBLIC TESTNET MARKET CATALOG — display only; refreshed this collection']
        for market in self.markets:
            lines.append(f'{market.native} / {market.symbol or "UNKNOWN"}: {market.status.upper()}; '
                         f'type={market.product_type}; {market.reason}')
        lines.extend('Catalog issue: ' + issue for issue in self.issues)
        return '\n'.join(lines)


def _inspect(raw, parsed, precision_mode):
    if type(raw) is not dict:
        return CatalogMarket('UNKNOWN', reason='malformed native product')
    native = raw.get('symbol')
    if not _code(native):
        return CatalogMarket('UNKNOWN', reason='missing or unsafe native product identifier')
    kind, status = raw.get('type'), raw.get('status')
    product_type = ('spot' if kind in ('Spot', 'spot') else
                    'dated_future' if kind in ('Future', 'Futures') else
                    'perpetual' if kind in ('Perpetual', 'PerpetualV2', 'PerpetualPilot') else 'unknown')
    entry = CatalogMarket(native, product_type=product_type)
    # Positive exclusions only. Missing/unfamiliar classification remains visible.
    if status in ('Delisted', 'Unlisted') or kind in ('Spot', 'spot', 'Future', 'Futures'):
        return replace(entry, status='excluded', reason='inactive, spot or dated future')
    settlement = raw.get('settleCurrency')
    if type(settlement) is str and settlement != 'USDT':
        return replace(entry, status='excluded', reason='not USDT-settled')
    quote = raw.get('quoteCurrency')
    if type(quote) is str and quote != 'USDT':
        return replace(entry, status='excluded', reason='not USDT linear quote/settlement')
    if kind != 'PerpetualV2' or status != 'Listed' or settlement != 'USDT' or quote != 'USDT':
        return replace(entry, reason='unverified product type, listing status or settlement schema')
    if any(raw.get(k) is not None for k in ('expiry', 'expiryTime', 'expiryDatetime')):
        return replace(entry, reason='unexpected native expiry; perpetual classification is unverified')
    underlying, base_id = raw.get('contractUnderlyingAssets'), raw.get('baseCurrency')
    if underlying is not None and base_id is not None and (
            type(underlying) is not str or type(base_id) is not str
            or underlying.replace(' ', '') != base_id.replace(' ', '')):
        return replace(entry, reason='contradictory base and underlying quantity units')
    matches = [m for m in parsed if type(m) is dict and m.get('id') == native and m.get('type') == 'swap']
    if len(matches) != 1:
        return replace(entry, reason='missing or duplicate CCXT market mapping')
    market = matches[0]
    if (market.get('swap') is not True or market.get('linear') is not True
            or market.get('active') is not True or market.get('settle') != 'USDT'
            or market.get('quote') != 'USDT' or market.get('spot') is not False
            or market.get('future') is not False or market.get('inverse') is not False
            or market.get('expiry') is not None):
        return replace(entry, reason='ineligible or contradictory CCXT market classification')
    symbol, base = market.get('symbol'), market.get('base')
    if (not valid_selector(symbol) or not _code(base)
            or symbol != base + '/USDT:USDT'
            or market.get('baseId') != raw.get('baseCurrency', raw.get('contractUnderlyingAssets'))):
        return replace(entry, reason='unverified symbol or base-asset quantity unit')
    entry = replace(entry, symbol=symbol, base=base)
    # Legacy merging must not silently override native business metadata.
    info = market.get('info')
    if type(info) is not dict or any(info.get(k) != raw.get(k) for k in
            ('symbol', 'type', 'status', 'settleCurrency', 'quoteCurrency', 'baseCurrency',
             'contractSize', 'qtyStepSize', 'tickSize', 'qtyPrecision', 'pricePrecision')):
        return replace(entry, reason='contradictory merged/native product metadata')
    size = contract_size_fact(market)
    # CCXT 4.5.90 forces contractSize=1 for USDT. A different native size is
    # unsupported, rather than treating sizeRq as contracts or guessing a unit.
    if size.value != Decimal(1) or decimal_value(market.get('contractSize')) != Decimal(1):
        return replace(entry, reason='unsupported or inconsistent USDT contract size')
    if raw.get('contractSize') is not None and decimal_value(raw['contractSize']) != Decimal(1):
        return replace(entry, reason='unverified native contract-size encoding')
    precision = market.get('precision')
    if precision_mode != ccxt.TICK_SIZE or type(precision) is not dict:
        return replace(entry, reason='unverified precision mode')
    values = []
    for native_key, normalized_key, digits_key in (
            ('qtyStepSize', 'amount', 'qtyPrecision'), ('tickSize', 'price', 'pricePrecision')):
        value = decimal_value(raw.get(native_key))
        if value is None or value <= 0 or value != decimal_value(precision.get(normalized_key)):
            return replace(entry, reason='missing, invalid or contradictory quantity/price grid')
        digits = raw.get(digits_key)
        if digits is not None and (type(digits) is not int or not 0 <= digits <= 40
                                  or not on_grid(value, Decimal(1).scaleb(-digits))):
            return replace(entry, reason='contradictory declared decimal precision')
        values.append(value)
    return replace(entry, status='supported', reason='reviewed PerpetualV2 base-quantity schema',
                   contract_size=size, quantity_step=reported(values[0], 'public/products.qtyStepSize'),
                   price_tick=reported(values[1], 'public/products.tickSize'))


def _schema_label(key):
    # Public schema names only; never include arbitrary values or response text.
    return key if type(key) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key, re.ASCII) else 'UNKNOWN_FIELD'


def _schema_shape(value):
    if type(value) is list:
        return 'array(' + str(len(value)) + ')'
    if type(value) is dict:
        return 'object(' + str(len(value)) + ')'
    return {str: 'string', int: 'integer', Decimal: 'decimal', bool: 'boolean', type(None): 'null'}.get(type(value), 'unknown')


def _unreviewed_products(value):
    """Expose product-like rows without inferring a new container's semantics.

    Bounded traversal includes keyed/nested inventories. Reaching a limit never
    removes the blocking schema issue or establishes complete market coverage.
    """
    found, pending, visited = [], [(value, 0)], 0
    while pending and visited < 20000:
        item, depth = pending.pop()
        visited += 1
        if type(item) is dict and ('symbol' in item or ('type' in item and 'settleCurrency' in item)):
            found.append(item)
        elif depth < 8:
            if type(item) is dict:
                pending.extend((child, depth + 1) for child in item.values() if type(child) in (list, dict))
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item if type(child) in (list, dict))
    return found


def _reviewed_leverage_margins(value):
    """Structural classification only; never a source of execution risk limits.

    Match the operator-observed public schema across the whole array, not just
    the diagnostic's first three elements. No coercion or unknown-field removal.
    """
    if type(value) is not list:
        return False
    tier_types = {'notionalValueRv': int, 'maxLeverage': Decimal,
                  'maintenanceMarginRateRr': str, 'maintenanceAmountRv': str}
    for entry in value:
        if (type(entry) is not dict or set(entry) != {'index_id', 'items'}
                or type(entry['index_id']) is not int or type(entry['items']) is not list):
            return False
        for tier in entry['items']:
            if (type(tier) is not dict or set(tier) != set(tier_types)
                    or any(type(tier[key]) is not expected for key, expected in tier_types.items())
                    or not tier['maxLeverage'].is_finite()):
                return False
    return True


def build_catalog(public, legacy, parsed, precision_mode):
    """Audit native rows before set_markets can overwrite duplicate identities."""
    rows, issues = [], []
    if type(public) is not dict:
        return MarketCatalog(issues=('Public product inventory unavailable',))
    for bucket in ('products', 'perpProductsV2'):
        value = public.get(bucket)
        if type(value) is not list:
            issues.append('Missing or malformed ' + bucket + ' inventory')
        else:
            rows.extend(value)
    entries = [_inspect(raw, parsed, precision_mode) for raw in rows]
    # These auxiliary containers are read by the pinned adapter and are not
    # independent product inventories. Unknown containers are never waved away
    # because their name looks like metadata, a checksum, or a version marker.
    reviewed_containers = {'products', 'perpProductsV2', 'currencies', 'riskLimits',
                           'riskLimitsV2', 'leverages', 'leveragesV2'}
    for key, value in public.items():
        # Observed optional field, after strict JSON/envelope acceptance. Literal
        # null discloses no inventory rows; it is not a JSON parser fallback.
        # This exact exception says nothing about non-null pilot schemas or the
        # completeness/availability of any other exchange product inventory.
        if key == 'perpProductsPilot' and value is None:
            continue
        if key == 'leverageMargins' and _reviewed_leverage_margins(value):
            continue
        if key not in reviewed_containers and ('product' in key.lower() or key == 'leverageMargins'
                                              or type(value) in (list, dict)):
            extra = _unreviewed_products(value)
            issues.append('Unreviewed product inventory schema: field=' + _schema_label(key)
                          + '; shape=' + _schema_shape(value) + '; product_rows=' + str(len(extra))
                          + '; coverage=UNKNOWN')
            entries.extend(replace(_inspect(raw, [], precision_mode), status='unsupported',
                                   reason='unreviewed product inventory schema') for raw in extra)
    native_ids = {r.get('symbol') for r in rows if type(r) is dict and _code(r.get('symbol'))}
    if type(legacy) is not list:
        issues.append('Legacy product inventory unavailable')
    else:
        legacy_counts = Counter(r['symbol'] for r in legacy
                                if type(r) is dict and _code(r.get('symbol')))
        entries = [replace(e, status='unsupported', reason='duplicate legacy product identity')
                   if legacy_counts[e.native] > 1 else e for e in entries]
        for raw in legacy:
            if type(raw) is not dict or not _code(raw.get('symbol')) or raw['symbol'] not in native_ids:
                entry = _inspect(raw, [], precision_mode)
                entries.append(entry if entry.status == 'excluded' else replace(
                    entry, status='unsupported', reason='unmatched legacy inventory; interpretation unverified'))
    # Product type plus exact native ID is the business identity. Unified
    # aliases are separately checked, also with exact exchange/CCXT spelling.
    # Folded matches are resolved conservatively at selection time, not used to
    # relabel unrelated spot products as ambiguous perpetuals.
    def identities(entry):
        return [(entry.product_type, field, value) for field, value in
                (('native', entry.native), ('unified', entry.symbol)) if value and value != 'UNKNOWN']
    counts = Counter(identity for entry in entries for identity in identities(entry))
    entries = [replace(e, status='unsupported', reason='ambiguous native or unified market identity')
               if any(counts[identity] > 1 for identity in identities(e)) else e
               for e in entries]
    return MarketCatalog(tuple(entries), tuple(issues))
