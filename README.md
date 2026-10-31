# Phemex trading CLI — safety foundation

This is the first implementation increment, **not a completed trading application**.
The supported integration is CCXT, limited to active USDT-settled linear perpetuals.
BTC, ETH, SOL and XRP are supported when the selected environment lists them with
valid instrument metadata; their current availability has not been checked.

**All exchange mutations are blocked in both environments.** There is no runtime
flag to enable them. The CLI currently offers read-only commands and dry previews.
This temporary restriction is necessary until account/mode discovery, position-aware
risk checks and durable operation reconciliation are implemented and tested.

## Setup

The version pins in [requirements.txt](requirements.txt) cover the installed runtime
dependency closure on **Linux / Python 3.10**. Direct dependencies are recorded in
[requirements.in](requirements.in). Other Python/platform targets require a separately
resolved and verified lock. Package hashes and a clean-environment reinstall have
not yet been verified; these are version pins, not a reproducible binary build.

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp test.env.example test.env
chmod 600 test.env
```

Edit the credential placeholders locally. Use a testnet key with minimal permissions;
withdrawal permissions are unnecessary. Never commit credential files. The existing
[.gitignore](.gitignore) excludes `*.env`, the virtual environment and trade logs.

```bash
.venv/bin/python trade_cli.py --help
.venv/bin/python trade_cli.py
```

[terminal_v2.py](terminal_v2.py) is now a compatibility entry point to the same CLI.
[phemex_client.py](phemex_client.py) is retained as a native reference client; its
mutations are also blocked. It is not a second supported trading implementation.

## Configuration

- Default profile: `test.env` beside [config.py](config.py), independent of the
  current working directory. Live uses `live.env`; `--env-file PATH` overrides the
  selected path explicitly.
- Profile settings load before limit validation. Exported environment values take
  precedence. An exported credential pair must contain **both** key and secret;
  credentials are never silently assembled from separate sources.
- Configuration loading does not modify process environment variables or interpolate
  secrets. POSIX credential files must have owner-only permissions.
- `MAX_NOTIONAL_USDT` (default `200`) and `MAX_LEVERAGE` (default `5`) must be finite
  positive numbers. These are preview limits, not aggregate account risk controls.
- Live connectivity requires `--live`, `PHEMEX_ALLOW_LIVE=true` and an interactive
  `LIVE` confirmation **before** client creation. It remains read-only. Setting the
  variable alone does not select live endpoints.
- [live.env.example](live.env.example) provides a separate opt-in template. Choose
  credentials for the selected endpoint; the program cannot determine which
  environment issued an arbitrary key before authenticating.

## Commands

```text
balance
positions [symbol=BTCUSDT]
info symbol=BTCUSDT
orders symbol=BTCUSDT
buy symbol=BTCUSDT usdt=100 price=64000 sl=60000 tp=70000 dry
sell symbol=ETHUSDT qty=0.02 dry
amend symbol=BTCUSDT ord_id=EXCHANGE_ID qty=0.002 dry
cancel symbol=BTCUSDT cl_id=CLIENT_ID dry
cancelall symbol=BTCUSDT dry
close symbol=BTCUSDT [position=Long|Short] dry
leverage symbol=BTCUSDT x=2 dry
help
exit
```

Use exactly one of `qty` and `usdt` for a new order. `qty` is base-asset quantity,
converted through contract size; amendment `qty` is the new total base quantity,
including any fills. Prices and quantities normalize using CCXT instrument rules
before validation and preview. A price implies a limit order; market orders cannot
include a price. New-order previews describe opening intent; reductions must use
`close`. `Merged` entry previews are unavailable pending account-mode discovery.

`close` currently previews a **full** reduce-only close using the position's explicit
exchange side/mode. Partial-close quantities are rejected rather than ignored.
Unsupported commands, unknown/duplicate parameters, unknown/repeated flags, empty
values and non-finite/non-positive numeric input are rejected. Only one order ID
may be supplied. Every mutation command requires `dry` in this increment.

Previews are not reserved orders or a statement that execution is safe. Quotes can
be stale, market orders can slip, and account modes/exposure/protection are not fully
verified. Generated UUID client IDs avoid timestamp collisions but do **not** yet
provide durable duplicate prevention or restart recovery.

Raw exchange exceptions, responses, commands and tracebacks are not logged by the
CLI. Exchange failures display the exception class and unverified-state warning;
there are no automatic retries. Do not enable CCXT verbose request logging with
real credentials. Persistent audit logging will be implemented with reconciliation.

## Tests and verified scope

```bash
.venv/bin/python -B -m unittest discover -v
.venv/bin/python -m pip check
```

The suite blocks HTTP/socket connections, uses synthetic credentials and instrument
fixtures, and exercises the installed CCXT precision helpers. No Phemex account,
order, fill, cancellation, setting or protection lifecycle has been verified.
A passing suite does not establish production readiness.

| Review finding | Foundation treatment |
| --- | --- |
| Config limits loaded before the profile | Fixed with isolated configuration loading and validation |
| Unknown, duplicate or ignored CLI input | Rejected by command schemas; partial-close input is rejected |
| Quantity-only amendment bypasses cap | Complete resulting notional is checked; filled quantity is checked |
| SL/TP validation precedes rounding | Normalization precedes validation and display; ordinary amendment attachments checked |
| Spot/inverse symbols enter USDT-only path | Unsupported, inactive, ambiguous and invalid-contract markets rejected |
| Fixed precision and float conversion | Instrument precision helpers receive decimal strings; contract size included |
| Position side guessed from trade direction | Missing/invalid exchange side is rejected; close preview preserves Merged |
| Timestamp client-ID collisions | UUID-based proposed IDs; durable reservation remains pending |
| Dry leverage unexpectedly mutates | Dry preview only; CLI and adapter mutation barriers tested |
| Incomplete cancel-all, unsafe leverage/margin assumptions | Mutations blocked, not implemented as working lifecycle operations |
| Unknown submission outcome, aggregate exposure, SL/TP coverage | Pending; mutation barrier prevents use of incomplete execution paths |
| Native client no timeout/API-error check or correct USDT balance query | Read timeout/error handling and balance query fixed; legacy writes blocked |
| Old terminal starts on import / API error appears as empty orders | Old terminal consolidated; imports inert; exchange errors reported as unverified |

## Next increment (requires approval)

Start with authenticated **read-only** discovery of classic versus unified/multi-asset
accounting, actual position/margin modes, equity and available margin. Add sanitized
raw-response fixtures and verify interpretation before building the durable intent
journal and reconciliation state machine. Unsupported/ambiguous account states must
block trading; the application must never change settings to match assumptions.

Subsequent work must cover position-aware exposure, full/partial reduce-only
execution, protection lifecycle, cancellation categories, deliberate settings
changes, history/PnL and restart recovery. Testnet order testing requires a separately
reviewed increment. Live mutations, fund transfers and withdrawals require explicit
authorization for the specific operation.
