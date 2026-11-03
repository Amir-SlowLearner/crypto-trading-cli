"""Offline fixed-plan transcript adapter; no HTTP, credentials or authority.

The closed in-memory provider enforces the existing four-slot sequence. Inputs
are assertions, including previously collected responses. No provider callback,
runtime, client, credential or request parameters can be supplied here.
"""
from dataclasses import dataclass, field
import re

from account_discovery import Fact, unknown
from position_inventory_crosscheck import (
    Clock, Endpoint, InventoryCrosscheck, Origin, PositionObservation, compare_position_inventory,
    _MAX_AGE, _utc,
)
from testnet_status_collection import (
    MAX_RESPONSE_BYTES, MockFailure, MockHttpResponse, MockResponseProvider,
)
from testnet_transport import HTTP_SUCCESS_STATUS, PLAN, decode_response_json


@dataclass(frozen=True, repr=False)
class CollectedGetResponse:
    response: MockHttpResponse = field(repr=False)
    started_at: object
    completed_at: object
    window_id: str = field(repr=False)
    origin: Origin = Origin.SYNTHETIC
    clock: Clock = Clock.COLLECTION_UTC

    def __repr__(self):
        return 'CollectedGetResponse(input_withheld)'


@dataclass(frozen=True)
class CollectionSlot:
    label: str
    expected_source: str
    consumed: bool = False
    accepted: bool = False
    origin: Origin = Origin.UNKNOWN
    started_at: object = None
    completed_at: object = None
    reason: str = 'Not consumed after a conservative stop'


@dataclass(frozen=True)
class OfflineInventoryCollection:
    slots: tuple
    collection_contract: Fact
    comparison: InventoryCrosscheck

    @property
    def collection_complete(self):
        return self.collection_contract.value is True

    @property
    def crosscheck_complete(self):
        return self.collection_complete and self.comparison.crosscheck.value is True

    @property
    def authenticated_evidence_verified(self):
        return False

    @property
    def execution_authorized(self):
        return False

    @property
    def may_resolve_journal(self):
        return False

    def render(self):
        lines = ['OFFLINE FOUR-RESPONSE POSITION CROSS-CHECK',
                 'No requests sent; no authentication or execution authority.',
                 'Origins, timestamps, scope and quantity mappings are unverified input assertions.']
        for slot in self.slots:
            state = 'ACCEPTED-IN-MODEL' if slot.accepted else 'REJECTED' if slot.consumed else 'NOT-CONSUMED'
            lines.append(slot.label + ': ' + state + '; expected GET ' + slot.expected_source
                         + '; origin=' + slot.origin.value + '; ' + slot.reason)
            lines.append('Collection UTC: ' + (slot.started_at.isoformat() if slot.started_at else 'UNKNOWN')
                         + ' -> ' + (slot.completed_at.isoformat() if slot.completed_at else 'UNKNOWN'))
        lines += ['Four-response collection contract: ' + (
            'SATISFIED-IN-MODEL' if self.collection_complete else 'UNKNOWN (' + self.collection_contract.reason + ')'),
            'Public metadata: envelope checked only; instrument specifications and quantity units NOT verified.',
            self.comparison.render()]
        return '\n'.join(lines)


def crosscheck_offline_collection(records, scope, *, mappings=(), now):
    """Consume only bounded response objects, never a callable/client/runtime.

    An exact tuple of up to four CollectedGetResponse records is required in
    PLAN order. Each record wraps the existing inert MockHttpResponse model;
    SUPPLIED_RESPONSE marks already-collected input as still unverified. JSON
    bytes are required so duplicate fields cannot disappear during adaptation.
    """
    slots = [CollectionSlot(entry.label, entry.url) for entry in PLAN]
    positions = [None, None]

    def result(reason):
        complete = len(slots) == 4 and all(slot.accepted for slot in slots)
        contract = (Fact(True, 'input_assertion', 'fixed four-response transcript', '') if complete
                    else unknown(reason, 'fixed four-response transcript'))
        return OfflineInventoryCollection(tuple(slots), contract,
            compare_position_inventory(scope, *positions, mappings=mappings, now=now))

    if (type(records) is not tuple or len(records) > len(PLAN)
            or any(type(record) is not CollectedGetResponse or type(record.response) is not MockHttpResponse
                   for record in records)):
        return result('Expected only a bounded tuple of collected response records')

    # Reuse the closed, ordered, one-use provider; this is in-memory consumption,
    # not HTTP dispatch or durable authorization. It cannot invoke a backend.
    provider = MockResponseProvider(tuple(record.response for record in records))
    checked = _utc(now)
    window, origin, previous_end = None, None, None
    provider.begin()
    try:
        for index, entry in enumerate(PLAN):
            record = records[index] if index < len(records) else None
            start = _utc(record.started_at) if record is not None else None
            end = _utc(record.completed_at) if record is not None else None
            observed_origin = record.origin if record is not None and type(record.origin) is Origin else Origin.UNKNOWN
            reason = 'Response unavailable; no retry or fallback'
            accepted = False
            try:
                response = provider.get(entry)  # Failed/missing reads consume the slot once.
                if (type(response.label) is not str or response.label != entry.label
                        or type(response.url) is not str or response.url != entry.url
                        or type(response.redirected) is not bool or response.redirected is not False):
                    reason = 'Response endpoint/order/redirect mismatch; no retry or fallback'
                    break
                if response.failure is not None:
                    reason = ('Mock failure; no retry or fallback' if type(response.failure) is MockFailure
                              else 'Malformed failure indication; no retry or fallback')
                    break
                if type(response.status) is not int or response.status != HTTP_SUCCESS_STATUS:
                    reason = 'No accepted HTTP 200 response; no retry or fallback'
                    break
                if type(response.body) is not bytes or len(response.body) > MAX_RESPONSE_BYTES:
                    reason = 'Response encoding or size unreviewed; no retry or fallback'
                    break
                reason = 'Malformed or ambiguous response envelope; no retry or fallback'
                payload = decode_response_json(response.body, exact_numbers=True)
                expected = list if index == 1 else dict
                if (type(payload) is not dict or type(payload.get('code')) not in (int, str)
                        or payload['code'] not in (0, '0') or payload.get('error')
                        or type(payload.get('data')) is not expected):
                    break
                data = payload['data']
                if index >= 2 and (type(data.get('account')) is not dict or type(data.get('positions')) is not list):
                    break
                if (checked is None or start is None or end is None or not start <= end <= checked
                        or checked - start >= _MAX_AGE or previous_end is not None and start < previous_end
                        or record.clock is not Clock.COLLECTION_UTC
                        or type(record.window_id) is not str
                        or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', record.window_id) is None
                        or observed_origin is Origin.UNKNOWN
                        or window is not None and record.window_id != window
                        or origin is not None and observed_origin is not origin):
                    reason = 'Collection freshness, sequence, window or provenance unestablished'
                    break
                if window is None:
                    window, origin = record.window_id, observed_origin
                previous_end = end
                if index >= 2:
                    positions[index - 2] = PositionObservation(
                        Endpoint.POSITIONS if index == 2 else Endpoint.ACCOUNT_POSITIONS,
                        response.body, start, end, window, observed_origin, record.clock, response.status)
                accepted = True
                reason = 'Accepted envelope and collection assertions only; no authentication established'
            except Exception:
                # Never propagate exception strings, response fields or private data.
                pass
            finally:
                slots[index] = CollectionSlot(entry.label, entry.url, True, accepted,
                                              observed_origin, start, end, reason)
            if not accepted:
                break
    finally:
        provider.end()
    return result('Collection incomplete or one or more input assertions rejected')
