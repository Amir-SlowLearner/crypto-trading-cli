"""File-backed metadata for credential-free, network-free preparation."""
import json
from decimal import Decimal
from pathlib import Path
import re
from types import SimpleNamespace

import ccxt

from config import RiskLimits
from instrument_specs import inspect_instrument
from safety import SafetyError


class CachedPreparationClient:
    """Contains no transport, credentials or account methods."""
    live = False

    def __init__(self, markets, provenance):
        symbols, identifiers = set(), set()
        for market in markets:
            if not isinstance(market, dict):
                raise SafetyError('Invalid market cache entry')
            for key, seen in (('symbol', symbols), ('id', identifiers)):
                value = market.get(key)
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9:/._-]{1,80}', value) or value in seen:
                    raise SafetyError('Missing, invalid or ambiguous cached instrument identity')
                seen.add(value)
            if not isinstance(market.get('info', {}), dict):
                raise SafetyError('Invalid cached instrument metadata')
            for key in ('precision', 'limits'):
                if not isinstance(market.get(key, {}), dict):
                    raise SafetyError('Invalid cached instrument specifications')
            for value in (market.get('limits') or {}).values():
                if value is not None and not isinstance(value, dict):
                    raise SafetyError('Invalid cached instrument limit structure')
        self.catalog = SimpleNamespace(markets={m['symbol']: m for m in markets}, precisionMode=ccxt.TICK_SIZE)
        self.limits = RiskLimits()
        self.metadata_provenance = ('SYNTHETIC EXAMPLE — not exchange specifications' if provenance == 'synthetic'
                                    else 'USER-SUPPLIED PUBLIC SNAPSHOT — provenance and freshness unverified')

    def instrument(self, symbol):
        return inspect_instrument(self.catalog, symbol)


def load_market_cache(path):
    try:
        data = json.loads(Path(path).read_text(), parse_float=Decimal)
    except (OSError, ValueError, UnicodeError):
        raise SafetyError('Cannot read a valid public instrument cache; contents omitted') from None
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or data.get('precision_mode') != 'tick_size'
            or data.get('provenance') not in ('synthetic', 'user_supplied_public_snapshot')
            or not isinstance(data.get('markets'), list) or not data['markets']):
        raise SafetyError('Unsupported instrument cache format; no default specifications assumed')
    return CachedPreparationClient(data['markets'], data['provenance'])
