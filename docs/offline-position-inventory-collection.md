# Stage 2: offline position cross-check collection adapter

[position_inventory_collection.py](../position_inventory_collection.py) connects the
existing four-slot in-memory collection plan to the pure
[position inventory comparison engine](../position_inventory_crosscheck.py).
There is no network backend, credential loader, CLI command, worker, IPC call or
journal connection in this adapter. The manual Testnet workflow remains at its
three existing requests, and the separate four-request transport remains blocked.

## Reused boundaries

- The exact `PLAN` in [testnet_transport.py](../testnet_transport.py), in order:
  `GET /public/products`, `GET /v1/exchange/public/products`,
  `GET /g-accounts/positions?currency=USDT`,
  `GET /g-accounts/accountPositions?currency=USDT`, all on
  `https://testnet-api.phemex.com`.
- The inert `MockHttpResponse` model and closed, single-use
  `MockResponseProvider` in [testnet_status_collection.py](../testnet_status_collection.py).
  Only its in-memory transcript consumption is used. No existing status collector,
  CCXT client, Requests session, signing code or transport is invoked.
- The existing strict duplicate-key-rejecting JSON decoder, with exact Decimal
  decoding before interpretation.
- Stage 1's position, identity, scope, quantity and cross-endpoint comparison
  rules, including its overflow-safe fixed-offset UTC normalization and strict
  five-second freshness limit.

The existing status interpreter has BTC-specific instrument assumptions. This
adapter deliberately calls the symbol-independent Stage 1 engine instead of
manufacturing BTC context for another selected contract. No existing module is
modified.

## Input API

```python
from position_inventory_collection import (
    CollectedGetResponse, crosscheck_offline_collection,
)
from position_inventory_crosscheck import PositionScope

# records: a tuple of CollectedGetResponse objects, in the fixed PLAN order.
# mappings: explicit endpoint/symbol/base/field/origin QuantityMapping objects.
# evaluation_time: an explicit timezone-aware datetime; no clock is read here.
report = crosscheck_offline_collection(
    records, PositionScope("XRPUSDT", "XRP"),
    mappings=mappings, now=evaluation_time,
)
print(report.render())
```

Each `CollectedGetResponse` contains:

| Field | Contract |
| --- | --- |
| `response` | Exact existing `MockHttpResponse` object, with label, exact expected URL, raw JSON **bytes**, HTTP status, redirect indicator and optional modeled failure |
| `started_at`, `completed_at` | Asserted collection interval, never fabricated by the adapter |
| `window_id` | Same bounded identifier for all four responses; not rendered |
| `origin` | `Origin.SYNTHETIC` or `Origin.SUPPLIED_RESPONSE`; never authenticated provenance |
| `clock` | Explicit common collection UTC assertion, subject to Stage 1 timestamp restrictions |

The default origin is synthetic. Already-collected responses may be wrapped in
the same inert response model with `Origin.SUPPLIED_RESPONSE`. That label remains
unverified. The adapter does not obtain actual HTTP responses or authenticate
where supplied bytes originated. Caller callbacks, generators, provider objects,
client/runtime objects, subclasses and additional operation arguments are not
accepted as an input backend. Raw bytes are required so adaptation cannot hide
duplicate JSON object keys in an already-parsed dictionary.

At most four records are accepted. More records or malformed record containers
are rejected before provider consumption. Shorter transcripts produce UNKNOWN;
the first missing slot is consumed once in memory and no later slot is requested.
There is no arbitrary URL, query, HTTP method, body, header or transport parameter.
Response URLs are equality-checked against the fixed source; no normalization,
redirect, alternate host/port, encoded path or extra query parameter is accepted.
An expected slot URL in a report is not a claim that a rejected response came
from that URL.

## Sequence and failure behavior

1. Validate the bounded input container and construct one closed mock provider.
2. Begin its one-use transcript session.
3. Consume the next fixed plan slot exactly once, including missing/failed input.
4. Check exact label/URL, no redirect, no modeled transport failure, integer HTTP
   200, byte-size bound, unambiguous JSON success envelope and expected data shape.
