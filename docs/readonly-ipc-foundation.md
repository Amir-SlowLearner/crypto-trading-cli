# B2 Step 1 — offline approval and IPC foundation

This is source-only, offline protocol/storage code. There is no socket listener,
peer-credential syscall, credential loader, CCXT client, HTTP callback, service,
or executable command. All replies are BLOCKED. No exchange observation,
account identity, execution evidence or order authority can be produced.

## Separation and reuse

[readonly_ipc_approvals.py](../readonly_ipc_approvals.py) reuses the existing
[worker store](../readonly_worker_store.py)'s fixed four endpoint literals,
strict hexadecimal validator, token hashing and private-file guard. Its old
issuance/transaction APIs hard-code the synthetic worker's purpose, so inheriting
them would allow inappropriate cross-purpose APIs. The bound store instead has
its own minimal SQLite schema and transactions. No shared module is modified.

| Property | Existing synthetic worker | New B2 foundation |
| --- | --- | --- |
| Purpose | offline-readonly-worker-v1 | authenticated-readonly-intent-v2 |
| Protocol/schema version | 1 | 2 |
| SQLite application ID | 0x524F4754 | 0x52495032 |
| Additional mode | synthetic worker | OFFLINE_FOUNDATION_ONLY |
| Storage identity | existing offline deployment binding | separate random 128-bit identity supplied by trusted bootstrap |

The new namespace models *intent* for a future authenticated read. These records
are still offline artifacts. The mode, synthetic peer identities and fixed reply
provenance prohibit claiming that real administrator/peer authentication or
exchange verification has occurred. A future authenticated worker must not accept
these offline artifacts unchanged; promotion requires an explicit reviewed
protocol/storage/deployment decision, not a runtime enable flag.

The committed Ubuntu B1 manifest/launcher still deploy only the original blocked
worker. They do not import or install these new modules or adopt this database.

## Wire contract

[readonly_ipc.py](../readonly_ipc.py) accepts one complete UTF-8 JSON object of at
most 1024 bytes. The future trusted framing layer must bound reads before calling
it. Extra bytes/objects, truncation, invalid UTF-8, BOM, duplicate keys at every
depth, nonstandard constants, invalid types and unknown fields are rejected.

Request (the hexadecimal values below are placeholders):

```json
{"version":2,"purpose":"authenticated-readonly-intent-v2","operation":"collect_testnet_readonly","run_id":"<32 lowercase hex>","token":"<64 lowercase hex>"}
```

There are no URL, HTTP method, header, body, UID, profile, release, plan, database,
credential path, exchange operation, issuance, renewal or command arguments.
The token is a bearer secret: pass it only through a future protected IPC channel,
never argv, environment, shell logs or reports. Dataclass representations hide it.
Only its SHA-256 hash is persisted. Unique run/token constraints prevent replacement.

Response (at most 512 bytes):

```json
{"version":2,"purpose":"authenticated-readonly-intent-v2","outcome":"BLOCKED","reason":"TRANSPORT_DISABLED","provenance":"offline_ipc_foundation","execution_authority":false}
```

The only other reason is REQUEST_REJECTED. No request fields, paths, profile IDs,
exception text, tracebacks, raw responses or signing material are echoed. The
decoder returns only one of those fixed diagnostic strings, never a report or
an execution-policy object. Response framing is not authentication of a server.

## Trusted identities and approval flow

```text
UNTRUSTED caller: complete message bytes (run ID + secret token only)
       |
       | future protected IPC + independently obtained peer UID (NOT IMPLEMENTED)
       v
Trusted boundary: fixed Binding + expected storage identity
       |-- strict codec and synthetic-peer injection, for offline testing only
       |-- BoundApprovalStore.claim: atomic one-use transition
       |-- reserve_plan: atomic irreversible reservation of four fixed slots
       `-- block: no HTTP implementation, fixed BLOCKED reply

Separate trusted administrator boundary (NOT INSTALLED):
       independently authenticated administrator -> OfflineAdministratorIssuer
       -> binds caller UID, plan, testnet, opaque profile, release, times and run ID
