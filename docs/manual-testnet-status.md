# Local manual Phemex Testnet status and dynamic market catalog

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

`BTCUSDT` remains the default. Select any supported active USDT-settled linear
perpetual using its Phemex-native or CCXT unified symbol:

```bash
.venv/bin/python -B testnet_readonly.py \
  --status --symbol ETHUSDT --env-file manual-testnet.env --authorize-testnet-read
.venv/bin/python -B testnet_readonly.py \
  --status --symbol 'SOL/USDT:USDT' --env-file manual-testnet.env --authorize-testnet-read
.venv/bin/python -B testnet_readonly.py \
  --status --markets --env-file manual-testnet.env --authorize-testnet-read
```

These are future authenticated commands requiring explicit operator authorization;
development tests do not run them. The examples do not define a symbol allowlist.
Every collection refreshes the two existing public catalogs, so newly listed
products are discovered without code changes or a persistent metadata cache.
`--markets` lists SUPPORTED, UNSUPPORTED and positively EXCLUDED products using
only the two public GETs; it never requests positions or claims authentication.
The existing explicit profile selection is still required, but public requests
carry no credentials. `--markets` and `--symbol` are mutually exclusive.

The `--status` mode
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
Dynamic discovery adds no endpoints or query parameters: the private USDT response
already contains position rows across symbols. There is no request per symbol.

## Market interpretation and unsupported products

Catalog selection requires exact normalized `swap=True`, `linear=True`,
`settle="USDT"` and `active=True`. Spot, dated futures, inverse contracts,
inactive products and other settlement currencies are excluded. Native listing,
perpetual type, quote/settlement, expiry and normalized classification are
crosschecked. The native inventory is retained before CCXT can omit products or
coalesce duplicate identities. Unknown/malformed product schemas, ambiguous native
or unified aliases, duplicate legacy metadata and contradictory merged fields
are explicitly UNSUPPORTED. Invalid identifiers are displayed as UNKNOWN rather
than copying arbitrary response text into the terminal. Catalog structure errors
make collection incomplete. An unsupported selection stops before private reads.
Unrelated unsupported products do not hide a usable selected product; status
shows their count and `--markets` provides the individual reasons.

Native product identities retain their exact case and authoritative product type.
A spot product named `sOLUSDT` is distinct from a perpetual named `SOLUSDT`:
no prefix is stripped, and the name alone never proves product type. The catalog
prints `type=spot`, `type=perpetual` or an explicit unknown classification based
on metadata. Selection first checks exact native/unified spelling in the
perpetual scope. An exact spot selector rejects instead of falling through to
a similarly spelled perpetual. Case-insensitive convenience matching is used
only when no exact selector exists and there is exactly one unambiguous candidate
in the permitted scope. Unsupported/unknown candidates are retained during that
check. Duplicate exact perpetual IDs or unified aliases still reject; independent
spot identities cannot invalidate an otherwise verified perpetual mapping.

An `Unreviewed product inventory schema` warning does **not** prove another
contract table exists. Older diagnostics flagged any additional field containing
the word `product`, including scalar metadata. Current diagnostics identify the
public field name, JSON shape and bounded count of product-like rows found,
without printing field values or raw responses. For example, the **synthetic**
diagnostic `field=productVersion; shape=string; product_rows=0; coverage=UNKNOWN`
describes a string, not a verified inventory. That example is not a claim about
the field returned by a real Testnet collection.

Unknown product-named fields and unexpected containers remain blocking until
their actual public schemas are reviewed. Nested/keyed product-like rows are
listed as UNSUPPORTED, including containers whose names lack `product`. Inspection
is bounded to depth 8 and 20,000 nodes; row counts are diagnostic observations,
not proof of complete coverage. The pinned adapter's reviewed currency, risk and
leverage tables are auxiliary metadata, not independent product inventories.
A zero detected row count, checksum-like field name or successful CCXT parse
never establishes that no eligible contracts are missing. No unfamiliar nonempty
inventory is automatically enabled. The actual field and element structure must
be reviewed before treating a container as auxiliary configuration.

The observed optional `perpProductsPilot: null` is recognized narrowly: literal
JSON null in a successfully accepted public response discloses no product rows
in that field. Strict decoding never substitutes null for a parse failure, and
HTTP/envelope failures and malformed required inventories still reject. This is
not independent proof that the exchange has no pilot markets. Non-null pilot
values (including unfamiliar arrays/objects) retain the coverage warning; a
nonempty pilot inventory remains unsupported pending schema review.

The operator's subsequent public diagnostic identified the `leverageMargins`
layout as risk-tier configuration. It is recognized as ancillary metadata only
after validating **every** array entry and **every** nested tier, not just the
first three examples or the observed length of 205:

- Each entry has exactly `index_id` (integer) and `items` (array).
- Each tier has exactly `notionalValueRv` (integer), `maxLeverage` (finite Decimal
  decoded from a JSON decimal number), `maintenanceMarginRateRr` (string), and
  `maintenanceAmountRv` (string).

No coercion occurs: booleans are not integers; numeric strings, floats or JSON
integer encodings are not silently substituted for the reviewed Decimal field.
Missing/extra fields, incompatible types, mixed structures or unexpected nesting
preserve the blocking coverage UNKNOWN warning. Unknown fields are not dropped;
product-like data within an invalid structure is still exposed as UNSUPPORTED.
Duplicate JSON keys reject before this validator runs.

Empty outer/tier arrays have no records requiring interpretation and match the
reviewed array shape. They do not establish zero, unlimited or absent risk limits.
This is structural classification only: string contents, tier ordering, index
relationships and financial correctness are not verified or used as execution
evidence. These records never become markets and never set configured leverage,
available margin, account classification or execution authorization. Other
unreviewed inventories remain blocking independently.

Schema handling is maintained in the [catalog parser](../manual_market_catalog.py)
and its [offline regression tests](../tests/test_manual_market_catalog.py).
Passing offline tests do not independently verify every live row.

The reviewed interpretation is the pinned CCXT 4.5.90 `PerpetualV2` schema:
`sizeRq` is a **base-asset quantity**, and CCXT uses `contractSize=1` for USDT
contracts. An absent native contract size is explicitly labeled as that CCXT
derived convention, not independent native verification. A conflicting size,
unfamiliar unit encoding, legacy `Perpetual` or `PerpetualPilot` schema remains
UNSUPPORTED until separately verified. No assumption is made that every catalog
entry has a reviewed position schema.

Each supported product must have positive, finite native `qtyStepSize` and
`tickSize` matching CCXT amount/price tick precision. Supplied decimal precision
must agree; values outside supported exact arithmetic bounds remain unsupported.
No quantity or price is rounded. For a position, the reported base quantity is
kept distinct from its quantity-grid compatibility and derived contract count.
An off-grid or malformed quantity prevents a complete status result. Raw
observations are not silently rewritten to fit a grid. Minimum order quantity
remains UNKNOWN: a step alone does not establish the exchange minimum.

For multiplier-named bases such as `1000SHIB`, quantities are labeled in the
catalog's base unit, `1000SHIB`; they are not silently converted to SHIB units.
No separate coin multiplier is inferred from the symbol text. Each selected
market uses its own grid and base currency, never BTC's specifications.

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

The report displays the collection UTC window and selected Long/Short base
quantities, position mode, margin mode and configured leverage. Existing field
provenance distinguishes reported fields from documented derivations, such as
configured leverage from absolute `leverageRr` and margin mode from its sign.
An accepted private response establishes only a successful authenticated-read
observation, not stable identity, account verification or order acceptance.
Explicit Long/Short Hedge Mode remains the supported position layout. One-way or
contradictory modes are UNKNOWN, without changing exchange settings. A supported
market with no returned legs still has UNKNOWN positions, not invented zero legs.

Missing legs never become zero. Duplicated legs, inconsistent mode/currency,
contradictory quantities/directions and missing fields remain UNKNOWN. Individual
usable fields may remain visible when another field is unknown. Pagination makes
the report incomplete. Accounting classification, account-wide funds, available
margin, stable account identity and complete order coverage remain UNKNOWN.

- Exit 0: supported, scoped status fields were available and consistent; for
  `--markets`, a structurally complete public inventory was inspected (individual
  unsupported products are still reported).
- Exit 2: no authorization, invalid arguments/configuration, partial/unknown
  status, rejected response or request failure. Missing account-wide information
  does not by itself fail the scoped status check; it remains explicitly unknown.
- Exit 130: interrupted manual status command.

There is no execution authority, mutation capability or ordinary journal access.
Offline fixtures and report labels do not authenticate exchange observations.
Operator-reported Testnet checks have observed public catalog discovery and
explicit BTCUSDT Hedge Mode legs. ETHUSDT and XRPUSDT have no matching returned
legs and remain UNKNOWN. These observations do not establish complete position
coverage, compatibility with other accounts or exchange order acceptance.

## Offline checks

```bash
.venv/bin/python -B -m unittest tests.test_manual_testnet_status tests.test_manual_market_catalog tests.test_discovery_commands -q
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
git diff --check
```

All HTTP adapters are mocked and socket/DNS access is denied in the M1 tests.
No actual credentials, Phemex requests or protected deployment activation are
part of those checks.
