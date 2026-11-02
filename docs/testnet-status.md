# T1B — offline Testnet status interface

This checkpoint sends **no HTTP requests**, loads no credentials and constructs
no exchange client or Requests session. Every response and timestamp in the demo
is invented. It does not activate the worker, IPC, approvals, journals or deployment.
The existing offline transport's real-send block remains unconditional.

## Command

From the repository root:

```bash
.venv/bin/python -B testnet_status.py --help
.venv/bin/python -B testnet_status.py status --help
.venv/bin/python -B testnet_status.py status --mock-demo
```

`status --mock-demo` is the only collection mode. There is no live/testnet network
flag, credential/profile option, URL, HTTP method, fixture-file loader or arbitrary
CCXT command. Missing/unknown/abbreviated options fail with process exit code 2.
Help exits 0 before importing the collection implementation. Importing either new
module constructs no client/session and reads no credential file or environment.

Exit codes:

- `0`: help, or all scoped mock status checks completed.
- `2`: invalid command, partial/failed mock status or sanitized internal failure.
- `130`: interrupted mock collection.

Zero means neither exchange authentication nor full account verification. A flat
Short or Long leg does not prevent successful read-only status inspection.

## Public Python contract

[testnet_status_collection.py](../testnet_status_collection.py) exposes:

- `MockHttpResponse`: immutable label, raw JSON bytes, exact response URL, integer
  HTTP status, redirect indicator and optional fixed mock failure category.
- `MockResponseProvider(tuple_of_responses)`: a closed in-memory transcript. No
  callback, session, client or real backend is accepted; subclasses are refused
  by collection. A provider can be collected once, including failed collections.
- `collect_status(provider, clock=...)`: fixed four-request mock orchestration.
  The optional clock supplies aware timestamps; tests/demo use invented clocks.
- `StatusReport`: immutable tuples of endpoint observations and requirements,
  collection times, completeness properties and `render()` for terminal output.
- `mock_demo()`: returns a new invented provider and deterministic fixture clock.

Example:

```python
from testnet_status_collection import collect_status, mock_demo

provider, clock = mock_demo()
report = collect_status(provider, clock=clock)
print(report.render())
assert report.synthetic
assert not report.authenticated_evidence_verified
assert not report.execution_authorized
assert not report.may_resolve_journal
```

The mocked transcript follows the existing plan exactly:

| Order | Method | URL |
| --- | --- | --- |
| 1 | GET | `https://testnet-api.phemex.com/public/products` |
| 2 | GET | `https://testnet-api.phemex.com/v1/exchange/public/products` |
| 3 | GET | `https://testnet-api.phemex.com/g-accounts/positions?currency=USDT` |
| 4 | GET | `https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT` |

Provider access is ordered and bounded. An attempted slot is consumed before
reading its mock response; missing responses, failures and malformed bodies do
not permit retry. A failure stops the remaining plan. Response URLs must match
the canonical plan, and redirects reject. Responses are bounded to 4 MiB each.
The mock provider's per-object counters are **not** durable independent approval
or security against Python method replacement, object copying or process restart.
No actual request budget or authenticated approval is conferred by this API.

## Interpretation and exact numbers

The collector reuses the existing pure instrument inspector on an in-memory
native metadata view. It supplies no CCXT contract-size default or minimum size.
Both public envelopes must be usable; conflicting overlapping companion product
fields, ambiguous BTCUSDT products and invalid market scope stop private collection.
An absent/invalid individual specification remains UNKNOWN rather than receiving
a guessed default. This is not independent CCXT normalization verification: the
view carries explicit fixture-native values, not markets fetched by a CCXT client.

The narrow `interpret_mock_status` helper in
[testnet_compatibility.py](../testnet_compatibility.py) reuses the existing identity,
position-scope, per-field crosscheck, freshness and requirement interpretation.
It still returns a synthetic-only compatibility report. The original verifier,
its restricted signer/final transport and all synthetic-only report restrictions
are preserved; neither new code path invokes the original exchange constructor.

Raw response bytes use the shared duplicate-rejecting JSON decoder with
`exact_numbers=True`. JSON fractional/exponent literals become `Decimal` directly,
before any binary float conversion. Integer envelope codes remain integers;
booleans and decimal/exponent forms are not accepted as integer status codes.
Nonstandard NaN/Infinity literals, malformed UTF-8 and duplicate names at every
nesting level (including identical/escaped duplicate names) reject. The existing
decoder's default numeric behavior is unchanged for older callers.

Only HTTP 200 plus a valid success envelope and expected data structure can
support an observation. Private responses must contain account-object and
position-list structure; absent structure is not modeled authentication.
1xx, other 2xx, redirects, 4xx/5xx and unsuccessful/malformed envelopes fail.
Diagnostics retain only approved HTTP/exchange codes, categories and fixed reason
labels. Raw bodies, private identifiers and exception messages are never rendered.
An HTTP 500/code 10500 produces a failed observation, no retry and no fallback.
Neither risk endpoint is part of this collection.

