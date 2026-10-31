# Read-only account discovery (increment two)

All mutations remain disabled. This increment inspects account state; it does not
establish that orders, protection or recovery are safe. No authenticated integration
requests have been executed during development.

## Commands

- `account`: fresh authentication/accounting probe, USDT wallet, separate risk units
  and nonzero/unknown-size positions.
- `balance`: the same discovery, focused on wallet and risk-unit figures.
- `positions [symbol=BTCUSDT]`: position details with source and derivation labels.
- `modes [symbol=BTCUSDT]`: mode/leverage per returned instrument and leg, including
  zero-size configuration rows. No rows means unknown configuration, not one-way.
- `info symbol=BTCUSDT`: cached public instrument specifications and restrictions,
  including inactive/delisted instruments. This command makes no private request.

All private views make fresh bounded reads; there is no account-state cache or
background polling. Reads are sequential and not an atomic exchange-wide snapshot.
The output includes the local start/end times. A mode change or fill between reads
can affect consistency. Detected conflicts are reported, not resolved by guessing.

`Minimum discovery checks passed` checks supported accounting, authenticated access,
position completeness/identity/mode/margin fields, wallet balance and scoped risk-unit
equity. It does **not** mean all fields are present, current or suitable for trading.
Unknown fields and all issues must still be inspected. Future execution must recheck
state rather than treating this flag as authorization.

## Why raw Phemex endpoint extensions are needed

The integration still uses the pinned CCXT client for metadata, authentication,
signing, timeouts, rate limiting and error classes. There is no additional HTTP
transport. Fixed implicit GET methods expose raw fields that the unified interface
cannot reliably represent in CCXT 4.5.90:

| Endpoint through CCXT | Reason / semantics |
| --- | --- |
| `GET /uta-api/risk/risk-mode` | No unified account-accounting discovery method. Only documented `Classic` and `MultiAsset` establish the accounting type. |
| `GET /g-accounts/positions?currency=USDT` | One raw frame retains account and position fields, reported uPnL, flat configuration rows and explicit mode evidence. Normalized `parse_position` defaults missing `crossMargin` to isolated and drops the sign of `leverageRr`. |
| `GET /g-accounts/risk-unit` (Classic) | Scoped risk-unit equity and explicitly estimated available balance are absent from the unified balance view. `CrossAsset` here names a cross-margin scope; it does not establish MultiAsset accounting. |
| `GET /uta-api/risk/risk-units?currency=USDT` (MultiAsset or unknown mode) | Provides typed cross/isolated scopes, equity, costs and debt. It does not document estimated available margin. It must not be used to guess a failed account-type probe. |
| `GET /uta-biz/assets?currency=USDT` (MultiAsset only) | Explicit wallet balance, borrowed balance, interest and collateral encumbrance. The legacy account balance is not substituted for this wallet. |

The production `ReadOnlyPhemex` subclass allows only reviewed GET routes, including
metadata, USDT order book and open orders. Every other route/method is blocked
before signing/transport. The existing application-level mutation barrier remains
in place too. No discovery path changes margin, leverage or position modes.

Authentication success means at least one valid success envelope from an
authenticated endpoint; it does not prove all permissions or all account fields.
Authentication rejection, network failures and rate limits stop further requests.
Server errors normally stop the snapshot as well. The single exception is an
otherwise unclassified HTTP 500 from the fixed risk-mode GET: discovery retains
the failure and proceeds with the independent position/risk-unit GETs. Each GET
has one attempt, including when CCXT retry options are configured elsewhere.
HTTP redirects are rejected before a second request can reach an unreviewed route.
There is no retry of the failed risk-mode probe. Recognized authentication,
permission, rate-limit or request-validation errors, conflicting error evidence,
non-JSON failures and other 5xx responses do not qualify for this exception.
The shared server-error stop rule and all mutation barriers remain in place.
Endpoint permission/unsupported errors otherwise keep their existing handling:
independent reads may proceed, but the rejected capability remains unknown. There
is no fallback from an unavailable risk-mode endpoint to Classic accounting.

## Field provenance and scope

`reported` identifies a returned API field or explicitly named normalized metadata.
`derived` identifies an arithmetic calculation, documented sign interpretation or
CCXT amount convention. `unknown` carries a static explanation without reflecting
untrusted response values. Raw private responses, credentials and account identifiers
are not retained in the report or written to fixtures/logs.

