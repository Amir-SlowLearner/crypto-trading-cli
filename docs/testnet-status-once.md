# T1C Step 2C: run-bound one-shot Testnet read, production blocked

**Not activated. No real credentials were read and no exchange request was sent.**
The dedicated `_require_step2_activation` now checks the durable run context at
each worker/profile/runtime/dispatch stage. A SEPARATE
`_require_protected_bootstrap` remains unconditional: it denies production
issue/claim/worker startup, credential-file reads and HTTP session creation.
There is no environment toggle, constructed object, approval record or command
that enables production. The original
`OfflineGetTransport._send_http` remains unconditionally blocked.

## Smallest operator boundary

This preparation uses a root-supervised one-shot command, not the trading CLI,
an IPC listener, the B2 synthetic peer model or the old synthetic worker. No
existing approval purpose is promoted to actual-read authority. No CLI trading
behavior, verifier, deployment manifest, execution policy or journal is changed.

```text
Independent root operator (future protected installation)
    -> explicitly issue one short-lived token into a private descriptor
    -> root supervisor consumes token in SQLite BEFORE launching child
    -> attach exactly one dedicated-UID PID BEFORE writing child's stdin
    -> isolated fixed child command, sanitized environment, no token in argv
    -> child validates durable lease BEFORE attempting profile loading
    -> existing protected Testnet-only profile loader [STILL BLOCKED]
    -> existing signer + exact signed/prepared-request validation
    -> SQLite UNCERTAIN reservation, one-use random dispatch permit
    -> exact prepared-request check, atomic dispatch consumption
    -> independent protected-bootstrap boundary [STILL BLOCKED]
    -> strict JSON / Decimal / scope crosschecks, display-only output
    -> bounded response to supervisor, no ordinary journal access

Independent parent: 30-second deadline, SIGKILL process group, no restart
```

The configured service UID must be the dedicated non-root `phemex-ro` identity,
not the CLI user's UID. No user/group, protected directory or installation was
created by this checkpoint. The existing B1 launcher still runs the synthetic
worker; it is deliberately not repurposed.

## APIs and storage separation

[`testnet_read_run_store.py`](../testnet_read_run_store.py) implements a new
SQLite purpose `prepared-testnet-status-read-v1`, application ID and random
storage identity. Only deterministic plan constants and token hashing/validation
helpers are reused from the existing worker store. Its schema and existing
offline worker/IPC databases remain untouched and incompatible with this store.
The prepared-read schema is now version 2. Version 1 stores reject without
automatic migration, adoption, reset or replacement. Tests use fresh temporary
databases; no deployment database is initialized in this checkpoint.

- Explicit initialization is exclusive, under the dedicated worker identity in
  a pre-provisioned private directory. Open never creates, adopts, overwrites,
  restores, migrates or repairs a missing/corrupt database.
- A root operator alone may issue and claim. `os.geteuid()`, not supplied UID
  fields, synthetic peers or consent strings, is checked. Tests patch this OS
  call for invented identities; that is not real administrator authentication.
- Bindings include Testnet, fixed-plan fingerprint, opaque profile ID, independent
  release-manifest digest, worker UID, storage identity, unique run ID and expiry.
  This initial interface has no untrusted caller; its operator is root. It does
  not claim to provide the future CLI/Unix-peer authentication boundary.
- Tokens are 32 random bytes, returned once to the administrator; only SHA-256
  hashes are stored. Lease nonces and per-request dispatch permits are
  independently random and hashed in storage. Dispatch permits are returned only
  after reservation COMMIT and cannot be recovered by a restart/audit API.
  Neither credentials, signed requests, raw responses nor account IDs are stored.
- `BEGIN IMMEDIATE`, uniqueness constraints, `synchronous=EXTRA` and DELETE
  journaling protect claim/reservation transactions across connections/processes.
  Tests exercise concurrent connections and lost COMMIT acknowledgments.
- Each transaction verifies purpose/storage identity and pinned device/inode;
  database/parent modes are 0600/0700, worker-owned, one link. All ancestors are
  checked for symlinks, unsafe ownership/writes and POSIX ACLs. Filesystem mount
  policy and root-controlled installation are external prerequisites.

Lifecycle:

```text
APPROVED -> CLAIMED -> RUNNING -> COMPLETE (all four accepted responses)
                 \          \-> BLOCKED (failure/incomplete run)
                  \-> BLOCKED (launch/attach failure)

Each slot: absent -> UNCERTAIN(consumed=0) -> UNCERTAIN(consumed=1)
                                                     -> OBSERVED or FAILED
```

No backwards, renew, reset, replay, resume or replenishment API exists. A second
claim fails even if no request was sent. A new issuance is rejected while an
APPROVED/CLAIMED/RUNNING record remains outstanding. Dead/expired outstanding
records need separate operator review; there is intentionally no recovery command
which clears them in this checkpoint.