5. Validate the asserted origin, common window and timestamps. Every record must
   start less than five seconds before evaluation; its interval must be ordered,
   nonfuture and finish before or at the next record's start. Public observations
   use the same strict window bound as the private observations. Overlap, reversed
   ordering, incomparable clocks, extreme conversion errors and mixed origins
   fail closed. These assertions do not prove an atomic exchange snapshot.
6. Adapt accepted private records to endpoint-specific `PositionObservation`
   objects, preserving native selection, origin, times, window and exact bytes.
7. Stop at the first collection-contract failure; do not skip, retry, fall back,
   request another page or use later records. Always close the provider.
8. Call the existing pure comparison engine with accepted private observations
   and caller-supplied mappings. An independent usable primary observation may
   remain visible when the secondary failed. Cross-check corroboration remains
   UNKNOWN if any required counterpart is missing or unusable.

Failed HTTP statuses, including 500/code 10500, never cause another consumption
attempt. Exception strings and failed raw bodies are withheld. Consumed slots
describe **in-memory processing**, not HTTP dispatch, durable approval or exchange
authentication. Reanalyzing the same transcript is permissible: it produces no
requests and does not create independent run-wide request authorization.

## Interpretation and output

`OfflineInventoryCollection` contains four fixed `CollectionSlot` summaries and
the sanitized Stage 1 `InventoryCrosscheck`. Summaries retain only expected
sources, origin enums, normalized timestamps, consumption/acceptance flags and
fixed reasons. Raw response bodies, account/user identifiers, balances, signed
headers, window identifiers and unrelated position rows are not retained in the
report. Input record representations withhold the input body and window.

`collection_complete` means four valid envelope/collection assertions were
processed; **it does not mean complete position or account inventory**. Empty
positions can satisfy the envelope shape while their missing legs remain UNKNOWN.
`crosscheck_complete` additionally requires Stage 1's complete matching-field
comparison. Duplicate legs, missing identities, settlement/symbol conflicts,
one-way or contradictory position contexts and unknown quantities prevent it.

Both public responses are checked for their success-envelope/data shape only.
This adapter does not validate a catalog, bind selected instrument specifications,
establish contract size or infer quantity conventions from public metadata.
Even an empty public metadata dictionary cannot supply a quantity mapping. This
limited envelope contract is reported explicitly; instrument interpretation is
not relabeled as verified.

Every present `sizeRq`/`size` field still needs an explicit `QuantityMapping` for
its exact endpoint, native symbol, base asset, origin and supported encoding.
There is no default mapping, contract conversion or float rounding. A mapping is
an assertion about reviewed units, not proof that a review occurred. Unknown or
unverified conventions must be omitted or marked unknown. Future real collection
integration requires separate compatibility review of those conventions.

Observed zero, corroborated selected-position zero and unknown account-wide
exposure remain distinct, as documented in
[Stage 1](./position-inventory-crosscheck.md). Successful input consistency cannot
authenticate a response, prove stable account binding, establish complete order
coverage or determine buying power. `authenticated_evidence_verified`,
`execution_authorized` and `may_resolve_journal` are always false. An entirely
fabricated but consistent transcript may pass the model checks; this is an
analytical API, not a security sandbox against hostile Python code.

## Offline verification and future approval

Run the focused tests without credentials or network access:

```sh
.venv/bin/python -B -m unittest tests.test_position_inventory_collection tests.test_position_inventory_crosscheck -q
```

[The integration tests](../tests/test_position_inventory_collection.py) deny and
explicitly assert no calls to network, client/session construction, credential,
existing collector/transport, mutation and journal interfaces. Fixtures cover
BTCUSDT/ETHUSDT/XRPUSDT, quantity units and precision, empty/duplicate/conflicting
legs, scope/identity mismatches, incomplete/failed transcripts, exact plan order
and budget, strict JSON, timing/provenance, redaction and import isolation.

Stage 2 is ready for offline adversarial review, not runtime use. Changing the
manual three-request plan, enabling any four-request transport, loading Testnet
credentials or collecting the second private response requires separate explicit
approval and a reviewed runtime integration. Actual endpoint schemas, per-market
units and missing-position completeness semantics remain unverified. No protected
deployment files, endpoint allowlists, execution gates or journals are changed.
