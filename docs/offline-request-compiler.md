# Increment 4, checkpoint 2: offline request compilation and recovery

This checkpoint adds **no exchange execution**. It makes no API request, constructs
no CCXT exchange client, loads no credentials, signs nothing, and supplies no
transport callback or submission gateway. Existing CLI commands, discovery and
mutation barriers are unchanged. Checkpoint 3 has not started.

None of these offline models, compiled requests, journal states, authorization
tokens or simulated observations provides exchange execution authority. They
must not be accepted as permission to submit a testnet or live order.

## Decisions at the checkpoint 1 boundary

Checkpoint 1 stores a semantic plan, not an unsigned request. Its authorization
binds that semantic plan, and its reconciliation methods model caller-supplied
observations. Reusing those observations in a future executor would be unsafe.

The conservative resolution is a **separate simulation database purpose**, rather
than interpreting simulated observations as authenticated exchange evidence:

- [OperationJournal](../operation_journal.py) retains SQLite application ID `0`,
  including existing checkpoint 1 journals. No migration is required or performed.
- [SimulationJournal](../offline_simulation.py) creates SQLite application ID
  `0x53494D55` (`SIMU`) and a separate immutable request-binding table. Both journal
  classes reject the other's database purpose **before recovery changes state**.
- The simulation workflow accepts exactly `SimulationJournal`, never an ordinary
  journal, client, callback, or transport. Terminal simulated outcomes can release
  only simulation blockers through the supported workflow with unchanged database
  purpose. They cannot clear an ordinary journal's blockers through that workflow.
- Both inherit the same exclusive session lock, account/environment uniqueness,
  expiry/replay checks, private-file requirements, SQLite durability settings,
  conservative recovery, and emergency stop. Database purpose is a safeguard for
  cooperating code, not protection against hostile Python code or the file owner.

Two reproduced limitations make that cooperating-code boundary explicit:

- Direct invocation of inherited base journal methods can bypass the
  simulation-specific request-binding checks. For example, calling
  `OperationJournal.authorize(simulation_journal, ...)` followed by
  `OperationJournal.consume_authorization(simulation_journal, ...)` can reach local
  `SUBMITTING` without the simulation authorization wrapper or compiled-request
  checks. The base expiry, replay and emergency-stop checks still apply. This is
  not an exchange submission, and future execution code must not accept this
  route as an authorization boundary.
- Database purpose is verified at opening, before recovery, **not continuously**
  during the session. Deliberate in-process database-header modification, such as
  changing `PRAGMA application_id` from `SIMU` to `0` while a simulation journal is
  open, can undermine purpose isolation. The open simulation object can continue
  its local transitions, and the modified file can subsequently be accepted as an
  ordinary journal. Current guards do not protect against this deliberate
  modification. Stronger execution integration must account for this limitation.

No real execution journal or authenticated reconciliation interface is introduced.
A future gateway must reject `SIMU` databases and simulated results. It must not
interpret checkpoint 1's generic offline transition methods as exchange proof.

## Compiler and pinned CCXT behavior

[phemex_request_compiler.py](../phemex_request_compiler.py) reevaluates the typed
policy assertions and compiles only BTCUSDT, explicit Hedge Mode Long/Short,
reduce/close, reduce-only limit IOC. It reuses all existing policy/preparation
checks, including accounting, capability assertions, freshness, exact grids,
direction, native maximum, the 200 USDT preview cap, and close equality.

The reused policy supplies a fixed, unused preparation placeholder ID. It no
longer generates a random preview ID during validation. The compiler requires an
explicit `tn_` plus 32 lowercase hex digits client ID; it never invents/replaces
one. Reservation is authoritative only after the simulation journal records it.

The installed dependency remains **CCXT 4.5.90**, already pinned in the repository.
`verify_installed_ccxt()` explicitly inspects local source and checks version and
SHA-256 pins for `phemex.create_order`, `phemex.describe`, the amount/price precision
helpers, and `decimal_to_precision`. It never calls the order method. Simulation
reservation requires this check. The pure compiler performs no file I/O and
checks the version; the offline tests also check the installed source profile
and generated `privatePostGOrders` entry. An upgrade/source change requires review,
not automatic adaptation. These are selected behavioral pins, not a full
dependency integrity or supply-chain attestation.

Local source inspection established these construction rules:

