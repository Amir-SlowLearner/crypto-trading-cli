# Checkpoint 3, Stage 3: offline one-run read-only worker

**No real networking or exchange authority is enabled.** This stage implements
a separate worker entry point and durable, one-use offline approvals. Successful
tests mock its final network boundary with invented data; the shipped boundary
always raises. The worker has fixed fictitious signing credentials, no credential
loader, no real-transport option, no arbitrary HTTP callback input and no execution
gateway. A completed worker run is not complete account verification.

## Modules and interface

- [readonly_worker_store.py](../readonly_worker_store.py): an independent SQLite
  approval store, with its own application ID, schema version, offline purpose
  and fixed-plan fingerprint. It neither imports nor modifies order journals,
  execution policy, emergency-stop controls or simulation state.
- [testnet_readonly_worker.py](../testnet_readonly_worker.py): a single-message,
  single-run worker. It reuses the existing restricted verifier and CCXT Phemex
  signer; it does not instantiate a second unrestricted exchange client.
- [tests/test_readonly_worker.py](../tests/test_readonly_worker.py): mocked
  transport tests, independent spawned processes, SQLite failures and crashes.

The process entry point accepts no command-line arguments. It reads one bounded
JSON message from stdin, ending at EOF, and writes one bounded JSON reply. A
30-second **soft timeout** raises a catchable, one-shot signal exception during
input, collection or output. It is not an enforced process-lifetime limit:
cleanup or output may continue after the signal has been handled. An independent
supervisor must impose an external hard deadline and terminate the worker before
Phase B deployment. There is no automatic relaunch or resume. The only allowed
message fields are:

```json
{"version": 1, "approval_id": "32 lowercase hex characters", "token": "64 lowercase hex characters"}
```

The displayed placeholders are not usable approvals. Invalid types, duplicate
keys, extra fields, oversized/truncated messages and multiple JSON messages
reject. There are no URL, HTTP method, query, headers, body, budget, reset,
credential, fixture, database-path or CCXT-operation arguments.

The process has a fixed operator-controlled store path:
`/var/lib/phemex-readonly-worker/offline-approvals.sqlite3`. Missing or invalid
stores fail closed; the worker does not create them. The library's `serve_once`
store-path/clock arguments are **trusted bootstrap/test configuration**, not RPC
fields. There is no deployed supervisor or service installed by this stage.

Store creation and `issue` are trusted operator APIs, not worker commands.
Issuing requires explicit offline-model consent and creates a random one-use ID
and token, valid for five minutes. The database stores a token hash, never the
token or exchange credentials. These approvals have **offline-only purpose**;
they are not authorization for actual authenticated GETs or any exchange write.

## Worker-owned plan and durable quota

The worker independently pins these literal URLs and GET method:

1. `https://testnet-api.phemex.com/public/products`
2. `https://testnet-api.phemex.com/v1/exchange/public/products`
3. `https://testnet-api.phemex.com/g-accounts/positions?currency=USDT`
4. `https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT`

Its plan must agree with the existing transport plan. It independently validates
the final prepared request, URL scope and fixed TLS/proxy/redirect options before
reserving a slot. All existing canonical URL, GET, query, body, signing-header,
HTTP-200/envelope, cache-redaction and freshness checks remain in place. Public
products memoization still avoids CCXT's duplicate physical metadata request.

Before accepting any public or private response envelope, the worker uses the
same duplicate-rejecting JSON decoder as the verifier. Repeated object member
names reject at every nesting level, including objects inside arrays and identical
or escaped duplicate names. A response containing both error/success `code`
fields, conflicting `posMode`/quantity fields or repeated instrument precision
fields is never resolved by choosing one value. Malformed or ambiguous bodies
fail before OBSERVED is recorded; the reserved attempt becomes FAILED where
persistence permits, otherwise remains UNCERTAIN. Both leave the approval spent.
There is no retry or quota replenishment, and no raw body or duplicate field/value
is included in a failure reply. The verifier independently validates bodies before
CCXT interpretation, including when it is used without the worker.

An approval is atomically claimed with `BEGIN IMMEDIATE` before any request.
Only APPROVED, unexpired records for this exact offline plan can become RUNNING.
The worker receives an ephemeral owner capability, bound in SQLite to its
process ID; neither owner capability nor database access is supplied by the
caller. Concurrent starts and a second process holding a copied capability
cannot claim/resume the same run.

Every network-bound attempt is separately reserved in SQLite. Its ordinal must
equal the durable next ordinal, and every earlier attempt must be OBSERVED.
The worker commits an UNCERTAIN attempt and increments the counter **before**
calling the still-blocked network boundary. The primary key on approval/ordinal
prevents duplicates; a copied transport or reopened store consults the same
database instead of acquiring a fresh counter. There is no reset or resume API.

The legacy standalone transport's per-object quota can still be copied. This
stage resolves that bypass **on the worker-backed path**, under the trusted-store
assumptions below: copied worker transports produce at most four mocked sends
in total for one approval. Directly constructing a standalone offline transport
does not confer worker approval or access to an HTTP backend.

SQLite uses rollback-journal DELETE mode, foreign keys, private files and
`synchronous=EXTRA` on every connection. Creation fsyncs the containing directory.
Transactions fail closed on contention or I/O errors. Slot consumption is
independent of in-memory transport counters and survives process replacement.

## States, failures and recovery

Approval transitions:

```text
APPROVED -> RUNNING -> COMPLETE
    |           |
    +-> BLOCKED <+
```

