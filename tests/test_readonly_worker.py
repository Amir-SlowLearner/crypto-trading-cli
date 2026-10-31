"""Offline worker/store adversaries. Spawned workers deny sockets and HTTP too."""
from copy import copy
from datetime import timedelta
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import requests

from readonly_worker_store import ApprovalStore, CONSENT, WorkerStoreError
from safety import SafetyError
from testnet_compatibility import VerificationPhemex
from testnet_readonly_worker import MAX_MESSAGE, _WorkerTransport, decode_reply, main, serve_once
from testnet_transport import PLAN
from tests.compatibility_fixtures import NOW, duplicate_json_cases, responses
from tests.helpers import OfflineTestCase


def mocked_http(prepared, **options):
    assert options == {'timeout': 15, 'verify': True, 'allow_redirects': False, 'proxies': {}}
    assert prepared.method == 'GET' and prepared.body is None
    spec = next(item for item in PLAN if item.url == prepared.url)
    path = ('/v1/' if spec.api == 'v1' else '/') + spec.path
    response = requests.Response()
    response.url = prepared.url
    response.status_code = 200
    response.encoding = 'utf-8'
    response._content = json.dumps(responses()[path]).encode()
    return response


def child_run(path, message, queue, crash=None):
    with patch('socket.socket.connect', side_effect=AssertionError('Network prohibited')), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP prohibited')):
        claim = ApprovalStore.claim
        record = ApprovalStore.record
        finish = ApprovalStore.finish
        def claimed(store, *args):
            result = claim(store, *args)
            if crash == 'claim': os._exit(17)
            return result
        def observed(store, owner, slot, success):
            result = record(store, owner, slot, success)
            if crash == 'observed': os._exit(19)
            return result
        def http(*args, **kwargs):
            if crash == 'reserved': os._exit(18)
            return mocked_http(*args, **kwargs)
        def finished(store, *args):
            result = finish(store, *args)
            if crash == 'finished' and result: os._exit(20)
            return result
        with patch.object(ApprovalStore, 'claim', claimed), patch.object(ApprovalStore, 'record', observed), \
             patch.object(ApprovalStore, 'finish', finished), \
             patch.object(_WorkerTransport, '_network_send', side_effect=http):
            raw, code = serve_once(path, message, clock=lambda: NOW)
        queue.put((raw, code))


def child_owner(path, owner, queue):
    with patch('socket.socket.connect', side_effect=AssertionError('Network prohibited')), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP prohibited')):
        try:
            ApprovalStore(path, clock=lambda: NOW).reserve(owner, 0)
        except WorkerStoreError:
            queue.put('rejected')
        else:
            queue.put('incorrectly accepted')


class WorkerTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'approvals.sqlite3'
        self.now = NOW
        self.store = ApprovalStore.create(self.path, clock=lambda: self.now)
        self.approval = self.store.issue(CONSENT)

    def run_mock(self, message=None, backend=mocked_http):
        with patch.object(_WorkerTransport, '_network_send', side_effect=backend) as http:
            raw, code = serve_once(self.path, self.approval.message() if message is None else message,
                                   clock=lambda: self.now)
        return raw, code, http

    def process(self, crash=None):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        process = context.Process(target=child_run,
                                  args=(str(self.path), self.approval.message(), queue, crash))
        process.start()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail('Worker test exceeded deadline; no retry was started')
        return process, queue

    def test_mocked_worker_collects_exact_four_and_returns_only_synthetic_report(self):
        raw, code, http = self.run_mock()
        report = decode_reply(raw, code)
        self.assertIn('SYNTHETIC MOCK REPORT', report)
        self.assertIn('UNKNOWN stable_account_binding', report)
        self.assertEqual([call.args[0].url for call in http.call_args_list], [item.url for item in PLAN])
        self.assertEqual(self.store.snapshot(self.approval.approval_id),
                         {'state': 'COMPLETE', 'attempts': tuple((i, 'OBSERVED') for i in range(4))})
        for secret in (self.approval.token, 'OFFLINE_WORKER_KEY', 'OFFLINE_WORKER_SECRET',
                       '111111111111', '222222222222', 'x-phemex-request-signature'):
            self.assertNotIn(secret, raw.decode())

    def test_shipped_worker_send_remains_unconditionally_blocked(self):
        raw, code = serve_once(self.path, self.approval.message(), clock=lambda: self.now)
        with self.assertRaises(SafetyError): decode_reply(raw, code)
        snapshot = self.store.snapshot(self.approval.approval_id)
        self.assertEqual(snapshot['state'], 'BLOCKED')
        self.assertEqual(snapshot['attempts'], ((0, 'FAILED'),))

    def test_same_approval_cannot_start_a_second_run(self):
        self.run_mock()
        raw, code, http = self.run_mock()
        http.assert_not_called()
        with self.assertRaises(SafetyError): decode_reply(raw, code)
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['state'], 'COMPLETE')

    def test_copied_worker_transports_share_durable_quota(self):
        owner = self.store.claim(self.approval.approval_id, self.approval.token)
        transport = _WorkerTransport(self.store, owner)
        transport.begin(lambda: self.now)
        duplicate = copy(transport)
        exchange = VerificationPhemex('OFFLINE_WORKER_KEY', 'OFFLINE_WORKER_SECRET')
        with patch.object(_WorkerTransport, '_network_send', side_effect=mocked_http) as http:
            for entry in PLAN:
                signed = exchange.sign(entry.path, [entry.api], 'GET', dict(entry.query))
                with transport.signed_request(signed):
                    transport.dispatch(exchange, signed['url'], signed['method'], signed['headers'], signed['body'])
            signed = exchange.sign(PLAN[0].path, [PLAN[0].api], 'GET', {})
            with duplicate.signed_request(signed), self.assertRaises(SafetyError):
                duplicate.dispatch(exchange, signed['url'], signed['method'], signed['headers'], signed['body'])
            self.assertEqual(http.call_count, 4)
        self.assertTrue(self.store.finish(owner, True))

    def test_store_copies_and_reopening_do_not_reset_approval(self):
        owner = self.store.claim(self.approval.approval_id, self.approval.token)
        self.store.reserve(owner, 0)
        for store in (copy(self.store), ApprovalStore(self.path, clock=lambda: self.now)):
            with self.assertRaises(WorkerStoreError): store.claim(self.approval.approval_id, self.approval.token)
            with self.assertRaises(WorkerStoreError): store.reserve(owner, 0)
            with self.assertRaises(WorkerStoreError): store.reserve(owner, 1)
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['attempts'], ((0, 'UNCERTAIN'),))

    def test_concurrent_spawned_workers_get_one_authorized_run(self):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        processes = [context.Process(target=child_run, args=(str(self.path), self.approval.message(), queue)) for _ in range(2)]
        for process in processes: process.start()
        for process in processes:
            process.join(15)
            if process.is_alive(): process.terminate(); process.join(5)
            self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=3) for _ in processes]
        self.assertEqual(sum(code == 0 for _, code in results), 1)
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['attempts'],
                         tuple((i, 'OBSERVED') for i in range(4)))

    def test_spawned_worker_success_cannot_be_restarted_with_same_approval(self):
        process, queue = self.process()
        self.assertEqual(process.exitcode, 0)
        decode_reply(*queue.get(timeout=3))
        process, queue = self.process()
        self.assertEqual(process.exitcode, 0)
        with self.assertRaises(SafetyError): decode_reply(*queue.get(timeout=3))

    def test_crash_after_claim_is_consumed_before_any_dispatch(self):
        process, _ = self.process('claim')
        self.assertEqual(process.exitcode, 17)
        self.assertEqual(self.store.snapshot(self.approval.approval_id), {'state': 'RUNNING', 'attempts': ()})
        _, code, http = self.run_mock()
        self.assertEqual(code, 1)
        http.assert_not_called()

    def test_crash_after_reservation_keeps_uncertain_attempt_blocking_restart(self):
        process, _ = self.process('reserved')
        self.assertEqual(process.exitcode, 18)
        self.assertEqual(self.store.snapshot(self.approval.approval_id),
                         {'state': 'RUNNING', 'attempts': ((0, 'UNCERTAIN'),)})
        _, code, http = self.run_mock()
        self.assertEqual(code, 1)
        http.assert_not_called()

    def test_crash_after_observation_does_not_authorize_remaining_requests(self):
        process, _ = self.process('observed')
        self.assertEqual(process.exitcode, 19)
        self.assertEqual(self.store.snapshot(self.approval.approval_id),
                         {'state': 'RUNNING', 'attempts': ((0, 'OBSERVED'),)})
        _, code, http = self.run_mock()
        self.assertEqual(code, 1)
        http.assert_not_called()

    def test_crash_after_completion_before_reply_does_not_restore_approval(self):
        process, _ = self.process('finished')
        self.assertEqual(process.exitcode, 20)
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['state'], 'COMPLETE')
        _, code, http = self.run_mock()
        self.assertEqual(code, 1)
        http.assert_not_called()

    def test_owner_capability_cannot_be_reused_in_another_process(self):
        owner = self.store.claim(self.approval.approval_id, self.approval.token)
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        process = context.Process(target=child_owner, args=(str(self.path), owner, queue))
        process.start()
        process.join(10)
        if process.is_alive(): process.terminate(); process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(queue.get(timeout=3), 'rejected')
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['attempts'], ())

    def test_dispatch_sees_durable_uncertain_slot_before_mock_http(self):
        def http(prepared, **options):
            snapshot = ApprovalStore(self.path, clock=lambda: self.now).snapshot(self.approval.approval_id)
            slot = [entry.url for entry in PLAN].index(prepared.url)
            self.assertEqual(snapshot['attempts'][-1], (slot, 'UNCERTAIN'))
            return mocked_http(prepared, **options)
        _, code, http = self.run_mock(backend=http)
        self.assertEqual(code, 0)
        self.assertEqual(http.call_count, 4)

    def test_timeout_is_consumed_and_never_retried(self):
        def timeout(*args, **kwargs): raise requests.Timeout('OFFLINE_WORKER_SECRET')
        raw, code, http = self.run_mock(backend=timeout)
        self.assertEqual(code, 1)
        self.assertEqual(http.call_count, 1)
        self.assertNotIn('OFFLINE_WORKER_SECRET', raw.decode())
        _, _, retry = self.run_mock()
        retry.assert_not_called()

    def test_failed_or_malformed_response_blocks_entire_run(self):
        for kind in ('500', 'redirect', 'envelope'):
            approval = self.store.issue(CONSENT)
            def http(prepared, **options):
                result = mocked_http(prepared, **options)
                if kind == '500': result.status_code = 500
                if kind == 'redirect': result.status_code = 302
                if kind == 'envelope': result._content = b'{"code":0,"data":null}'
                return result
            _, code, sender = self.run_mock(approval.message(), http)
            self.assertEqual(code, 1)
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(self.store.snapshot(approval.approval_id)['state'], 'BLOCKED')

    def test_duplicate_json_is_failed_before_observation_and_approval_stays_spent(self):
        for name, path, raw_body in duplicate_json_cases():
            approval = self.store.issue(CONSENT)
            def reply(prepared, **options):
                result = mocked_http(prepared, **options)
                if urlparse(prepared.url).path == path:
                    result._content = raw_body
                return result
            with self.subTest(case=name), \
                 patch('operation_journal.OperationJournal', side_effect=AssertionError('Journal access')) as journal, \
                 patch('ccxt.phemex.create_order', side_effect=AssertionError('Mutation')) as mutation:
                raw, code, sender = self.run_mock(approval.message(), reply)
                journal.assert_not_called()
                mutation.assert_not_called()
                slot = next(index for index, entry in enumerate(PLAN) if urlparse(entry.url).path == path)
                self.assertEqual(code, 1)
                self.assertEqual(sender.call_count, slot + 1)
                with self.assertRaises(SafetyError): decode_reply(raw, code)
                self.assertEqual(self.store.snapshot(approval.approval_id),
                                 {'state': 'BLOCKED', 'attempts': tuple(
                                     (index, 'OBSERVED' if index < slot else 'FAILED')
                                     for index in range(slot + 1))})
                for value in ('OFFLINE_WORKER_KEY', 'OFFLINE_WORKER_SECRET', 'PRIVATE_SENTINEL',
                              approval.token, 'FIXTURE_SECRET', raw_body.decode()):
                    self.assertNotIn(value, raw.decode())
                _, restart_code, retry = self.run_mock(approval.message())
                self.assertEqual(restart_code, 1)
                retry.assert_not_called()

    def test_message_cannot_supply_endpoint_method_parameters_or_budget(self):
        message = json.loads(self.approval.message())
        for key, value in (('url', 'https://api.phemex.com/g-orders'), ('method', 'POST'),
                           ('query', {'extra': '1'}), ('budget', 8), ('reset', True),
                           ('store_path', 'other.sqlite3'), ('fixture', 'success'), ('operation', 'create_order')):
            raw, code, http = self.run_mock(json.dumps(dict(message, **{key: value})).encode())
            self.assertEqual(code, 1)
            http.assert_not_called()
            self.assertNotIn('api.phemex.com', raw.decode())
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['state'], 'APPROVED')

    def test_malformed_duplicate_and_truncated_messages_fail_closed(self):
        for message in (b'', b'{', b'[]', b'null', b'{}', b'\xff', b'{}{}', b'x' * (MAX_MESSAGE + 1),
                        b'{"version":1,"version":1}', self.approval.message()[:-1]):
            _, code, http = self.run_mock(message)
            self.assertEqual(code, 1)
            http.assert_not_called()

    def test_wrong_or_unknown_approval_cannot_claim(self):
        for approval_id, token in (('0' * 32, self.approval.token), (self.approval.approval_id, '0' * 64)):
            message = json.dumps({'version': 1, 'approval_id': approval_id, 'token': token}).encode()
            _, code, http = self.run_mock(message)
            self.assertEqual(code, 1)
            http.assert_not_called()

    def test_approval_expiry_is_durable_and_does_not_reactivate_on_clock_rollback(self):
        self.now += timedelta(seconds=300)
        _, code, http = self.run_mock()
        self.assertEqual(code, 1)
        http.assert_not_called()
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['state'], 'BLOCKED')
        self.now = NOW
        _, _, retry = self.run_mock()
        retry.assert_not_called()

    def test_record_replay_and_budget_exhaustion_are_rejected(self):
        owner = self.store.claim(self.approval.approval_id, self.approval.token)
        for slot in range(4):
            self.store.reserve(owner, slot)
            self.store.record(owner, slot, True)
            with self.assertRaises(WorkerStoreError): self.store.record(owner, slot, True)
        for slot in (0, 3, 4, -1, True):
            with self.assertRaises(WorkerStoreError): self.store.reserve(owner, slot)
        self.assertTrue(self.store.finish(owner, True))
        with self.assertRaises(WorkerStoreError): self.store.finish(owner, True)

    def test_unresolved_attempt_cannot_be_finished_as_complete(self):
        owner = self.store.claim(self.approval.approval_id, self.approval.token)
        self.store.reserve(owner, 0)
        self.assertFalse(self.store.finish(owner, True))
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['state'], 'BLOCKED')
        with self.assertRaises(WorkerStoreError): self.store.claim(self.approval.approval_id, self.approval.token)

    def test_missing_or_corrupt_store_does_not_create_or_replenish_it(self):
        missing = self.path.parent / 'missing.sqlite3'
        self.assertEqual(serve_once(missing, self.approval.message())[1], 1)
        self.assertFalse(missing.exists())
        self.path.write_bytes(b'corrupted')
        self.assertEqual(serve_once(self.path, self.approval.message())[1], 1)
        with self.assertRaises(WorkerStoreError): ApprovalStore.create(self.path)

    def test_insecure_store_or_directory_permissions_are_rejected(self):
        os.chmod(self.path, 0o644)
        self.assertEqual(serve_once(self.path, self.approval.message())[1], 1)
        os.chmod(self.path, 0o600)
        os.chmod(self.path.parent, 0o755)
        try:
            self.assertEqual(serve_once(self.path, self.approval.message())[1], 1)
        finally:
            os.chmod(self.path.parent, 0o700)

    def test_store_has_private_permissions_full_durability_and_no_plaintext_token(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.store._transaction() as db:
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 3)
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertNotIn(self.approval.token.encode(), self.path.read_bytes())

    def test_sqlite_contention_fails_closed_without_dispatch(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute('BEGIN IMMEDIATE')
        try:
            _, code, http = self.run_mock()
        finally:
            connection.close()
        self.assertEqual(code, 1)
        http.assert_not_called()

    def test_failed_reservation_commit_never_reaches_http_and_claim_stays_spent(self):
        original = sqlite3.connect
        class Connection:
            def __init__(self, *args, **kwargs):
                self.inner = original(*args, **kwargs)
                self.inner.row_factory = sqlite3.Row
                self.fault = False
            def execute(self, sql, args=()):
                if sql.startswith('INSERT INTO attempts'): self.fault = True
                if sql == 'COMMIT' and self.fault: raise sqlite3.OperationalError('PRIVATE_SENTINEL')
                return self.inner.execute(sql, args)
            def close(self): self.inner.close()
        with patch('readonly_worker_store.sqlite3.connect', side_effect=Connection):
            raw, code, http = self.run_mock()
        self.assertEqual(code, 1)
        http.assert_not_called()
        self.assertNotIn('PRIVATE_SENTINEL', raw.decode())
        self.assertEqual(self.store.snapshot(self.approval.approval_id), {'state': 'BLOCKED', 'attempts': ()})
        _, _, retry = self.run_mock()
        retry.assert_not_called()

    def test_failed_observation_commit_preserves_uncertainty_without_retry(self):
        original = sqlite3.connect
        class Connection:
            def __init__(self, *args, **kwargs):
                self.inner = original(*args, **kwargs)
                self.inner.row_factory = sqlite3.Row
                self.fault = False
            def execute(self, sql, args=()):
                if sql.startswith('UPDATE attempts SET state'): self.fault = True
                if sql == 'COMMIT' and self.fault: raise sqlite3.OperationalError('PRIVATE_SENTINEL')
                return self.inner.execute(sql, args)
            def close(self): self.inner.close()
        with patch('readonly_worker_store.sqlite3.connect', side_effect=Connection):
            _, code, http = self.run_mock()
        self.assertEqual(code, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['attempts'], ((0, 'UNCERTAIN'),))
        _, _, retry = self.run_mock()
        retry.assert_not_called()

    def test_independent_worker_guard_rejects_prepared_mutations_before_reservation(self):
        owner = self.store.claim(self.approval.approval_id, self.approval.token)
        transport = _WorkerTransport(self.store, owner)
        for url, method, headers, body in (
                ('https://api.phemex.com/public/products', 'GET', {}, None),
                (PLAN[0].url, 'POST', {}, '{}'),
                (PLAN[0].url + '?extra=1', 'GET', {}, None),
                (PLAN[0].url, 'GET', {'Cookie': 'PRIVATE_SENTINEL'}, None),
                (PLAN[0].url, 'GET', {}, 'body')):
            prepared = requests.Request(method, url, headers=headers, data=body).prepare()
            with patch.object(transport, '_network_send') as http, self.assertRaises(SafetyError):
                transport._send_http(prepared, timeout=15, verify=True, allow_redirects=False, proxies={})
            http.assert_not_called()
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['attempts'], ())

    def test_duplicate_approval_record_cannot_be_reissued(self):
        with patch('readonly_worker_store.secrets.token_hex', side_effect=[self.approval.approval_id, self.approval.token]):
            with self.assertRaises(WorkerStoreError): self.store.issue(CONSENT)
        self.assertEqual(self.store.snapshot(self.approval.approval_id)['state'], 'APPROVED')

    def test_main_rejects_arguments_and_input_failure_without_leaking_errors(self):
        for args in (['worker', '--url=https://api.phemex.com'], ['worker']):
            output = Mock(buffer=io.BytesIO())
            source = Mock(buffer=Mock(read=Mock(side_effect=OSError('PRIVATE_SENTINEL'))))
            with patch('sys.argv', args), patch('sys.stdin', source), patch('sys.stdout', output):
                self.assertEqual(main(), 1)
            self.assertNotIn(b'PRIVATE_SENTINEL', output.buffer.getvalue())
            with self.assertRaises(SafetyError): decode_reply(output.buffer.getvalue(), 1)

    def test_unexpected_exit_and_incomplete_or_fake_real_replies_are_rejected(self):
        raw, _, _ = self.run_mock()
        for code in (-9, 1, 17, None, True):
            with self.assertRaises(SafetyError): decode_reply(raw, code)
        for payload in (b'', b'{}', raw[:-1], raw + raw, raw.replace(b'offline_worker_synthetic', b'authenticated_testnet')):
            with self.assertRaises(SafetyError): decode_reply(payload, 0)

    def test_worker_does_not_load_credentials_or_touch_execution_modules(self):
        with patch('config.load_settings', side_effect=AssertionError('Credentials')), \
             patch('operation_journal.OperationJournal', side_effect=AssertionError('Order journal')), \
             patch('ccxt.phemex.create_order', side_effect=AssertionError('Order submission')):
            _, code, _ = self.run_mock()
        self.assertEqual(code, 0)
