# F2 Step 1: pure offline IOC reconciliation

[ioc_reconciliation.py](../ioc_reconciliation.py) handles one BTCUSDT Hedge Mode
reduce-only limit IOC operation. It performs no I/O, loads no credentials, constructs
no exchange, changes no journal and exposes no submission or retry interface. No
CLI, endpoint allowlist, deployment or existing shared module is changed.

## Trust boundary

Every input is an **untrusted normalized assertion**. These models are not native
Phemex response schemas and none of their field names promises an exchange field.
Frozen dataclasses prevent ordinary mutation; construction is not authentication.
Validation occurs at `reconcile`, including exact model, enum, boolean and Decimal
types. Malformed input produces fixed UNRESOLVED reasons, never raw exceptions.

This checkpoint accepts only explicitly `Provenance.SYNTHETIC` observation stamps
for offline proposals. User-supplied, unknown, string or authenticated-looking
provenance labels are refused. A synthetic proposal remains untrusted; a caller
who fabricates a fully consistent synthetic story can obtain a synthetic proposal.
The existing compiled request also contains offline evidence assertions, not proof
of network origin. There is no authenticated collector seam or promotion flag.

Every returned result permanently reports `execution_authority=False`,
`authenticated_evidence_verified=False`, `may_resolve_journal=False` and
`may_resubmit=False`. These properties are not an OS sandbox or a security token.
Nothing consumes a result to resolve an ordinary or simulation journal. A future
trusted reconciliation adapter must be separately designed and reviewed; adding an
authenticated label or accepting these result objects would not implement it.

## Input contract

`OperationContext` holds the original local operation ID, existing immutable
`CompiledRequest`, paired-leg baseline, attempted-submission time, waiting deadline
and optional previously associated exchange order ID. It generates no IDs.
It also requires explicit synthetic `ClockInfo` for the compiled plan's times,
attempt, evaluation time and deadline. Collection stamps, order event updates and
executions carry matching clock metadata; missing metadata defaults to unknown,
not to comparable clocks or zero uncertainty.
`check_compiled` is reused to reconstruct the original policy/preparation/compiler
decision; this module does not reimplement trading eligibility or change its gates.
An attempt must lie inside the original plan's validity interval. Reconciliation
can happen after plan expiry; it never grants a fresh submission authorization.

`Correlation` binds every order, fill collection and execution to the exact local
operation, client ID, request fingerprint, opaque account binding, testnet,
BTCUSDT unified symbol and selected Long/Short leg. Exchange order IDs must agree
with any recorded ID and with each other. With no recorded exchange ID, one
matching order may establish the association **inside this synthetic model only**.
Fills alone cannot establish a conclusive order result.

`OrderObservation` carries the compiler's exact business-parameter tuple, native-
independent status, cumulative base fill and an explicitly supplied event-update
time. No parameter is optional, guessed or silently repaired. A future decoder
must prove each mapping, including IOC/reduce-only/leg semantics, before use.
An acknowledgment lacking those fields cannot be manufactured into a full order
observation. Missing history/open orders is represented by no order observation,
not by a rejected or canceled order.

`FillCoverage` contains one consolidated, explicitly bounded history window with
`pages_complete` and stamp completeness. It must cover submission through the
terminal order update. `FillObservation` includes stable execution identity, exact
order correlation, execution time, base quantity and price. Identical executions
with the same ID are counted once; differing values under that ID fail closed.
There is no deduplication by price or timestamp. Coverage and page completeness
are assertions, not established by this module. Pagination acquisition and native
execution-ID semantics remain unverified.

All quantities are explicitly BTC base units. Contract quantities or floats are
rejected. The compiler already restricts contract size to one. Finite Decimals
are limited to 40 significant digits and adjusted exponents within +/-40, matching
the preparation domain. Grid tests, fill totals and position-effect comparisons
use exact rational arithmetic, independent of ambient Decimal precision. Execution
prices must satisfy the native tick and submitted limit direction. No rounding
occurs. Up to 256 order observations, 256 post-position observations and 4096 fill
rows are accepted per call, including duplicates.

`PositionObservation` contains both nonnegative, on-grid Hedge legs, account,
environment and symbol scope. Baseline selected quantity must equal the compiled
position context. Opposite-leg changes fail closed. Chronologically separated
selected-leg increases and conflicting overlapping reads also fail closed. Every
post-attempt selected-leg quantity is checked against the original baseline;
returning to the expected final size cannot hide an intervening increase. All
pairs of collection intervals are compared, including nonadjacent, nested and
containing intervals; at most 256 observations bound this to 32640 comparisons.
Consistent overlaps remain usable. This
is deliberately conservative: external/manual trades or inconsistent snapshots
are not attributed to this operation.

