# Checkpoint 3, Stage 2: offline final GET boundary

**No networking is enabled.** [The transport](../testnet_transport.py) contains
no HTTP adapter, socket send or enable flag. `_send_http` always raises. All
passing HTTP scenarios use invented responses returned by a test-only mock
beneath the final validation boundary. TLS connectivity, DNS, actual endpoint
support, credentials and account identity have not been verified.

## Bounded request lifecycle

1. The existing verifier requires the explicit read-only phrase, the pinned
   CCXT 4.5.90 version and the exact sandbox URL profile. It starts one
   single-use plan owned by the collecting thread.
2. Only these exact requests are planned, in order:

   | Order | Method | Exact URL |
   | --- | --- | --- |
   | 1 | GET | `https://testnet-api.phemex.com/public/products` |
   | 2 | GET | `https://testnet-api.phemex.com/v1/exchange/public/products` |
   | 3 | GET | `https://testnet-api.phemex.com/g-accounts/positions?currency=USDT` |
   | 4 | GET | `https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT` |

3. `request` restricts CCXT API group/path/parameters. The installed Phemex
   `sign` implementation constructs its ordinary unsigned/public or signed/private
   GET. No order-construction or general-purpose CCXT HTTP code is called.
4. An ephemeral fingerprint binds the exact URL, method, body and headers to
   this one pending dispatch. The final transport independently checks the plan,
   calling thread, configuration, URL, headers, method and body. Direct `fetch`
   calls without this pending binding reject.
5. Requests prepares a `PreparedRequest` **without a Session**. The boundary
   validates it again and compares it to the pending signed fingerprint. This
   detects preparation-time URL/header/body changes.
6. The boundary clears the pending binding and consumes the endpoint before
   attempting the send. Each transport object allows one attempt per endpoint
   and four in total, including failures. There is no retry loop or endpoint
   fallback in the verifier. This is **not an independently enforced run-wide
   quota**: copying an active transport duplicates its counter and can permit
   eight mocked sends across the two objects. Ordinary begin/end reuse rejects,
   but copying, replacing objects or starting another process is not run-wide
   consent enforcement. This standalone duplication limitation remains; the
   [Stage 3 offline worker](testnet-readonly-worker.md) adds a separate durable
   approval/quota boundary on its worker-backed path, subject to trusted-store
   and OS deployment requirements. It does not make this object an authority.
7. The send is unconditionally blocked in the shipped code. Tests replace only
   this method. They assert the supplied fixed options: `verify=True`,
   `allow_redirects=False`, `proxies={}`, and a 15-second timeout. There is no
   proof of real TLS behavior until a separately reviewed backend exists.
8. Only an integer HTTP **200** is accepted as a successful GET response, and it
   must still contain a valid Phemex success envelope. Provisional 1xx, other 2xx
   (including 204), 3xx, malformed statuses, redirect history and changed response
   URLs reject. The code never follows Location, including same-origin redirects.
   HTTP 4xx/5xx bodies may reach sanitized CCXT error decoding to preserve numeric
   status/code diagnostics, but cannot establish successful observations even
   when their body says `code=0` or CCXT has no mapped exception for the status.
9. Reports use timestamped final-transport receipts. These receipts are always
   synthetic in this stage. Missing/error/stale/inconsistent evidence remains
   UNKNOWN. Finally, the plan is closed and private comparison data is discarded.

CCXT normally requests products twice during `load_markets`, through its currency
and market parsers. Only the successful first public response is reused during
that one load. It does not authorize another HTTP attempt or reuse a failure.
The companion legacy-products GET is still needed by the pinned market parser.

## Validation and secrecy

URL comparison uses the exact four canonical strings, rather than permissive
URL normalization. Only HTTPS with the exact lowercase hostname and the default
HTTPS port is accepted. Explicit ports, including `:443`, userinfo, fragments,
trailing delimiters, alternate slashes, percent-encoded paths/queries, duplicate
or extra parameters, alternate currency spelling and all other URLs reject.

The method must be exactly GET and the body must be `None`, including rejecting
an empty string or byte body. Public headers must be empty. Private headers must
be exactly the three CCXT-generated lower-case Phemex signing headers, with
bounded token/expiry/signature formats and exact pending-fingerprint equality.
Caller `Host`, Cookie, Authorization, User-Agent and extra headers reject. The
private signature still uses the pinned CCXT HMAC construction; it is tested
offline against an independently calculated expected HMAC with fake credentials.

Both TLS-verification flags and the blocked session's verification setting must
be literally True. All reviewed CCXT proxy settings and callbacks reject before
callbacks are called. The session has no adapters/connection pool, does not
inherit environment proxies or CA overrides, and rejects request/send/GET/POST
entry points. Modified session objects, headers, proxies, redirects or retry
configuration reject. The final boundary repeats configuration checks.

The verifier's `fetch2` routes through its strict request gate. Unbound base
`fetch2` cannot acquire a pending dispatch binding. Unbound base `fetch` reaches
the blocked session instead of an HTTP client. Normal implicit metadata calls
therefore cannot expand the endpoint set. The implementation does not simply
call `super().fetch()`.

No credential loader is added. The verifier still accepts credentials only in
memory; offline fixtures use invented values. There is no raw request/response
logging. Disabling last-request fields alone is insufficient: inherited CCXT
`fetch2` has a separate history-write path on both success and failure. The
verifier now overrides that write hook to discard entries and exposes a
discard-only, zero-capacity history deque; attempts by inherited writers to
install a nonzero cache are discarded. This remains effective with a positive
`fetchHistoryCacheSize`, including direct inherited `fetch2`, cache readers and
cache writers. It is an explicit verifier protection, not an inherent CCXT
guarantee. Deliberate runtime replacement of these protections remains outside
the in-process security boundary.
Reports exclude credentials, signatures, private responses and sensitive account
identifiers. Error text is replaced with fixed messages while existing numeric
status/code diagnostics are retained. Receipt URLs contain only the fixed query.
Secrets necessarily exist briefly in signing/HTTP stack locals and remain visible
to a debugger or code with access to process memory; exception-context suppression
is not memory erasure. Test mocks retain only fictitious signing material.

