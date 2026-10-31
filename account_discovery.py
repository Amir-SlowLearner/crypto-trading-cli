"""Read-only Phemex discovery and conservative interpretation of raw CCXT info.

No credentials or raw private responses are retained in reports. Missing values
are unknown, never zero. Financial totals keep their original currency and scope.
See docs/account-discovery.md for endpoint semantics and adapter limitations.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from diagnostics import ResponseEvidence, diagnose


@dataclass(frozen=True)
class Fact:
    value: object = None
    origin: str = 'unknown'
    source: str = ''
    reason: str = 'not supplied'

    def __str__(self):
        if self.value is None:
            return f'unknown ({self.reason})'
        return f'{self.value} [{self.origin}: {self.source}]'


def reported(value, source):
    return Fact(value, 'reported', source, '')


def derived(value, source):
    return Fact(value, 'derived', source, '')


def unknown(reason='not supplied', source=''):
    return Fact(source=source, reason=reason)


def number(row, name, source, *, nonnegative=False, positive=False):
    value = row.get(name)
    if value is None:
        return unknown(source=source + '.' + name)
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or (nonnegative and result < 0) or (positive and result <= 0):
            raise ValueError
    except (InvalidOperation, ValueError):
        return unknown('invalid numeric response', source + '.' + name)
    return reported(result, source + '.' + name)


def choice(row, name, allowed, source):
    value = row.get(name)
    if not isinstance(value, str) or value not in allowed:
        return unknown('missing or unsupported value', source + '.' + name)
    return reported(allowed[value], source + '.' + name)


def aliases(row, names, source, **kwargs):
    present = [number(row, name, source, **kwargs) for name in names if row.get(name) is not None]
    if not present:
        return unknown(source=source + '.' + '/'.join(names))
    if any(f.value is None for f in present) or len({f.value for f in present}) != 1:
        return unknown('invalid or inconsistent aliases', source)
    return present[0]


def contract_size_fact(market):
    normalized = number(market, 'contractSize', 'CCXT market', positive=True)
    if normalized.value is None:
        return normalized
    info = market.get('info') or {}
    if info.get('contractSize') is None:
        return derived(normalized.value, 'CCXT USDT contract amount convention')
    native = number(info, 'contractSize', 'CCXT market.info', positive=True)
    if native.value is None or native.value != normalized.value:
        return unknown('invalid or inconsistent native/normalized contract size')
    return native


@dataclass
class AccountReport:
    started_at: str
    completed_at: str = ''
    authenticated: Fact = field(default_factory=unknown)
    account_type: Fact = field(default_factory=unknown)
    wallet: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)
    risk_units: list = field(default_factory=list)
    positions_available: bool = False
    positions_complete: bool = False
    issues: list = field(default_factory=list)

    @property
    def complete(self):
        # Completion means readable/consistent supported responses, not safe trading.
        return (not self.issues and self.authenticated.value is True
                and self.account_type.value is not None and self.positions_complete
                and self.wallet.get('balance', unknown()).value is not None
                and bool(self.risk_units)
                and all(p[key].value is not None for p in self.positions
                        for key in ('symbol', 'contracts', 'position_side', 'position_mode', 'margin_mode'))
                and all(unit[key].value is not None for unit in self.risk_units
                        for key in ('currency', 'risk_type', 'equity')))


class AccountDiscovery:
    """A fresh bounded snapshot, using fixed GET methods only; no fallback guesses."""
    def __init__(self, exchange):
        self.ex = exchange
        self.report = AccountReport(datetime.now(timezone.utc).isoformat())
        self.successes = 0
        self.authentication_failed = False
        self.stopped = False
        self._last_failure = None

    def issue(self, message):
        if message not in self.report.issues:
            self.report.issues.append(message)

    def read(self, label, method, params, expected):
        self._last_failure = None
        if self.stopped:
            self.issue(label + ': skipped after authentication/network/server/rate-limit failure')
            return None
        # Reset before invoking the implicit method, including failures before
        # transport/signing. Never reuse a previous endpoint's HTTP evidence.
        reset = getattr(type(self.ex), 'reset_read_diagnostic', None)
        if reset is not None:
            reset(self.ex)
        try:
            response = method(params)
        except Exception as error:
            self.failed_read(label, error=error)
            return None
        if (not isinstance(response, dict) or type(response.get('code')) not in (int, str)
                or response['code'] not in (0, '0') or response.get('error')
                or not isinstance(response.get('data'), expected)):
            self.failed_read(label, response=response)
            return None
        self.successes += 1
        return response['data']

    def failed_read(self, label, *, error=None, response=None):
        evidence = vars(self.ex).get('last_read_diagnostic')
        if not isinstance(evidence, ResponseEvidence):
            evidence = None
        failure = diagnose(error, evidence, response)
        self._last_failure = failure
        self.issue(label + ': ' + str(failure))
        self.authentication_failed |= failure.category == 'authentication'
        self.stopped |= failure.stop

    def discover(self):
        # A reused reader must never retain a previously successful account state.
        self.report = AccountReport(datetime.now(timezone.utc).isoformat())
        self.successes = 0
        self.authentication_failed = False
        self.stopped = False
        self._last_failure = None
        mode = self.read('risk_mode', self.ex.privateGetUtaApiRiskRiskMode, {}, dict)
        # This exception belongs ONLY to this fixed GET in account discovery.
        # Shared diagnostics and transport keep stopping on server errors. There
        # is no retry and no fallback account classification. Typed/recognized
        # failures, other HTTP statuses, and every other endpoint keep their rules.
        failure = self._last_failure
        if (failure is not None and failure.category == 'server_error'
                and failure.http_status == 500 and failure.generic_exchange_error
                and failure.reason in ('unclassified', 'retry_later')):
            self.stopped = False
            self.issue('risk_mode: continuing independent discovery GETs; failed probe will not be retried')
        if mode is not None:
            self.report.account_type = choice(mode, 'riskMode',
                {'Classic': 'classic', 'MultiAsset': 'unified_multi_asset'}, 'uta/risk-mode')
        if self.report.account_type.value is None:
            self.issue('risk_mode: accounting type is unknown; no classic fallback')

        data = self.read('account_positions', self.ex.privateGetGAccountsPositions,
                         {'currency': 'USDT'}, dict)
        account = {}
        if data is not None:
            if isinstance(data.get('account'), dict) and data['account'].get('currency') == 'USDT':
                account = data['account']
            else:
                self.issue('account_positions: missing account or settlement currency mismatch')
            rows = data.get('positions')
            if isinstance(rows, list):
                self.report.positions_available = True
                self.report.positions_complete = True
                self.report.positions = [self.position(row) for row in rows]
                self.validate_positions()
                if data.get('nextPageArg'):
                    self.report.positions_complete = False
                    self.issue('account_positions: incomplete/paginated position response')
            else:
                self.issue('account_positions: positions unavailable; not an empty account')

        classic = self.report.account_type.value == 'classic'
        # CrossAsset on the legacy risk-unit endpoint is a scope name, not proof
        # of MultiAsset accounting. Only risk-mode establishes the account type.
        method = self.ex.privateGetGAccountsRiskUnit if classic else self.ex.privateGetUtaApiRiskRiskUnits
        risk_rows = self.read('risk_units', method, {} if classic else {'currency': 'USDT'}, list)
        if risk_rows is not None:
            self.report.risk_units = [self.risk_unit(row, classic) for row in risk_rows]
            self.validate_risk_units()

        self.report.wallet = (self.classic_wallet(account) if self.report.account_type.value == 'classic'
                              else self.unknown_wallet())
        if self.report.account_type.value == 'unified_multi_asset':
            asset = self.read('asset', self.ex.privateGetUtaBizAssets, {'currency': 'USDT'}, dict)
            if asset is not None and asset.get('currency') == 'USDT':
                self.report.wallet.update({
                    'balance': number(asset, 'balanceRv', 'uta/assets'),
                    'bonus': number(asset, 'totalBonusRv', 'uta/assets', nonnegative=True),
                    'borrowed': number(asset, 'borrowedRv', 'uta/assets', nonnegative=True),
                    'interest': number(asset, 'interestRv', 'uta/assets', nonnegative=True),
                    'frozen_as_collateral': number(asset, 'frozenBalanceAsCollateralRv', 'uta/assets', nonnegative=True),
                })
            else:
                self.issue('asset: USDT wallet unavailable or currency mismatch')

        if self.authentication_failed:
            self.report.authenticated = (unknown('inconsistent authentication results') if self.successes else
                                         reported(False, 'authenticated GET rejected'))
        elif self.successes:
            self.report.authenticated = reported(True, 'successful authenticated GET envelope')
        else:
            self.report.authenticated = unknown('no successful authenticated response')
        for values in [self.report.wallet, *self.report.positions, *self.report.risk_units]:
            if any(f.value is None and ('invalid' in f.reason or 'inconsistent' in f.reason)
                   for f in values.values() if isinstance(f, Fact)):
                self.issue('snapshot: invalid or inconsistent fields are unknown')
        self.report.completed_at = datetime.now(timezone.utc).isoformat()
        return self.report

    @staticmethod
    def unknown_wallet():
        return {name: unknown('account-wide value not established for this accounting scope')
                for name in ('balance', 'used', 'bonus', 'borrowed', 'interest', 'frozen_as_collateral',
                             'equity', 'unallocated_wallet_balance', 'available_margin')}

    def classic_wallet(self, account):
        result = self.unknown_wallet()
        result.update({
            'balance': number(account, 'accountBalanceRv', 'g-accounts/positions.account'),
            'used': number(account, 'totalUsedBalanceRv', 'g-accounts/positions.account', nonnegative=True),
            'bonus': number(account, 'bonusBalanceRv', 'g-accounts/positions.account', nonnegative=True),
        })
        balance, used = result['balance'].value, result['used'].value
        if balance is not None and used is not None:
            result['unallocated_wallet_balance'] = derived(balance - used, 'wallet balance - used; NOT available margin')
        positions = self.report.positions
        if (balance is not None and self.report.positions_complete
                and all(p['contracts'].value is not None
                        and (p['contracts'].value == 0 or p['unrealized_pnl'].value is not None) for p in positions)):
            pnl = sum((p['unrealized_pnl'].value for p in positions if p['contracts'].value), Decimal(0))
            result['equity'] = derived(balance + pnl, 'classic USDT balance + all reported open-position uPnL; estimate')
        # Risk-unit equity and estimated available margin stay scoped to each unit.
        return result

    def instrument(self, raw):
        if not isinstance(raw, str):
            return None
        matches = [m for m in self.ex.markets.values()
                   if raw in (m.get('id'), m.get('symbol')) and m.get('swap') is True
                   and m.get('linear') is True and m.get('settle') == 'USDT']
        return matches[0] if len(matches) == 1 else None

    def position(self, raw):
        row = raw if isinstance(raw, dict) else {}
        source = 'g-accounts/positions.position'
        market = self.instrument(row.get('symbol'))
        symbol = reported(market['symbol'], source + '.symbol') if market else unknown('unsupported/missing instrument')
        size = aliases(row, ('sizeRq', 'size'), source, nonnegative=True)
        if not isinstance(raw, dict) or row.get('currency') not in (None, 'USDT') or market is None:
            self.report.positions_complete = False
            self.issue('account_positions: invalid or unsupported position scope')
        if row.get('currency') not in (None, 'USDT'):
            size = unknown('settlement currency mismatch', source)
        if size.value is None:
            self.report.positions_complete = False
            self.issue('account_positions: position size unknown')
        pos_side = choice(row, 'posSide', {'Long': 'Long', 'Short': 'Short', 'Merged': 'Merged'}, source)
        mode = choice(row, 'posMode', {'Hedged': 'hedge', 'OneWay': 'one_way'}, source)
        if mode.value and pos_side.value and ((mode.value == 'one_way') != (pos_side.value == 'Merged')):
            mode = unknown('inconsistent position mode and side', source)
            self.issue('account_positions: inconsistent position mode and side')
        direction = choice(row, 'side', {'Buy': 'long', 'Sell': 'short', 'None': 'flat'}, source)
        if size.value and (direction.value == 'flat' or (pos_side.value in ('Long', 'Short')
                          and direction.value and direction.value != pos_side.value.lower())):
            direction = unknown('inconsistent size/direction/position side', source)
            self.issue('account_positions: inconsistent direction')
        if size.value != 0 and any(f.value is None for f in (pos_side, mode, direction)):
            self.report.positions_complete = False
            self.issue('account_positions: open-position identity or mode is unverified')
        leverage = number(row, 'leverageRr', source)
        sign_mode = None if leverage.value in (None, 0) else ('cross' if leverage.value < 0 else 'isolated')
        flag = row.get('crossMargin')
        if flag is not None and not isinstance(flag, bool):
            margin = unknown('invalid crossMargin flag', source)
        elif flag is not None and sign_mode and (flag != (sign_mode == 'cross')):
            margin = unknown('inconsistent leverage sign and crossMargin', source)
            self.issue('account_positions: conflicting margin-mode evidence')
        elif flag is not None:
            margin = reported('cross' if flag else 'isolated', source + '.crossMargin')
        elif sign_mode:
            margin = derived(sign_mode, source + '.leverageRr sign (documented)')
        else:
            margin = unknown('no reliable margin-mode evidence', source)
        fields = {
            'symbol': symbol, 'position_side': pos_side, 'position_mode': mode,
            'margin_mode': margin, 'contracts': size, 'direction': direction,
            'configured_leverage': (derived(abs(leverage.value), leverage.source + ' absolute value')
                                    if leverage.value not in (None, 0) else unknown('configured leverage unavailable')),
        }
        # Phemex sizeRq is base quantity; pinned CCXT USDT contracts have size 1.
        # Record size as contracts only when that mapping is established.
        contract = contract_size_fact(market or {})
        fields['base_quantity'] = size
        if size.value is not None and contract.value is not None:
            fields['contracts'] = derived(size.value / contract.value, 'sizeRq / CCXT contractSize')
        elif size.value is not None:
            fields['contracts'] = unknown('contract size unavailable')
            self.report.positions_complete = False
        for key, native, nonnegative in (
            ('entry_price', 'avgEntryPriceRp', True), ('mark_price', 'markPriceRp', True),
            ('liquidation_price', 'liquidationPriceRp', True), ('unrealized_pnl', 'unRealisedPnlRv', False),
            ('realized_pnl_current_term', 'curTermRealisedPnlRv', False),
            ('position_margin', 'positionMarginRv', True), ('assigned_balance', 'assignedPosBalanceRv', True),
            ('used_balance', 'usedBalanceRv', True), ('notional', 'valueRv', True),
            ('maintenance_margin_rate', 'maintMarginReqRr', True), ('risk_limit', 'riskLimitRv', True),
        ):
            fields[key] = number(row, native, source, nonnegative=nonnegative)
        fields['status'] = choice(row, 'positionStatus',
            {'Normal': 'normal', 'Liq': 'liquidation', 'Adl': 'adl'}, source)
        return fields

    def validate_positions(self):
        groups = {}
        identities = set()
        for p in self.report.positions:
            symbol, side = p['symbol'].value, p['position_side'].value
            if symbol is not None and side is not None:
                identity = symbol, side
                if identity in identities:
                    self.report.positions_complete = False
                    self.issue('account_positions: duplicate position rows; totals unavailable')
                identities.add(identity)
                groups.setdefault(symbol, []).append(p)
        for positions in groups.values():
            modes = {p['position_mode'].value for p in positions if p['position_mode'].value is not None}
            if len(modes) > 1:
                self.report.positions_complete = False
                for p in positions:
                    p['position_mode'] = unknown('conflicting modes for the same instrument')
                self.issue('account_positions: conflicting modes within one instrument')

    def risk_currency(self, row):
        named = [row[k] for k in ('currency', 'valuationCurrency') if row.get(k) is not None]
        numeric = row.get('valuationCcy')
        if numeric is not None:
            currencies = getattr(self.ex, 'currencies', {}) or {}
            mapped = [code for code, c in currencies.items()
                      if type(numeric) in (str, int) and (c.get('info') or {}).get('code') is not None
                      and str((c.get('info') or {})['code']) == str(numeric)]
            if len(mapped) != 1:
                return unknown('valuation currency code is not mapped by market metadata')
            named += mapped
        if not named or len(set(str(n) for n in named)) != 1 or named[0] not in ('USDT', 'BTC', 'ETH', 'USDC', 'USD'):
            return unknown('missing, unsupported or inconsistent valuation currency')
        return reported(named[0], 'risk-unit currency')

    def risk_unit(self, raw, classic):
        row = raw if isinstance(raw, dict) else {}
        source = 'g-accounts/risk-unit' if classic else 'uta/risk-units'
        mode = choice(row, 'riskMode', {'CrossAsset': 'cross_scope', 'Classic': 'classic',
                                      'MultiAsset': 'unified_multi_asset', 'Isolated': 'isolated_scope'}, source)
        risk_type = choice(row, 'riskType', {'SingleAssetContractCross': 'classic_cross',
                           'MultiAssetContractCross': 'multi_asset_cross', 'Isolated': 'isolated'}, source)
        if classic and mode.value in ('cross_scope', 'isolated_scope'):
            risk_type = derived('classic_cross' if mode.value == 'cross_scope' else 'isolated', mode.source)
        symbol_market = self.instrument(row.get('symbol'))
        fields = {
            'currency': self.risk_currency(row), 'risk_mode': mode, 'risk_type': risk_type,
            'symbol': reported(symbol_market['symbol'], source + '.symbol') if symbol_market else unknown('cross scope or unsupported instrument'),
            'position_side': choice(row, 'posSide', {'Long': 'Long', 'Short': 'Short', 'Merged': 'Merged', 'Merge': 'Merged'}, source),
            'user_status': choice(row, 'userStatus', {'Normal': 'normal', 'Banned': 'banned'}, source),
        }
        for name, native, nonnegative in (
            ('balance', 'totalBalanceRv', False), ('equity', 'totalEquityRv', False),
            ('unrealized_pnl', 'totalPosUnpnlRv', False), ('debt', 'totalDebtRv', True),
            ('position_cost', 'totalPosCostRv', True), ('maintenance_margin', 'totalPosMMRv', True),
            ('order_used_balance', 'totalOrdUsedBalanceRv', True), ('margin_ratio', 'marginRatioRr', True),
        ):
            fields[name] = number(row, native, source, nonnegative=nonnegative)
        fields['fixed_used'] = aliases(row, ('fixedUsedRv', 'fixUsedRv'), source, nonnegative=True)
        fields['available_margin_estimate'] = (number(row, 'estAvailableBalanceRv', source) if classic else
                                               unknown('unified risk-unit endpoint does not document available margin'))
        if not isinstance(raw, dict):
            self.issue('risk_units: malformed row')
        if fields['currency'].value is None or risk_type.value is None:
            self.issue('risk_units: currency or scope unknown; totals must not be aggregated')
        return fields

    def validate_risk_units(self):
        account = self.report.account_type.value
        expected = {'classic': 'classic_cross', 'unified_multi_asset': 'multi_asset_cross'}.get(account)
        identities = set()
        for unit in self.report.risk_units:
            kind = unit['risk_type'].value
            mode = unit['risk_mode'].value
            if account and ((kind in ('classic_cross', 'multi_asset_cross') and kind != expected)
                            or (mode in ('classic', 'unified_multi_asset') and mode != account)):
                self.report.account_type = unknown('inconsistent accounting-mode responses')
                self.issue('risk_units: accounting mode changed or responses conflict')
            currency = unit['currency'].value
            # Cross symbol is a representative, not a separate balance scope.
            identity = (currency, kind, unit['symbol'].value if kind == 'isolated' else None,
                        unit['position_side'].value if kind == 'isolated' else None)
            if identity in identities:
                self.issue('risk_units: duplicate scope; do not aggregate')
            identities.add(identity)