```

SyntheticPeer is deliberately named and immutable. Only the trusted test boundary
injects it; JSON can never supply one. OfflineAdministratorIssuer requires an
injected administrator peer with UID 0. A consent string, integer UID, dictionary,
or non-admin peer is rejected. There is no issuer method in the IPC handler.

**This does not authenticate a real administrator.** An arbitrary Python process
can construct SyntheticPeer(0). The interface is a test seam, not an OS boundary.
Do not expose it to an untrusted caller or connect it unchanged to a real socket.
The future issuer must independently authenticate its administrator; the receiver
must independently obtain kernel peer credentials. CLI isolation also requires
separate OS identities, protected worker code/configuration, and denial of CLI
filesystem access to approval storage. None of that is installed by this step.
Within the intended message interface the caller cannot mint, renew or reset an
approval. Arbitrary trusted-process execution or private database access defeats
that guarantee; underscores on internal methods are not an access-control boundary.

Binding is frozen and strictly validated. Caller UID must be a non-root integer;
environment is exactly testnet. The plan fingerprint covers purpose, environment,
GET and the four literal endpoints. Profile ID is opaque 128-bit hex, never a
credential location. Release ID is the independently approved deployment manifest's
SHA-256. Neither identifier proves stable exchange account identity. Issue time
and expiry are persisted in UTC integer seconds; lifetime is at most 300 seconds.

## State and crash handling

Only explicit administrator fixture provisioning creates a database. Opening
storage never creates/adopts/repairs it. Initialization is exclusive and retains
partial files on failure. The expected random storage identity must come from
trusted configuration, not be automatically learned from a caller's database.

Transactions use BEGIN IMMEDIATE, foreign keys, DELETE journaling and synchronous
EXTRA. Database application ID, schema version, purpose, mode, storage identity
and the pinned process-local device/inode are checked each transaction. Missing,
corrupt, insecure, replaced, wrong-purpose or locked storage fails closed. There
is a bounded SQLite busy wait, not a Python retry loop. No dispatcher exists.

| Boundary | Durable state after successful commit | Recovery |
| --- | --- | --- |
| Issue | APPROVED | One authorized claim until expiry |
| Claim | CLAIMED, nonce hash and process owner | Spent, even with no reservations |
| Reserve plan | Four unique slots 0–3, atomically | All budget slots spent; never interpreted as requests sent |
| Block | BLOCKED | Terminal, never reusable |
| Crash/lost reply | CLAIMED or BLOCKED | No restart, resumption, renewal or reset API |

The entire four-slot budget is reserved conservatively up front in this step;
there is no per-request dispatch or successful-observation API. These rows must
not later be treated as reusable HTTP permits or authenticated receipts. The
existing worker's per-request reservation-before-dispatch, uncertainty handling,
exact URL checks and unconditional network block remain untouched. A future
integration needs separate review of request-attempt and observation recording.

Malformed requests are rejected before a claim, so they cannot burn an unrelated
approval. Wrong tokens/UIDs/bindings also fail before consuming another approval.
An authentic matching expired/future-dated approval is permanently blocked. Once
CLAIMED, failures, malformed subsequent messages, cancellation, partial storage
failure and lost replies never restore APPROVED. If blocking cannot persist,
CLAIMED remains non-reclaimable. A failed transaction before claim commits causes
no dispatch; it does not imply that a request was sent or authorize auto-retry.

## Remaining gates

Real administrator authentication, Linux peer credentials, protected IPC framing,
root-controlled issuer/worker deployment, separate storage receipts, credential
profile administration, external hard deadlines and OS network confinement remain
unimplemented. Actual Ubuntu dependencies/install readiness remain blocked by B1.
Wall-clock integrity, filesystem/fsync guarantees, protected ancestors and trusted
worker code are assumptions. The file guard does not remove filesystem TOCTOU;
worker/root database edits, in-place rollback, copying a database with its trusted
identity, or privileged snapshots defeat local anti-replay guarantees. No external
anti-rollback anchor is supplied. Python immutability is not protection against
arbitrary code execution. Real credentials, networking and all exchange mutations
remain disabled. No B2 approval authorizes order execution or journal changes.

Tests: `python -B -m unittest tests.test_readonly_ipc -v` exercises synthetic peers,
temporary private SQLite files, concurrent spawned processes and crash injection.
Network-denial mocks are installed in parent and child tests. The full existing
suite continues to exercise the unchanged transport, worker, journals and CLI.