The plan is always, in order, at most one attempt each:

1. `GET https://testnet-api.phemex.com/public/products`
2. `GET https://testnet-api.phemex.com/v1/exchange/public/products`
3. `GET https://testnet-api.phemex.com/g-accounts/positions?currency=USDT`
4. `GET https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT`

No input can add a URL/method/query/body/header or fifth slot. A failed response
stops the collection. Risk endpoints, balances/orders/history routes, settings and
mutations are absent. Existing signing, final canonical wire checks, TLS,
proxy/redirect denial, strict JSON decoding and response bounds are reused.

## Crash and uncertainty guarantees

Claim commits before `Popen`. Child ownership commits before its stdin is sent.
Every slot commits UNCERTAIN before entering the existing HTTP activation gate.
The final HTTP entry requires the same context and prepared fingerprint, then
atomically changes consumed=0 to consumed=1 with the exact random permit.
Only a confirmed consumption COMMIT can proceed to the still-blocked bootstrap
boundary. A reservation alone does not authorize sending. A copied permit or
transport cannot win a second consumption. `dispatch_snapshot` reveals only slot
numbers/consumption booleans; consumed means attempted/uncertain, not HTTP success.
Only a consumed slot with a valid HTTP-200 JSON success envelope can be recorded
OBSERVED and permit the next reservation. The runtime records this directly;
the supervisor no longer replaces the runtime's get() method or its budget.
Account interpretation may still be UNKNOWN or INCOMPATIBLE; response acceptance
is not account verification or execution readiness.

Crash after claim/attach/reservation leaves consumed authority and potentially
UNCERTAIN slots. A request may have reached the exchange even when no response
or completion commit was acknowledged. No exception retries, changes IDs, reuses
the approval or dispatches an alternate endpoint. Lost responses and malformed
child output cannot produce success at the supervisor. If a completion COMMIT
succeeded but its acknowledgment/reply was lost, the DB may say COMPLETE while
the operator receives an uncertainty error. That never makes the token reusable.

Failure before claim COMMIT leaves APPROVED and no dispatch/reservations; no
child was launched. Failure before reservation COMMIT sends nothing; the claim
is already consumed. The collector stops, rather than retrying that reservation.
If final blocking persistence also fails, CLAIMED/RUNNING/UNCERTAIN still block
new claims. SQLite cannot guarantee durability when storage lies, loses committed
data, is restored or replaced by a privileged actor. Never restore/back up an old
database into an active approval identity to recover a run.

A failed/uncertain authorization or consumption write poisons the in-process
context; shallow copies share that failure latch. No send or automatic retry
follows either a before-COMMIT or lost-acknowledgment failure. If consumption
committed, its marker is irreversible through these APIs; if it did not, the
slot still remains reserved and the run claim is consumed. The collector attempts
to mark the run BLOCKED, without retrying the request. If all blocking persistence
also fails, this relies on the trusted worker stopping plus PID binding and the
external deadline: Python objects cannot prevent a compromised worker from
deliberately bypassing its own failure latch or directly modifying storage.

The worker is trusted and owns its storage. This does not withstand arbitrary
code execution as that UID, root compromise, direct trusted-code method calls,
in-place privileged database rollback, malicious mounts or copying the full
private store/configuration. File identity checks cannot solve those threats.
A root issuer crash may leave root-owned SQLite recovery files which the worker
cannot recover; fail closed and review manually, never delete/reinitialize them
automatically.

## Credentials and imports

Only the future fixed protected configuration may choose the opaque profile and path;
the approval/child messages cannot. The existing profile decoder enforces exact
Testnet origin/purpose/fields. Existing descriptor-relative no-follow loading,
0700 directory and 0600 single-link file checks are preserved. No environment,
mainnet profile, argv key, generic exchange client or fallback credential source
is added. The loader checks the expected path/lease before its bootstrap block,
and validates the decoded profile ID against the same run. SQLite authorization
checks may access the prepared-read store; credential-file access remains blocked.

The proposed root configuration is
`/etc/phemex-readonly/testnet-status-once.json`, root-owned 0644 with protected
root ancestors. The paths are fixed in the implementation:

| Path | Future boundary |
| --- | --- |
| `/opt/phemex-readonly-status/python/bin/python3` | Reviewed isolated root-controlled Python/dependencies |
| `/opt/phemex-readonly-status/app/testnet_status_once.py` | Reviewed root-controlled installed package, never checkout |
| `/opt/phemex-readonly-status/release.json` | Independently approved root-controlled release inventory |
| `/var/lib/phemex-readonly-status/approvals.sqlite` | Separate persistent private prepared-read database |
| `/var/lib/phemex-readonly-status/credentials/testnet.json` | Dedicated worker-owned Testnet profile |