## Status and completeness

Every report prominently states **T1B MOCK REPORT — SYNTHETIC**. `VERIFIED` means
that the relevant check passed in invented evidence, not that Phemex was reached.
The authentication row describes acceptance of a private mock success envelope;
it is never proof of real credentials, account ownership or stable account binding.

- Public and private fixture provenance remain separate, with exact endpoint
  sources and collection start/completion times.
- Explicit consistent Long/Short Hedge legs are required. Missing quantities or
  absent legs never become zero. Contradictory partial rows remain relevant.
- Margin, configured leverage, status and quantity crosschecks are per leg.
  A missing value is not agreement, even if both responses omit it.
- Quantities are BTC base units. Raw observed quantity is separate from quantity
  compatibility with a current native grid; no quantity is rounded. A fresh raw
  quantity can remain usable while stale/missing grid metadata makes grid-dependent
  requirements UNKNOWN.
- Public metadata must be less than 300 seconds old; private observations less
  than 5 seconds old at `checked_at`. Future/inconsistent clocks invalidate checks.
  Rendering later does not renew the historical snapshot.
- Account-wide balance, equity, available margin, accounting classification,
  stable account binding and complete ordinary/conditional order coverage remain
  UNKNOWN. The four responses do not currently establish those semantics.
- Native minimum quantity remains UNKNOWN; quantity step is not a minimum.

`collection_complete` means four mock responses were accepted. `complete` also
requires the declared core status fields: modeled authentication, active market,
native contract size/quantity step/price tick, USDT position scope, Hedge legs,
full position-field crosscheck, raw quantities, per-leg margin and leverage.
It does not require account-wide funds or execution readiness. Optional grid,
identity and specification unknowns remain visible. Missing/contradictory core
fields give a partial status even when all four envelopes were accepted.

Representative excerpt (all values invented):

```text
PHEMEX TESTNET STATUS — READ ONLY
T1B MOCK REPORT — SYNTHETIC; NOT AUTHENTICATED EXCHANGE OBSERVATIONS
VERIFIED authentication: accepted private mock success envelope
VERIFIED explicit_hedge_legs: Long + Short
VERIFIED Long.observed_base_quantity: 0.002
VERIFIED Long.margin_mode: cross
VERIFIED Long.configured_leverage: 15
VERIFIED Short.observed_base_quantity: 0
VERIFIED Short.margin_mode: isolated
VERIFIED Short.configured_leverage: 3
UNKNOWN stable_account_binding: -
UNKNOWN accounting_type: -
UNKNOWN account.available_margin: -
Four-response collection: COMPLETE
Scoped mock status checks: COMPLETE
Full account verification: NOT ESTABLISHED
No HTTP requests sent. No credentials loaded. Exchange mutations remain disabled.
Execution authority: NONE; journal resolution: NONE.
```

The actual report includes endpoint times, all specification fields, provenance
and explicit reasons. It omits account/user identifiers and their comparison hashes.

## T1C remains separately blocked

This mock response seam must not become an arbitrary callable HTTP backend or be
connected to real credentials. T1C needs a separately reviewed authenticated-read
entry point, actual final-transport receipts and display provenance. Relabeling
mock data or toggling a `synthetic` flag cannot establish any of these.

Activation still requires reviewed credential handling and read-only Testnet key
permissions; trusted code/identity separation; distinct real-read approvals and
durable request reservations; verified TLS without redirects, proxies or retries;
an external hard deadline and appropriate independent egress restrictions. The
existing offline worker/IPC approvals remain offline, and Ubuntu dependencies
remain unapproved for installation. No installation is performed by T1B.

The existing URL/method/query/signing/prepared-request checks and unconditional
`OfflineGetTransport._send_http` block are unchanged. Existing signing/bypass tests
continue to cover that boundary; T1B does not claim to exercise new real signing,
TLS, network isolation or endpoint compatibility. No new GET route is approved.
No report can resolve a journal or authorize exchange execution.

[T1C Step 1](testnet-status-runtime.md) now adds a separate activation-blocked
runtime and `collect_runtime_status` seam for mocked transport tests. It reuses
this collection engine and UNKNOWN rules while retaining synthetic-only output.
The existing command still accepts only `status --mock-demo`; no actual-read mode,
credential option, worker activation or network permission has been added.

## Offline verification

```bash
.venv/bin/python -B -m unittest tests.test_testnet_status -v
.venv/bin/python -B -m unittest tests.test_testnet_transport tests.test_testnet_compatibility -q
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

Focused tests deny socket/Requests access, client/session construction, credential
loading, worker/IPC activation and ordinary-journal access. Subprocess import and
CLI tests install their own denial guards. No server, credentials or exchange is used.
