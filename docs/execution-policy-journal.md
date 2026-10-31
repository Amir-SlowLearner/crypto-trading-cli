# Increment 4, checkpoint 1: offline policy and operation journal

This checkpoint models eligibility and durable recovery **offline only**. It adds
no CLI command, authenticated collector, request compiler, submission gateway,
CCXT order call, or exchange write route. Existing CLI, discovery, mutation
barriers and offline previews are unchanged. Checkpoint 2 is not implemented.

## Trust boundary and eligibility

[execution_policy.py](../execution_policy.py) defines frozen models for account,
position, instrument, open-order and reviewed compatibility evidence. Each has
explicit provenance, source, collection time, completeness and environment.
Private and compatibility evidence must have the same opaque account binding.
The binding is a 64-character lowercase hexadecimal local identifier, not an API
key, secret, raw user ID or signed header. A future collector must establish a
stable account identity across credentials and restarts. That collector does not
exist here.

**Constructing a model with an authenticated provenance label does not authenticate
anything.** These are assertions for a future trusted collector. Synthetic tests
deliberately construct such assertions; they are not Phemex integration evidence.
The existing `AccountReport.complete` and offline `current_qty` are not accepted
as execution evidence or authorization.

`evaluate` accepts explicit direction, action and selected Long/Short leg, and uses
only the separate `PositionEvidence.base_quantity` as position context. Missing or
zero position quantity cannot become a reduction. It returns fixed diagnostic
codes and either no validated intent or an immutable semantic plan. Both decision
and validated intent always report `submission_enabled=False`.

The initial modeled capability is deliberately restricted:

- BTCUSDT, USDT-settled linear perpetual, explicit Hedge Mode Long/Short leg.
- `reduce` or `close`, limit IOC, explicit reduce-only, no attached TP/SL.
- Known supported accounting type (`classic` or `unified_multi_asset`), successful
  authentication, normal account and position status, known cross/isolated margin
  and positive configured leverage.
- Account-specific reviewed evidence of explicit Hedge Mode reduce-only, IOC and
  client-ID lookup compatibility. Missing/false evidence rejects; a known accounting
  type alone does not establish those capabilities.
- Complete selected-symbol active **and conditional** order coverage, both empty.
- Exact finite Decimal inputs and required specifications; no float comparisons.

Existing [order preparation](../order_preparation.py) is reused for intent,
direction, grid, native maximum, notional and close-equality checks. Reductions may
normalize down, with requested and normalized quantities retained separately;
closes must remain exactly equal before and after normalization. The 200 USDT
default preview cap is retained. Known leverage is observed, never changed; its
value is not proof of buying power. Optional price bounds remain optional and
are checked when present. There is no financial eligibility policy for opening or
increasing; these actions are rejected regardless of balances or the cap.

No account-wide equity, available margin or buying power is inferred. Those fields
are not required to justify *new* exposure because only proven reduction intent
is modeled. Accounting type, account restrictions, position state, instrument
rules and compatibility remain mandatory. The observed risk-mode and risk-unit
HTTP 500/code 10500 failures retain their [unresolved status](account-discovery.md).
In particular, unknown accounting still rejects this offline policy.

Freshness uses aware timestamps and explicit `now`, normalized to UTC. Future,
naive, missing and expired timestamps reject. The fixed bounds are:

| Evidence | Maximum age (exclusive) |
| --- | --- |
| Account, position, open/conditional orders | 5 seconds |
| Public testnet instrument specifications | 5 minutes |
| Account-specific compatibility review | 24 hours |

The plan expires at the earliest evidence deadline. These are conservative model
constants requiring review before any execution UI; they are not an established
Phemex timing guarantee. No environment setting weakens them. Sequential reads
and freshness cannot make exchange state atomic or prevent external trading.

## Fingerprints and modeled authorization

The frozen plan contains only allowlisted semantic business fields, including
requested/normalized base quantities, contracts, limit price and authenticated
position context. Its canonical JSON uses strings for numbers and sorted keys.
A SHA-256 fingerprint binds that plan; a separate fingerprint binds the typed
evidence. Account/environment binding and validity are stored separately.