An expired valid approval becomes BLOCKED. RUNNING, BLOCKED and COMPLETE records
cannot be claimed again. Completion requires exactly four OBSERVED slots,
unexpired approval and the original owner. Incomplete runs become BLOCKED when
possible. COMPLETE means the four synthetic HTTP-200 success envelopes were
collected, not that identity, accounting or trading compatibility was verified.

Attempt transitions:

```text
absent -> UNCERTAIN -> OBSERVED
                  +-> FAILED
```

OBSERVED means accepted synthetic HTTP/envelope observation only. Neither it nor
FAILED can become UNCERTAIN again. Failure blocks the entire run. There is no
automatic retry, replacement approval, replacement client order ID or fallback.

| Boundary | Durable result / recovery rule |
| --- | --- |
| Invalid/unidentifiable message or failure before claim commits | No authorized run or dispatch occurred. An unclaimed approval may remain APPROVED until expiry; the worker never retries it automatically. |
| Claim committed, before first reservation | RUNNING remains consumed after a crash, even with zero attempts. A new worker rejects the approval. |
| Reservation transaction fails | No HTTP dispatch is allowed. The earlier claim remains spent; cleanup blocks the run where persistence permits. |
| Reservation committed, before/during send | UNCERTAIN remains blocking. A crash or lost response cannot release or replenish the slot. |
| HTTP succeeds, observation commit fails | The committed UNCERTAIN slot remains. No retry is allowed. |
| Observation committed, before next request | RUNNING is still one-use; replacement workers cannot resume it. |
| All observations and completion committed, before reply delivery | COMPLETE remains spent even if the caller sees a crash, timeout or incomplete reply. |

Unresolved RUNNING/UNCERTAIN records are audit evidence, not resumable jobs. Any
future collection requires fresh operator consent and a **new** approval; it is
a new read-only snapshot, not recovery/retry of an old run. There is no automatic
startup recovery that resets old records.

Replies contain only a fixed outcome, offline provenance and, on complete mocked
collection, the existing sanitized synthetic report. Failures never echo input,
tokens, store paths, exception text or private responses. `decode_reply` rejects
nonzero/unexpected exits, blocked/incomplete replies, duplicate keys and fabricated
real provenance. It returns display text, never an execution-evidence model.
Reply framing is not cryptographic peer authentication; deployment must bind the
pipe to the trusted worker. No result can modify order journals or grant exchange
execution authority.

## Three separate trust boundaries

1. **In-process guards.** Existing URL/signing/fingerprint and discard-only cache
   controls protect normal CCXT paths, not arbitrary Python code replacing
   methods, changing globals or constructing another client.
2. **Worker-owned durable authority.** The narrow message interface, atomic claim
   and SQLite slots enforce a run-wide budget across competing worker processes
   and copied transports **provided the caller cannot alter worker code, bootstrap
   configuration or approval storage**. A client is not given Python objects,
   arbitrary code execution, owner capabilities or database-path selection.
3. **OS permissions and network confinement.** These are deployment requirements,
   **not protections installed or proven in this environment**. Tests spawn real
   processes but use the same OS user and temporary stores. Owner-only file checks
   do not stop hostile code running as that owner. No separate service UID,
   seccomp/network namespace, firewall policy or independent egress broker has
   been configured. Real networking therefore remains unconditionally disabled.

Before Phase B, install immutable trusted code/dependencies and a fixed bootstrap
under a worker/operator identity separate from the caller. The operator must
control approval issuance and an owner-only store directory/file (0700/0600);
callers must not be able to invoke the issuer, select another database, delete
records, copy/restore a snapshot, replace code or inject Python startup modules.
Use an authenticated, permission-restricted local process/pipe interface and a
sanitized environment. A fresh worker handles one approval and exits; the
supervisor must enforce a hard process deadline and must not retry failed or
uncertain runs. The in-process soft timeout is not a substitute for that deadline.

Independently deny caller-process egress and constrain the trusted worker's real
HTTP boundary to the same fixed testnet plan, TLS hostname/certificate checks,
no proxies, no redirects and no adapter retries. This stage adds no such HTTP
backend. Profile names or successful authentication must not be treated as proof
of stable account identity. Real testnet-only credentials and genuine collection
receipts require a separate reviewed implementation and explicit approval.

SQLite durability assumes a filesystem that honors locking and fsync. Privileged
database editing, restoring an old APPROVED snapshot, disk corruption/rollback,
or loss of both trusted state and its administrative history can invalidate
one-use guarantees. Do not copy/restore active approval stores as recovery, and
never automatically replace a corrupt/missing store. No external anti-rollback
anchor or hardware-backed monotonic counter is implemented. Deployment changes
must retire old approvals instead of silently making them usable again.

## Offline validation

```bash
.venv/bin/python -B -m unittest tests.test_readonly_worker -v
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

Tests deny socket connections and Requests HTTP calls, including in spawned
children, and patch only the worker's blocked network send for successful cases.
They cover copied transports/stores, process races, repeated approvals, process
ownership, restart/crash boundaries, pre-dispatch durable consumption, failed
reservation/observation commits, SQLite contention, malformed input/output,
unexpected exits, endpoint/method/body/header substitution, quota exhaustion,
expiry, permissions, corruption, redaction and unchanged execution barriers.
Raw-byte regressions also cover duplicate success/error codes, modes, quantities,
quantity steps and price ticks; identical and escaped duplicates; and deeply
nested objects/arrays. Each rejects without OBSERVED acceptance or approval reuse.
