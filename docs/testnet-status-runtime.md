# T1C Step 2C — run-bound runtime, production bootstrap blocked

**No real request or credential loading is authorized or enabled.** This is code
and mocked-network testing only. No command, environment flag, approval phrase,
offline ticket or constructed context enables real I/O. `_require_step2_activation`
now validates stage-specific durable authorization. A SEPARATE explicit
`_require_protected_bootstrap` always raises before credential-file I/O or
creation of an HTTP-capable session. No protected bootstrap or OS confinement
is established by this checkpoint, including when a prepared approval is valid.
The old `OfflineGetTransport._send_http` remains unchanged and always blocked.

No service, worker, IPC listener, approval issuer, operating-system identity,
firewall or installation is activated. Existing offline approval purposes are
unchanged. No journal, emergency stop or trading permission is accessed.

## Interface and data flow

[testnet_status_runtime.py](../testnet_status_runtime.py) provides:

- `TestnetCredentialProfile`: explicit immutable Testnet profile with hidden
  credential/profile-ID representations. Labels do not prove key permissions or
  account ownership. No environment values are inherited.
- `parse_testnet_profile(raw_bytes)`: pure strict JSON decoder. Tests use only
  invented credential bytes. No filesystem or environment access occurs.
- `ReadRunContext`: expected configuration plus the existing durable lease.
  Every check verifies run/storage identity, profile/release/worker/plan binding,
  current UID/PID and expiry. Constructing an object does not authenticate it.
- `load_testnet_profile(path, context=...)`: requires the exact configured path,
  active lease and matching decoded profile ID. The separate bootstrap block
  still denies before credential-file access. Existing descriptor-relative,
  no-follow directory traversal and bounded reads are preserved.
- `TestnetReadOnlyRuntime(profile, context=...).collect(clock=...)`: one-shot fixed-plan
  runtime feeding the existing status engine through a separate exact-type seam.
  Unpatched, the first send is blocked and collection reports failure. Tests
  mock ONLY the bootstrap boundary **and** the HTTP send. Run-bound checks and
  SQLite consumption remain active; no test enables sockets. A runtime without
  context cannot send even when the bootstrap boundary is mocked.

The existing T1B `collect_status` still accepts only its closed in-memory provider.
It does not accept this runtime or an arbitrary callback. `collect_runtime_status`
accepts only the exact runtime type. Both produce display-only `StatusReport`
objects whose `synthetic` property stays true and whose authenticated-evidence,
execution-authority and journal-resolution properties stay false.

The CLI remains:

```bash
.venv/bin/python -B testnet_status.py status --mock-demo
```

There is **no enabled authenticated-read CLI command**. Prepared one-shot entry
points are described in [the supervisor contract](testnet-status-once.md).
Imports and CLI help do not
construct a client/session, read a profile, or access environment credentials.

```text
Explicit fake Testnet profile (tests only)
    -> one-shot runtime + durable lease/context
    -> existing pinned CCXT Phemex GET signer, without Exchange construction
    -> existing signed/prepared request validation
    -> SQLite UNCERTAIN reservation, random one-use dispatch permit
    -> prepared fingerprint + irreversible SQLite permit consumption
    -> unconditional protected-bootstrap block (still blocked in shipped code)
    -> isolated HTTPS session / streamed bounded response (mocked in tests)
    -> T1B exact JSON decoding and position/specification crosschecks
    -> T1C STEP 1 MOCK TRANSPORT REPORT, always synthetic/non-authoritative
```

## Request and signing contract

Only these exact GET requests can be dispatched, in this order, at most once each:

1. `https://testnet-api.phemex.com/public/products`
2. `https://testnet-api.phemex.com/v1/exchange/public/products`
3. `https://testnet-api.phemex.com/g-accounts/positions?currency=USDT`
4. `https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT`

The runtime reuses the existing fixed plan, `_validate_wire`, signed-request
binding and prepared-request revalidation. It does not call CCXT transport,
`load_markets`, currencies, balances or other implicit methods. Unexpected calls
cannot add a fifth or alternate request. No risk, order, history, transfer or
account-setting route is added. No POST/PUT/PATCH/DELETE method is accepted.