**This is not a Phemex wire payload.** Exact request compilation and final
authenticated revalidation belong to later checkpoints. No wire-payload digest or
actual user submission authorization is claimed here. A future gateway must not
trust a reconstructed dataclass or a journal state without fresh validation.

The journal's `authorize` method requires a specific `AUTHORIZE OFFLINE <id>`
phrase, matching payload fingerprint, current version, unexpired evidence and
armed offline control. It returns a random one-use token; only its hash is stored.
Rearming invalidates prior tokens through a control generation. Expired, changed,
wrong-operation and replayed authorization is refused. Token consumption changes
only local state to `SUBMITTING`; it never sends anything or returns an executable
permit. There is no generic state-setting API.

## Durable state machine

[operation_journal.py](../operation_journal.py) uses standard-library SQLite with
FULL synchronous commits, rollback journaling, foreign keys, schema version checks
and integrity checks. It requires Linux/POSIX owner-only directories (0700), regular
files (0600), no symlinked parent paths, and an exclusive advisory session lock.
The journal file identity is checked before transactions; replacement, missing
files and permission changes fail closed. Opening a missing journal never creates
one; creation must be explicit and refuses existing files. There is no reset,
delete or migration API.

Each operation has durable UUID-based local and client IDs. The client ID is
reserved forever in that journal, including after terminal outcomes. SQL uniqueness
and a partial unique index allow only one nonterminal operation per account and
environment. Intent fields are immutable through an SQL trigger. Versioned
compare-and-swap transitions and atomic audit events reject stale callers. SQLite
busy/failure conditions do not trigger automatic retries.

| Current state | Allowed next state / method |
| --- | --- |
| `PREPARED` | `AUTHORIZED` via authorization; or `ABORTED`, `BLOCKED`, expired `EXPIRED` |
| `AUTHORIZED` | `SUBMITTING` via one-use consumption; or unsent terminal outcomes |
| `SUBMITTING` | `ACKNOWLEDGED`, `UNCERTAIN`, `RECONCILING` |
| `ACKNOWLEDGED` | `UNCERTAIN`, `RECONCILING` |
| `UNCERTAIN` | `RECONCILING` |
| `RECONCILING` | `UNCERTAIN`, reconciled `FILLED`, `CANCELED`, `REJECTED` |
| Terminal states | No transitions |

Terminal states are `FILLED`, `CANCELED`, `REJECTED`, `ABORTED`, `EXPIRED`, and
`BLOCKED`. The last three apply **only before** the modeled submission boundary.
Possibly submitted operations cannot be cleared as blocked/aborted/expired to
make room for another order. Acknowledgment alone is nonterminal.

Reconciled `FILLED` requires exact cumulative fill equality, `REJECTED` requires
zero fills, and `CANCELED` permits a partial fill. Partial cancellation is not a
fully closed position. Resolution records are caller-supplied offline observations,
not authenticated proof; exchange identity/parameter matching, visibility delays
and fresh position reconciliation must be implemented and tested later.

## Restart and emergency stop

Every successful open starts a new exclusive session, disarms the offline model,
and atomically performs conservative recovery:

- `PREPARED` / `AUTHORIZED` become `ABORTED`; stored authorization hashes are cleared.
- `SUBMITTING` / `ACKNOWLEDGED` / `RECONCILING` become `UNCERTAIN`.
- Existing uncertainty stays unresolved; terminal records and reserved IDs remain.
- An uncertain operation continues blocking another operation for that account.

There is no attempt to infer that a request was unsent, no placement retry and no
replacement client ID. SQLite rolls back uncommitted transactions after a process
crash. A crash after committing `SUBMITTING` remains uncertain even if no network
request actually happened. Clock rollback is a blocking journal error.

The model defaults to disarmed. `ARM OFFLINE MODEL` arms only local state; it
enables no exchange capability. The emergency state file must exist, be private,
and contain the exact ready marker; missing, unreadable, corrupted or symlinked
state blocks both arming and authorization consumption.

