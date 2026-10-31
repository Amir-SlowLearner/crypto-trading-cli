# Increment three: offline scenario preparation

All exchange mutations remain disabled. Preparation produces a local report, not
an executable order payload or proof of exchange acceptance. It neither reads nor
changes an account. The risk-mode and risk-unit endpoints are not prerequisites.
Their [known testnet failures](account-discovery.md#known-testnet-limitations-and-observed-verification)
and conservative discovery handling remain unchanged.

## Inputs and intent

`buy`, `sell` and the `close` alias require `dry`, `symbol`, `action`,
`position_side` and `current_qty`. Every quantity is in base-asset units.
`position_side=long|short` explicitly selects the hypothetical Hedge Mode leg.
It is not a claim about actual exchange position mode. The old `position=` spelling
is rejected for preparation, and missing position context is never inferred.

| Action | Supplied current size | Direction for Long | Direction for Short | Quantity rule | Reduce-only |
| --- | --- | --- | --- | --- | --- |
| `open` | Exactly zero | Buy | Sell | Positive | False or omitted |
| `increase` | Positive | Buy | Sell | Positive | False or omitted |
| `reduce` | Positive | Sell | Buy | Strictly less than current size | Explicit `reduce_only=true` |
| `close` | Positive | Sell | Buy | Exactly equal to current size | Explicit `reduce_only=true` |

`current_qty` is **user-supplied and unverified**. It describes only the selected
leg in a scenario, never an authenticated position. Preparation rejects off-grid
position context instead of altering it. Projected remaining size is arithmetic
under that scenario; outstanding orders, fills and competing operations are unknown.
The close alias requires `action=close` and derives direction from that explicit
action/leg. It no longer performs an authenticated position lookup.

Use `qty=` for base quantity. Opening and increasing can alternatively use `usdt=`
as a notional budget. Reductions and full closes require `qty=` because an estimated
notional conversion must not decide how much of a position is closed.

Use `type=limit price=...` or `type=market reference_price=...`. A supplied price
implies limit; omitting it implies market. `reference_price` is a user-supplied,
unverified market estimate, never an actual quote fetched by preparation. It is
required for market notional/minimum-value/cap checks and does not bound slippage.
Limit notional uses the supplied limit price; fills and fees remain unverified.

## Specification and precision checks

The pure preparer reuses strict command parsing, instrument inspection, explicit
field provenance and configured preview limits. It has no transport or credential
dependency. CLI preparation reads only cached specifications already in memory.

Required fields are active listing evidence, matching USDT linear perpetual identity,
contract size, native/fallback base quantity step, CCXT contract precision tick,
minimum/maximum contract quantity and minimum notional. Limit/TP/SL prices also
require native/fallback price tick and CCXT price precision tick. Missing, invalid,
conflicting or unsupported required fields reject the affected preparation.

Prices are never rounded. Each supplied order/TP/SL price must satisfy both price
grids and any available price bounds. The existing amendment price helper also
rejects rounding. Quantities may normalize **down** to the base step, with the
requested and normalized sizes shown and a warning when they differ. The resulting
contract quantity must independently satisfy CCXT precision and instrument bounds.
No quantity can round up. Full closes cannot leave a normalization remainder.

Notional conversions use exact rational division before step truncation. Decimal
arithmetic rejects loss of precision; input/specification values beyond 40 significant
digits or an absolute adjusted exponent of 40 are unsupported. No float round-trip
is used to parse cache decimals or normalize order values.

Optional maximum notional and price bounds are checked when available. Unknown
optional checks are disclosed; invalid explicitly supplied bounds reject validation.
The existing `MAX_NOTIONAL_USDT` per-order preview cap remains in effect, including
for reductions. It is not a position-exposure, available-margin or buying-power check.
The file-backed offline mode uses the default 200 USDT cap without loading an
environment profile. Interactive previews use the configured client's cap.

## TP/SL and Hedge Mode representation

Attached TP and/or SL trigger previews support **open only**. Supply `sl=` and/or
`tp=`, with a common `trigger=mark|last` and an unverified `trigger_price=` for that
source. Long SL is below both entry/reference and trigger reference, with TP above
both; Short reverses these directions. Equality or an already-crossed supplied
trigger reference is rejected. Prices must be on-grid; nothing is rounded.

These settings correspond to documented Phemex `stopLossRp`, `takeProfitRp`,
`slTrigger`/`tpTrigger` and `ByMarkPrice`/`ByLastPrice` fields. TP/SL for increase,
reduce or close is rejected because existing protection and overwrite effects are
unknown. Separate trigger sources, child limit prices, trailing stops, conditional
entry orders, bracket lifecycle and protection modification are not implemented.

Phemex documents explicit `posSide=Long|Short` and `reduceOnly=true`. A synthetic,
mocked-transport test checks that pinned CCXT preserves these fields when an
explicit leg is supplied. No production mutation barrier is bypassed. Account-specific
Hedge Mode acceptance, reduce-only enforcement, fills, protective-order activation
and liquidation constraints remain **unverified**. No preview is executable.

Sources: [Phemex USDT order parameters](https://phemex-docs.github.io/#place-order-http-put-prefered-2)
and the installed CCXT 4.5.90 Phemex adapter's explicit `posSide` branch.

## Credential-free manual verification

Run from the project directory. These commands use the deliberately **synthetic**
[example cache](../examples/order-markets.synthetic.json), which is not a statement
of actual Phemex instrument specifications. They make no network requests, load no
credentials and need no environment file.

```bash
.venv/bin/python -B trade_cli.py --market-cache examples/order-markets.synthetic.json --preview 'buy symbol=BTCUSDT action=open position_side=long current_qty=0 qty=0.002 price=60000 dry'

.venv/bin/python -B trade_cli.py --market-cache examples/order-markets.synthetic.json --preview 'sell symbol=BTCUSDT action=open position_side=short current_qty=0 qty=0.002 price=60000 sl=61000 tp=59000 trigger=mark trigger_price=60000 dry'

.venv/bin/python -B trade_cli.py --market-cache examples/order-markets.synthetic.json --preview 'buy symbol=BTCUSDT action=increase position_side=long current_qty=0.002 qty=0.001 type=market reference_price=60000 dry'

.venv/bin/python -B trade_cli.py --market-cache examples/order-markets.synthetic.json --preview 'sell symbol=BTCUSDT action=reduce position_side=long current_qty=0.002 qty=0.001 reference_price=60000 reduce_only=true dry'

.venv/bin/python -B trade_cli.py --market-cache examples/order-markets.synthetic.json --preview 'buy symbol=BTCUSDT action=close position_side=short current_qty=0.002 qty=0.002 reference_price=60000 reduce_only=true dry'
```

Successful local preparation exits 0 and prominently prints that no order was
submitted, actual account verification was not performed, and execution is disabled.
Exit 0 is not complete account verification or trading authorization. Invalid
scenarios exit 2. For example, this off-grid price must fail:

```bash
.venv/bin/python -B trade_cli.py --market-cache examples/order-markets.synthetic.json --preview 'buy symbol=BTCUSDT action=open position_side=long current_qty=0 qty=0.002 price=60000.05 dry'
```

The cache schema requires `version: 1`, `precision_mode: "tick_size"`,
`provenance: "synthetic"` or `"user_supplied_public_snapshot"`, and a nonempty
`markets` array of CCXT normalized public market records. Duplicate identities are
rejected. Raw `/public/products` responses are not that normalized format. This
increment does not add automatic metadata refresh/export or snapshot authentication;
a user-supplied public snapshot's provenance and freshness remain unverified.
Never put credentials or private account responses in a market cache.

`--preview` accepts only dry buy/sell/close scenarios. Combining it with `--live`,
`--env-file`, or private/read-only inspection commands is rejected before credentials
are loaded. Existing account inspection commands and their authorization gates
remain separate. Legacy amend/cancel inspection previews still explicitly read
open orders; they are not available through the offline preparation entry point.

## Verification and remaining limits

The complete offline suite blocks sockets/HTTP. Synthetic fixtures cover direction,
leg/action/context requirements, quantity and price boundaries, reduce-only intent,
TP/SL, missing/ambiguous metadata, cache loading and mutation barriers. Nothing in
this increment has been tested using authenticated exchange requests or orders.

One-way mode, live/testnet execution, financial risk approval, slippage controls,
position/order reconciliation, durable client-ID reservation, fees, liquidation
checks, settings changes and protection lifecycle remain outside this increment.
Unknown accounting remains unknown. Increment four requires separate approval.
