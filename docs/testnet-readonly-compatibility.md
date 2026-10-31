# Increment 4, checkpoint 3, Phase A: read-only compatibility

Phase A implements and tests an observational BTCUSDT compatibility report. It
does **not** contact Phemex. The final HTTP send unconditionally blocks real
network transport. There is no enable flag, credential loader, command-line
entry point, submission gateway, or execution authorization. Phase B requires
separate explicit user approval and review of transport isolation and activation.
See [the final GET boundary and activation design](testnet-get-transport.md).
The [Stage 3 offline worker](testnet-readonly-worker.md) adds one-use durable
approvals outside caller-side transports. It has no real HTTP backend or actual
credentials, and its required OS deployment separation remains unverified.

## Architecture and boundaries

[testnet_compatibility.py](../testnet_compatibility.py) provides:

- `VerificationPhemex`, a restricted subclass of the existing `ReadOnlyPhemex`.
  It uses a four-route subset of the shared GET allowlist, zero retries, zero
  redirects, disabled private logging/response caches and diagnostic sanitization. It
  checks exact pinned testnet URLs before each request, fixes parameters, limits
  each endpoint to one attempt, and disables environment proxy inheritance.
  Phemex signing is reused, but CCXT's general-purpose HTTP transport and retry
  loop are not called. Its session is replaced with a non-networking object.
  Signed request and response caches are disabled. Separate inherited history
  writers are explicitly discarded, including with a positive history-cache size;
  a discard-only history view prevents inherited writers from enabling storage.
- `_Discovery`, an internal subclass reusing `AccountDiscovery` and its conservative
  GET/error/position parsers, with a separate fixed two-private-GET sequence.
  It adds final-transport start/completion receipts, an independent identity
  crosscheck and ephemeral identifier comparisons. Permission failures stop further
  verifier reads. Existing discovery and CLI behavior are unchanged.
- `CompatibilityVerifier`, a single-use collector requiring an explicit read-only
  authorization phrase even for mocked tests. Phase A permits synthetic reporting
  only; it cannot label mocked input as actual exchange verification.
- Frozen `CompatibilityReport`/`Requirement` models. These cannot be used as
  execution-policy evidence or journal authorization. `execution_authorized` is
  always false. No journal, simulator, emergency-stop state or execution policy
  is modified or armed.

The installed CCXT version is checked as 4.5.90. Its generated GET mappings and
`fetch_positions`, `fetch_balance`, `fetch_markets`, `fetch_currencies` and
`parse_swap_market` were inspected locally. No network documentation lookup was
performed. All Phemex-specific behavior remains subject to an approved Phase B
observation; local adapter code is not proof of account compatibility.

[testnet_transport.py](../testnet_transport.py) owns the immutable ordered plan,
one-use signed-request binding and prepared-HTTP-request checks. The only final
send implementation raises a fixed safety error. Tests mock this send beneath
all validation, while explicit socket and Requests network guards remain active.

CCXT normally calls `/public/products` once for currencies and again for markets.
Within one market load, the verifier reuses the same successful public response
for both parsers, avoiding a second GET. It never reuses a private response or
failed public response. The temporary response is discarded after loading. Market
loading explicitly reloads and cannot reuse a pre-existing synthetic/old cache.
Duplicate BTCUSDT products, malformed public envelopes or failed public loading
prevent all private collection.

## Exact proposed Phase B GET requests

**None has been performed during Phase A.** Every origin is fixed to
`https://testnet-api.phemex.com`; live URLs, HTTP URLs, alternate hosts, embedded
credentials and changed path prefixes reject before signing. No route is added to
the shared allowlist. Every parameter below is fixed; extra parameters reject.

| Sequence / condition | GET path | Parameters | Information / limits |
| --- | --- | --- | --- |
| First | `/public/products` | none | Fresh native BTCUSDT metadata plus currencies. Successful response reused locally for CCXT's two parser calls. |
| Second | `/v1/exchange/public/products` | none | Companion legacy product input required by pinned CCXT market loading; no additional account probe. |
| Third, after successful metadata load | `/g-accounts/positions` | `currency=USDT` | Primary account/position configuration, explicit legs, size, margin mode, configured leverage, status. Missing data never means zero. |
| Only after a successful primary position envelope | `/g-accounts/accountPositions` | `currency=USDT` | Independent account-identifier consistency and BTCUSDT position-context crosscheck; no assumption that this endpoint is supported for every account. |

Each transport object permits at most four physical sends, in this exact order.
This is not a run-wide quota: copying an active object duplicates its counter.
That standalone limitation remains. The optional Stage 3 worker-backed path
instead checks one durable approval store across transport copies and processes;
its trust assumptions and remaining deployment requirements are documented separately.
Failure stops can reduce the count. Each attempt is consumed before the send, including a
timeout, a blocked send or HTTP failure. Public-product memoization makes no
additional HTTP request. There is no retry or fallback. Authentication, permission,
rate-limit, network and HTTP server errors stop remaining private collection.
Only HTTP 200 plus a valid success envelope can establish successful observations;
provisional, redirect, unexpected success and error statuses cannot authenticate.

