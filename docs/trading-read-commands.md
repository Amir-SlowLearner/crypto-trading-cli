# Trading-readiness commands (Testnet, read only)

The existing terminal now accepts:

```text
price BTCUSDT
balance
positions BTCUSDT
orders BTCUSDT
order-status EXCHANGE_ORDER_ID symbol=BTCUSDT
order-status EXCHANGE_ORDER_ID
```

The last form requires a previous successful `orders` or `order-status` read in
the same session to bind that exact exchange ID to a symbol. Otherwise it rejects
before any private request. There is no default BTC symbol for an unknown ID.
Existing `symbol=BTCUSDT` syntax remains accepted. Client IDs are not accepted
as exchange order IDs.

After separate operator authorization for real Testnet reads, the terminal entry
point is `.venv/bin/python -B trade_cli.py --env-file YOUR_TESTNET_PROFILE.env`.
Do not run it merely because mocked tests pass. This increment performs no real
requests and verifies no additional Phemex behavior.

## Request contract

The read terminal uses the pinned CCXT 4.5.90 signer/parser. Session startup reads
`GET /public/products` and `GET /v1/exchange/public/products` once, validates them
with the existing public catalog inspector, and registers supported markets.
It does not use `load_markets()` or implicitly fetch currencies. Reviewed currency
codes are retained from the same public inventory. Metadata is a session snapshot;
restart explicitly to refresh it. No background refresh, retry or fallback exists.

- `price`: one `GET /md/v2/orderbook?symbol=NATIVE`, showing bid, ask, exchange
  timestamp when supplied and local UTC collection time. Missing/off-grid/crossed
  quotes remain UNKNOWN. A quote is an observation, not a guaranteed fill price.
- `positions SYMBOL`: one `GET /g-accounts/positions?currency=USDT`, retaining both
  explicit Hedge Mode legs, including explicit zero quantities. Missing, duplicate,
  one-way, mismatched, malformed or paginated evidence remains UNKNOWN. Entry price
  for an absent/unverified/zero position is UNKNOWN. Margin and leverage use the
  existing conservative raw-field interpreter. No risk endpoint is needed here.
- `balance`: existing bounded account discovery, including the risk-mode/risk-unit
  probes and conditional accounting-specific reads documented in
  [account discovery](account-discovery.md). It retains provenance and per-risk-unit
  scope. Failed accounting discovery never defaults to Classic. An estimated
  risk-unit balance is not account-wide available margin.
- `orders SYMBOL`: a fresh USDT positions-frame account-scope read, then
  `GET /g-orders/activeList?symbol=NATIVE` only if account/user identity is usable.
- `order-status ID symbol=SYMBOL`: the same fresh scope read, then
  `GET /api-data/g-futures/orders/by-order-id?symbol=NATIVE&orderID=ID`.
  This is the one additional GET route in the dedicated read-terminal subclass,
  derived from the installed CCXT `fetch_order()` mapping. Its account-specific
  response/permission compatibility is **not yet verified against Testnet**.
  Method and query fields are fixed; mutations still hit the original GET guard.

The new price/scoped-position/order commands require Testnet sandbox configuration
and HTTPS Testnet API origins. Order details require exact native symbol, exact
exchange ID, unique rows and a matching explicit account ID; supplied user IDs
must agree as well. A user ID alone is not a subaccount binding. Identifiers used for
account binding are not printed or retained in reports. Missing fields, malformed
envelopes and inaccessible endpoints produce UNKNOWN, never fabricated defaults.
An omitted order is not proof of rejection, cancellation or zero execution.
Conditional-order coverage and complete history remain UNKNOWN. An order status
is an observation, not fill reconciliation or journal resolution.

The underlying CCXT client decodes JSON decimal literals as Decimal and rejects
duplicate keys. Request-history retention is disabled. Raw upstream exceptions
are not printed by the terminal. These are read-command restrictions, not an OS
sandbox or a new production deployment boundary.

The existing manual three-request status command, experimental four-request
collector, protected T1C bootstrap, offline HTTP block and trading mutation
barriers are unchanged. No command here submits, cancels, amends, changes leverage
or settings, or accesses operation journals. Public catalog support does not
establish order acceptance. Unknown essential fields still block future trading.

## Offline checks

```sh
.venv/bin/python -B -m unittest tests.test_trading_reads tests.test_cli -q
.venv/bin/python -B -m unittest discover -s tests -q
```

Tests use synthetic responses, mocked transport and prohibited-call assertions.
No credentials or exchange requests are needed. Real Testnet verification requires
separate approval, especially for the new exact-ID route and account-identity
fields. The currently observed missing ETHUSDT/XRPUSDT position rows remain UNKNOWN.
