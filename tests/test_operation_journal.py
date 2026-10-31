from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from operation_journal import JournalError, OperationJournal, State
from tests.execution_fixtures import NOW, validated
from tests.helpers import OfflineTestCase


def crash_at_boundary(path, boundary):
    journal = OperationJournal(path, clock=lambda: NOW)
    journal.arm('ARM OFFLINE MODEL')
    operation = journal.reserve(validated())
    if boundary != 'prepared':
        authorization = journal.authorize(operation.operation_id, operation.version,
                                           operation.payload_fingerprint, 'AUTHORIZE OFFLINE ' + operation.operation_id)
        if boundary != 'authorized':
            operation = journal.consume_authorization(authorization, operation.payload_fingerprint)
            if boundary == 'acknowledged':
                journal.acknowledge(operation.operation_id, operation.version, 'synthetic-order')
    os._exit(0)  # No cleanup: simulate process loss after durable transitions.


def competing_writer(path):
    try:
        journal = OperationJournal(path, clock=lambda: NOW)
    except JournalError:
        os._exit(0)
    journal.close()
    os._exit(9)


def crash_uncommitted(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute('BEGIN IMMEDIATE')
    connection.execute("UPDATE operations SET state='SUBMITTING'")
    os._exit(0)


def verify_stopped_in_fresh_process(path, operation_id):
    # Spawn starts with no inherited process-local revocation set.
    with patch('socket.socket.connect', side_effect=AssertionError('Network prohibited')), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP prohibited')):
        with OperationJournal(path, clock=lambda: NOW) as journal:
            assert not journal.is_armed()
            assert journal.get(operation_id).state is State.ABORTED
            try:
                journal.arm('ARM OFFLINE MODEL')
            except JournalError:
                pass
            else:
                raise AssertionError('Durable stop did not survive process replacement')


def double_failure_in_helper(path):
    with patch.object(OperationJournal, '_persist_database_stop', return_value=False), \
         patch.object(Path, 'unlink', side_effect=OSError('SECRET_SENTINEL')):
        try:
            OperationJournal.emergency_stop(path)
        except JournalError as error:
            assert 'not durable' in str(error) and 'Other processes may remain armed' in str(error)
            assert 'SECRET_SENTINEL' not in str(error)
        else:
            raise AssertionError('Double failure must not report durable success')


def database_stop_in_helper(path):
    with patch.object(Path, 'unlink', side_effect=OSError('SECRET_SENTINEL')):
        result = OperationJournal.emergency_stop(path)
    assert result.database_revoked and not result.filesystem_revoked


class OperationJournalTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'operations.sqlite3'
        self.now = NOW
        self.journal = OperationJournal(self.path, create=True, clock=lambda: self.now)
        self.addCleanup(self.close_journal)

    def close_journal(self):
        if self.journal is not None:
            self.journal.close()
            self.journal = None

    def prepared(self):
        return self.journal.reserve(validated())

    def authorized(self):
        operation = self.prepared()
        self.journal.arm('ARM OFFLINE MODEL')
        return self.journal.authorize(operation.operation_id, operation.version,
                                      operation.payload_fingerprint, 'AUTHORIZE OFFLINE ' + operation.operation_id)

    def submitting(self):
        authorization = self.authorized()
        return self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def child(self, target, *args, context='fork'):
        process = multiprocessing.get_context(context).Process(target=target, args=args)
        process.start()
        process.join(10 if context == 'spawn' else 5)
        if process.is_alive():
            process.terminate()
            process.join(2)
            self.fail('Offline concurrency/recovery child timed out')
        self.assertEqual(process.exitcode, 0)

    def test_default_disarmed_and_identifiers_are_durable(self):
        self.assertFalse(self.journal.is_armed())
        operation = self.prepared()
        self.assertRegex(operation.operation_id, r'^op_[0-9a-f]{32}$')
        self.assertRegex(operation.client_order_id, r'^tn_[0-9a-f]{32}$')
        self.assertLessEqual(len(operation.client_order_id), 40)
        self.assertEqual(self.journal.get(operation.operation_id), operation)
        with self.assertRaises(JournalError):
            self.journal.authorize(operation.operation_id, 0, operation.payload_fingerprint,
                                   'AUTHORIZE OFFLINE ' + operation.operation_id)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_only_explicit_arm_confirmation_is_accepted(self):
        for phrase in ('', 'yes', 'ARM', 'SECRET_SENTINEL'):
            with self.subTest(phrase=phrase), self.assertRaises(JournalError) as caught:
                self.journal.arm(phrase)
            self.assertNotIn('SECRET_SENTINEL', str(caught.exception))
        self.journal.arm('ARM OFFLINE MODEL')
        self.assertTrue(self.journal.is_armed())

    def test_one_unresolved_operation_per_account(self):
        operation = self.prepared()
        with self.assertRaises(JournalError):
            self.journal.reserve(validated())
        self.assertEqual(self.journal.unresolved(), (operation,))
        self.journal.finish_unsent(operation.operation_id, 0, State.ABORTED)
        self.assertEqual(self.journal.unresolved(), ())
        self.assertNotEqual(self.prepared().client_order_id, operation.client_order_id)

    def test_duplicate_ids_remain_reserved_after_terminal_outcome(self):
        operation = self.prepared()
        self.journal.finish_unsent(operation.operation_id, 0, State.ABORTED)
        for overrides in ({'operation_id': operation.operation_id}, {'client_order_id': operation.client_order_id}):
            with self.subTest(overrides=overrides), self.assertRaises(JournalError):
                self.journal.reserve(validated(), **overrides)

    def test_payload_environment_and_binding_validation(self):
        intent = validated()
        for changed in (replace(intent, environment='live'), replace(intent, account_binding='SECRET_SENTINEL'),
                        replace(intent, payload=intent.payload + (('apiKey', 'SECRET_SENTINEL'),)),
                        replace(intent, payload=intent.payload[:-1]),
                        replace(intent, payload_fingerprint='0' * 64),
                        replace(intent, evidence_fingerprint='invalid'),
                        replace(intent, expires_at=NOW + timedelta(days=1))):
            with self.subTest(changed=type(changed)), self.assertRaises(JournalError) as caught:
                self.journal.reserve(changed)
            self.assertNotIn('SECRET_SENTINEL', str(caught.exception))

    def test_authorization_is_specific_and_cannot_be_replayed(self):
        authorization = self.authorized()
        self.assertNotIn(authorization.token, repr(authorization))
        for changed in (replace(authorization, token='0' * 64), replace(authorization, version=0),
                        replace(authorization, payload_fingerprint='0' * 64),
                        replace(authorization, expires_at_us=authorization.expires_at_us + 1)):
            with self.subTest(changed=changed.version), self.assertRaises(JournalError):
                self.journal.consume_authorization(changed, authorization.payload_fingerprint)
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, '0' * 64)
        operation = self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.assertEqual(operation.state, State.SUBMITTING)
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_wrong_confirmation_hash_or_version_cannot_authorize(self):
        operation = self.prepared()
        self.journal.arm('ARM OFFLINE MODEL')
        for version, digest, phrase in ((1, operation.payload_fingerprint, 'AUTHORIZE OFFLINE ' + operation.operation_id),
                                        (0, '0' * 64, 'AUTHORIZE OFFLINE ' + operation.operation_id),
                                        (0, operation.payload_fingerprint, 'yes')):
            with self.subTest(version=version, phrase=phrase), self.assertRaises(JournalError):
                self.journal.authorize(operation.operation_id, version, digest, phrase)
        self.assertEqual(self.journal.get(operation.operation_id).state, State.PREPARED)

    def test_expiry_and_backwards_clock_block_authorization_consumption(self):
        authorization = self.authorized()
        self.now = NOW + timedelta(seconds=5)
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.assertEqual(self.journal.get(authorization.operation_id).state, State.AUTHORIZED)
        self.journal.finish_unsent(authorization.operation_id, authorization.version, State.EXPIRED)
        self.now = NOW
        with self.assertRaises(JournalError):
            self.journal.reserve(validated())

    def test_expired_evidence_cannot_be_reserved_or_authorized(self):
        operation = self.prepared()
        self.journal.arm('ARM OFFLINE MODEL')
        self.now += timedelta(seconds=5)
        with self.assertRaises(JournalError):
            self.journal.authorize(operation.operation_id, 0, operation.payload_fingerprint,
                                   'AUTHORIZE OFFLINE ' + operation.operation_id)
        self.journal.finish_unsent(operation.operation_id, 0, State.EXPIRED)
        with self.assertRaises(JournalError):
            self.journal.reserve(validated())

    def test_rearming_revokes_previous_authorization(self):
        authorization = self.authorized()
        self.journal.arm('ARM OFFLINE MODEL')
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_authorization_cannot_be_rebound_to_another_account_operation(self):
        first = self.prepared()
        second = self.journal.reserve(replace(validated(), account_binding='b' * 64))
        self.journal.arm('ARM OFFLINE MODEL')
        first_auth = self.journal.authorize(first.operation_id, 0, first.payload_fingerprint,
                                            'AUTHORIZE OFFLINE ' + first.operation_id)
        second_auth = self.journal.authorize(second.operation_id, 0, second.payload_fingerprint,
                                             'AUTHORIZE OFFLINE ' + second.operation_id)
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(replace(first_auth, operation_id=second.operation_id),
                                                second.payload_fingerprint)
        self.assertEqual(self.journal.consume_authorization(second_auth, second.payload_fingerprint).state,
                         State.SUBMITTING)

    def test_persistent_stop_revokes_and_survives_reopen(self):
        authorization = self.authorized()
        result = OperationJournal.emergency_stop(self.path)
        self.assertTrue(result.database_revoked and result.filesystem_revoked and result.marker_installed)
        row = self.journal._connection.execute('SELECT auth_hash,auth_generation FROM operations').fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.assertEqual(self.journal.get(authorization.operation_id).state, State.AUTHORIZED)
        self.close_journal()
        self.child(verify_stopped_in_fresh_process, self.path, authorization.operation_id, context='spawn')

    def test_double_persistence_failure_revokes_current_process_but_reports_no_durability(self):
        authorization = self.authorized()
        with patch.object(OperationJournal, '_persist_database_stop', return_value=False), \
             patch.object(Path, 'unlink', side_effect=OSError('SECRET_SENTINEL')):
            with self.assertRaises(JournalError) as caught:
                OperationJournal.emergency_stop(self.path)
        self.assertIn('not durable', str(caught.exception))
        self.assertIn('Other processes may remain armed', str(caught.exception))
        self.assertNotIn('SECRET_SENTINEL', str(caught.exception))
        self.assertEqual(self.journal._control()['mode'], 'ARMED')
        self.assertEqual(self.journal.stop_path.read_bytes(), b'READY\n')
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        with self.assertRaises(JournalError):
            self.journal.arm('ARM OFFLINE MODEL')

    def test_helper_double_failure_cannot_revoke_owner_without_any_persistence_channel(self):
        authorization = self.authorized()
        self.child(double_failure_in_helper, self.path)
        # Deliberate counterexample: a different process's memory is not IPC.
        self.assertTrue(self.journal.is_armed())
        operation = self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.assertEqual(operation.state, State.SUBMITTING)
        OperationJournal.emergency_stop(self.path)
        self.assertEqual(self.journal.get(operation.operation_id).state, State.SUBMITTING)

    def test_helper_database_revocation_blocks_owner_even_when_unlink_fails(self):
        authorization = self.authorized()
        self.child(database_stop_in_helper, self.path)
        self.assertEqual(self.journal._control()['mode'], 'STOPPED')
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_stop_with_busy_database_and_early_unlink_failure_latches_locally(self):
        authorization = self.authorized()
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            with patch.object(Path, 'unlink', side_effect=OSError('SECRET_SENTINEL')):
                with self.assertRaisesRegex(JournalError, 'not durable'):
                    OperationJournal.emergency_stop(self.path)
            self.assertFalse(self.journal.is_armed())
        finally:
            connection.rollback()
            connection.close()
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_stop_with_unavailable_database_still_revokes_through_filesystem(self):
        authorization = self.authorized()
        with patch.object(OperationJournal, '_persist_database_stop', return_value=False):
            result = OperationJournal.emergency_stop(self.path)
        self.assertFalse(result.database_revoked)
        self.assertTrue(result.filesystem_revoked and result.marker_installed and result.durable)
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.close_journal()
        self.child(verify_stopped_in_fresh_process, self.path, authorization.operation_id, context='spawn')

    def test_stop_database_commit_and_cleanup_errors_do_not_skip_filesystem_revocation(self):
        authorization = self.authorized()
        import operation_journal
        original_connect = operation_journal._connect
        calls = []
        class CommitAndCleanupFailure:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, statement, *args):
                if statement in ('COMMIT', 'ROLLBACK'):
                    calls.append(statement)
                    raise sqlite3.OperationalError('SECRET_SENTINEL')
                return self.connection.execute(statement, *args)
            def close(self):
                self.connection.close()
                raise sqlite3.OperationalError('SECRET_SENTINEL')
        with patch('operation_journal._connect', side_effect=lambda path: CommitAndCleanupFailure(original_connect(path))):
            result = OperationJournal.emergency_stop(self.path)
        self.assertEqual(calls.count('COMMIT'), 1)
        self.assertTrue(result.filesystem_revoked)
        self.assertFalse(result.database_revoked)
        self.assertNotIn('SECRET_SENTINEL', repr(result))
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_directory_sync_failure_after_removal_does_not_claim_filesystem_durability(self):
        authorization = self.authorized()
        with patch('operation_journal._sync_directory', side_effect=OSError('SECRET_SENTINEL')):
            result = OperationJournal.emergency_stop(self.path)
        self.assertTrue(result.database_revoked)
        self.assertFalse(result.filesystem_revoked or result.marker_installed)
        self.assertFalse(self.journal.stop_path.exists())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_double_failure_after_removal_does_not_claim_unsynced_absence_is_durable(self):
        authorization = self.authorized()
        with patch.object(OperationJournal, '_persist_database_stop', return_value=False), \
             patch('operation_journal._sync_directory', side_effect=OSError('SECRET_SENTINEL')):
            with self.assertRaisesRegex(JournalError, 'not durable'):
                OperationJournal.emergency_stop(self.path)
        self.assertFalse(self.journal.stop_path.exists())
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_failed_stop_marker_installation_can_rely_on_durable_absence(self):
        authorization = self.authorized()
        with patch.object(OperationJournal, '_persist_database_stop', return_value=False), \
             patch('operation_journal.os.replace', side_effect=OSError('SECRET_SENTINEL')):
            result = OperationJournal.emergency_stop(self.path)
        self.assertTrue(result.filesystem_revoked and result.durable)
        self.assertFalse(result.marker_installed or result.database_revoked)
        self.assertFalse(self.journal.stop_path.exists())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.close_journal()
        self.child(verify_stopped_in_fresh_process, self.path, authorization.operation_id, context='spawn')

    def test_stop_during_consumption_rolls_back_before_committing_submission_state(self):
        authorization = self.authorized()
        original = self.journal._change
        def stop_after_change(*args, **kwargs):
            original(*args, **kwargs)
            result = OperationJournal.emergency_stop(self.path)
            self.assertFalse(result.database_revoked)  # Consumption holds SQLite's write lock.
            self.assertTrue(result.filesystem_revoked)
        with patch.object(self.journal, '_change', stop_after_change):
            with self.assertRaises(JournalError):
                self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.assertEqual(self.journal.get(authorization.operation_id).state, State.AUTHORIZED)
        self.close_journal()
        self.journal = OperationJournal(self.path, clock=lambda: NOW)
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.arm('ARM OFFLINE MODEL')

    def test_stop_after_final_check_cannot_recall_already_inflight_model_transition(self):
        authorization = self.authorized()
        original = self.journal.is_armed
        calls = []
        def stop_after_final_check():
            armed = original()
            calls.append(armed)
            if len(calls) == 2:
                result = OperationJournal.emergency_stop(self.path)
                self.assertTrue(result.filesystem_revoked)
                self.assertFalse(result.database_revoked)
            return armed
        with patch.object(self.journal, 'is_armed', stop_after_final_check):
            operation = self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        self.assertEqual(operation.state, State.SUBMITTING)
        self.assertFalse(self.journal.is_armed())
        self.assertEqual(len(self.journal.unresolved()), 1)
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_early_readiness_unlink_failure_revokes_existing_authorization(self):
        authorization = self.authorized()
        original = Path.unlink
        def fail_readiness(path, *args, **kwargs):
            if path == self.journal.stop_path:
                raise OSError('SECRET_SENTINEL')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_readiness):
            result = OperationJournal.emergency_stop(self.path)
        self.assertTrue(result.database_revoked)
        self.assertFalse(result.filesystem_revoked)
        self.assertTrue(result.durable)
        self.assertEqual(self.journal.stop_path.read_bytes(), b'READY\n')
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)
        with self.assertRaises(JournalError):
            self.journal.arm('ARM OFFLINE MODEL')
        self.assertEqual(self.journal._control()['mode'], 'STOPPED')
        self.close_journal()
        self.child(verify_stopped_in_fresh_process, self.path, authorization.operation_id, context='spawn')

    def test_stop_latches_while_sqlite_is_busy(self):
        authorization = self.authorized()
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            result = OperationJournal.emergency_stop(self.path)
            self.assertTrue(result.filesystem_revoked)
            self.assertFalse(result.database_revoked)
            self.assertFalse(self.journal.is_armed())
        finally:
            connection.rollback()
            connection.close()
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_corrupted_stop_marker_and_missing_control_fail_closed(self):
        self.journal.arm('ARM OFFLINE MODEL')
        self.journal.stop_path.write_bytes(b'CORRUPTED_SECRET_SENTINEL')
        self.assertFalse(self.journal.is_armed())
        self.journal._connection.execute('DELETE FROM control')
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError) as caught:
            self.prepared()
        self.assertNotIn('SECRET_SENTINEL', str(caught.exception))

    def test_missing_unreadable_or_symlinked_stop_state_blocks_arming(self):
        self.journal.arm('ARM OFFLINE MODEL')
        self.journal.stop_path.unlink()
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.arm('ARM OFFLINE MODEL')
        self.journal.stop_path.write_bytes(b'READY\n')
        self.journal.stop_path.chmod(0o644)
        self.assertFalse(self.journal.is_armed())
        self.journal.stop_path.unlink()
        target = self.path.with_name('other-control')
        target.write_bytes(b'READY\n')
        target.chmod(0o600)
        self.journal.stop_path.symlink_to(target)
        self.assertFalse(self.journal.is_armed())

    def test_replaced_database_and_closed_session_fail_closed(self):
        self.journal.arm('ARM OFFLINE MODEL')
        moved = self.path.with_name('moved.sqlite3')
        self.path.rename(moved)
        self.path.write_bytes(moved.read_bytes())
        self.path.chmod(0o600)
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.prepared()
        self.close_journal()
        self.journal = OperationJournal(self.path, clock=lambda: NOW)
        self.journal._connection.close()
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.prepared()

    def test_identifiers_and_opaque_bindings_remain_separate(self):
        for key in ('operation_id', 'client_order_id'):
            for value in ('', False, 'SECRET_SENTINEL'):
                with self.subTest(key=key, value=value), self.assertRaises(JournalError):
                    self.journal.reserve(validated(), **{key: value})
        first = self.prepared()
        second = self.journal.reserve(replace(validated(), account_binding='b' * 64))
        self.assertNotEqual(first.account_binding, second.account_binding)
        self.assertEqual(len(self.journal.unresolved()), 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal._connection.execute("UPDATE operations SET account_binding=? WHERE operation_id=?",
                                             ('b' * 64, first.operation_id))

    def test_rejected_zero_fill_is_terminal_and_audit_events_are_consistent(self):
        operation = self.submitting()
        self.journal.start_reconciliation(operation.operation_id, operation.version)
        operation = self.journal.get(operation.operation_id)
        self.journal.resolve(operation.operation_id, operation.version, State.REJECTED, filled_quantity=Decimal('0'))
        self.assertEqual(self.journal.unresolved(), ())
        rows = self.journal._connection.execute('SELECT version,state FROM events WHERE operation_id=? ORDER BY version',
                                               (operation.operation_id,)).fetchall()
        self.assertEqual([row['version'] for row in rows], list(range(5)))
        self.assertEqual(rows[-1]['state'], 'REJECTED')
        self.journal._connection.execute('DELETE FROM events WHERE version=4')
        with self.assertRaises(JournalError):
            self.journal.get(operation.operation_id)

    def test_failed_stop_persistence_is_sanitized(self):
        self.journal.arm('ARM OFFLINE MODEL')
        with patch('operation_journal.os.replace', side_effect=OSError('SECRET_SENTINEL')):
            result = OperationJournal.emergency_stop(self.path)
        self.assertTrue(result.durable)
        self.assertFalse(result.marker_installed)
        self.assertNotIn('SECRET_SENTINEL', repr(result))
        self.assertFalse(self.journal.is_armed())

    def test_missing_corrupted_and_insecure_journals_are_not_reinitialized(self):
        self.close_journal()
        missing = self.path.with_name('missing.sqlite3')
        with self.assertRaises(JournalError):
            OperationJournal(missing)
        self.assertFalse(missing.exists())
        self.path.write_bytes(b'CORRUPTED_SECRET_SENTINEL')
        with self.assertRaises(JournalError) as caught:
            OperationJournal(self.path)
        self.assertNotIn('SECRET_SENTINEL', str(caught.exception))
        self.assertEqual(self.path.read_bytes(), b'CORRUPTED_SECRET_SENTINEL')
        self.path.chmod(0o644)
        with self.assertRaises(JournalError):
            OperationJournal(self.path)

    def test_existing_journal_cannot_be_reset_by_create(self):
        operation = self.prepared()
        self.close_journal()
        before = self.path.read_bytes()
        with self.assertRaises(JournalError):
            OperationJournal(self.path, create=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.journal = OperationJournal(self.path, clock=lambda: NOW)
        self.assertEqual(self.journal.get(operation.operation_id).client_order_id, operation.client_order_id)

    def test_competing_process_cannot_own_journal(self):
        operation = self.prepared()
        self.child(competing_writer, self.path)
        self.assertEqual(self.journal.get(operation.operation_id), operation)

    def test_sqlite_busy_fails_without_retry_and_can_recover(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            with self.assertRaises(JournalError):
                self.prepared()
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(self.journal.unresolved(), ())
        self.assertEqual(self.prepared().state, State.PREPARED)

    def test_failed_commit_rolls_back_operation_and_event(self):
        original = self.journal._connection
        class CommitFailure:
            def __getattr__(self, name):
                return getattr(original, name)
            def execute(self, statement, *args):
                if statement == 'COMMIT':
                    raise sqlite3.OperationalError('SECRET_SENTINEL')
                return original.execute(statement, *args)
        self.journal._connection = CommitFailure()
        try:
            with self.assertRaises(JournalError) as caught:
                self.prepared()
            self.assertNotIn('SECRET_SENTINEL', str(caught.exception))
        finally:
            self.journal._connection = original
        self.assertEqual(self.journal.unresolved(), ())
        self.assertEqual(original.execute('SELECT count(*) FROM events').fetchone()[0], 0)

    def test_interrupted_transaction_rolls_back_without_reusing_partial_state(self):
        with patch.object(self.journal, '_event', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepared()
        self.assertFalse(self.journal._connection.in_transaction)
        self.assertEqual(self.journal.unresolved(), ())
        self.assertEqual(self.prepared().state, State.PREPARED)

    def test_unsent_restart_disarms_and_invalidates_authorization(self):
        authorization = self.authorized()
        self.close_journal()
        self.journal = OperationJournal(self.path, clock=lambda: NOW)
        operation = self.journal.get(authorization.operation_id)
        self.assertEqual(operation.state, State.ABORTED)
        self.assertFalse(self.journal.is_armed())
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(authorization, authorization.payload_fingerprint)

    def test_process_crashes_recover_without_resubmission(self):
        for boundary in ('prepared', 'authorized', 'submitting', 'acknowledged'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'crash.sqlite3'
                OperationJournal(path, create=True, clock=lambda: NOW).close()
                self.child(crash_at_boundary, path, boundary)
                with OperationJournal(path, clock=lambda: NOW) as journal:
                    self.assertFalse(journal.is_armed())
                    rows = journal._connection.execute('SELECT operation_id FROM operations').fetchall()
                    operation = journal.get(rows[0][0])
                    self.assertEqual(operation.state, State.ABORTED if boundary in ('prepared', 'authorized') else State.UNCERTAIN)
                    if operation.state is State.UNCERTAIN:
                        with self.assertRaises(JournalError):
                            journal.reserve(validated())

    def test_crash_during_sqlite_transaction_rolls_back(self):
        operation = self.prepared()
        self.close_journal()
        self.child(crash_uncommitted, self.path)
        self.journal = OperationJournal(self.path, clock=lambda: NOW)
        self.assertEqual(self.journal.get(operation.operation_id).state, State.ABORTED)

    def test_acknowledgment_is_nonterminal_and_reconciliation_is_required(self):
        operation = self.submitting()
        self.journal.acknowledge(operation.operation_id, operation.version, 'synthetic-order')
        operation = self.journal.get(operation.operation_id)
        self.assertEqual(operation.state, State.ACKNOWLEDGED)
        self.assertEqual(len(self.journal.unresolved()), 1)
        with self.assertRaises(JournalError):
            self.journal.resolve(operation.operation_id, operation.version, State.FILLED, filled_quantity=Decimal('0.001'))
        self.journal.start_reconciliation(operation.operation_id, operation.version)
        operation = self.journal.get(operation.operation_id)
        self.journal.resolve(operation.operation_id, operation.version, State.FILLED, filled_quantity=Decimal('0.001'))
        self.assertEqual(self.journal.unresolved(), ())
        self.assertEqual(self.journal.get(operation.operation_id).state, State.FILLED)

    def test_uncertain_operations_cannot_be_aborted_or_replaced(self):
        operation = self.submitting()
        self.journal.mark_uncertain(operation.operation_id, operation.version)
        operation = self.journal.get(operation.operation_id)
        for target in (State.ABORTED, State.BLOCKED, State.EXPIRED):
            with self.subTest(target=target), self.assertRaises(JournalError):
                self.journal.finish_unsent(operation.operation_id, operation.version, target)
        with self.assertRaises(JournalError):
            self.prepared()
        self.journal.start_reconciliation(operation.operation_id, operation.version)
        operation = self.journal.get(operation.operation_id)
        self.journal.mark_uncertain(operation.operation_id, operation.version)
        self.assertEqual(len(self.journal.unresolved()), 1)

    def test_invalid_terminal_quantities_leave_reconciliation_unresolved(self):
        operation = self.submitting()
        self.journal.start_reconciliation(operation.operation_id, operation.version)
        operation = self.journal.get(operation.operation_id)
        for target, quantity in ((State.FILLED, Decimal('0.0005')), (State.REJECTED, Decimal('0.001')),
                                 (State.CANCELED, Decimal('0.002')), (State.CANCELED, Decimal('-1')),
                                 (State.FILLED, Decimal('NaN')), (State.CANCELED, 0.0)):
            with self.subTest(target=target, quantity=quantity), self.assertRaises(JournalError):
                self.journal.resolve(operation.operation_id, operation.version, target, filled_quantity=quantity)
        self.assertEqual(self.journal.get(operation.operation_id), operation)
        self.journal.resolve(operation.operation_id, operation.version, State.CANCELED, filled_quantity=Decimal('0.0005'))
        self.assertEqual(self.journal.get(operation.operation_id).filled_quantity, '0.0005')

    def test_sqlite_immutable_payload_and_stale_transition_guards(self):
        operation = self.prepared()
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal._connection.execute("UPDATE operations SET payload='SECRET_SENTINEL'")
        with self.assertRaises(JournalError):
            self.journal.finish_unsent(operation.operation_id, 1, State.ABORTED)
        with self.assertRaises(JournalError):
            self.journal.acknowledge(operation.operation_id, 0, 'synthetic-order')
        self.journal.finish_unsent(operation.operation_id, 0, State.BLOCKED)
        with self.assertRaises(JournalError):
            self.journal.finish_unsent(operation.operation_id, 1, State.ABORTED)

    def test_no_exchange_or_credentials_are_used_for_lifecycle(self):
        with patch('config.load_settings', side_effect=AssertionError('Credential access')), \
             patch('exchange_client.PhemexClient', side_effect=AssertionError('Exchange construction')):
            operation = self.submitting()
            self.assertEqual(operation.state, State.SUBMITTING)
