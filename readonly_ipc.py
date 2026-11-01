"""Bounded B2 IPC codec and OFFLINE handler: no sockets, credentials or transport."""
from dataclasses import dataclass, field
import json

from readonly_ipc_approvals import (ApprovalError, Binding, BoundApprovalStore,
                                    PURPOSE, SyntheticPeer, Ticket, VERSION)
from readonly_worker_store import _hex


MAX_REQUEST = 1024
MAX_RESPONSE = 512
PROVENANCE = 'offline_ipc_foundation'
OPERATION = 'collect_testnet_readonly'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate IPC member')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('Invalid JSON constant')


def _decode(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ApprovalError('IPC message rejected')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ApprovalError('IPC message rejected') from None


@dataclass(frozen=True)
class Request:
    run_id: str
    token: str = field(repr=False)


def decode_request(raw):
    value = _decode(raw, MAX_REQUEST)
    if (type(value) is not dict or set(value) != {'version', 'purpose', 'operation', 'run_id', 'token'}
            or type(value['version']) is not int or value['version'] != VERSION
            or value['purpose'] != PURPOSE or value['operation'] != OPERATION
            or not _hex(value['run_id'], 32) or not _hex(value['token'], 64)):
        raise ApprovalError('IPC message rejected')
    return Request(value['run_id'], value['token'])


def encode_request(ticket):
    if type(ticket) is not Ticket:
        raise ApprovalError('Bound read-only ticket required')
    raw = json.dumps({'version': VERSION, 'purpose': PURPOSE, 'operation': OPERATION,
                      'run_id': ticket.run_id, 'token': ticket.token}, separators=(',', ':')).encode()
    decode_request(raw)
    return raw


def _response(reason):
    return json.dumps({'version': VERSION, 'purpose': PURPOSE, 'outcome': 'BLOCKED',
                       'reason': reason, 'provenance': PROVENANCE,
                       'execution_authority': False}, separators=(',', ':')).encode()


def decode_response(raw):
    """Returns a fixed diagnostic reason only, never exchange evidence/authority."""
    value = _decode(raw, MAX_RESPONSE)
    if (type(value) is not dict
            or set(value) != {'version', 'purpose', 'outcome', 'reason', 'provenance', 'execution_authority'}
            or type(value['version']) is not int or value['version'] != VERSION
            or value['purpose'] != PURPOSE or value['outcome'] != 'BLOCKED'
            or value['reason'] not in ('REQUEST_REJECTED', 'TRANSPORT_DISABLED')
            or value['provenance'] != PROVENANCE or value['execution_authority'] is not False):
        raise ApprovalError('IPC response rejected')
    return value['reason']


class OfflineIPC:
    """Trusted bootstrap owns scope/store. Caller controls only raw message bytes.

    peer is an explicitly synthetic test injection, not real peer authentication.
    No public service/listener or real peer adapter is provided by this step.
    """
    def __init__(self, store, binding):
        if type(store) is not BoundApprovalStore or type(binding) is not Binding:
            raise ApprovalError('Trusted IPC configuration required')
        self._store, self._binding = store, binding

    def handle(self, raw, *, peer=None):
        lease = None
        try:
            request = decode_request(raw)
            if type(peer) is not SyntheticPeer:
                raise ApprovalError('Independently obtained peer required')
            lease = self._store.claim(request.run_id, request.token, peer=peer, binding=self._binding)
            self._store.reserve_plan(lease)
            self._store.block(lease)
            return _response('TRANSPORT_DISABLED')
        except BaseException:
            if lease is not None:
                try:
                    self._store.block(lease)
                except BaseException:
                    pass  # CLAIMED is also permanently consumed; never reset.
            return _response('REQUEST_REJECTED')
