# Offline position-inventory cross-check

Stage 1 adds a pure analytical API in [position_inventory_crosscheck.py](../position_inventory_crosscheck.py).
It does not collect responses, load credentials, access journals, or authorize
exchange operations. The existing manual collector still has exactly its three
reviewed requests. The separate four-request runtime remains blocked.

## Input contract

`compare_position_inventory(scope, primary, secondary, *, mappings=(), now=...)`
compares two supplied full Phemex response envelopes. Their independent source
slots are fixed:

| Slot | Expected source; no request is performed |
| --- | --- |
| Primary | `GET https://testnet-api.phemex.com/g-accounts/positions?currency=USDT` |
| Secondary | `GET https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT` |

Each `PositionObservation` carries its endpoint, body, HTTP status, collection
start/end timestamps, window identifier, asserted clock convention, and origin.
Bodies may be dictionaries or UTF-8 JSON bytes. Bytes use the existing strict
duplicate-key-rejecting decoder and preserve Decimal numeric values. A dictionary
cannot establish whether an earlier decoder discarded duplicate keys. Use bytes
when testing that boundary. Only an HTTP 200 success envelope with `data.account`
and `data.positions` in the reviewed shape is accepted.

`PositionScope` supplies an exact native symbol and base asset. BTCUSDT, ETHUSDT,
XRPUSDT and other syntactically valid native symbols use the same implementation.
There is no BTC fallback, case folding, unified-symbol conversion, or inferred
base asset. This API does not establish catalog eligibility or instrument sizing;
those supplied scope values remain assertions.

`sizeRq` and `size` are **not interchangeable by default**. Every present size
field needs exactly one `QuantityMapping` bound to its endpoint, native symbol,
base asset, field and input origin. Currently the only supported convention is
explicitly asserted unscaled base-asset quantity. No contract-to-base conversion,
power-of-ten scaling, rounding, float comparison or zero shortcut is provided.
If both fields exist, both need mappings and their Decimal values must agree
exactly. Unknown or unverified scaling should be represented by `Encoding.UNKNOWN`
or by omitting a mapping; the affected quantity remains UNKNOWN.

For a synthetic example, given two invented envelopes `first_body` and
`second_body` and fixed UTC collection times:

```python
from position_inventory_crosscheck import (
    Endpoint, Encoding, PositionObservation, PositionScope, QuantityMapping,
    compare_position_inventory,
)

scope = PositionScope("ETHUSDT", "ETH")
primary = PositionObservation(
    Endpoint.POSITIONS, first_body, first_start, first_end, "synthetic_window",
)
secondary = PositionObservation(
    Endpoint.ACCOUNT_POSITIONS, second_body, second_start, second_end,
    "synthetic_window",
)
# These conventions are invented fixture assertions, not verified Phemex units.
mappings = (
    QuantityMapping(Endpoint.POSITIONS, "ETHUSDT", "ETH", "sizeRq",
                    Encoding.UNSCALED_BASE),
    QuantityMapping(Endpoint.ACCOUNT_POSITIONS, "ETHUSDT", "ETH", "size",
                    Encoding.UNSCALED_BASE),
)
report = compare_position_inventory(
    scope, primary, secondary, mappings=mappings, now=evaluation_time,
)
print(report.render())
```

This is an API example, not an authenticated collection command. All timestamps
must be timezone-aware standard-library fixed-offset datetimes; the engine never
reads the clock itself. Both observations must assert the same UTC clock and
window, be nonfuture and strictly younger than five seconds from collection
start. The primary interval must finish before or at the secondary interval's
start. Overlapping or reversed collections are not accepted as a comparable
window. These checks do not prove clock alignment or an atomic exchange snapshot.

## Binding and comparison

The account currency must explicitly be USDT. Present envelope/account/selected
row scope fields must agree with the selected settlement currency and native
symbol. Existing identity helpers check account/user aliases and row account
bindings; the two observations must report consistent identities. Missing or
conflicting identifiers prevent a bound cross-check. Neither identifiers nor
their internal comparison digests enter the result. Agreement within these two
inputs never establishes stable account identity across runs.

Selected rows require explicit `Hedged` mode and `Long`/`Short` position sides.
One-way, merged, missing or contradictory contexts remain UNKNOWN. Each leg
requires exactly one row. Missing records never mean zero; identical duplicate
rows are ambiguous too. An unassignable row or a nonempty/malformed pagination
cursor blocks interpretation rather than prompting another request. Absence of
a cursor is only a syntactic check, not proof of full inventory coverage.

Quantities, mode, direction, margin mode, configured leverage and position status
are compared individually. Known nonzero quantities must have the correct
direction for their leg. Leverage sign and any explicit cross-margin flag must
agree. A field needs known equal values in both bound observations to yield
agreement; a missing field is never agreement. Independent valid fields can
remain visible when another field is unknown. Any incomplete or conflicting
required field prevents an overall successful cross-check.

The result separates:

1. **Observed selected-position zero:** one usable scoped response explicitly
   reports both unique hedge legs with established base quantities equal to zero.
2. **Corroborated selected-position zero:** both bound fresh inputs explicitly
   report those zeros and all compared fields agree.
3. **Account-wide/future exposure:** always UNKNOWN. Other contracts, missing-row
   semantics, ordinary and conditional orders, and later position changes are
   outside this comparison. Equal positive Long/Short quantities are not zero
   gross exposure.

`False` for either zero flag means explicit nonzero selected-position quantity;
`None` means UNKNOWN. A successful cross-check is not a completeness assertion.
Stale, failed, malformed, duplicate-key, ambiguous, incompatible-scope and missing
observations produce UNKNOWN rather than fabricated certainty. HTTP errors never
cause retries or fallbacks.

## Trust and reporting limits

All origins, clocks, identities and quantity conventions are input assertions.
`Origin.SUPPLIED_RESPONSE` labels already-collected input as unverified; it does
not authenticate collection. Synthetic and supplied origins cannot be mixed into
a successful cross-check. A caller can fabricate mutually consistent inputs, so
even `VERIFIED-IN-MODEL` in rendered output means internal consistency only.
The API never produces trusted execution-policy evidence.

`authenticated_evidence_verified`, `execution_authorized` and
`may_resolve_journal` are always false. Stable account binding, full inventory
completeness and account-wide exposure always remain UNKNOWN. There is no
submission, reconciliation, journal update, request retry or endpoint dispatch.

The sanitized result retains only selected interpreted fields, fixed endpoint
labels, origin enums, normalized timestamps and fixed diagnostic reasons. It
retains no raw response, account/user identifier, balance, signing header or
unrelated position. Input representations withhold the body and window token.
Envelope/row counts, numeric representations and JSON byte size are bounded.
This is not a sandbox for hostile Python objects or an authenticity mechanism for
manually constructed report objects. Frozen inputs are shallow: callers must not
mutate a response dictionary concurrently while comparison is in progress.

## Verification and next boundary

Offline regressions in [test_position_inventory_crosscheck.py](../tests/test_position_inventory_crosscheck.py)
use synthetic envelopes and deny network, credential, exchange and journal access.
They cover dynamic symbols, scope/identity, quantity conventions, Decimal
precision, missing/duplicate/conflicting legs, partial fields, timing, provenance,
strict JSON, response bounds and report redaction.

Real endpoint response compatibility, per-market quantity conventions, omitted
position semantics and full inventory coverage remain unverified. A later,
separately approved integration must establish those conventions from reliable
instrument and endpoint evidence and review any change to the three-request
manual boundary. Stage 1 neither changes that boundary nor claims the two
endpoints establish zero account-wide exposure.