| Field | Treatment |
| --- | --- |
| Accounting type | Reported `riskMode`: Classic → classic, MultiAsset → unified multi-asset. This describes API accounting/risk mode, not an inferred account-migration/UI status. |
| Position mode | Reported `posMode` on each returned symbol/leg; no propagation to unreturned symbols. Conflicting modes for one symbol are unknown. |
| Margin mode | Reported boolean `crossMargin` when supplied. Otherwise derived from documented negative/positive `leverageRr`. Contradiction → unknown; missing evidence → unknown. Zero leverage alone does not determine USDT margin mode. |
| Configured leverage | Derived absolute signed leverage. Zero/missing/invalid → unknown. Not effective leverage or a maximum permitted by the current risk tier. |
| Position quantity | Reported real base size (`sizeRq`, or matching `size` alias); contracts derived using CCXT contract size. Missing/invalid/conflicting size or duplicate position identity prevents aggregate equity estimates. |
| USDT contract size | Pinned CCXT uses a unit amount convention for USDT contracts. This is labeled derived unless consistent native numeric contract-size metadata is present. Conflicting metadata → unknown. |
| Entry/mark/liquidation price, position margins, reported uPnL | Reported real-value fields when present and valid. No fallback to an independently fetched market price. Price freshness is not established. |
| Realized PnL | `curTermRealisedPnlRv` is explicitly current-term realized PnL, not lifetime/account-wide PnL. |
| Classic wallet balance/used/bonus | Reported from the account portion of the USDT positions frame. No currency mismatch is relabeled as USDT. |
| Classic equity estimate | Derived wallet balance + reported uPnL of every nonzero position in the complete frame. Missing uPnL/size, duplicate rows or pagination prevents the estimate. It is not an authoritative spendable-equity figure. |
| Unallocated Classic wallet balance | Derived balance − used. Not called available margin; negative results are not clamped. |
| MultiAsset USDT wallet | Reported asset balance, bonus, borrowed amount, interest and frozen collateral. Wallet balance is not the same as collateral-adjusted equity. |
| Risk-unit equity, balances, debt, maintenance margin, costs, ratio | Reported separately by valuation currency and risk scope. Cross symbol is a representative, not a separate wallet. No account grand total is created by adding these rows. |
| Classic available-margin estimate | Reported `estAvailableBalanceRv`, labeled estimated and limited to its individual risk unit. No account-wide spendable balance is inferred. |
| MultiAsset available margin / account-wide equity | Unknown when no documented authoritative field establishes the requested scope. No equity-minus-cost or balance-minus-used approximation is presented as available margin. |

Zero is preserved as a value and is distinct from missing. Invalid numbers,
non-finite values, inappropriate negative quantities, currency mismatches and
conflicting aliases become unknown. Valid PnL, wallet balance and estimated margin
may be negative. Isolated and cross risk units, and different valuation currencies,
are not aggregated because their scopes can overlap or represent correlated assets.
Numeric valuation currency codes are resolved only using CCXT currency metadata;
there is no hardcoded assumption that code 3 always means USDT.

## Instrument restrictions

CCXT market metadata is loaded at client construction and remains cached for the
session. `info` reports native fields retained in `market.info`, including status,
quantity step, price tick, maximum real order quantity, minimum order value, price
bounds and maximum leverage when available. Normalized precision is interpreted as
a tick only when CCXT's precision mode is `TICK_SIZE`.

The pinned adapter omits USDT `maxOrderQtyRq`/`minOrderValueRv` from normalized limits
and maps legacy `maxOrderQty` into `cost.max`, despite the quantity/notional unit
difference. Inspection and dry-preview bounds now use the retained native USDT
fields; the legacy cost mapping is explicitly unavailable. Native tick/step checks
reject a normalized preview that violates the native grid.

USDT tier tables and a notional-dependent maximum leverage are still unavailable.
A product's maximum leverage must not be interpreted as permission for that leverage
at every position size. Account-specific permissions, risk tier, restrictions and
future changes in cached metadata still require further verification.

## Offline and optional integration tests

```bash
.venv/bin/python -B -m unittest discover -v
.venv/bin/python -m pip check
```

The fixtures in [account_fixtures.py](../tests/account_fixtures.py) are synthetic,
shaped from documented response structures. They are not evidence of the responses
returned by your account. HTTP/socket calls are blocked in the offline suite.
Tests exercise the real pinned CCXT request/signing and market-loading paths using
mocked transport, not solely mocked application methods.

An optional integration entry point is provided:

```bash
.venv/bin/python testnet_readonly.py --authorize-testnet-read
```

Run that command only when you authorize authenticated GET-only inspection of the
selected testnet account. Without the explicit flag it exits before loading
credentials or constructing a client. `--env-file PATH` is supported; `--live` and
abbreviated options are rejected. Sandbox mode and testnet API hosts are checked
before authenticated discovery. No mutation is enabled and no raw response is saved.
Exit code 0 means minimum discovery checks passed; 2 means no authorization,
configuration failure, failed/partial discovery or interruption. Neither result
certifies trading safety. User-run testnet checks first established authentication
and BTCUSDT position discovery, while both UTA risk reads failed. A subsequent run
using status/code diagnostics reported HTTP 500/Phemex 10500/CCXT ExchangeError on
risk mode; the old server-error stop rule then skipped the independent reads.
The latest user-run check verified the narrow continuation behavior described
below. No authenticated requests were made by the development agent.

## Known testnet limitations and observed verification

These observations were supplied by the user from the selected testnet account;
they are not raw response fixtures or a guarantee for other accounts/environments.