All risk/account-asset routes are now excluded, including the previously failing
risk-mode and risk-units endpoints. Accounting classification and account-wide
restrictions/financial figures remain UNKNOWN, explicitly marked `not_collected`.
No order, price-book, trade/history, transfer, setting or non-GET route is collected.
The existing broader discovery implementation and its narrowly scoped risk-mode
continuation policy are unchanged. Code 10500 alone proves neither invalid
credentials nor an unsupported endpoint; its previously observed root cause
remains **unresolved** and this plan does not probe it again.

## Credentials, identifiers and provenance

No credentials are loaded from disk or environment by the verifier. Its adapter
accepts credentials only in memory; Phase A tests use explicit fictitious values.
If Phase B is approved, private GET authentication necessarily sends CCXT's
authentication headers to Phemex over TLS. Those headers contain sensitive key
and signing material and must never be printed, logged, saved, or copied into
reports. Public requests carry no private authentication headers. Outbound
network routing, debugger inspection and packet capture remain operational
exposure considerations for a future approved run, not capabilities enabled here.

The inherited logger and private response caches stay disabled. Reports retain
only fixed diagnostic categories, numeric HTTP/exchange codes and allowlisted
reason labels, not exception text, raw private responses or signed request data.
Malformed inputs are rendered as UNKNOWN or a fixed safety error.

The identity check compares reviewed `accountId`/`accountID` and `userID`/`userId`
aliases from the two authenticated envelopes, plus available position identities.
Only bounded positive numeric identifiers are accepted. Missing fields,
conflicting aliases, position/account mismatches, unsupported formats or different
identities across the two GETs leave consistency UNKNOWN. Identifiers and their
ephemeral comparison digests are neither rendered nor persisted. No identity is
derived from an API key or authentication success.

Matching identifiers verify only **within-snapshot consistency**, not a stable
binding across runs, API-key changes, subaccounts or restored journal files.
`stable_account_binding` therefore remains UNKNOWN: no enrolled reference or
reviewed binding protocol exists. Identity consistency is distinct from whether
the requested position configuration was reported.

Provenance is explicit:

- Phase A report fields use `synthetic_authenticated_fixture` or
  `synthetic_public_fixture`. VERIFIED means a check passed **within invented
  fixtures**, never that an exchange fact was observed.
- Uncollected risk/accounting requirements have `not_collected` provenance,
  unavailable timestamps and no claim that their excluded endpoint was observed.
- Final-transport receipts contain only approved URLs, labels, timestamps,
  numeric HTTP status and `synthetic_http_fixture` provenance. Private and public
  report timestamps come from their actual mocked HTTP collection intervals.
- A future approved Phase B must introduce actual collection provenance; it may
  not simply relabel a synthetic report or construct trusted evidence dataclasses.
- Private observations and public metadata remain separate. CCXT-derived
  conventions are not promoted to verified native instrument specifications.
- Grid-checked quantity and reducibility use combined
  `synthetic_private_and_public_fixture` provenance; raw observed quantity uses
  private-fixture provenance only. Neither is authenticated execution evidence.
- User-provided order quantities/prices, offline `current_qty`, simulated fills
  and caller-provided account labels are not accepted as account observations.

## Status, freshness and remaining blockers

`VERIFIED` means a fresh, internally consistent observation supports that specific
requirement, scoped to the stated provenance and evaluation timestamp. It is not
an execution permit. `UNKNOWN` includes missing, unsupported, failed, stale,
contradictory or insufficient evidence. `INCOMPATIBLE` is reserved for a known
conflict with this scope, such as consistent OneWay mode, a delisted market or a
known non-USDT account/position scope. A consistently flat or non-normal position
is INCOMPATIBLE with the reducible-position requirement.

Private observations must be less than five seconds old, and public metadata less
than five minutes old, measured conservatively from request start. Future,
backward or inconsistent clocks invalidate the affected observations. Report
statuses are historical as of `checked_at`; rendering later does not renew them.
Sequential GETs are not an atomic snapshot and cannot prevent external trading.

Every compatibility response body, public or private, is decoded with the same
duplicate-rejecting JSON parser before CCXT diagnostics, error interpretation or
verifier evidence extraction. Object member names must be unique at every depth,
including objects within arrays. Identical duplicate values are rejected too;
escaped spellings that decode to the same name also count as duplicates. Neither
the first nor the last value is selected. An ambiguous or malformed body cannot
support VERIFIED requirements. Independently successful observations may still
support their own requirements, such as authentication from a prior valid GET.

Rejected bodies produce fixed sanitized errors without raw fields or values.
Only the HTTP status is retained by the verifier before body validation; an
ambiguous exchange `code` is not selected for diagnostics. The endpoint remains
attempted, without retry or replacement authorization. On the worker-backed path,
duplicate rejection occurs before a slot can be recorded OBSERVED; the failed
attempt leaves the approval spent and blocks completion. Reports remain synthetic
and cannot authorize execution or modify order journals.