Quantity reporting separates observation from current-grid compatibility:

- `<leg>.observed_base_quantity` is the exact raw base-asset quantity (BTC units),
  requiring matching fresh private observations and consistent position context.
  It does not depend on public grid freshness and never means that an order is
  acceptable. In Phase A its provenance is still synthetic, not actual exchange
  authentication.
- `<leg>.base_quantity` preserves the existing grid-checked result. It additionally
  requires a reported, valid, consistent native quantity step and public metadata
  less than 300 seconds old. Its combined private/public provenance and sources
  make that dependency explicit. There is no rounding.
- `<leg>.reducible_position` depends on the grid-checked quantity and matching
  Normal status. It is UNKNOWN when the grid is missing, contradictory or stale,
  even if raw private quantity is positive or zero. No missing value means zero.

Thus fresh raw quantity and private Hedge Mode/leverage/margin/status observations
can remain VERIFIED in fixtures while current grid-dependent requirements become
UNKNOWN. Raw field crosscheck equality does not attest to current instrument-grid
compatibility. Every report still has no execution authority.

## In-process limits and required isolation

This is a guard against unintended CCXT transport paths, **not an airtight
security sandbox against arbitrary Python code in the same process**. An owner
can replace methods/attributes, invoke inherited unbound methods with a different
session, construct another HTTP client, alter the plan/receipt objects through
runtime replacement, or call sockets outside this module. Private names and
fingerprints do not prevent that. Synthetic receipts are assertions within an
offline test; they provide neither authenticated evidence nor execution authority.

There is no working HTTP backend to exploit in this module today. Nevertheless,
replacing `_send_http` with `requests.Session.send` or `super().fetch`, or allowing
arbitrary caller-supplied send callbacks, would **not** establish the stronger
isolation required for Phase B. The current verifier object must not be presented
as an independently enforced egress sandbox. The offline tests do not prove that
an arbitrary replacement backend will honor TLS/proxy/redirect options.

Before Phase B, use an isolated, short-lived read-only worker/egress broker with
independent policy enforcement. Deny direct verifier-process egress through OS
controls. The broker must independently validate the same canonical four-request
plan, query/header/body rules, one-attempt budget, order and run binding at its
actual socket/HTTP boundary. It must use verified TLS to the fixed hostname,
disable proxy inheritance, redirects and adapter retries, and avoid a generic
URL-forwarding interface. Network destination enforcement must account for DNS
and certificate validation; an IP allowlist alone is not hostname verification.
The Stage 3 worker implements a separate one-run process interface and durable
offline approvals; see [its trust boundary](testnet-readonly-worker.md). Its real
network backend and OS deployment confinement are **not implemented**.

## Exact future activation work (not implemented or authorized)

There is no current command or flag that activates Phase B. It requires a new
reviewed implementation and separate approval:

1. Deploy and verify the isolated egress design above. Bind the run to exactly
   this plan and fresh consent; do not add POST, risk, account-setting or other
   routes. Retain the ordinary CLI's existing mutation barrier.
2. Add a separate explicit read-only runner, outside fixture/report modules,
   requiring per-run approval. It must load only a deliberately selected
   owner-readable testnet credential profile, never live credentials or generic
   ambient credential overrides. If reusing the current configuration loader,
   pass an explicit testnet profile, `live=False` and an empty `environ` mapping;
   it currently otherwise permits generic exported credentials to override files.
   Do not assume profile labels establish which exchange account owns a key.
3. Connect that runner to the broker through a narrowly typed interface, not an
   injectable arbitrary HTTP callable. Reuse the reviewed CCXT Phemex signer
   within the trusted worker, never a second unrestricted exchange client.
4. Introduce a separate collection path for real transport receipts with actual
   authenticated/public provenance and request timestamps. Do not enable it by
   toggling the existing `synthetic` flag or relabeling fixture receipts.
5. Review the activation diff, broker confinement, credential redaction and
   offline rejection tests before authorizing the four real testnet GETs.

Successful read-only authentication will still not establish stable account
binding, accounting type, native minimum quantity, account restrictions, active
order coverage or reduce-only IOC acceptance. The excluded 500/10500 risk-endpoint
failures remain unexplained; this plan will not contact them. No report may grant
exchange execution authority, consume an order authorization, release an ordinary
journal blocker or change emergency-stop state.

## Offline checks

```bash
.venv/bin/python -B -m unittest tests.test_testnet_transport tests.test_testnet_compatibility -v
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

Transport tests inherit explicit socket-connect and Requests network-denial guards.
Successful paths mock only `_send_http`; no local HTTP server or exchange is used.
They exercise URL/query encodings, headers/bodies, pinned signing, implicit metadata
calls, CCXT/base/session entry points, proxies, TLS flags, redirects, retry/budget
failures, thread ownership, timestamps, synthetic provenance and redaction.
Correction regressions also cover inherited history writes with positive cache
sizes, HTTP 101/199/200/204/301/302/400/500 plus other/malformed statuses, and fresh
private observations paired with public metadata at or beyond its freshness
boundary. The standalone per-object quota remains copyable; the worker-backed
durable quota is tested separately and is not yet deployed with OS confinement.