A minimal signer facade invokes the **installed pinned CCXT Phemex `sign` method**
with fixed GET parameters and only the CCXT helper methods that it needs. It is
not an Exchange subclass and exposes no fetch/request/order/settings methods.
It has no network-capable session; its session is the existing `BlockedSession`.
The existing local CCXT version/source integrity check runs before initialization.
No new HMAC algorithm is implemented. Independent tests calculate the expected
HMAC-SHA256 using fake secrets and compare all three private signing headers.
Public requests carry no credentials. The expiry offset remains 60 seconds.

Exact canonical URLs reject mainnet, HTTP, explicit ports, userinfo, fragments,
encoded variants, extra/duplicate query parameters and unexpected bodies/headers.
Before the final send, the exact signed request has been bound and revalidated.
The send consumes both an in-process fingerprint binding and the durable random
dispatch permit. Direct private entry, copied budgets, missing permits and base
budget substitutions reject. Failure/uncertainty poisons the shared in-process
context; constructing arbitrary new objects is not a trusted-worker sandbox.

## HTTP behavior below the blocked gate

The separate backend contains a reviewed Requests implementation, exercised with
mocked `Session.send` and, separately, mocked `HTTPAdapter.send`:

- HTTPS only; TLS verification and hostname checking remain enabled.
- No environment proxy, `.netrc`, generic credentials or CA override inheritance.
  `trust_env=False`; proxies are empty; explicit `verify=True` is passed to send.
- A fresh local session carries no default caller headers, cookies or auth.
  Only an HTTPS adapter is installed, with `max_retries=0`.
- Connect timeout 5 seconds; read timeout 10 seconds; streaming enabled.
- Redirects are not followed. Redirect preparation is also disabled: Requests
  otherwise may prepare a redirect even with `allow_redirects=False`, consuming
  its body before our bounded reader. A regression exercises actual Requests
  session logic over a mocked adapter and rejects an attempted mainnet redirect.
- Global HTTP debug output must be disabled, or sending fails before session
  creation, to avoid lower-level printing of signed headers.
- Responses must have the original URL, no redirect history and a supported
  HTTP status type. Non-identity content encoding rejects rather than allowing
  decompression outside the byte cap. Read chunks are bounded to 4 MiB total.
- Only HTTP 200 plus a valid expected success envelope supports observations.
  Other statuses, malformed JSON, duplicate fields and incomplete responses
  fail closed. A rejected response never replenishes a request slot.
- Returned response data is detached from Requests' signed request, connection
  and response history. Profiles/signing keys are dereferenced on close; this
  is not cryptographic memory erasure or protection from a debugger.

The runtime also checks a monotonic 30-second deadline before sends and between
body chunks. This is a **soft cooperative deadline**, not a hard process limit:
DNS, TLS, reads, scheduling and cleanup can overrun it. An independently enforced
hard deadline is mandatory before actual activation. No automatic restart or
request retry exists. HTTP 500/code 10500 stops collection; no risk fallback occurs.

Pre-response transport rejection may leave HTTP status UNKNOWN; the runtime
does not invent a status or expose raw exception text to improve diagnostics.

## Profile format and file boundary

Future profile JSON has exactly these fields:

```json
{
  "version": 1,
  "purpose": "phemex-testnet-readonly-profile-v1",
  "environment": "testnet",
  "origin": "https://testnet-api.phemex.com",
  "profile_id": "<32 lowercase hexadecimal characters>",
  "api_key": "<operator-provisioned Testnet read-only key>",
  "api_secret": "<operator-provisioned secret>"
}
```

This is a schema example, not a usable profile or instruction to supply secrets
in chat. There is no ambient-variable fallback and no mixing of credential sources.
Extra/duplicate fields, wrong types, invalid encoding, missing credentials,
non-Testnet origin/purpose and oversized profiles reject with fixed messages.