These are proposed paths, not installed paths. The bootstrap checks the expected
release digest but does not authenticate a self-modifiable manifest or replace
the required independently reviewed full code/dependency inventory. That new
installation/bootstrap is NOT implemented by this step. B1 target dependencies
remain unapproved. The code is not installation-ready.

The child command is fixed, with `-I -B`, cwd `/`, only `LANG=C.UTF-8`, explicit
worker UID/GID, empty supplementary groups and fresh session/process group. It
needs reviewed packages installed into the protected interpreter; a development
checkout/PYTHONPATH is never a deployed import source. Root-controlled site/.pth
and native dependencies still require installation review. Secrets remain in
memory while signing; Python cannot promise memory erasure or debugger defense.

## Provenance and non-authority

`collect_fixture_run` accepts invented profiles for offline tests, never a
provenance selector. Its receipt stays `mocked_http`, its nested status remains
synthetic, and all execution/journal/trusted-evidence properties are false.
Tests mock ONLY the separate bootstrap boundary AND `Session.send`, while socket connection
and generic HTTP calls remain denied. No fixture claims actual authentication.

Only the protected child entry, after durable lease validation, protected profile
loading and the runtime collection path, emits `protected_testnet_worker` output.
Successful public endpoint rows are labeled `public_testnet_http`; successful
private rows `authenticated_testnet_http`. Failed/unattempted rows are unavailable.
Each row retains its actual fixed request URL, start/end UTC times and HTTP status.
This path remains unreachable in shipping code because the separate protected
bootstrap boundary rejects. Run-bound checks are not mocked in integration tests.

These are transport-origin descriptions, not cryptographic attestations. Python
labels/objects, raw JSON replies, patched test functions and copied receipt text
do not authenticate observations. The supervisor trusts only its future protected
child channel, checks run/release receipt bindings and crosschecks completion
with its own store. Neither model nor
report is accepted as execution-policy evidence, a trading authorization or
journal resolution. No ordinary journal is imported/accessed. Position data does
not prove order acceptance; accounting, stable account identity, account-wide
funds, minimum quantity and full order coverage retain existing UNKNOWN rules.

## External deadline and remaining OS requirements

The independent parent starts a 30-second monotonic deadline before writing the
bounded child message. It reads at most 128 KiB from a pipe and uses SIGKILL on
the child's process group on timeout/error and when cleaning up. It never restarts
the child. Tests run only synthetic local sleep/output programs to exercise this
mechanism, not a service, HTTP client or deployed worker.

This is stronger than the existing cooperative runtime timer, but not an OS
cgroup guarantee: a compromised child could leave its process group, parent death
could leave descendants, and Linux cannot promise instantaneous kill of an
uninterruptible task. A protected root supervisor under a reviewed systemd cgroup
with an independent deadline/kill policy is still required before actual use.
OS identity, filesystem restrictions and independent egress confinement remain
uninstalled and unverified. IP filtering alone cannot enforce hostname/path/GET;
application wire checks remain essential. No firewall, network namespace or
permission was modified here.

## Future operator procedure — NOT enabled or authorized now

Before any commands below can collect: independently review/install the protected
runtime, Python/artifact inventory, separate worker identity, config/state/profile
permissions, state initialization/storage identity, cgroup hard deadline and
network confinement. Review activation changes separately; do not monkeypatch or
remove the current gate as an operational procedure. Obtain explicit approval for
ONE four-GET Testnet collection. Do not combine this approval with trading rights.

Manually create a dedicated Testnet API key with read-only permissions and IP
restrictions where supported; confirm no trading/settings/transfer/withdrawal
rights. Do not paste it into chat, logs, argv, the repository or generic exported
variables. Provision only the private Testnet profile. Check system UTC/CA/DNS.

After those separate gates are approved, the intended command sequence is:

```bash
# All paths below refer to a FUTURE protected installation, not this checkout.
# The operator first opens an owner-only temporary ticket via a protected FD.
# Never use shell tracing, log/tee the ticket, or put its contents in argv.
sudo -n /opt/phemex-readonly-status/python/bin/python3 -I -B \
  /opt/phemex-readonly-status/app/testnet_status_once.py issue > /private/operator/ticket.json
sudo -n /opt/phemex-readonly-status/python/bin/python3 -I -B \
  /opt/phemex-readonly-status/app/testnet_status_once.py run < /private/operator/ticket.json
```

The ticket destination must already be in an operator-owned 0700 directory and
created with 0600 mode/umask077 without symlinks; the command does not safely
provision that path. No reusable public approval phrase is involved. Current
commands reject at the bootstrap boundary before config/store/credential reads or launch.
Never rerun `run` after an error. Inspect sanitized output and durable state; seek
new review if anything is incomplete. No automatic cleanup, state restoration,
approval issuance, retry, service installation or exchange request occurs here.

Offline validation:

```bash
.venv/bin/python -B -m unittest tests.test_testnet_status_once tests.test_testnet_status_runtime -q
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```
