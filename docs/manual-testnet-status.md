# M1: local manual Phemex Testnet status

This is a development/testing command for Ubuntu 22.04 and the existing Python
3.10 virtual environment. It is independent of the protected T1C deployment:
no dedicated user, service, SSH, Ubuntu 24.04 installation, approval database or
production bootstrap is needed. The existing protected runtime remains blocked.
This command cannot authorize trading, change settings or resolve journals.

## Operator prerequisites

Use a key created on **Phemex Testnet**, with read-only permissions and IP
restrictions where available. Do not enable trading, transfer or withdrawal
permissions. Confirm the selected key belongs to the intended Testnet account.
The software cannot prove account ownership or key permissions from a filename.
Do not paste credentials into chat, command arguments, shell history or logs.

The first actual collection still requires separate explicit authorization.
Implementation tests use invented credentials and mocked HTTP adapters only.
Do not run the authenticated commands below merely because offline tests pass.

## Profile-file procedure, after approval

From the repository root, create a private profile without overwriting one:

```bash
(umask 077; cp --no-clobber test.env.example manual-testnet.env)
chmod 600 manual-testnet.env
git check-ignore -- manual-testnet.env
nano manual-testnet.env
```

Check that `git check-ignore` names the file **before entering credentials**.
The existing local ignore rules cover `*.env`; do not stage or force-add a
profile. An explicit private `.env` file outside the checkout is also supported.
Set `PHEMEX_API_KEY` and `PHEMEX_API_SECRET` to the Testnet pair, retaining
`PHEMEX_ALLOW_LIVE=false`. The example's preview-limit settings are accepted but
do not affect status. Files must be owned by the current UID, regular, mode 0600,
one hard link, with no final symlink, and at most 8192 bytes. Duplicate keys,
unknown settings, malformed syntax and live opt-in reject. Values are not
interpolated or executed. File mode ignores all exported credential variables.

Inspect help without reading a profile or constructing an exchange:

```bash
.venv/bin/python -B testnet_readonly.py --help
```

After explicit permission for one real manual collection:

```bash
.venv/bin/python -B testnet_readonly.py \
  --status --symbol BTCUSDT \
  --env-file manual-testnet.env \
  --authorize-testnet-read
```

`BTCUSDT` is the only supported symbol and is the default. The `--status` mode
requires exactly one of `--env-file` and `--credentials-env`. Without the
authorization flag it does not read credentials or construct an exchange.
Unknown flags, abbreviated flags and `--live` reject. Without `--status`, the
previous integration-check behavior remains unchanged, including its full
discovery probes. Use `--status` for this narrowly scoped manual collection.

## Explicit environment alternative

Instead of a file, explicitly select the dedicated Testnet environment pair.
For example, in Bash, after authorization:

```bash
read -r -s -p 'Testnet API key: ' PHEMEX_TESTNET_API_KEY; printf '\n'
read -r -s -p 'Testnet API secret: ' PHEMEX_TESTNET_API_SECRET; printf '\n'
export PHEMEX_TESTNET_API_KEY PHEMEX_TESTNET_API_SECRET
.venv/bin/python -B testnet_readonly.py \
  --status --credentials-env --authorize-testnet-read
unset PHEMEX_TESTNET_API_KEY PHEMEX_TESTNET_API_SECRET
```

Do not use shell tracing. Generic `PHEMEX_API_KEY` / `PHEMEX_API_SECRET`, live
profiles and a partial environment pair are never fallback sources in this mode.
Environment credentials are visible to sufficiently privileged local processes;
this is a trusted local operator tool, not a multi-user secret-isolation service.

## Exact request and interpretation contract

The fixed ordered plan is at most one attempt for each:

1. `GET https://testnet-api.phemex.com/public/products`
2. `GET https://testnet-api.phemex.com/v1/exchange/public/products`
3. `GET https://testnet-api.phemex.com/g-accounts/positions?currency=USDT`

CCXT 4.5.90 provides signing and market parsing. The command calls
`fetch_markets()` explicitly rather than `load_markets()`; the latter would
implicitly fetch currencies and add another request. Existing account position
interpretation is reused; full `AccountDiscovery.discover()` is never called.
No risk, balance, order, history or account-settings endpoint is permitted.

The dedicated manual subclass checks routes and settings before CCXT dispatch;
a separate guarded Requests session checks the **prepared** URL, method, body,
headers and request order immediately before the adapter sends. Initialization
cannot send. Exact literal URLs reject mainnet, alternate ports, encodings,
fragments, duplicate/extra queries and any fourth endpoint. Public requests carry
no authentication headers; private requests carry the three Phemex signing
headers. Direct inherited CCXT fetch calls still reach the session guard.

TLS verification is mandatory. Environment proxies, `.netrc` and CA overrides
are ignored; explicit proxies reject. Redirect following/preparation, automatic
retries, response hooks, cookies, extra headers and HTTP debug output are
disallowed. CCXT's reviewed per-route rate-limit weights are retained. The
per-request connect/read timeout is 15 seconds; it is not a hard wall-clock
deadline for DNS or the complete collection. Responses are streamed with
a 4 MiB cap; non-identity compression rejects. Only HTTP 200 with an expected
successful JSON envelope may support observations. Strict UTF-8 and JSON decoding
reject duplicate object keys at every depth and preserve decimal numeric values
before interpretation. Error reporting uses sanitized diagnostic identifiers,
not exception messages or raw account responses. CCXT request/response history
and last-request storage are disabled. Profile/key references are cleared on
close, without claiming secure memory erasure.

The final request slot is consumed before send. A timeout, failure or uncertain
response stops the collection with no retry. Quota is per process/object, not a
durable production approval. An operator can explicitly launch another command;
do not automatically rerun after failure. No OS sandbox or independent egress
restriction is claimed. Arbitrary code running as this user can bypass its own
Python restrictions; API-key permissions remain an additional external control.

## Output and exit status

The report displays the collection UTC window and selected Long/Short BTC base
quantities, position mode, margin mode and configured leverage. Existing field
provenance distinguishes reported fields from documented derivations, such as
configured leverage from absolute `leverageRr` and margin mode from its sign.
An accepted private response establishes only a successful authenticated-read
observation, not stable identity, account verification or order acceptance.

Missing legs never become zero. Duplicated legs, inconsistent mode/currency,
contradictory quantities/directions and missing fields remain UNKNOWN. Individual
usable fields may remain visible when another field is unknown. Pagination makes
the report incomplete. Accounting classification, account-wide funds, available
margin, stable account identity and complete order coverage remain UNKNOWN.

- Exit 0: supported, scoped status fields were available and consistent.
- Exit 2: no authorization, invalid arguments/configuration, partial/unknown
  status, rejected response or request failure. Missing account-wide information
  does not by itself fail the scoped status check; it remains explicitly unknown.
- Exit 130: interrupted manual status command.

There is no execution authority, mutation capability or ordinary journal access.
Offline fixtures and report labels do not authenticate exchange observations.
Compatibility and response shape on the actual Testnet account remain unverified
until a separately approved real run.

## Offline checks

```bash
.venv/bin/python -B -m unittest tests.test_manual_testnet_status tests.test_discovery_commands -q
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

All HTTP adapters are mocked and socket/DNS access is denied in the M1 tests.
No actual credentials, Phemex requests or protected deployment activation are
part of those checks.