The loader requires an absolute trusted-bootstrap path, protected ancestors
owned by root or the worker identity without group/other write permission, an
owner-only 0700 containing directory, and a regular owner-only 0600 file with one
link. All components are opened without following symlinks, using directory FDs.
Missing/unsafe files fail; nothing is created, repaired or chmodded. Tests read
only fabricated profiles in temporary fixtures. The development checkout is
not a trusted installation boundary; tests model protected ancestor metadata
without changing host permissions.

These checks do not establish root-controlled installation, ACL/mount policy,
stable exchange identity or resistance to privileged replacement/rollback.
An attacker controlling the worker process can access its secret memory. The
Testnet origin is fixed independently of profile labels, but only manual key
provisioning and later exchange observations establish what account a key serves.

## Reports and unresolved evidence

T1C Step 1 output is labeled `T1C STEP 1 MOCK TRANSPORT REPORT`. Collection clocks
can be injected for deterministic tests and are not trusted timing attestations.
The prepared worker has a separate display-only output envelope, but its actual
production origin is unavailable until a reviewed bootstrap/deployment exists.
These runtime reports stay synthetic. Fixture receipts, synthetic clocks and
profile assertions cannot establish real exchange authentication or authority.

All T1B UNKNOWN/completeness rules remain: accounting, stable account binding,
account-wide balance/equity/available margin, native minimum quantity and full
ordinary/conditional order coverage remain unknown. Raw base quantity is distinct
from grid compatibility. Missing legs never become zero. Contradictory fields,
stale reads and mismatched scope remain unavailable. Successful mocked reads
cannot authorize trading, resolve journals or change emergency-stop state.

## Exact gates before a first real four-GET collection

The separate prepared-read store now provides durable one-use claims,
reservation-before-dispatch and atomic dispatch consumption. It does not provide
the independent protected installation or OS egress boundary. Its schema v2
rejects v1 stores without migration. No active approval database is created by
imports, CLI help or ordinary open. Existing offline worker/IPC approvals retain
their incompatible purposes and cannot be promoted to actual-read approvals.

Before the first real run:

1. Separately review this implementation, rejection tests and deployment boundary.
2. Review this Step 2C implementation and approve a separate verified bootstrap
   and activation integration. Root operator checks precede issue/claim; a claimed
   worker lease is required before profile access and every final dispatch.
   No public consent string authenticates an administrator. Do not remove the
   bootstrap block or substitute an environment flag as an activation procedure.
3. Verify protected worker code/dependencies, separate OS identity, private storage,
   sanitized launch environment and independent egress confinement. IP restrictions
   alone do not enforce hostname, URL path or method. Ubuntu installation remains
   blocked by unreviewed target dependency artifacts; this step installs nothing.
4. Manually provision a dedicated **Testnet read-only** API key with no trading,
   settings, transfer or withdrawal rights where Phemex supports those permissions.
   If permissions cannot be restricted as expected, stop for review. Keep it in the
   private profile above, never argv, reports, logs, Git or exported generic vars.
5. Confirm system time, DNS/CA availability and a supervisor-enforced hard deadline,
   with no restart policy. Review the precise worker invocation; no real invocation
   is provided or enabled by this checkpoint.
6. Obtain a separate explicit authorization for **one** collection: the four listed
   GETs only, at most one attempt each, no risk/order/other endpoints and no retries.
   An operator launches one short-lived worker, receives a sanitized partial or
   complete display-only report, and stops. Failures require new review/consent;
   they cannot silently resume, repeat or refill the old run.

Simply patching/removing the activation function is not this procedure and does
not supply the missing authorization, provenance, isolation or durability gates.
Actual credential reads and exchange requests still require separate approval.

## Offline verification

```bash
.venv/bin/python -B -m unittest tests.test_testnet_status_runtime tests.test_testnet_status tests.test_testnet_transport tests.test_testnet_compatibility -q
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

All runtime tests use invented profiles and mock the final HTTP boundary while
denying sockets, full Exchange construction, credential environment loading,
worker/IPC activation and ordinary journal access. Loader tests mock only the
bootstrap boundary and read invented files under valid synthetic SQLite leases.
The complete supervisor/worker integration test models OS identity and HTTP; its
production-branch labels are not evidence that a real exchange was contacted.
