from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from offline_simulation import (Outcome, SimulatedObservation, SimulationAuthorization, SimulationJournal,
                                simulate_reconciliation, simulate_submission)
from operation_journal import JournalError, OperationJournal, State
from phemex_request_compiler import CompilationError
from tests.execution_fixtures import NOW, evidence, validated
from tests.helpers import OfflineTestCase
from tests.test_phemex_request_compiler import compiled


def crash_simulation(path, boundary):
    # Fork child deliberately terminates without close/cleanup, exercising SQLite.
    import os
    with SimulationJournal(path, create=True, clock=lambda: NOW) as journal:
        request = compiled()
        if boundary == 'request_binding_gap':
            # Exact durable boundary between semantic reservation and binding.
            OperationJournal.reserve(journal, request.plan, client_order_id=request.client_order_id)
            os._exit(0)
        operation = journal.reserve_compiled(request)
        if boundary != State.PREPARED:
            journal.arm('ARM OFFLINE MODEL')
            token = journal.authorize(operation.operation_id, 0, request, 'AUTHORIZE OFFLINE ' + operation.operation_id)
            if boundary != State.AUTHORIZED:
                journal.consume_authorization(token, request)
                operation = journal.get(operation.operation_id)
                if boundary is State.ACKNOWLEDGED:
                    journal.acknowledge(operation.operation_id, operation.version, 'sim_' + request.client_order_id)
                elif boundary is State.RECONCILING:
                    journal.start_reconciliation(operation.operation_id, operation.version)
                elif boundary is State.UNCERTAIN:
                    journal.mark_uncertain(operation.operation_id, operation.version)
        os._exit(0)


def competing_session(path):
    try:
        with SimulationJournal(path, clock=lambda: NOW):
            raise AssertionError('Concurrent simulation session was accepted')
    except JournalError:
        pass


class OfflineSimulationTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'simulation.sqlite3'
        self.now = NOW
        self.journal = SimulationJournal(self.path, create=True, clock=lambda: self.now)
        self.addCleanup(lambda: self.journal.close())
        self.request = compiled()

    def authorized(self):
        operation = self.journal.reserve_compiled(self.request)
        self.journal.arm('ARM OFFLINE MODEL')
        return self.journal.authorize(operation.operation_id, 0, self.request,
                                      'AUTHORIZE OFFLINE ' + operation.operation_id)

    def observation(self, operation_id, outcome=Outcome.FULL_FILL, filled='0.001'):
        return SimulatedObservation(operation_id, self.request.client_order_id, self.request.request_fingerprint,
                                    self.request.plan.account_binding, 'testnet', outcome, Decimal(filled),
                                    'sim_' + self.request.client_order_id)

    def child(self, target, *args):
        process = multiprocessing.get_context('fork').Process(target=target, args=args)
        process.start()
        process.join(8)
        if process.is_alive():
            process.terminate()
            process.join(2)
            self.fail('Offline simulation child timed out')
        self.assertEqual(process.exitcode, 0)

    def test_ordinary_and_simulation_journals_refuse_each_other_without_recovery(self):
        ordinary_path = self.path.with_name('ordinary.sqlite3')
        with OperationJournal(ordinary_path, create=True, clock=lambda: NOW) as ordinary:
            operation = ordinary.reserve(validated())
            ordinary.arm('ARM OFFLINE MODEL')
            token = ordinary.authorize(operation.operation_id, 0, operation.payload_fingerprint,
                                       'AUTHORIZE OFFLINE ' + operation.operation_id)
            ordinary.consume_authorization(token, operation.payload_fingerprint)
        with self.assertRaises(JournalError): SimulationJournal(ordinary_path, clock=lambda: NOW)
        # A rejected simulation open must not even recover/change the ordinary operation.
        with sqlite3.connect(ordinary_path) as connection:
            self.assertEqual(connection.execute('SELECT state FROM operations').fetchone()[0], 'SUBMITTING')
        self.journal.close()
        with self.assertRaises(JournalError): OperationJournal(self.path, clock=lambda: NOW)

    def test_simulation_workflow_rejects_ordinary_journal_and_preserves_blocker(self):
        ordinary_path = self.path.with_name('ordinary.sqlite3')
        with OperationJournal(ordinary_path, create=True, clock=lambda: NOW) as ordinary:
            operation = ordinary.reserve(validated())
            with self.assertRaises(JournalError): simulate_submission(ordinary, None, self.request, Outcome.FULL_FILL)
            with self.assertRaises(JournalError):
                simulate_reconciliation(ordinary, self.request, self.observation(operation.operation_id))
            self.assertEqual(ordinary.get(operation.operation_id).state, State.PREPARED)

    def test_binding_matches_exact_request_semantics_account_and_evidence(self):
        operation = self.journal.reserve_compiled(self.request)
        self.assertEqual(self.journal.bound_operation(operation.operation_id, self.request), operation)
        values = list(evidence())
        for index, changes in ((0, {'price': Decimal('60000.1')}), (2, {'configured_leverage': Decimal('10')})):
            changed = values.copy()
            changed[index] = replace(changed[index], **changes)
            other = compiled(changed)
            with self.assertRaises(JournalError): self.journal.bound_operation(operation.operation_id, other)
        with self.assertRaises(JournalError):
            self.journal.bound_operation(operation.operation_id, compiled(client_id='tn_' + '2' * 32))
        changed = values.copy()
        for index in (1, 2, 3, 4, 5):
            changed[index] = replace(changed[index], stamp=replace(changed[index].stamp, account_binding='b' * 64))
        with self.assertRaises(JournalError): self.journal.bound_operation(operation.operation_id, compiled(changed))

    def test_binding_is_immutable_and_cannot_be_deleted(self):
        self.journal.reserve_compiled(self.request)
        for sql in ("UPDATE simulation_requests SET request='changed'", 'DELETE FROM simulation_requests'):
            with self.assertRaises(sqlite3.IntegrityError): self.journal._connection.execute(sql)

    def test_generic_reservation_and_generic_token_cannot_enter_simulation(self):
        with self.assertRaises(JournalError): self.journal.reserve(validated())
        token = self.authorized()
        with self.assertRaises(JournalError): self.journal.consume_authorization(token.authorization, self.request)
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(replace(token, simulation_only=False), self.request)

    def test_request_hash_change_token_replay_and_mutation_are_rejected(self):
        token = self.authorized()
        for changed in (replace(token, request_fingerprint='0' * 64),
                        replace(token, authorization=replace(token.authorization, payload_fingerprint='0' * 64))):
            with self.assertRaises(JournalError): self.journal.consume_authorization(changed, self.request)
        parameters = dict(self.request.parameters)
        parameters['reduceOnly'] = False
        with self.assertRaises(JournalError):
            self.journal.consume_authorization(token, replace(self.request, parameters=tuple(sorted(parameters.items()))))
        simulate_submission(self.journal, token, self.request, Outcome.TIMEOUT)
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)
        self.assertEqual(self.journal.get(token.authorization.operation_id).state, State.UNCERTAIN)

    def test_authorization_expiry_is_not_reset_by_compilation(self):
        token = self.authorized()
        self.now += timedelta(seconds=5)
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)
        self.assertEqual(self.journal.get(token.authorization.operation_id).state, State.AUTHORIZED)

    def test_cannot_authorize_without_arming_or_exact_phrase(self):
        operation = self.journal.reserve_compiled(self.request)
        with self.assertRaises(JournalError): self.journal.authorize(operation.operation_id, 0, self.request, 'yes')
        self.journal.arm('ARM OFFLINE MODEL')
        with self.assertRaises(JournalError): self.journal.authorize(operation.operation_id, 0, self.request, 'yes')

    def test_emergency_stop_blocks_simulated_consumption(self):
        token = self.authorized()
        self.assertTrue(SimulationJournal.emergency_stop(self.path).durable)
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)
        self.assertEqual(self.journal.get(token.authorization.operation_id).state, State.AUTHORIZED)

    def test_early_unlink_failure_still_revokes_simulation_authorization(self):
        token = self.authorized()
        with patch.object(Path, 'unlink', side_effect=OSError('SECRET_SENTINEL')):
            result = SimulationJournal.emergency_stop(self.path)
        self.assertTrue(result.database_revoked)
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)

    def test_missing_and_corrupted_control_files_fail_closed(self):
        token = self.authorized()
        self.journal.stop_path.unlink()
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)
        self.journal.stop_path.write_bytes(b'CORRUPT')
        self.journal.stop_path.chmod(0o600)
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)

    def test_acknowledgment_is_not_terminal_or_authenticated(self):
        token = self.authorized()
        result = simulate_submission(self.journal, token, self.request, Outcome.ACKNOWLEDGED)
        self.assertEqual(result.state, State.ACKNOWLEDGED)
        self.assertTrue(result.simulation_only)
        self.assertFalse(result.authenticated)
        self.assertFalse(result.submission_enabled)
        self.assertEqual(len(self.journal.unresolved()), 1)

    def test_definite_simulated_rejection_is_terminal_only_in_simulation(self):
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.REJECTED)
        self.assertEqual(result.state, State.REJECTED)
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(self.journal.get(result.operation_id).filled_quantity, '0')

    def test_full_fill_is_explicitly_simulated_and_terminal(self):
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.FULL_FILL)
        self.assertEqual(result.state, State.FILLED)
        self.assertEqual(self.journal.get(result.operation_id).filled_quantity, '0.001')
        self.assertTrue(result.simulation_only)
        self.assertFalse(result.authenticated)

    def test_partial_ioc_fill_records_cancellation_not_full_close(self):
        values = list(evidence())
        values[0] = replace(values[0], action='close', quantity=Decimal('0.002'))
        self.request = compiled(values)
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.PARTIAL_FILL,
                                     partial_quantity=Decimal('0.001'))
        self.assertEqual(result.state, State.CANCELED)
        self.assertEqual(self.journal.get(result.operation_id).filled_quantity, '0.001')

    def test_timeout_5xx_unknown_stay_blocking_with_no_replacement_ids(self):
        for outcome in (Outcome.TIMEOUT, Outcome.SERVER_ERROR, Outcome.UNCERTAIN):
            with tempfile.TemporaryDirectory() as directory:
                with SimulationJournal(Path(directory)/'sim.db', create=True, clock=lambda: NOW) as journal:
                    operation = journal.reserve_compiled(self.request)
                    journal.arm('ARM OFFLINE MODEL')
                    token = journal.authorize(operation.operation_id, 0, self.request, 'AUTHORIZE OFFLINE ' + operation.operation_id)
                    result = simulate_submission(journal, token, self.request, outcome)
                    self.assertEqual(result.state, State.UNCERTAIN)
                    self.assertEqual(journal.unresolved()[0].client_order_id, self.request.client_order_id)
                    with self.assertRaises(JournalError): journal.reserve_compiled(compiled(client_id='tn_' + '2' * 32))
                    with self.assertRaises(JournalError): journal.finish_unsent(result.operation_id, 2, State.ABORTED)

    def test_wrong_simulated_observation_cannot_release_uncertainty(self):
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.TIMEOUT)
        observation = self.observation(result.operation_id)
        for changes in ({'operation_id': 'op_'+'0'*32}, {'client_order_id': 'tn_'+'2'*32},
                        {'request_fingerprint': '0'*64}, {'account_binding': 'b'*64}, {'environment': 'live'},
                        {'authenticated': True}, {'simulation_only': False}, {'exchange_order_id': 'real-order'},
                        {'outcome': Outcome.UNCERTAIN}, {'outcome': 'not_found'}, {'filled_quantity': Decimal('0')},
                        {'filled_quantity': Decimal('0.002')}, {'filled_quantity': Decimal('NaN')}):
            with self.subTest(changes=changes), self.assertRaises(JournalError):
                simulate_reconciliation(self.journal, self.request, replace(observation, **changes))
            self.assertEqual(self.journal.get(result.operation_id).state, State.UNCERTAIN)

    def test_explicit_simulated_reconciliation_can_resolve_simulated_timeout(self):
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.TIMEOUT)
        resolved = simulate_reconciliation(self.journal, self.request, self.observation(result.operation_id))
        self.assertEqual(resolved.state, State.FILLED)
        with self.assertRaises(JournalError):
            simulate_reconciliation(self.journal, self.request, self.observation(result.operation_id))

    def test_reconciliation_after_stop_remains_possible_without_new_authorization(self):
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.TIMEOUT)
        SimulationJournal.emergency_stop(self.path)
        resolved = simulate_reconciliation(self.journal, self.request, self.observation(result.operation_id))
        self.assertEqual(resolved.state, State.FILLED)
        with self.assertRaises(JournalError): self.journal.arm('ARM OFFLINE MODEL')

    def test_duplicate_client_and_operation_ids_remain_reserved_after_terminal_outcome(self):
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.FULL_FILL)
        with self.assertRaises(JournalError): self.journal.reserve_compiled(self.request)
        with self.assertRaises(JournalError):
            self.journal.reserve_compiled(compiled(client_id='tn_'+'2'*32), operation_id=result.operation_id)

    def test_binding_write_failure_keeps_unsent_id_blocking_and_unauthorizable(self):
        self.journal._connection.execute("""CREATE TRIGGER fail_binding BEFORE INSERT ON simulation_requests
                                        BEGIN SELECT RAISE(ABORT,'SECRET_SENTINEL'); END""")
        with self.assertRaises(JournalError) as error: self.journal.reserve_compiled(self.request)
        self.assertNotIn('SECRET_SENTINEL', str(error.exception))
        operation = self.journal.unresolved()[0]
        self.assertEqual(operation.state, State.PREPARED)
        self.journal.arm('ARM OFFLINE MODEL')
        with self.assertRaises(JournalError):
            self.journal.authorize(operation.operation_id, 0, self.request, 'AUTHORIZE OFFLINE '+operation.operation_id)
        self.journal.close()
        self.journal = SimulationJournal(self.path, clock=lambda: self.now)
        self.assertEqual(self.journal.get(operation.operation_id).state, State.ABORTED)
        with self.assertRaises(JournalError): self.journal.reserve_compiled(self.request)

    def test_sqlite_contention_is_single_attempt_and_cannot_create_duplicate(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            with self.assertRaises(JournalError): self.journal.reserve_compiled(self.request)
        finally:
            connection.close()
        self.assertFalse(self.journal.unresolved())
        self.journal.reserve_compiled(self.request)
        self.child(competing_session, self.path)

    def test_crashes_at_each_nonterminal_boundary_recover_conservatively(self):
        for state in (State.PREPARED, State.AUTHORIZED, State.SUBMITTING, State.ACKNOWLEDGED,
                      State.RECONCILING, State.UNCERTAIN):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'crash.db'
                self.child(crash_simulation, path, state)
                with SimulationJournal(path, clock=lambda: NOW) as journal:
                    rows = journal._connection.execute('SELECT operation_id FROM operations').fetchall()
                    operation = journal.bound_operation(rows[0][0], self.request)
                    self.assertEqual(operation.state, State.ABORTED if state in (State.PREPARED, State.AUTHORIZED) else State.UNCERTAIN)
                    self.assertFalse(journal.is_armed())
                    self.assertEqual(operation.client_order_id, self.request.client_order_id)
                    if state not in (State.PREPARED, State.AUTHORIZED):
                        with self.assertRaises(JournalError): journal.reserve_compiled(compiled(client_id='tn_'+'2'*32))

    def test_restart_retains_binding_and_revokes_old_simulation_token(self):
        token = self.authorized()
        self.journal.close()
        self.journal = SimulationJournal(self.path, clock=lambda: self.now)
        self.assertEqual(self.journal.bound_operation(token.authorization.operation_id, self.request).state, State.ABORTED)
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)

    def test_process_loss_between_reservation_and_binding_cannot_authorize_or_reuse_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'binding-gap.db'
            self.child(crash_simulation, path, 'request_binding_gap')
            with SimulationJournal(path, clock=lambda: NOW) as journal:
                operation_id = journal._connection.execute('SELECT operation_id FROM operations').fetchone()[0]
                self.assertEqual(journal.get(operation_id).state, State.ABORTED)
                self.assertFalse(journal._connection.execute('SELECT * FROM simulation_requests').fetchall())
                journal.arm('ARM OFFLINE MODEL')
                with self.assertRaises(JournalError):
                    journal.authorize(operation_id, 1, self.request, 'AUTHORIZE OFFLINE ' + operation_id)
                with self.assertRaises(JournalError): journal.reserve_compiled(self.request)

    def test_restart_preserves_uncertainty_until_explicit_simulated_observation(self):
        result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.TIMEOUT)
        self.journal.close()
        self.now += timedelta(seconds=30)
        self.journal = SimulationJournal(self.path, clock=lambda: self.now)
        self.assertEqual(self.journal.bound_operation(result.operation_id, self.request).state, State.UNCERTAIN)
        self.assertFalse(self.journal.is_armed())
        # Reuse an exact retained offline artifact; this is NOT fresh evidence or
        # a new authorization. Recovery never synthesizes either of those.
        resolved = simulate_reconciliation(self.journal, self.request, self.observation(result.operation_id))
        self.assertEqual(resolved.state, State.FILLED)
        self.assertTrue(resolved.simulation_only)

    def test_corrupted_binding_hash_cannot_consume_token(self):
        token = self.authorized()
        # Simulate logical corruption beyond the write API's immutable trigger.
        self.journal._connection.execute('DROP TRIGGER immutable_simulation_request')
        self.journal._connection.execute("UPDATE simulation_requests SET request_fingerprint=?", ('0'*64,))
        with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)
        self.assertEqual(self.journal.get(token.authorization.operation_id).state, State.AUTHORIZED)

    def test_invalid_fill_is_rejected_before_consuming_authorization(self):
        token = self.authorized()
        for value in (None, Decimal('0'), Decimal('0.001'), Decimal('0.0005'), Decimal('1e-100000'),
                      Decimal('NaN'), 0.0005):
            with self.subTest(value=value), self.assertRaises(JournalError):
                simulate_submission(self.journal, token, self.request, Outcome.PARTIAL_FILL, partial_quantity=value)
        self.assertEqual(self.journal.get(token.authorization.operation_id).state, State.AUTHORIZED)

    def test_failure_after_consumption_never_retries_or_clears_blocker(self):
        token = self.authorized()
        with patch.object(self.journal, 'acknowledge', side_effect=JournalError('Simulated journal failure')) as acknowledge:
            with self.assertRaises(JournalError): simulate_submission(self.journal, token, self.request, Outcome.FULL_FILL)
        self.assertEqual(acknowledge.call_count, 1)
        self.assertEqual(self.journal.unresolved()[0].state, State.SUBMITTING)
        self.journal.close()
        self.journal = SimulationJournal(self.path, clock=lambda: self.now)
        self.assertEqual(self.journal.unresolved()[0].state, State.UNCERTAIN)

    def test_simulation_never_constructs_exchange_loads_credentials_or_calls_mutators(self):
        with patch('ccxt.phemex.__init__', side_effect=AssertionError('Exchange construction')), \
             patch('ccxt.phemex.sign', side_effect=AssertionError('Signing')), \
             patch('ccxt.phemex.request', side_effect=AssertionError('Transport')), \
             patch('config.load_settings', side_effect=AssertionError('Credentials')):
            result = simulate_submission(self.journal, self.authorized(), self.request, Outcome.TIMEOUT)
        self.assertEqual(result.state, State.UNCERTAIN)
