"""Single-message, single-run OFFLINE worker. No HTTP backend or credential loader.

The deployed entry point has a fixed operator-controlled store path. Unit tests
use a trusted bootstrap with a temporary store and patch only _network_send.
"""
import json
from pathlib import Path
import signal
import sys

import requests

from readonly_worker_store import ApprovalStore, URLS, _hex
from safety import SafetyError
from testnet_compatibility import AUTHORIZATION, CompatibilityVerifier, VerificationPhemex
from testnet_transport import (HTTP_SUCCESS_STATUS, PLAN, OfflineGetTransport, _validate_wire,
                               decode_response_json)


STORE_PATH = Path('/var/lib/phemex-readonly-worker/offline-approvals.sqlite3')
MAX_MESSAGE = 1024
MAX_REPLY = 65536
PROVENANCE = 'offline_worker_synthetic'


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SafetyError('Duplicate worker message field')
        result[key] = value
    return result


def _message(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_MESSAGE:
        raise SafetyError('Invalid worker message')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError):
        raise SafetyError('Invalid worker message') from None
    if (type(value) is not dict or set(value) != {'version', 'approval_id', 'token'}
            or type(value['version']) is not int or value['version'] != 1
            or not _hex(value['approval_id'], 32) or not _hex(value['token'], 64)):
        raise SafetyError('Invalid worker message')
    return value


def _reply(complete=False, report=None):
    return json.dumps({'version': 1, 'outcome': 'COMPLETE' if complete else 'BLOCKED',
                       'provenance': PROVENANCE, 'report': report if complete else None}).encode()


def decode_reply(raw, exit_code):
    """Caller-side fail-closed decoding. This result is never execution evidence."""
    if type(exit_code) is not int or exit_code != 0 or type(raw) is not bytes or not 0 < len(raw) <= MAX_REPLY:
        raise SafetyError('Worker failed, crashed or returned an incomplete result')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError):
        raise SafetyError('Malformed worker reply') from None
    if (type(value) is not dict or set(value) != {'version', 'outcome', 'provenance', 'report'}
            or type(value['version']) is not int or value['version'] != 1
            or value['outcome'] != 'COMPLETE' or value['provenance'] != PROVENANCE
            or type(value['report']) is not str or len(value['report']) > MAX_REPLY
            or not value['report'].startswith('BTCUSDT READ-ONLY COMPATIBILITY — NO EXECUTION AUTHORITY\n'
                                             'SYNTHETIC MOCK REPORT — NOT EXCHANGE VERIFICATION\n')):
        raise SafetyError('Worker result is blocked, malformed or has invalid provenance')
    # A string for display, not an execution-policy/journal evidence object.
    return value['report']


class _WorkerTransport(OfflineGetTransport):
    def __init__(self, store, owner):
        super().__init__()
        self._store = store
        self._worker_owner = owner

    def _send_http(self, prepared, *, timeout, verify, allow_redirects, proxies):
        # Independent literal URL list belongs to the worker, not client PLAN or
        # the copied transport's _index. All copies consult one durable approval.
        if (prepared.url not in URLS or timeout != 15 or verify is not True
                or allow_redirects is not False or type(proxies) is not dict or proxies):
            raise SafetyError('Worker rejected unapproved transport scope or configuration')
        _validate_wire(prepared.url, prepared.method, dict(prepared.headers), prepared.body)
        slot = URLS.index(prepared.url)
        self._store.reserve(self._worker_owner, slot)  # Durable COMMIT before dispatch.
        try:
            result = self._network_send(prepared, timeout=15, verify=True, allow_redirects=False, proxies={})
            valid = (type(result) is requests.Response and result.url == URLS[slot]
                     and not result.history and type(result.status_code) is int
                     and result.status_code == HTTP_SUCCESS_STATUS)
            if valid:
                try:
                    body = decode_response_json(result.content)
                    expected = list if slot == 1 else dict
                    valid = (type(body) is dict and type(body.get('code')) in (int, str)
                             and body['code'] in (0, '0') and not body.get('error')
                             and type(body.get('data')) is expected)
                except (ValueError, UnicodeError, TypeError):
                    valid = False
            self._store.record(self._worker_owner, slot, valid)
            return result  # Shared guards/parsers still validate and sanitize it.
        except BaseException:
            # If recording fails too, UNCERTAIN/RUNNING stays consumed and cannot
            # be reclaimed. Process death before this handler has the same rule.
            try:
                self._store.record(self._worker_owner, slot, False)
            except Exception:
                pass
            raise

    def _network_send(self, prepared, *, timeout, verify, allow_redirects, proxies):
        # No callback injection or mode flag. The inherited send ALWAYS raises.
        return super()._send_http(prepared, timeout=timeout, verify=verify,
                                  allow_redirects=allow_redirects, proxies=proxies)


def serve_once(store_path, raw, *, clock=None):
    """Trusted worker bootstrap, not an RPC allowing caller-selected store paths."""
    store = owner = None
    try:
        message = _message(raw)
        store = ApprovalStore(store_path, clock=clock)
        owner = store.claim(message['approval_id'], message['token'])
        if tuple(entry.url for entry in PLAN) != URLS:
            raise SafetyError('Worker and transport plans disagree')
        # Stage 3 has no real credentials: even fully mocked signing is synthetic.
        exchange = VerificationPhemex('OFFLINE_WORKER_KEY', 'OFFLINE_WORKER_SECRET')
        exchange._transport = _WorkerTransport(store, owner)
        report = CompatibilityVerifier(exchange, clock=clock).collect(authorization=AUTHORIZATION)
        rendered = report.render()
        if not store.finish(owner, True):
            raise SafetyError('Worker run incomplete or blocked')
        reply = _reply(True, rendered)
        if len(reply) > MAX_REPLY:
            raise SafetyError('Worker result exceeds protocol bounds')
        return reply, 0
    except BaseException:
        if store is not None and owner is not None:
            try:
                store.finish(owner, False)
            except Exception:
                pass
        return _reply(), 1  # Never echo input, credentials, private data or exception text.


def main():
    # No store-path, URL, method, fixture, credential or CCXT command arguments.
    def deadline(*_):
        raise TimeoutError('Worker deadline exceeded')
    previous_handler = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)  # Includes input/EOF waits; never relaunch on timeout.
    try:
        try:
            if len(sys.argv) != 1:
                reply, code = _reply(), 1
            else:
                reply, code = serve_once(STORE_PATH, sys.stdin.buffer.read(MAX_MESSAGE + 1))
        except BaseException:
            reply, code = _reply(), 1
        try:
            sys.stdout.buffer.write(reply + b'\n')
            sys.stdout.buffer.flush()
        except BaseException:
            return 1
        return code
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


if __name__ == '__main__':
    raise SystemExit(main())