| Concern | Inspected CCXT behavior / compiler restriction |
| --- | --- |
| Route | USDT/USDC branch calls `privatePostGOrders`; generated entry is private `POST g-orders`. Only USDT BTCUSDT is compiled here. |
| Testnet origin | `describe` declares `https://testnet-api.phemex.com` for private sandbox routes. Compiler uses this fixed origin, never a user URL. This is data, not a transport configuration. |
| Quantity | Stable-settled branch writes `amount_to_precision(amount)` directly to `orderQtyRq`; no contract-size multiplication occurs there. The compiler requires `contract_size=1` so prepared contracts and base quantity agree. Other sizes fail closed. |
| Price | Stable-settled limit branch writes `price_to_precision(price)` to `priceRp`, without integer `Ep` scaling. |
| Hedge leg | Explicit `posSide=Long/Short` is preserved. Missing `posSide` can invoke CCXT's hedge-side inference and omission of `reduceOnly`; that path is excluded. |
| Reduction | Explicit `reduceOnly` is preserved as Boolean `true`. Account-specific acceptance is still unverified. |
| IOC | `timeInForce` is passed through in `orderParams`; the compiler emits exchange literal `ImmediateOrCancel`, not unified string `IOC`. |
| Client ID | Explicit `clOrdID` is preserved, max 40 characters according to the source comment; reserved-format IDs here are 35. CCXT's fallback broker/UUID generation is excluded. |
| Extra parameters | CCXT merges caller parameters into its request. The compiler accepts no free-form parameters and rejects every extra, missing or changed field. |

Only CCXT's standalone numeric formatter runs during compilation, in an isolated
Decimal context of precision 100. It uses the inspected truncate-for-quantity and
round-for-price modes, then requires exact Decimal equality to the already
validated values. Any value change rejects; there is no additional quantity or
price rounding. The compiler's output uses canonical decimal strings, with no
float comparisons. Existing permitted downward quantity normalization is retained
in the semantic plan's separate requested and normalized fields. Close equality
cannot be weakened by compilation.

For the invented fixture (Long reduction, 0.001 BTC at 60000 USDT), the exact
unsigned business representation is:

```json
{
  "api": "private",
  "environment": "testnet",
  "method": "POST",
  "origin": "https://testnet-api.phemex.com",
  "parameters": {
    "clOrdID": "tn_11111111111111111111111111111111",
    "ordType": "Limit",
    "orderQtyRq": "0.001",
    "posSide": "Long",
    "priceRp": "60000",
    "reduceOnly": true,
    "side": "Sell",
    "symbol": "BTCUSDT",
    "timeInForce": "ImmediateOrCancel"
  },
  "path": "/g-orders"
}
```

The fingerprint hashes sorted compact JSON of this representation. It binds the
method, origin, API group, path, environment and every parameter. It is an exact
business-field representation, **not signed HTTP bytes**. CCXT's locally inspected
signing code serializes private POST parameters as JSON, but signing is never
called here; byte ordering, expiry headers and signatures are deliberately absent.

`check_compiled` reconstructs the expected request from the immutable context and
compares the entire model, rather than trusting a caller's recomputed hash.
Returned dictionaries are copies and cannot mutate the frozen request.

## Evidence is still unverified

Frozen objects and `authenticated_testnet` provenance tags do not establish
network origin. They remain assertions. The compiler explicitly reports
`authenticated_evidence_verified=False` and `submission_enabled=False`. The
tests deliberately fabricate otherwise acceptable evidence. A caller able to
construct/re-hash an entirely new assertion set can construct another offline
plan; that is not a credential or authorization to trade.

There is still no trusted collector, credential/account identity derivation,
atomic exchange snapshot or financially safe/executable order claim. Offline
`current_qty` is not an input to this workflow. Synthetic cache metadata is not
promoted to verified instrument metadata.

Unknown accounting or required specs continue to reject the policy. No equity,
margin or buying power is inferred. The observed testnet `risk_mode` / `risk_units`
HTTP 500/code 10500 failures remain unresolved, and no new requests investigated
them during this checkpoint. Account-specific Hedge Mode `posSide` plus
`reduceOnly`, IOC support, client-ID uniqueness/lookup, contract units, restrictions,
fill reporting and classic/unified route applicability all require later
authenticated, explicitly authorized read-only checks and reviewed testnet trials.
Local CCXT source establishes construction behavior, not Phemex acceptance.