`OperationJournal.emergency_stop(path)` first latches revocation in the caller
process, before any filesystem access. All journal objects using that canonical
path in the caller process refuse arming and authorization consumption afterward.
This local latch has no reset API and is not a cross-process notification channel.

It then makes two independent durable-revocation attempts, with no automatic retry:

1. A separate SQLite connection attempts one immediate transaction that persists
   control mode `STOPPED`, advances its generation, and clears stored authorization
   hashes for unsent operations. Operation states, versions, payloads, account
   bindings and uniqueness constraints are unchanged. Restart preserves `STOPPED`;
   arming cannot override it, even if the readiness file still says ready.
2. The filesystem path removes readiness and syncs the directory, then installs
   and syncs a stopped marker. A failed marker installation is safe if readiness
   removal was already synced. Unsynced absence is not claimed to be durable.

The return value is an immutable `StopResult` with `database_revoked`,
`filesystem_revoked`, and `marker_installed` flags. `durable` means at least one
durable channel completed under the local SQLite/filesystem durability assumptions.
A database failure does not prevent the filesystem attempt, and vice versa. An
early unlink failure is therefore no longer enough to leave authorization usable:
the local latch blocks the caller and the SQLite channel can revoke other processes.
SQLite contention does not delay or retry that attempt; the filesystem channel can
still stop other processes without acquiring the journal session lock.

If neither channel establishes durable revocation, the method raises a sanitized
`JournalError` explicitly saying **only the caller process is revoked and other
processes may remain armed**. This is not durable stop confirmation. In particular,
if a helper process encounters both a database lock/failure and a readiness unlink
failure, an existing owner with unchanged durable controls may still consume its
authorization. The test suite preserves this counterexample. A process-local flag
cannot guarantee cross-process revocation when all persistence/communication paths
are unavailable. Interrupted stop attempts also must not be assumed durable without
a returned successful result. Future execution integration must treat unconfirmed
stop as fatal, notify/control the owning executor through an independently reviewed
mechanism, and must not report that new submissions have been globally stopped.

Consumption rechecks stop after its modeled state update and rolls back if the stop
is observed there. It cannot recall a transition already past its final check and
committing: such an in-flight operation may still finish as `SUBMITTING` after a
concurrent stop, remains blocking, and requires reconciliation. No actual exchange
write follows that state here. A future gateway still needs a stop check and reviewed
coordination at its actual submission boundary; this model is not that gateway.

No stop-reset API is supplied in checkpoint 1. Persisted stops remain latched through
reopen. Reads/reconciliation remain possible. Hardware/filesystem failures that lose
acknowledged writes, restored/copied journals, and hostile file-owner changes remain
outside the tested guarantees.

A stop cannot recall an in-flight exchange request and never cancels orders or
flattens positions. There is no in-flight exchange request in this checkpoint.

## Verification and remaining limitations

```bash
.venv/bin/python -B -m unittest tests.test_execution_policy tests.test_operation_journal -v
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

Tests prohibit HTTP/socket connections, use temporary private journals and invented
evidence, and include forked-process crash/lock tests, SQLite contention, transaction
rollback, duplicate IDs, missing/corrupted controls, authorization replay/expiry,
payload binding and uncertain recovery. Stop fault injection covers early unlink,
directory sync, marker replacement, SQLite commit/rollback/cleanup failures,
contention, helper processes and fresh-process restarts without inherited memory.
It also records both-channel failure and final-check races as explicit limitations.
The tests do not test hardware power loss,
network filesystems, compromised operating systems or hostile modification by the
file owner. Hashes detect inconsistencies; this is not an encrypted or tamper-proof
ledger. The lock is advisory and assumes cooperating local processes.

A future execution workflow must use one canonical durable journal and stable
account binding; switching/deleting databases cannot safely bypass unresolved
operations. Storage loss, backup/restore policy, stop-reset protocol, actual
credential/account binding, request compilation and trustworthy evidence collection
remain review questions. All Phemex-specific compatibility assertions remain
unverified against an exchange. Existing mutations stay disabled.