Both explicit Long/Short legs are required to verify Hedge Mode for BTCUSDT.
Both GETs must independently supply complete, unpaginated selected-leg structure,
valid mode and side, and consistent direction evidence. Missing/duplicate legs,
unassignable rows, malformed mode/side or conflicting modes are UNKNOWN. A
contradictory mode or side is retained even when that row lacks quantity; it
cannot be discarded to promote the other GET to VERIFIED. Disagreement in
selected-symbol quantity or direction also invalidates the position context.

Crosschecks join by explicit position leg, not list order. Quantity, configured
leverage, margin mode and position status are parsed and compared independently.
Each field requires valid, equal evidence from both GETs to be VERIFIED. Missing
or malformed values never count as agreement, including when both GETs omit the
same field. An incomplete composite `position_crosscheck` is UNKNOWN while
independently matching fields can remain VERIFIED. For example, a missing Long
quantity leaves that quantity and reducibility UNKNOWN, while matching Hedge
Mode, leverage, margin mode, status and the Short quantity can remain VERIFIED.
Leverage conflicts invalidate leverage; margin conflicts invalidate margin;
status conflicts invalidate status and reducibility. A reducible position requires
matching positive quantity and matching Normal status. This describes observed
position context, never permission or proof that an order will be accepted.

`position_account_scope` requires a USDT account currency in **both** responses.
All present reviewed scope fields (`currency`, `settleCurrency`, `quoteCurrency`
and `symbol`) on the account, data envelope and selected rows must agree with
USDT/BTCUSDT. A selected row may omit currency only when its enclosing account
provides validated USDT scope; an explicitly null/malformed row currency is not
treated as absence. Missing or malformed scope is UNKNOWN. A known different
currency or symbol is INCOMPATIBLE with the scope requirement; dependent USDT
position requirements remain UNKNOWN. A BTC-scoped account cannot verify USDT
positions even if it contains apparently valid BTCUSDT rows. Independently usable
authentication, public instrument and accounting observations are preserved.
Scope validation does not establish account ownership or stable identity binding,
and unreviewed scope aliases are not claimed to be validated.

`<leg>.observed_base_quantity` records matching fresh private quantities in base
units (BTC), independently of the public grid. `<leg>.base_quantity` is the
existing grid-checked quantity: it requires a valid reported native step and
fresh public metadata as well as fresh matching private reads. Missing,
contradictory or stale public grid evidence makes the grid-checked quantity and
dependent reducibility UNKNOWN while preserving a usable raw observation.
An observed off-grid quantity does not become verified grid compatibility.
Both paths reject unsupported float inputs or excessive arithmetic precision;
neither rounds. Margin mode and leverage remain
per-leg observations; their documented leverage-sign/absolute-value derivations
are disclosed, with no exchange settings changed.

Native quantity/price grids must agree with normalized CCXT precision. Missing,
invalid or conflicting fields stay UNKNOWN. The pinned USDT parser supplies a
contract-size convention of one, but the verifier requires native confirmation to
mark contract size VERIFIED. It also leaves normalized minimum order quantity
unset. A quantity step is **not** silently substituted for minimum order size.
Native minimum quantity mapping remains an unresolved documentation/API question.

Before any future submission, the following remain blockers:

1. Stable account/environment binding and a reviewed trusted evidence collector.
2. Fresh complete Hedge Mode leg identity, quantity, mode, status and leverage
   observations for the intended account; a positive reducible position.
3. Reliable native contract units, minimum quantity and all relevant trading
   restrictions, beyond the subset observed here.
4. Complete active/conditional-order coverage and account restriction checks.
   Neither is attested by this verifier.
5. Account-specific acceptance of explicit `posSide` plus `reduceOnly`, limit IOC
   and client-ID lookup semantics. Position GETs and CCXT construction logic
   cannot establish acceptance; this requirement always remains UNKNOWN here.
6. Exact candidate quantity/price validation, final authorization, durable real
   submission binding, uncertain-outcome recovery and coordinated emergency stop.
   Existing offline models provide none of that execution authority.
7. Separate authorization for any future exchange write. Phase B approval will
   authorize only the reviewed GET observations, not an order trial.

Equity, available margin, buying power and successful `AccountReport.complete`
never become execution authorization. Existing emergency-stop limitations and
all journal/execution blockers are unchanged.

## Offline verification

```bash
.venv/bin/python -B -m unittest tests.test_testnet_compatibility -v
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

Tests inherit socket/HTTP connection prohibitions and mock only transport
responses. They cover identities, Hedge Mode conflicts/missing legs, inconsistent
grids, stale/clock-invalid observations, malformed responses, errors and 10500,
partial contradictions, per-field crosschecks, account/envelope/row scope,
fixed GET routes, mutation/endpoint rejection and report redaction. They also
prove that the actual Phase A transport cannot run and that compatibility reports
cannot be passed as execution-policy evidence.

See the generated [synthetic sample report](testnet-compatibility-synthetic.md).
Its numbers and identifiers are invented; no exchange account was contacted.
