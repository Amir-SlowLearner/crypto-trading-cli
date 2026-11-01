"""Synthetic IPC identities and temporary databases; all HTTP/socket use denied."""
from copy import copy
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from readonly_ipc import (MAX_REQUEST, MAX_RESPONSE, OfflineIPC, decode_request,
                          decode_response, encode_request)
from readonly_ipc_approvals import (APPLICATION_ID, MODE, PLAN_HASH, PURPOSE, VERSION,
                                    ApprovalError, Binding, BoundApprovalStore,
                                    OfflineAdministratorIssuer, SyntheticPeer)
from readonly_worker_store import ApprovalStore, CONSENT, URLS
from safety import SafetyError
from tests.helpers import OfflineTestCase


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
ADMIN = SyntheticPeer(0)
CALLER = SyntheticPeer(1001)
BINDING = Binding(1001, 'a' * 32, 'b' * 64)


def child_claim(path, storage_id, ticket, queue, crash=None):
    with patch('socket.socket.connect', side_effect=AssertionError('Network prohibited')), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP prohibited')):
        try:
            store = BoundApprovalStore(path, storage_id, clock=lambda: NOW)
            lease = store.claim(ticket.run_id, ticket.token, peer=CALLER, binding=BINDING)
            if crash == 'claim':
                os._exit(21)
            store.reserve_plan(lease)
            if crash == 'reservation':
                os._exit(22)
            store.block(lease)
            if crash == 'reply':
                os._exit(23)
            queue.put('claimed')
        except SafetyError:
            queue.put('rejected')


def child_reuse_lease(path, storage_id, lease, queue):
    with patch('socket.socket.connect', side_effect=AssertionError('Network prohibited')):
        try:
            BoundApprovalStore(path, storage_id, clock=lambda: NOW).reserve_plan(lease)
        except SafetyError:
            queue.put('rejected')
        else:
            queue.put('incorrectly accepted')


class ReadonlyIPCTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bound-readonly.sqlite3'
        self.now = NOW
        self.store = BoundApprovalStore.initialize(self.path, administrator=ADMIN, clock=lambda: self.now)
        self.issuer = OfflineAdministratorIssuer(self.store)
        self.ticket = self.issue()
        self.ipc = OfflineIPC(self.store, BINDING)

    def issue(self, binding=BINDING, **kwargs):
        return self.issuer.issue(administrator=ADMIN, binding=binding, **kwargs)

    def claim(self, ticket=None, **kwargs):
        ticket = ticket or self.ticket
        return self.store.claim(ticket.run_id, ticket.token, peer=kwargs.get('peer', CALLER),
                                binding=kwargs.get('binding', BINDING))

    def snapshot(self):
        return self.store.snapshot(self.ticket.run_id)

    def message(self, **changes):
        value = json.loads(encode_request(self.ticket))
        value.update(changes)
        return json.dumps(value).encode()

    def assert_rejected(self, raw, peer=CALLER):
        response = self.ipc.handle(raw, peer=peer)
        self.assertEqual(decode_response(response), 'REQUEST_REJECTED')
        self.assertEqual(self.snapshot(), {'state': 'APPROVED', 'reserved_slots': ()})

    def test_valid_request_consumes_approval_and_reserves_only_fixed_plan_without_transport(self):
        with patch('testnet_readonly_worker.serve_once', side_effect=AssertionError('Legacy worker')) as worker, \
             patch('operation_journal.OperationJournal', side_effect=AssertionError('Execution journal')) as journal:
            raw = self.ipc.handle(encode_request(self.ticket), peer=CALLER)
        worker.assert_not_called()
        journal.assert_not_called()
        self.assertEqual(decode_response(raw), 'TRANSPORT_DISABLED')
        self.assertEqual(json.loads(raw)['execution_authority'], False)
        self.assertEqual(self.snapshot(), {'state': 'BLOCKED', 'reserved_slots': (0, 1, 2, 3)})
        self.assertEqual(URLS, (
            'https://testnet-api.phemex.com/public/products',
            'https://testnet-api.phemex.com/v1/exchange/public/products',
            'https://testnet-api.phemex.com/g-accounts/positions?currency=USDT',
            'https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT'))

    def test_protocol_roundtrip_hides_token_in_repr(self):
        request = decode_request(encode_request(self.ticket))
        self.assertEqual(request.run_id, self.ticket.run_id)
        self.assertEqual(request.token, self.ticket.token)
        self.assertNotIn(self.ticket.token, repr(request))
        self.assertNotIn(self.ticket.token, repr(self.ticket))

    def test_token_is_only_persisted_as_a_hash(self):
        self.assertNotIn(self.ticket.token.encode(), self.path.read_bytes())

    def test_distinct_storage_namespace_and_all_binding_fields_are_persisted(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA application_id').fetchone()[0], APPLICATION_ID)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], VERSION)
            self.assertEqual(db.execute('SELECT * FROM identity').fetchall(),
                             [(1, PURPOSE, MODE, self.store.storage_id)])
            row = db.execute('SELECT caller_uid,profile_id,release_id,environment,plan_hash,issued,expires '
                             'FROM approvals WHERE run_id=?', (self.ticket.run_id,)).fetchone()
            self.assertEqual(row, (*BINDING.values(), int(NOW.timestamp()), int(NOW.timestamp()) + 300))

    def test_replay_after_blocked_reply_never_reclaims_or_resets(self):
        message = encode_request(self.ticket)
        self.ipc.handle(message, peer=CALLER)
        before = self.snapshot()
        self.assertEqual(decode_response(self.ipc.handle(message, peer=CALLER)), 'REQUEST_REJECTED')
        self.assertEqual(self.snapshot(), before)

    def test_double_claim_is_rejected_before_any_reservation(self):
        self.claim()
        with self.assertRaises(SafetyError): self.claim()
        self.assertEqual(self.snapshot(), {'state': 'CLAIMED', 'reserved_slots': ()})

    def test_malformed_request_after_claim_never_restores_approval(self):
        self.claim()
        before = self.snapshot()
        self.assertEqual(decode_response(self.ipc.handle(encode_request(self.ticket)[:-1], peer=CALLER)),
                         'REQUEST_REJECTED')
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(SafetyError): self.claim()

    def test_wrong_token_or_run_is_rejected_without_consuming_other_approvals(self):
        for changes in ({'token': '0' * 64}, {'run_id': '0' * 32}):
            self.assert_rejected(self.message(**changes))

    def test_missing_untrusted_or_wrong_peer_identity_is_rejected(self):
        for peer in (None, {'uid': 1001}, 1001, 'root', SyntheticPeer(1002), ADMIN):
            self.assert_rejected(encode_request(self.ticket), peer=peer)

    def test_caller_supplied_identity_and_all_operation_overrides_are_rejected(self):
        for name, value in (('uid', 1001), ('peer', {'uid': 0}), ('url', URLS[0]),
                            ('method', 'GET'), ('body', None), ('database_path', str(self.path)),
                            ('credential_path', '/private/key'), ('profile_id', BINDING.profile_id),
                            ('release_id', BINDING.release_id), ('plan_hash', PLAN_HASH),
                            ('environment', 'testnet'), ('command', 'issue'), ('headers', {})):
            with self.subTest(field=name): self.assert_rejected(self.message(**{name: value}))

    def test_wrong_release_profile_or_caller_binding_is_rejected(self):
        for binding in (replace(BINDING, profile_id='c' * 32), replace(BINDING, release_id='d' * 64),
                        replace(BINDING, caller_uid=1002)):
            ticket = self.issue(binding)
            raw = self.ipc.handle(encode_request(ticket), peer=CALLER)
            self.assertEqual(decode_response(raw), 'REQUEST_REJECTED')
            self.assertEqual(self.store.snapshot(ticket.run_id)['state'], 'APPROVED')

    def test_mainnet_and_other_plan_bindings_cannot_be_issued(self):
        for changes in ({'environment': 'mainnet'}, {'environment': None}, {'plan_hash': 'f' * 64},
                        {'caller_uid': True}, {'caller_uid': 0}, {'caller_uid': -1},
                        {'profile_id': '/path/to/key'}, {'release_id': 'unreviewed'}):
            with self.subTest(changes=changes), self.assertRaises(ApprovalError): replace(BINDING, **changes)

    def test_stored_environment_or_plan_tampering_is_not_accepted(self):
        # Trusted-file tampering fixture; no API permits this update.
        for column, value in (('plan_hash', 'f' * 64), ('environment', 'mainnet')):
            ticket = self.issue()
            with sqlite3.connect(self.path) as db:
                db.execute('PRAGMA ignore_check_constraints=ON')
                db.execute('UPDATE approvals SET ' + column + '=? WHERE run_id=?', (value, ticket.run_id))
            raw = self.ipc.handle(encode_request(ticket), peer=CALLER)
            self.assertEqual(decode_response(raw), 'REQUEST_REJECTED')
            self.assertEqual(self.store.snapshot(ticket.run_id)['state'], 'APPROVED')

    def test_expired_approval_is_permanently_blocked(self):
        self.now += timedelta(seconds=300)
        self.assertEqual(decode_response(self.ipc.handle(encode_request(self.ticket), peer=CALLER)), 'REQUEST_REJECTED')
        self.assertEqual(self.snapshot()['state'], 'BLOCKED')
        self.now = NOW
        with self.assertRaises(SafetyError): self.claim()

    def test_future_dated_approval_is_permanently_blocked(self):
        self.now -= timedelta(seconds=1)
        self.assertEqual(decode_response(self.ipc.handle(encode_request(self.ticket), peer=CALLER)), 'REQUEST_REJECTED')
        self.now = NOW
        with self.assertRaises(SafetyError): self.claim()

    def test_lifetime_is_bounded_and_types_are_strict(self):
        for lifetime in (0, -1, 301, True, 1.5, '300', None):
            with self.assertRaises(ApprovalError): self.issue(lifetime=lifetime)

    def test_public_consent_string_and_nonadministrator_cannot_issue_or_initialize(self):
        for peer in (None, CONSENT, {'uid': 0}, CALLER, 0):
            with self.assertRaises(ApprovalError):
                self.issuer.issue(administrator=peer, binding=BINDING)
            path = self.path.with_name('unauthorized.sqlite3')
            with self.assertRaises(ApprovalError): BoundApprovalStore.initialize(path, administrator=peer)
            self.assertFalse(path.exists())
        self.assertFalse(hasattr(self.ipc, 'issue'))
        self.assertFalse(hasattr(self.store, 'issue'))

    def test_unknown_or_issuance_operations_cannot_reach_issuer(self):
        for operation in ('issue', 'renew', 'reset', 'create_order', 'collect', None):
            self.assert_rejected(self.message(operation=operation))

    def test_duplicate_keys_are_rejected_including_identical_and_nested_keys(self):
        raw = encode_request(self.ticket)
        cases = [raw.replace(b'"version":2', b'"version":2,"version":2'),
                 raw.replace(b'"version":2', b'"version":1,"version":2'),
                 raw[:-1] + b',"extra":{"x":1,"x":2}}',
                 raw[:-1] + b',"extra":[{"x":1,"x":1}]}']
        for raw in cases: self.assert_rejected(raw)

    def test_escaped_equivalent_duplicate_json_keys_are_rejected(self):
        raw = encode_request(self.ticket)
        for duplicate in (b'"version":2,"\\u0076ersion":2',
                          b'"version":1,"\\u0076ersion":2'):
            self.assert_rejected(raw.replace(b'"version":2', duplicate))

    def test_exact_request_size_boundary_accepts_1024_and_rejects_1025_bytes(self):
        raw = encode_request(self.ticket)
        at_limit = raw + b' ' * (1024 - len(raw))
        self.assertEqual(MAX_REQUEST, 1024)
        self.assertEqual(len(at_limit), 1024)
        self.assertEqual(decode_request(at_limit), decode_request(raw))
        over_limit = at_limit + b' '
        self.assertEqual(len(over_limit), 1025)
        self.assert_rejected(over_limit)
        # Rejection must not consume the approval; the exact boundary can still
        # reach the offline handler and consume it once, without any dispatch.
        reply = self.ipc.handle(at_limit, peer=CALLER)
        self.assertEqual(decode_response(reply), 'TRANSPORT_DISABLED')
        self.assertEqual(self.snapshot(), {'state': 'BLOCKED', 'reserved_slots': (0, 1, 2, 3)})

    def test_malformed_truncated_oversized_or_nonutf8_messages_fail_closed(self):
        valid = encode_request(self.ticket)
        for raw in (b'', valid[:-1], b'\xff', b'\xef\xbb\xbf' + valid, valid.decode(),
                    b' ' * (MAX_REQUEST + 1), b'[]', b'null', b'{"version":NaN}',
                    b'[' * 1000 + b']' * 10):
            self.assert_rejected(raw)

    def test_invalid_field_types_versions_or_purpose_fail_closed(self):
        for changes in ({'version': True}, {'version': 2.0}, {'version': 1}, {'purpose': 'offline-readonly-worker-v1'},
                        {'run_id': 1}, {'token': None}, {'token': 'A' * 64}, {'run_id': '../escape'}):
            self.assert_rejected(self.message(**changes))

    def test_no_tokens_paths_profiles_tracebacks_or_private_error_text_in_replies(self):
        with patch.object(self.store, 'reserve_plan', side_effect=RuntimeError('PRIVATE_RESPONSE API_SECRET')):
            raw = self.ipc.handle(encode_request(self.ticket), peer=CALLER)
        self.assertEqual(decode_response(raw), 'REQUEST_REJECTED')
        for secret in (self.ticket.token, str(self.path), BINDING.profile_id, BINDING.release_id,
                       'PRIVATE_RESPONSE', 'API_SECRET', 'Traceback'):
            self.assertNotIn(secret, raw.decode())
        self.assertEqual(self.snapshot()['state'], 'BLOCKED')

    def test_response_schema_cannot_claim_authenticated_evidence_or_execution_authority(self):
        raw = self.ipc.handle(encode_request(self.ticket), peer=CALLER)
        for changes in ({'execution_authority': True}, {'execution_authority': 0}, {'outcome': 'VERIFIED'},
                        {'provenance': 'authenticated_exchange'}, {'report': 'private data'},
                        {'version': True}, {'reason': 'COMPLETE'}):
            value = json.loads(raw)
            value.update(changes)
            with self.assertRaises(ApprovalError): decode_response(json.dumps(value).encode())
        for invalid in (raw[:-1], b' ' * (MAX_RESPONSE + 1),
                        raw.replace(b'"version":2', b'"version":2,"version":2')):
            with self.assertRaises(ApprovalError): decode_response(invalid)

    def test_reservation_is_atomic_and_cannot_be_replayed_or_replenished(self):
        lease = self.claim()
        self.store.reserve_plan(lease)
        self.assertEqual(self.snapshot()['reserved_slots'], (0, 1, 2, 3))
        for store in (self.store, copy(self.store), BoundApprovalStore(self.path, self.store.storage_id, clock=lambda: NOW)):
            with self.assertRaises(SafetyError): store.reserve_plan(lease)
            with self.assertRaises(SafetyError): store.claim(self.ticket.run_id, self.ticket.token, peer=CALLER, binding=BINDING)

    def test_failed_plan_transaction_does_not_partially_reserve_or_unclaim(self):
        lease = self.claim()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER fail_slot BEFORE INSERT ON reservations WHEN NEW.slot=2 "
                       "BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(SafetyError): self.store.reserve_plan(lease)
        self.assertEqual(self.snapshot(), {'state': 'CLAIMED', 'reserved_slots': ()})
        with self.assertRaises(SafetyError): self.claim()

    def assert_commit_failure(self, commit_number, *, after_commit, expected_state, expected_slots):
        real_connect = sqlite3.connect
        commits = failures = 0
        private_error = 'PRIVATE_COMMIT_ACK_FAILURE ' + self.ticket.token

        class CommitFaultConnection(sqlite3.Connection):
            def execute(connection, sql, *args):
                nonlocal commits, failures
                if sql == 'COMMIT':
                    commits += 1
                    if commits == commit_number:
                        failures += 1
                        if after_commit:
                            # The write is durable; only its acknowledgment is
                            # lost. Raising before this call models rollback.
                            super().execute(sql, *args)
                        raise sqlite3.OperationalError(private_error)
                return super().execute(sql, *args)

        def connect(*args, **kwargs):
            return real_connect(*args, factory=CommitFaultConnection, **kwargs)

        with ExitStack() as stack:
            # Assertions after handle() are essential: its fail-closed exception
            # handler would otherwise hide a prohibited action's AssertionError.
            guards = [stack.enter_context(patch(name, side_effect=AssertionError('Action prohibited')))
                      for name in ('socket.socket.connect', 'requests.sessions.Session.request',
                                   'requests.sessions.Session.send', 'ccxt.phemex.request',
                                   'ccxt.phemex.create_order', 'testnet_readonly_worker.serve_once',
                                   'testnet_compatibility.CompatibilityVerifier.collect')]
            stack.enter_context(patch('readonly_ipc_approvals.sqlite3.connect', side_effect=connect))
            raw = self.ipc.handle(encode_request(self.ticket), peer=CALLER)
            for guard in guards:
                guard.assert_not_called()

        self.assertEqual(failures, 1)  # Ensure the intended commit was reached.
        self.assertEqual(decode_response(raw), 'REQUEST_REJECTED')
        self.assertEqual(json.loads(raw)['outcome'], 'BLOCKED')
        self.assertIs(json.loads(raw)['execution_authority'], False)
        for sensitive in (private_error, 'PRIVATE_COMMIT_ACK_FAILURE', self.ticket.token, 'Traceback'):
            self.assertNotIn(sensitive, raw.decode())
        restarted = BoundApprovalStore(self.path, self.store.storage_id, clock=lambda: NOW)
        self.assertEqual(restarted.snapshot(self.ticket.run_id),
                         {'state': expected_state, 'reserved_slots': expected_slots})
        if expected_state == 'APPROVED':
            # No durable claim, reservation or dispatch occurred. The handler
            # did not retry; this is an unconsumed approval, not a restored one.
            self.assertEqual(commits, 1)
            self.assertEqual(expected_slots, ())
        else:
            with self.assertRaises(SafetyError):
                restarted.claim(self.ticket.run_id, self.ticket.token, peer=CALLER, binding=BINDING)

    def test_failure_immediately_before_claim_commit_leaves_approval_unconsumed(self):
        self.assert_commit_failure(1, after_commit=False, expected_state='APPROVED', expected_slots=())

    def test_failure_immediately_after_claim_commit_never_restores_approval(self):
        self.assert_commit_failure(1, after_commit=True, expected_state='CLAIMED', expected_slots=())

    def test_failure_immediately_before_reservation_commit_never_restores_approval(self):
        self.assert_commit_failure(2, after_commit=False, expected_state='BLOCKED', expected_slots=())

    def test_failure_immediately_after_reservation_commit_keeps_all_slots_consumed(self):
        self.assert_commit_failure(2, after_commit=True, expected_state='BLOCKED', expected_slots=(0, 1, 2, 3))

    def test_failure_immediately_before_blocked_commit_never_restores_approval(self):
        self.assert_commit_failure(3, after_commit=False, expected_state='BLOCKED', expected_slots=(0, 1, 2, 3))

    def test_failure_immediately_after_blocked_commit_never_restores_approval(self):
        self.assert_commit_failure(3, after_commit=True, expected_state='BLOCKED', expected_slots=(0, 1, 2, 3))

    def test_expiry_after_claim_prevents_reservation(self):
        lease = self.claim()
        self.now += timedelta(seconds=300)
        with self.assertRaises(SafetyError): self.store.reserve_plan(lease)
        with self.assertRaises(SafetyError): self.claim()

    def test_block_failure_keeps_claim_consumed_after_restart(self):
        with patch.object(self.store, 'block', side_effect=OSError('PRIVATE_FILESYSTEM')):
            raw = self.ipc.handle(encode_request(self.ticket), peer=CALLER)
        self.assertEqual(decode_response(raw), 'REQUEST_REJECTED')
        restarted = BoundApprovalStore(self.path, self.store.storage_id, clock=lambda: NOW)
        with self.assertRaises(SafetyError): restarted.claim(self.ticket.run_id, self.ticket.token, peer=CALLER, binding=BINDING)
        self.assertEqual(self.snapshot(), {'state': 'CLAIMED', 'reserved_slots': (0, 1, 2, 3)})

    def test_cancellation_does_not_restore_approval(self):
        with patch.object(self.store, 'reserve_plan', side_effect=KeyboardInterrupt):
            raw = self.ipc.handle(encode_request(self.ticket), peer=CALLER)
        self.assertEqual(decode_response(raw), 'REQUEST_REJECTED')
        self.assertEqual(self.snapshot()['state'], 'BLOCKED')

    def test_storage_lock_fails_closed_without_consumption_or_automatic_retry(self):
        with sqlite3.connect(self.path, isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            raw = self.ipc.handle(encode_request(self.ticket), peer=CALLER)
            self.assertEqual(decode_response(raw), 'REQUEST_REJECTED')
            db.execute('ROLLBACK')
        self.assertEqual(self.snapshot()['state'], 'APPROVED')  # No claim/send occurred.

    def test_missing_corrupted_or_wrong_identity_storage_is_never_recreated(self):
        missing = self.path.with_name('missing.sqlite3')
        with self.assertRaises(SafetyError): BoundApprovalStore(missing, self.store.storage_id)
        self.assertFalse(missing.exists())
        with self.assertRaises(SafetyError): BoundApprovalStore(self.path, '0' * 32)
        corrupt = self.path.with_name('corrupt.sqlite3')
        # Initialize a private fixture then corrupt its bytes, without chmod.
        fixture = BoundApprovalStore.initialize(corrupt, administrator=ADMIN)
        corrupt.write_bytes(b'NOT_SQLITE')
        with self.assertRaises(SafetyError): BoundApprovalStore(corrupt, fixture.storage_id)
        self.assertEqual(corrupt.read_bytes(), b'NOT_SQLITE')

    def test_existing_storage_is_not_adopted_or_reset(self):
        before = self.path.read_bytes()
        with self.assertRaises(SafetyError): BoundApprovalStore.initialize(self.path, administrator=ADMIN)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cross_purpose_storage_and_tokens_are_rejected_both_directions(self):
        offline_path = self.path.with_name('offline.sqlite3')
        offline = ApprovalStore.create(offline_path, clock=lambda: NOW)
        offline_ticket = offline.issue(CONSENT)
        with self.assertRaises(SafetyError): BoundApprovalStore(offline_path, self.store.storage_id)
        with self.assertRaises(SafetyError): ApprovalStore(self.path, clock=lambda: NOW)
        self.assert_rejected(offline_ticket.message())
        self.assert_rejected(self.message(run_id=offline_ticket.approval_id, token=offline_ticket.token))
        with self.assertRaises(SafetyError): offline.claim(self.ticket.run_id, self.ticket.token)

    def test_database_purpose_is_checked_on_each_transaction(self):
        with sqlite3.connect(self.path) as db: db.execute('UPDATE identity SET mode=?', ('AUTHENTICATED',))
        self.assertEqual(decode_response(self.ipc.handle(encode_request(self.ticket), peer=CALLER)), 'REQUEST_REJECTED')

    def test_duplicate_generated_ids_or_tokens_do_not_replace_approvals(self):
        with patch('readonly_ipc_approvals.secrets.token_hex', side_effect=[self.ticket.run_id, 'f' * 64]):
            with self.assertRaises(SafetyError): self.issue()
        with patch('readonly_ipc_approvals.secrets.token_hex', side_effect=['e' * 32, self.ticket.token]):
            with self.assertRaises(SafetyError): self.issue()
        self.assertEqual(self.snapshot()['state'], 'APPROVED')

    def process(self, crash=None, target=child_claim, lease=None):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        args = (str(self.path), self.store.storage_id, lease, queue) if lease is not None else (
            str(self.path), self.store.storage_id, self.ticket, queue, crash)
        process = context.Process(target=target, args=args)
        process.start()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail('Offline child exceeded test deadline')
        return process, queue

    def test_concurrent_process_claims_allow_only_one_winner(self):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        processes = [context.Process(target=child_claim, args=(str(self.path), self.store.storage_id,
                                                              self.ticket, queue)) for _ in range(2)]
        for process in processes: process.start()
        for process in processes:
            process.join(15)
            if process.is_alive(): process.terminate(); process.join(5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(queue.get(timeout=3) for _ in processes), ['claimed', 'rejected'])
        self.assertEqual(self.snapshot(), {'state': 'BLOCKED', 'reserved_slots': (0, 1, 2, 3)})

    def test_crash_after_claim_prevents_restart(self):
        process, _ = self.process('claim')
        self.assertEqual(process.exitcode, 21)
        with self.assertRaises(SafetyError): self.claim()
        self.assertEqual(self.snapshot(), {'state': 'CLAIMED', 'reserved_slots': ()})

    def test_crash_after_reservation_keeps_all_slots_consumed(self):
        process, _ = self.process('reservation')
        self.assertEqual(process.exitcode, 22)
        with self.assertRaises(SafetyError): self.claim()
        self.assertEqual(self.snapshot(), {'state': 'CLAIMED', 'reserved_slots': (0, 1, 2, 3)})

    def test_lost_reply_never_restores_approval(self):
        process, _ = self.process('reply')
        self.assertEqual(process.exitcode, 23)
        with self.assertRaises(SafetyError): self.claim()
        self.assertEqual(self.snapshot()['state'], 'BLOCKED')

    def test_claim_lease_is_not_usable_in_another_process(self):
        lease = self.claim()
        process, queue = self.process(target=child_reuse_lease, lease=lease)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(queue.get(timeout=3), 'rejected')
        self.assertEqual(self.snapshot()['reserved_slots'], ())