## Durable request binding and simulated lifecycle

1. Compile the typed assertions with an explicit client ID and inspect the frozen
   business request. No ID is reserved by compilation alone.
2. `reserve_compiled` verifies the local CCXT profile and exact compiled request,
   then commits the existing semantic reservation with that client ID.
3. A second SQLite transaction inserts canonical request, SHA-256 fingerprint and
   profile into `simulation_requests`, keyed by the operation ID. SQL triggers
   reject updates/deletion. Interruption/failure between these two transactions
   leaves an unsent, blocking reservation **without authorization eligibility**.
   Restart conservatively aborts it, retaining its IDs; nothing is retried or
   silently completed.
4. Authorization checks that the request matches the persisted row, account,
   environment, semantic payload and evidence fingerprints. The existing explicit
   offline phrase, arm, version and expiry checks apply. The returned frozen
   `SimulationAuthorization` wraps the one-use token and exact request fingerprint.
5. Consumption checks the complete binding and wrapper again before the existing
   stop/replay/expiry-guarded transition to local `SUBMITTING`. There is no exchange
   permit or write after this transition.
6. An explicit `Outcome` chooses one local scripted result, with no callback and
   no automatic retries:

| Scripted outcome | Local simulation state |
| --- | --- |
| Acknowledgment | `ACKNOWLEDGED`, still blocking |
| Definite rejection | Reconcile to `REJECTED`, exactly zero fills |
| Full fill | Reconcile to `FILLED`, exact order quantity |
| Partial fill + IOC cancellation | Reconcile to `CANCELED`, positive on-grid cumulative fill below quantity; never claims a full close |
| Timeout, 5xx, unknown outcome | `UNCERTAIN`, still blocking, same operation/client IDs |

Every result is explicitly `simulation_only=True`, `authenticated=False`, and
`submission_enabled=False`. Reconciliation requires the matching simulated
operation, client ID, account, environment, request digest and deterministic
`sim_` order ID. Negative, non-finite, excessive, off-grid or contradictory fills
reject. Unknown/not-found results cannot release a blocker. An explicit invented
terminal observation can resolve only a simulation journal; it is never exchange
reconciliation. Repeated terminal reconciliation rejects.

Simulated fills do not modify simulated account balances or positions. The
workflow scripts order-state outcomes; it is not a matching engine or an account
ledger. A simulated full fill does not establish an exchange position change,
available margin or buying power for any subsequent order.

Failures after modeled consumption remain `SUBMITTING` or another nonterminal
state. Restart uses checkpoint 1 recovery: unsent authorizations become aborted;
possibly submitted states become uncertain and retain blocking status. Simulated
reconciliation remains possible after a stop; it does not rearm or authorize.

Recovery preserves the exact request JSON and digest, but does not persist or
deserialize a new typed evidence context. To run explicit simulated reconciliation
after restart, the caller must retain/reconstruct the exact original offline
artifact and pass its binding checks. A missing artifact leaves the operation
blocking; it never licenses replacement evidence, a retry, or another client ID.
The original authorization expiry still prevents a new simulated submission;
examining a historical simulated result is not a new authorization.

The emergency-stop guarantees and limitations in
[checkpoint 1](execution-policy-journal.md#restart-and-emergency-stop) remain in
force. Both persistence channels can fail, and the final-check/commit race cannot
recall an already in-flight transition. This checkpoint supplies no new stop
reset, IPC or execution-boundary guarantee.

## Offline verification and remaining review

```bash
.venv/bin/python -B -m unittest tests.test_phemex_request_compiler tests.test_offline_simulation -v
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

Tests use fabricated evidence and temporary private SQLite databases with HTTP
and socket connections blocked. They cover exact Long/Short reduce/close requests,
precision boundaries, source pins, tampering, binding, expiry/replay, duplicates,
stop failures, concurrent locks, SQLite contention, crash boundaries, and all
scripted outcomes. They do not invoke CCXT order construction or signing methods.

Review is still required for trusted evidence collection, reliable instrument
provenance, real request compilation-to-transport binding, authenticated lookup,
account-specific route/parameter acceptance, filesystem power-loss behavior,
backup/restore/copy policy, external trading races and a coordinated executor
emergency stop. Application IDs and hashes are not tamper-proof against the
owner. Copying/restoring a journal or deliberately editing its identity cannot
be made safe by this offline model. No testnet or live execution is enabled.