## Time, ordering and history

All times must be aware and not in the future. Collection start must not follow
completion. Baseline collection must finish before the attempt and be less than
five seconds old at the attempt. Its age at later reconciliation is not a reason
to forget the original baseline.

Clock comparability is a separate requirement from an aware UTC timestamp.
`ClockInfo` must declare the same `synthetic_...` domain throughout the operation
and explicitly assert zero timestamp uncertainty. Missing/malformed clock data,
different domains, native clock labels, unknown precision or any nonzero
uncertainty return UNRESOLVED. No offset, tolerance, rounding, clock calibration
or native-to-local clock mapping is invented. In this offline checkpoint these
are **untrusted fixture assertions only**, not authentication or proof that two
real clocks agree. Zero uncertainty means exact points on the invented timeline;
it is not a claim that real clocks or native timestamps have infinite precision.

Order event updates and executions must not predate the attempt. Order updates
must not postdate collection completion. Sort order history by event-update time,
not arrival order. Cumulative fills cannot decrease; equal update times cannot
contain different states/fills; a terminal state cannot revert or change. Repeated
identical terminal states with later update times are allowed. Repeated reads with
the same update time are selected deterministically by collection times and ID.
Conflicting reuse of an observation evidence ID is rejected.

Execution chronology is crosschecked against **every** retained order observation,
including incomplete and superseded records. A known execution after any terminal
state contradicts that state, even if a later repeated terminal record looks valid.
Known executions before each order update form a lower bound on its cumulative
fill. With complete, sufficiently late fill coverage, their exact sum must equal
that observation's cumulative quantity. Prefix sums use exact rational arithmetic.
An execution timestamp equal to an order-update timestamp cannot establish the
necessary ordering in these models: it returns UNRESOLVED, without guessing a
sequence. Native timestamps with coarse granularity would need separately reviewed
ordering evidence; no such evidence or tolerance is invented here.

The latest order read, consolidated fill read and latest post-position read must
each be less than five seconds old, measured conservatively from request start.
Older historical observations remain usable for contradiction checks. Complete
fill collection must start after the retained order observations finish, and its
window must cover their updates before exact historical cumulative comparisons
can support a proposal. Insufficient temporal coverage stays pending. Terminal
updates must follow all executions, not just the last-looking terminal record.
Post-position collection must start after both terminal order and fill collection
complete. Every such post-position observation must agree with the final effect.

## Intermediate position and execution chronology

Every **complete** post-attempt position observation is also checked against the
deduplicated execution timeline, including observations collected before the
terminal order/fill reads. Matching final positions cannot erase a contradictory
intermediate position. Quantities and prefix sums use exact rational arithmetic
from validated Decimal base quantities, independent of ambient Decimal precision.
These are internally checked synthetic executions, never authenticated fills.

For a collection interval `[start, end]`, let `F_before` be the sum of executions
strictly before `start` and `F_possible` the sum at or before `end`. With complete
coverage, the observed reduction must lie between those two quantities:

```text
baseline - F_possible <= observed selected quantity <= baseline - F_before
```

If a snapshot finishes before the only execution, both sums are zero and the
selected quantity must still equal baseline. This rejects the reproduced early
flat-position/full-close case and premature partial reductions. A snapshot
strictly after an execution must reflect its reduction; multiple executions allow
exact intermediate quantities between them.

If `F_before != F_possible`, the collection interval contains an execution or
touches its timestamp. Even a quantity inside the possible range cannot prove
the snapshot's instant. The engine returns UNRESOLVED rather than choosing a
start/end snapshot or relying on a later matching read. Values outside that range
are contradictory. This deliberately rejects some potentially valid interval-
spanning snapshots; they need a separately reviewed as-of/sequence contract.
Execution equality with either collection boundary (including a zero-duration
interval) remains ambiguous. Strict one-microsecond separation is distinguishable
only in an explicitly exact synthetic clock domain.

Incomplete fill history provides only a lower bound from known executions; absent
rows are not interpreted as zero earlier fills. A nonterminal history window also
cannot establish the state after its cutoff. Complete final terminal-order/fill
evidence may establish the operation's full execution timeline beyond that cutoff;
without it, later positions remain insufficient. Incomplete position records stay
blocking under the historical-incompleteness rules below and still participate
in all existing binding, baseline, opposite-leg, monotonicity and overlap checks.

