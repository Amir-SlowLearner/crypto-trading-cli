"""Pure, display-only projection of selected-symbol private row schema.

No credentials, transport, quantity values, account fields or execution authority.
Unfamiliar symbols/values are never echoed or interpreted as known position legs.
"""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import json

from manual_market_catalog import valid_selector


_FIELDS = frozenset({
    'symbol', 'posMode', 'posSide', 'side', 'sizeRq', 'size', 'currency',
    'leverageRr', 'crossMargin', 'positionStatus',
    # Show only their types if unfamiliar representations use these names.
    # Their presence never makes them accepted aliases in the position parser.
    'positionSide', 'positionMode', 'position_side', 'position_mode',
    'positionIdx', 'contracts', 'quantity', 'qty', 'marginMode', 'leverage',
})
_MAX_ROWS = 20


def _kind(value):
    return {str:'string', int:'integer', Decimal:'decimal', bool:'boolean',
            type(None):'null', dict:'object', list:'array'}.get(type(value), 'unsupported')


def _enum(value, choices):
    return choices.get(value, 'UNKNOWN') if type(value) is str else 'UNKNOWN'


@dataclass(frozen=True)
class PositionRowDiagnostic:
    lines: tuple
    available: bool = True
    incomplete: bool = False


def describe_position_rows(data, market):
    """Reveal only selected identity, leg/mode labels and approved field types.

    Related unified/case variants are diagnostic candidates, never accepted
    position aliases. No unrelated symbols, counts, field names or values escape.
    """
    if (not valid_selector(market.native) or not valid_selector(market.symbol)
            or type(data) is not dict or type(data.get('positions')) is not list):
        return PositionRowDiagnostic(('Selected position schema unavailable; coverage UNKNOWN.',), False, True)
    native, unified = market.native, market.symbol
    groups = {name:Counter() for name in ('exact_native', 'exact_unified', 'case_variant')}
    shown, matched, displayed, unscopable = [], 0, 0, False
    for row in data['positions']:
        if type(row) is not dict or type(row.get('symbol')) is not str:
            unscopable = True
            continue
        symbol = row['symbol']
        if not valid_selector(symbol):
            unscopable = True
            continue
        if symbol == native:
            group = 'exact_native'
        elif symbol == unified:
            group = 'exact_unified'
        elif symbol.casefold() in (native.casefold(), unified.casefold()):
            group = 'case_variant'
        else:
            continue  # Never report anything about unrelated positions.
        leg = _enum(row.get('posSide'), {'Long':'Long', 'Short':'Short', 'Merged':'Merged'})
        mode = _enum(row.get('posMode'), {'Hedged':'HEDGE', 'OneWay':'ONE_WAY'})
        direction = _enum(row.get('side'), {'Buy':'LONG', 'Sell':'SHORT', 'None':'FLAT'})
        groups[group][leg] += 1
        matched += 1
        if displayed >= _MAX_ROWS:
            continue
        displayed += 1
        consistency = ('CONFLICT' if (mode == 'HEDGE' and leg == 'Merged')
                       or (mode == 'ONE_WAY' and leg in ('Long','Short')) else 'UNVERIFIED')
        fields = ', '.join(key + ':' + _kind(row[key]) for key in sorted(_FIELDS.intersection(row)))
        shown.append(f'Selected row {displayed}: symbol={json.dumps(symbol, ensure_ascii=True)}; '
                     f'match={group}; leg={leg}; mode={mode}; direction={direction}; mode_leg={consistency}')
        shown.append('  field types: ' + fields)
    lines = [f'Selected identity: native={native}; unified={unified}']
    for name, counts in groups.items():
        lines.append(name + ': rows=' + str(sum(counts.values())) + '; ' +
                     '; '.join(leg + '=' + str(counts[leg]) for leg in ('Long','Short','Merged','UNKNOWN')))
    lines.extend(shown)
    # Two lines are emitted per selected row; no account inventory totals.
    truncated = matched > _MAX_ROWS
    if truncated:
        lines.append('Selected-row detail truncated; counts include all matched rows; coverage UNKNOWN.')
    if unscopable:
        lines.append('Unscopable rows omitted; selected position coverage UNKNOWN.')
    paginated = bool(data.get('nextPageArg'))
    if paginated:
        lines.append('Response pagination is present; selected position coverage UNKNOWN.')
    lines += ['Only reviewed position-field names/types are shown; other fields are omitted.',
              'No matching row does not establish an absent or zero position.',
              'Related symbol spellings and enum labels are unverified observations, not accepted position aliases.']
    return PositionRowDiagnostic(tuple(lines), incomplete=truncated or unscopable or paginated)