| Read / behavior | Latest user-run result |
| --- | --- |
| `GET /uta-api/risk/risk-mode` | HTTP 500, Phemex code 10500, CCXT ExchangeError, fixed reason `retry_later`. Accounting type remains unknown. |
| `GET /uta-api/risk/risk-units?currency=USDT` | HTTP 500, Phemex code 10500, fixed reason `retry_later`. Risk-unit financial values remain unavailable. |
| Independent discovery after the risk-mode failure | Continued without retrying the failed probe; authentication and BTCUSDT position discovery succeeded. |
| BTCUSDT configuration | Hedge mode, cross margin, configured leverage 15x on both Long and Short legs; both reported sizes were zero. |
| Fresh configuration reads | The user changed leverage from 10x to 15x through the Phemex website between checks; the next CLI read reported 15x. This supports freshness for that setting and account, not all fields or symbols. |
| Overall result | Accounting type and account-wide financial values remained unknown; minimum discovery checks failed; all exchange mutations remained disabled. |

Both risk-endpoint failures are unresolved limitations observed on this testnet
account. Their root cause has **not** been established. These results do not prove
that the endpoints are unsupported, that the account is Classic, or that the same
behavior occurs on live Phemex. A successful position read does not establish UTA
permissions or complete account verification. No accounting fallback, invented
available-margin figure, mode change, or automatic retry is used to work around
the failures.

Classic and MultiAsset success-response interpretation is covered by synthetic
offline fixtures, not authenticated success checks for these risk endpoints.
Nonzero positions, fills, orders, closing, protection, history and exchange-side
setting changes through the CLI remain unverified; mutations remain blocked.

## Diagnosing failed reads safely

Account discovery displays a fixed failure category, HTTP status, numeric exchange
error code, allowlisted exception type and fixed reason label. Missing HTTP/code
evidence is `unknown`; an unrecognized message has reason `unclassified`.
HTTP evidence is captured before CCXT raises; it is reset before each request so
a later timeout or signing failure cannot inherit a previous response's status.
Only bounded numeric codes from `code` or `error.code` are retained. No exception
messages, request URLs, headers or private response bodies appear in diagnostics.
The adapter disables its own CCXT DEBUG/verbose logging and raw response caches.
Recognized phrases are converted internally to fixed labels such as
`api_key_unrecognized`, `signature_rejected`, `ip_restricted`, `missing_parameter`
and `retry_later`. Raw messages, including API keys appended to them, are never
retained in the diagnostic state or displayed. Only error envelopes are inspected;
text inside successful account data is not used to classify failures.

HTTP 401, 403, 404, 405, 429 and server errors are distinguished from API errors
returned with HTTP 200, transport failures and local failures. A 404 reports
`http_not_found`; it does not prove an authenticated endpoint is unsupported.
Only an explicit CCXT `NotSupported` error is labeled unsupported by the adapter.
An unmapped exchange code remains an exchange error without guessing its meaning.
Explicit authentication/permission/rate-limit evidence takes precedence over a
generic HTTP 500; all 5xx still stop shared handling. The risk-mode continuation
exception exists only immediately after that fixed GET inside account discovery,
and only for a generic CCXT ExchangeError with an unclassified or `retry_later`
reason. A successful later position response can establish authenticated access
to that read. It does not establish UTA permissions, accounting type or complete
account verification. All issues are retained and minimum checks remain failed.

Phemex 10500 has no unique documented meaning in the published error table. The
pinned CCXT adapter associates it with missing keys, signature failures and missing
parameters according to message text. A reported generic backend failure also
used this code. It must not be hardcoded as Classic, unsupported or an authentication
failure. The exact testnet cause and account-specific endpoint support remain unknown.

Pinned CCXT 4.5.90 maps the documented UTA
paths directly to the testnet private host, using the same GET HMAC signing path
as the successful position read. Risk mode has no parameters; `currency=USDT` is
valid for risk units and `riskType` is optional. Offline tests verify the exact
URL, query and signature. No endpoint or parameter defect has been established.

Unauthenticated GET probes returned HTTP 401/code 401 for both UTA risk paths and
the position path. This establishes gateway reachability only; it does not prove
authenticated route availability, account eligibility or permissions. Further
evidence requires a separately authorized testnet read or exchange confirmation.
Do not enable raw logging. No Classic fallback or alternate-host/endpoint attempt
is made automatically.

## Source documentation checked 2026-10-31

- [Risk/accounting mode](https://phemex-docs.github.io/#query-risk-mode)
- [CCXT report of generic Phemex 10500](https://github.com/ccxt/ccxt/issues/25506)
- [USDT account positions with reported uPnL and leverage sign](https://phemex-docs.github.io/#query-account-positions-with-unrealized-pnl)
- [Classic risk-unit response](https://phemex-docs.github.io/#query-risk-unit)
- [Unified asset response](https://phemex-docs.github.io/#query-asset)
- [Unified risk-unit response and scope definitions](https://phemex-docs.github.io/#query-risk-unit-2)
- [Risk-unit field semantics](https://phemex-docs.github.io/#fields-in-riskunit)
- [Public instrument metadata](https://phemex-docs.github.io/#query-product-information-plus)
- Pinned installed CCXT 4.5.90 Phemex adapter: `parse_position`, `parse_swap_balance`,
  `parse_swap_market`, implicit endpoint declarations and `sign`.