These sequencing rules are an offline contract, not a claim of an atomic Phemex
snapshot. Clock skew, native timestamp granularity, snapshot visibility and fill
retention need compatibility research. Missing reliable event times cannot be
replaced with invented exchange timestamps. The caller must retain and resupply
the full accumulated order/position history across calls: this pure engine cannot
detect omitted earlier contradictions or remember prior calls by itself.

## Narrow supersession of historical incompleteness

An incomplete order observation no longer blocks forever if another complete
observation demonstrably attests the **same modeled order version**. It must have
the same exchange/client/local IDs, account, environment, symbol, leg, request
fingerprint, parameters, status, cumulative quantity, unit and exact event-update
timestamp. Its collection must start at or after the incomplete collection ends.
An overlapping read, another incomplete read, or a newer event version is not
sufficient. This is internal consistency of synthetic assertions, not proof of
native Phemex version semantics or authenticated completeness.

The original record remains in supporting evidence and in all identity, field,
terminal-state, cumulative and execution-chronology checks. Supersession removes
only the missing-completeness blocker; it never erases a conflicting fact. A
contradiction still returns UNRESOLVED. Fresh, matching evidence can support a
proposal after the waiting deadline without changing the IDs or permitting retry.

**Position incompleteness retains an explicit fail-closed limitation.** The
current position model has neither a stable native snapshot version nor per-field
missing-information coverage. A later complete position read, even with identical
quantities and scope, cannot attest which information was missing in the earlier
snapshot. Incomplete historical positions therefore still yield WAIT before the
deadline and UNRESOLVED afterward, even when newer complete reads arrive. Their
known quantities, scope and mode remain subject to every contradiction check.
The engine does not drop these records or invent a version/coverage attestation.
Supporting broader recovery requires a separately reviewed observation contract.

## Decisions and IOC outcomes

`WAIT` means evidence is missing/incomplete or nonterminal and the explicitly
supplied waiting deadline has not elapsed. `UNRESOLVED` means invalid/contradictory/
stale input, no attempted submission, or insufficient evidence after the deadline.
The deadline controls reporting only: it proves neither rejection nor non-send,
replenishes no budget, releases no blocker and triggers no action. Late fresh
evidence can still propose an outcome for the same IDs.

`PROPOSED_OUTCOME` requires a correlated terminal order, complete matching fills
and matching paired-leg post-position evidence. It returns the terminal status,
exact cumulative fill, observed remaining selected quantity and supporting
observation IDs. Supporting IDs refer to the supplied immutable assertions; they
are not authenticated receipts or durable journal records.

| Candidate | Required terminal status and quantities |
| --- | --- |
| UNFILLED | CANCELED or EXPIRED, cumulative quantity zero, no fills and unchanged positions |
| PARTIALLY_FILLED | CANCELED or EXPIRED, strictly positive fill below submitted quantity, matching executions and reduction |
| FILLED | FILLED, cumulative quantity equals submitted quantity, matching executions and reduction |
| REJECTED | REJECTED, zero cumulative quantity, no executions and unchanged positions |

For every terminal outcome, unique fills must sum exactly to the order's final
cumulative fill, and `selected_after = selected_before - filled`, with the opposite
leg unchanged. A close IOC that fills only partially proposes PARTIALLY_FILLED,
never a closed position. A full-fill close requires observed zero selected size.
ACCEPTED and nonterminal PARTIALLY_FILLED never produce terminal proposals.
Position changes alone never establish execution. Cancellation with cumulative
fill equal to the whole order is conservatively refused as a status/quantity
conflict pending a reviewed native interpretation. Generic normalized `closed`
has no direct mapping. EXPIRED here describes an exchange order assertion, not
the journal's unsent authorization-expiry state.

## Validation and remaining blockers

Tests use synthetic fixtures with socket/HTTP denial guards, existing compilation
(which reuses preparation), simulation reservation and timeout recovery, real
offline child-process interruption, and ordinary journal blockers. Candidate
results leave all journal records unchanged. Existing lifecycle and emergency-stop
semantics remain untouched; this engine cannot inspect or bypass those controls.

Still missing: trusted authenticated acquisition and identity enrollment, accounting
and account restrictions, native minimum quantity, complete ordinary/conditional
coverage, account-specific Hedge reduce-only IOC acceptance, reliable order/client-
ID lookup, final fill coverage, native status/timestamp mappings and post-position
consistency guarantees. No new endpoints are approved. Secure deployment, peer
authentication, protected approval issuance, credentials, HTTP backend, external
deadline and egress confinement remain separate blocked work. This checkpoint
neither activates them nor authorizes any exchange operation.
