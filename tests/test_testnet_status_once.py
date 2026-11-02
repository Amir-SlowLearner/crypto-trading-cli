"""Offline only: fake profiles, private temporary DBs, mocked sends, no sockets."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import requests

from safety import SafetyError
from tests.helpers import OfflineTestCase
from tests.compatibility_fixtures import NOW, FAKE_ACCOUNT, FAKE_USER, responses
from testnet_transport import HOST, PLAN, OfflineGetTransport
from testnet_read_run_store import ReadRunStore, ReadBinding, ReadTicket, RunError, PLAN_HASH
import testnet_read_run_store as stores
import testnet_status_once as once
import testnet_status_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
KEY, SECRET = 'INVENTED_STEP2A_KEY', 'INVENTED_STEP2A_SECRET'


def profile():
    return runtime.TestnetCredentialProfile('a' * 32, KEY, SECRET)


def fixture_run_context(stack, directory, *, profile_path=None):
    """Real SQLite authorization with synthetic admin/ancestor identities only."""
    uid = os.geteuid()
    original = Path.lstat
    ancestors = set(directory.parents)
    def protected(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path in ancestors and stat.S_ISDIR(info.st_mode):
            values = list(info)
            values[0] &= ~0o022
            return os.stat_result(values)
        return info
    stack.enter_context(patch.object(Path,'lstat',protected))
    store = ReadRunStore.initialize(directory/'fixture.sqlite',worker_uid=uid,clock=lambda: NOW)
    binding = ReadBinding('a'*32,'b'*64,uid)
    with patch('os.geteuid',return_value=0):
        ticket = store.issue(binding)
        lease = store.claim(ticket,binding)
        store.attach(lease,os.getpid())
    return runtime.ReadRunContext(store,lease,binding,store.storage_id,store.path,
                                  profile_path or directory/'fixture-profile.json')


class StoreTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory(prefix='.t1c-2a-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'approvals.sqlite'
        self.uid = os.geteuid()
        self.assertNotEqual(self.uid, 0, 'Fixtures require a non-root test process')
        self.binding = ReadBinding('a' * 32, 'b' * 64, self.uid)
        self.now = NOW
        # Model protected ancestors ONLY for these fixtures. The checkout is
        # not installed/protected. No real ancestor permissions are changed.
        original_lstat = Path.lstat
        ancestors = set(self.directory.parents)
        def protected(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path in ancestors and stat.S_ISDIR(info.st_mode):
                values = list(info)
                values[0] &= ~0o022
                return os.stat_result(values)
            return info
        self.stack.enter_context(patch.object(Path, 'lstat', protected))
        self.store = ReadRunStore.initialize(self.path, worker_uid=self.uid, clock=lambda: self.now)
        self.http = self.stack.enter_context(patch('requests.sessions.Session.send',
            autospec=True, side_effect=AssertionError('No network send authorized')))

    def issue(self, **options):
        with patch('os.geteuid', return_value=0):
            return self.store.issue(options.pop('binding', self.binding), **options)

    def claim(self, ticket=None, binding=None):
        ticket = ticket or self.issue()
        with patch('os.geteuid', return_value=0):
            return self.store.claim(ticket, binding or self.binding)

    def lease(self):
        ticket = self.issue()
        lease = self.claim(ticket)
        with patch('os.geteuid', return_value=0):
            self.store.attach(lease, os.getpid())
        return ticket, lease

    def reopen(self):
        return ReadRunStore(self.path, storage_id=self.store.storage_id, worker_uid=self.uid, clock=lambda: self.now)

    def test_explicit_initialization_no_recreate_or_overwrite(self):
        content = self.path.read_bytes()
        with self.assertRaises(RunError):
            ReadRunStore.initialize(self.path, worker_uid=self.uid)
        self.assertEqual(content, self.path.read_bytes())

    def test_wrong_initialization_identity(self):
        with self.assertRaises(RunError):
            ReadRunStore.initialize(self.directory / 'wrong', worker_uid=self.uid + 1)

    def test_missing_store_is_not_created(self):
        with self.assertRaises(RunError):
            ReadRunStore(self.directory / 'missing', storage_id=self.store.storage_id, worker_uid=self.uid)
        self.assertFalse((self.directory / 'missing').exists())

    def test_corruption_wrong_purpose_and_storage_identity(self):
        for sql in ('PRAGMA application_id=0', "UPDATE identity SET purpose='offline-readonly-worker-v1'",
                    "UPDATE identity SET storage='cccccccccccccccccccccccccccccccc'"):
            with sqlite3.connect(self.path) as db:
                db.execute(sql)
            with self.assertRaises(RunError):
                self.reopen()
            with sqlite3.connect(self.path) as db:
                db.execute(f'PRAGMA application_id={stores.APPLICATION_ID}')
                db.execute('UPDATE identity SET purpose=?,storage=?', (stores.PURPOSE,self.store.storage_id))
        self.path.write_bytes(b'not sqlite')
        with self.assertRaises(RunError):
            self.reopen()

    def test_symlink_hardlink_unsafe_modes(self):
        link = self.directory / 'alias'
        link.symlink_to(self.path)
        with self.assertRaises(RunError):
            ReadRunStore(link, storage_id=self.store.storage_id, worker_uid=self.uid)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaises(RunError):
            self.reopen()
        link.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(RunError):
            self.reopen()

    def test_replacement_after_open_rejects(self):
        self.path.rename(self.directory / 'old')
        self.path.write_bytes((self.directory / 'old').read_bytes())
        self.path.chmod(0o600)
        with self.assertRaises(RunError):
            self.store.snapshot('c' * 32)

    def test_real_os_identity_required_for_issuance_and_claim(self):
        with self.assertRaises(RunError):
            self.store.issue(self.binding)
        ticket = self.issue()
        with self.assertRaises(RunError):
            self.store.claim(ticket, self.binding)

    def test_tokens_random_hashed_and_repr_hidden(self):
        ticket = self.issue()
        self.assertEqual(len(ticket.token), 64)
        self.assertNotIn(ticket.token, repr(ticket))
        self.assertNotIn(ticket.token.encode(), self.path.read_bytes())
        with sqlite3.connect(self.path) as db:
            stored = db.execute('SELECT token FROM runs').fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(ticket.token.encode()).hexdigest())

    def test_duplicate_outstanding_issue_not_replacement(self):
        self.issue()
        with self.assertRaises(RunError):
            self.issue()

    def test_bindings_and_types_reject(self):
        for kwargs in ({'environment':'live'},{'plan_hash':'0'*64},{'worker_uid':True},
                       {'profile_id':'bad'},{'release_id':'bad'}):
            with self.assertRaises(RunError):
                replace(self.binding, **kwargs)
        ticket = self.issue()
        for binding in (replace(self.binding, profile_id='c'*32), replace(self.binding, release_id='c'*64),
                        replace(self.binding, worker_uid=self.uid+1)):
            with self.assertRaises(RunError):
                self.claim(ticket, binding)
        self.assertEqual(self.store.snapshot(ticket.run_id)['state'], 'APPROVED')

    def test_bad_token_never_claims(self):
        ticket = self.issue()
        for token in ('bad', 'c'*64, True):
            with self.assertRaises(RunError):
                self.claim(ReadTicket(ticket.run_id, token))

    def test_replay_restart_and_copies(self):
        ticket, lease = self.lease()
        for store in (self.store, self.reopen(), copy.copy(self.store)):
            with patch('os.geteuid', return_value=0), self.assertRaises(RunError):
                store.claim(ticket, self.binding)
        self.store.reserve(lease, PLAN[0])
        for store in (self.store, self.reopen()):
            with self.assertRaises(RunError):
                store.reserve(lease, PLAN[0])

    def test_concurrent_claims_exactly_one(self):
        ticket = self.issue()
        def claim(_):
            try:
                self.store.claim(ticket, self.binding)
                return True
            except RunError:
                return False
        with patch('os.geteuid', return_value=0), ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(claim, range(8))), 1)

    def test_concurrent_reservations_exactly_one(self):
        _, lease = self.lease()
        def reserve(_):
            try:
                self.reopen().reserve(lease, PLAN[0])
                return True
            except RunError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve, range(8))), 1)

    def test_expiry_and_future_clock_consume_claim(self):
        for delta in (300, -1):
            ticket = self.issue()
            self.now = NOW + timedelta(seconds=delta)
            with self.assertRaises(RunError):
                self.claim(ticket)
            self.assertEqual(self.store.snapshot(ticket.run_id)['state'], 'BLOCKED')
            self.now = NOW

    def test_expiry_after_claim_never_restores(self):
        ticket, lease = self.lease()
        self.now += timedelta(seconds=300)
        with self.assertRaises(RunError):
            self.store.reserve(lease, PLAN[0])
        with self.assertRaises(RunError):
            self.claim(ticket)

    def test_attach_only_once_and_worker_pid_binding(self):
        _, lease = self.lease()
        with patch('os.geteuid', return_value=0), self.assertRaises(RunError):
            self.store.attach(lease, os.getpid())
        with patch('os.getpid', return_value=os.getpid()+1), self.assertRaises(RunError):
            self.store.reserve(lease, PLAN[0])

    def test_crashes_before_after_claim_attach_and_reservation(self):
        ticket = self.issue()
        lease = self.claim(ticket)
        self.assertEqual(self.reopen().snapshot(ticket.run_id), {'state':'CLAIMED','slots':()})
        with self.assertRaises(RunError):
            self.claim(ticket)
        with patch('os.geteuid', return_value=0):
            self.store.attach(lease, os.getpid())
        self.store.reserve(lease, PLAN[0])
        self.assertEqual(self.reopen().snapshot(ticket.run_id),
                         {'state':'RUNNING','slots':((0,'UNCERTAIN'),)})
        with self.assertRaises(RunError):
            self.reopen().reserve(lease, PLAN[1])
        self.http.assert_not_called()

    def test_exact_plan_and_budget(self):
        ticket, lease = self.lease()
        for entry in ('https://api.phemex.com', replace(PLAN[0], path='g-orders'),
                      replace(PLAN[0], query=(('extra','1'),)), PLAN[1]):
            with self.assertRaises(RunError):
                self.store.reserve(lease, entry)
        for entry in PLAN:
            permit = self.store.reserve(lease, entry)
            self.store.consume_dispatch(lease, entry, permit)
            self.store.record(lease, entry, True)
        with self.assertRaises(RunError):
            self.store.reserve(lease, PLAN[0])
        self.assertTrue(self.store.finish(lease, True))
        self.assertEqual(self.reopen().snapshot(ticket.run_id)['state'], 'COMPLETE')
        with self.assertRaises(RunError):
            self.claim(ticket)

    def test_failed_request_blocks_next_and_false_completion(self):
        ticket, lease = self.lease()
        permit = self.store.reserve(lease, PLAN[0])
        self.store.consume_dispatch(lease, PLAN[0], permit)
        self.store.record(lease, PLAN[0], False)
        with self.assertRaises(RunError):
            self.store.reserve(lease, PLAN[1])
        self.assertFalse(self.store.finish(lease, True))
        self.assertEqual(self.store.snapshot(ticket.run_id)['state'], 'BLOCKED')

    def test_duplicate_record_missing_reservation_and_bool_slot(self):
        _, lease = self.lease()
        with self.assertRaises(RunError):
            self.store.record(lease, PLAN[0], True)
        permit = self.store.reserve(lease, PLAN[0])
        self.store.consume_dispatch(lease, PLAN[0], permit)
        self.store.record(lease, PLAN[0], True)
        with self.assertRaises(RunError):
            self.store.record(lease, PLAN[0], True)
        with self.assertRaises(RunError):
            self.store.reserve(lease, True)

    def test_sqlite_contention_prevents_reservation(self):
        ticket, lease = self.lease()
        with sqlite3.connect(self.path, isolation_level=None) as lock:
            lock.execute('BEGIN IMMEDIATE')
            with self.assertRaises(RunError):
                self.store.reserve(lease, PLAN[0])
            lock.execute('ROLLBACK')
        self.assertEqual(self.store.snapshot(ticket.run_id)['slots'], ())
        self.http.assert_not_called()

    def commit_fault(self, stage, after):
        ticket = self.issue()
        lease = None
        if stage != 'claim':
            lease = self.claim(ticket)
            with patch('os.geteuid', return_value=0):
                self.store.attach(lease, os.getpid())
        if stage == 'finish':
            for entry in PLAN:
                permit = self.store.reserve(lease, entry)
                self.store.consume_dispatch(lease, entry, permit)
                self.store.record(lease, entry, True)
        if stage == 'consume':
            permit = self.store.reserve(lease, PLAN[0])
        connect = sqlite3.connect
        class FaultConnection:
            def __init__(self, *args, **kwargs):
                object.__setattr__(self, 'db', connect(*args, **kwargs))
            def __getattr__(self, name):
                return getattr(self.db, name)
            def __setattr__(self, name, value):
                setattr(self.db, name, value)
            def execute(self, sql, *args):
                if sql == 'COMMIT' and not after:
                    raise sqlite3.OperationalError('SECRET_COMMIT_SENTINEL')
                result = self.db.execute(sql, *args)
                if sql == 'COMMIT' and after:
                    raise sqlite3.OperationalError('SECRET_COMMIT_SENTINEL')
                return result
        with patch.object(stores.sqlite3, 'connect', FaultConnection):
            with self.assertRaises(RunError) as error:
                if stage == 'claim':
                    self.claim(ticket)
                elif stage == 'reserve':
                    self.store.reserve(lease, PLAN[0])
                elif stage == 'consume':
                    self.store.consume_dispatch(lease, PLAN[0], permit)
                else:
                    self.store.finish(lease, True)
        self.assertNotIn('SECRET_COMMIT_SENTINEL', str(error.exception))
        self.assertNotIn(ticket.token, str(error.exception))
        snapshot = self.reopen().snapshot(ticket.run_id)
        if stage == 'claim':
            self.assertEqual(snapshot, {'state':'CLAIMED' if after else 'APPROVED','slots':()})
        elif stage == 'reserve':
            self.assertEqual(snapshot, {'state':'RUNNING','slots':((0,'UNCERTAIN'),) if after else ()})
        elif stage == 'consume':
            self.assertEqual(snapshot, {'state':'RUNNING','slots':((0,'UNCERTAIN'),)})
            self.assertEqual(self.store.dispatch_snapshot(ticket.run_id), ((0,after),))
        else:
            self.assertEqual(snapshot['state'], 'COMPLETE' if after else 'RUNNING')
        if stage != 'claim' or after:
            with self.assertRaises(RunError):
                self.claim(ticket)
        self.http.assert_not_called()

    def test_failure_before_claim_commit(self):
        self.commit_fault('claim', False)

    def test_failure_after_claim_commit(self):
        self.commit_fault('claim', True)

    def test_failure_before_reservation_commit(self):
        self.commit_fault('reserve', False)

    def test_failure_after_reservation_commit(self):
        self.commit_fault('reserve', True)

    def test_failure_before_completion_commit(self):
        self.commit_fault('finish', False)

    def test_failure_after_completion_commit(self):
        self.commit_fault('finish', True)

    def test_failure_before_dispatch_consumption_commit(self):
        self.commit_fault('consume', False)

    def test_failure_after_dispatch_consumption_commit(self):
        self.commit_fault('consume', True)

    def test_v1_storage_is_not_implicitly_upgraded(self):
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA user_version=1')
        with self.assertRaises(RunError):
            self.reopen()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)

    def test_unconsumed_reservation_cannot_record_or_complete(self):
        ticket, lease = self.lease()
        self.store.reserve(lease, PLAN[0])
        with self.assertRaises(RunError):
            self.store.record(lease, PLAN[0], True)
        self.assertFalse(self.store.finish(lease, True))
        self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),((0,False),))

    def test_dispatch_permit_is_hashed_hidden_and_single_use(self):
        ticket, lease = self.lease()
        permit = self.store.reserve(lease, PLAN[0])
        self.assertNotIn(permit.nonce,repr(permit))
        self.assertNotIn(permit.nonce.encode(),self.path.read_bytes())
        self.store.consume_dispatch(lease,PLAN[0],permit)
        self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),((0,True),))
        with self.assertRaises(RunError):
            self.reopen().consume_dispatch(lease,PLAN[0],copy.copy(permit))

    def test_forged_dispatch_permissions_cannot_consume_reservation(self):
        ticket, lease = self.lease()
        permit = self.store.reserve(lease,PLAN[0])
        for changed in (replace(permit,nonce='c'*64),replace(permit,run_id='c'*32),
                        replace(permit,slot=True),replace(permit,slot=1),None):
            with self.assertRaises(RunError):
                self.store.consume_dispatch(lease,PLAN[0],changed)
        self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),((0,False),))
        self.store.consume_dispatch(lease,PLAN[0],permit)

    def test_concurrent_dispatch_consumers_have_one_winner(self):
        ticket, lease = self.lease()
        permit = self.store.reserve(lease,PLAN[0])
        def consume(_):
            try:
                self.reopen().consume_dispatch(lease,PLAN[0],permit)
                return True
            except RunError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(consume,range(8))),1)
        self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),((0,True),))
        self.http.assert_not_called()

    def test_restart_cannot_use_original_worker_dispatch_permission(self):
        ticket, lease = self.lease()
        permit = self.store.reserve(lease,PLAN[0])
        with patch('os.getpid',return_value=os.getpid()+1), self.assertRaises(RunError):
            self.reopen().consume_dispatch(lease,PLAN[0],permit)
        with self.assertRaises(RunError):
            self.claim(ticket)
        self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),((0,False),))

    def test_supervisor_launch_failure_consumes_claim_without_retry(self):
        ticket = self.issue()
        config = once.InstalledReadConfiguration(self.binding,self.store.storage_id,self.path,
                    self.directory/'unused-profile',once.INSTALLED_PYTHON,once.INSTALLED_ENTRY)
        with patch('os.geteuid',return_value=0), patch.object(once,'_store',return_value=self.store), \
                patch.object(once.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=self.uid,pw_gid=self.uid)), \
                patch.object(once.subprocess,'Popen',side_effect=OSError('SECRET_LAUNCH_SENTINEL')) as popen:
            with self.assertRaises(RunError) as error:
                once._supervise(config,ticket)
        self.assertNotIn('SECRET_LAUNCH_SENTINEL',str(error.exception))
        self.assertNotIn(ticket.token,str(error.exception))
        self.assertEqual(popen.call_count,1)
        self.assertEqual(self.reopen().snapshot(ticket.run_id),{'state':'BLOCKED','slots':()})
        self.http.assert_not_called()

    def test_supervisor_cannot_replace_fixed_bootstrap_with_arbitrary_command(self):
        ticket = self.issue()
        config = once.InstalledReadConfiguration(self.binding,self.store.storage_id,self.path,
                    self.directory/'unused-profile',Path('/cli/python'),Path('/cli/command'))
        with patch('os.geteuid',return_value=0), patch.object(once,'_store',return_value=self.store), \
                patch.object(once.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=self.uid,pw_gid=self.uid)), \
                patch.object(once.subprocess,'Popen') as popen:
            with self.assertRaises(RunError): once._supervise(config,ticket)
        popen.assert_not_called()
        self.assertEqual(self.reopen().snapshot(ticket.run_id),{'state':'BLOCKED','slots':()})
        self.http.assert_not_called()

    def test_attach_failure_never_sends_child_message_or_inherits_environment(self):
        ticket = self.issue()
        config = once.InstalledReadConfiguration(self.binding,self.store.storage_id,self.path,
                    self.directory/'unused-profile',once.INSTALLED_PYTHON,once.INSTALLED_ENTRY)
        with patch('os.geteuid',return_value=0), patch.object(once,'_store',return_value=self.store), \
                patch.object(once.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=self.uid,pw_gid=self.uid)), \
                patch.object(once.subprocess,'Popen') as popen, patch.object(once,'_kill') as kill, \
                patch.object(ReadRunStore,'attach',side_effect=RunError('attach failed')), \
                patch.object(once,'_read_child') as read:
            with self.assertRaises(RunError):
                once._supervise(config,ticket)
        read.assert_not_called()
        kill.assert_called_once_with(popen.return_value)
        args, options = popen.call_args
        self.assertEqual(args[0],[str(once.INSTALLED_PYTHON),'-I','-S','-B',str(once.INSTALLED_ENTRY),'_worker'])
        self.assertNotIn(ticket.token,repr(popen.call_args))
        self.assertEqual(options['env'],{'LANG':'C.UTF-8'})
        self.assertEqual(options['extra_groups'],())
        self.assertTrue(options['start_new_session'])
        self.assertEqual(self.reopen().snapshot(ticket.run_id),{'state':'BLOCKED','slots':()})

    def test_other_run_receipt_is_not_accepted(self):
        ticket = self.issue()
        config = once.InstalledReadConfiguration(self.binding,self.store.storage_id,self.path,
                    self.directory/'unused-profile',once.INSTALLED_PYTHON,once.INSTALLED_ENTRY)
        reply = {'version':1,'run_id':'c'*32,'release_id':self.binding.release_id,
                 'complete':False,'origin':'protected_testnet_worker','report':'offline fixture'}
        with patch('os.geteuid',return_value=0), patch.object(once,'_store',return_value=self.store), \
                patch.object(once.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=self.uid,pw_gid=self.uid)), \
                patch.object(once.subprocess,'Popen') as popen, patch.object(once,'_kill'), \
                patch.object(once,'_read_child',return_value=json.dumps(reply).encode()):
            popen.return_value.pid = 123456
            with self.assertRaises(RunError):
                once._supervise(config,ticket)
        self.assertEqual(self.reopen().snapshot(ticket.run_id)['state'],'BLOCKED')

    def test_malformed_lease_fails_closed(self):
        _, lease = self.lease()
        for changed in (replace(lease, binding=object()),replace(lease,nonce=True),replace(lease,run_id='bad')):
            with self.assertRaises(RunError):
                self.store.check_active(changed)
        with self.assertRaises(RunError):
            once.collect_fixture_run(self.store,replace(lease,binding=object()),profile())


class ContextTests(StoreTests):
    def context(self, lease):
        return runtime.ReadRunContext(self.store,lease,self.binding,self.store.storage_id,
                                      self.path,self.directory/'fake-testnet.json')

    def test_missing_or_constructed_empty_context_is_not_authority(self):
        for value in (None,object(),{},True):
            with self.assertRaises(SafetyError):
                runtime._require_step2_activation(value,stage='worker')
        self.http.assert_not_called()

    def test_same_run_checks_reject_every_mismatched_binding(self):
        _, lease = self.lease()
        original = self.context(lease)
        alternatives = (
            replace(original,lease=replace(lease,nonce='c'*64)),
            replace(original,lease=replace(lease,run_id='c'*32)),
            replace(original,binding=replace(self.binding,profile_id='c'*32)),
            replace(original,binding=replace(self.binding,release_id='c'*64)),
            replace(original,binding=replace(self.binding,worker_uid=self.uid+1)),
            replace(original,storage_id='c'*32),
            replace(original,store_path=self.directory/'other.sqlite'),
            replace(original,lease=replace(lease,storage_id='c'*32)),
        )
        for context in alternatives:
            with self.assertRaises(SafetyError):
                runtime._require_step2_activation(context,stage='worker')
        original.check()
        self.http.assert_not_called()

    def test_wrong_worker_uid_and_pid_reject(self):
        _, lease = self.lease()
        with patch('os.geteuid',return_value=self.uid+1), self.assertRaises(SafetyError):
            self.context(lease).check()
        with patch('os.getpid',return_value=os.getpid()+1), self.assertRaises(SafetyError):
            self.context(lease).check()

    def test_expiry_poison_survives_copies_and_clock_reversal(self):
        _, lease = self.lease()
        context = self.context(lease)
        duplicate = copy.copy(context)
        self.now += timedelta(seconds=300)
        with self.assertRaises(SafetyError):
            context.check()
        self.now = NOW
        with self.assertRaises(SafetyError):
            duplicate.check()

    def test_stages_paths_and_profile_ids_are_explicit(self):
        _, lease = self.lease()
        for options in ({'stage':'unknown'}, {'stage':'runtime','profile_id':'c'*32},
                        {'stage':'profile','path':self.directory/'other-profile'},
                        {'stage':'dispatch','entry':replace(PLAN[0],query=(('x','1'),))},
                        {'stage':'send','entry':'https://api.phemex.com'}):
            with self.assertRaises(SafetyError):
                runtime._require_step2_activation(self.context(lease),**options)

    def test_context_cannot_enable_real_profile_io(self):
        _, lease = self.lease()
        context = self.context(lease)
        with patch.object(runtime.os,'open',side_effect=AssertionError('credential read')) as opened:
            with self.assertRaises(SafetyError):
                runtime.load_testnet_profile(context.profile_path,context=context)
        opened.assert_not_called()

    def test_consumption_contention_poison_is_shared_by_context_copies(self):
        ticket, lease = self.lease()
        context = self.context(lease)
        permit = context.reserve(PLAN[0])
        duplicate = copy.copy(context)
        with sqlite3.connect(self.path,isolation_level=None) as lock:
            lock.execute('BEGIN IMMEDIATE')
            with self.assertRaises(SafetyError):
                context.consume_dispatch(PLAN[0],permit)
            lock.execute('ROLLBACK')
        with self.assertRaises(SafetyError):
            duplicate.consume_dispatch(PLAN[0],permit)
        self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),((0,False),))
        self.http.assert_not_called()

    def test_missing_or_corrupted_storage_rejects_existing_context(self):
        _, lease = self.lease()
        context = self.context(lease)
        self.path.write_bytes(b'corrupted synthetic store')
        with self.assertRaises(SafetyError) as error:
            context.check()
        self.assertNotIn(lease.nonce,str(error.exception))


class CollectionTests(StoreTests):
    # Do not inherit/run store tests twice; unittest selects tests defined on
    # this class through load_tests below (shared fixture methods only).
    def mock_sends(self):
        self.stack.enter_context(patch.object(runtime, '_require_protected_bootstrap', return_value=None))
        self.data, self.calls = responses(), []
        def respond(session, prepared, **kwargs):
            self.calls.append(prepared)
            result = requests.Response()
            result.status_code, result.url = 200, prepared.url
            path = prepared.url.removeprefix(HOST).split('?')[0]
            result.raw = io.BytesIO(json.dumps(self.data[path]).encode())
            return result
        self.http.side_effect = respond

    def test_collection_durable_complete_and_synthetic(self):
        self.mock_sends()
        ticket, lease = self.lease()
        receipt = once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.assertTrue(receipt.status.collection_complete)
        self.assertTrue(receipt.status.synthetic)
        self.assertEqual(receipt.origin, 'mocked_http')
        for name in ('execution_authorized','may_resolve_journal','evidence_trusted'):
            self.assertFalse(getattr(receipt, name))
        self.assertEqual(self.store.snapshot(ticket.run_id)['state'], 'COMPLETE')
        for secret in (KEY, SECRET, FAKE_ACCOUNT, FAKE_USER, ticket.token, lease.nonce):
            self.assertNotIn(secret, receipt.render())

    def test_unpatched_gate_records_attempt_and_never_sends(self):
        ticket, lease = self.lease()
        receipt = once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.assertFalse(receipt.status.collection_complete)
        self.assertEqual(self.store.snapshot(ticket.run_id), {'state':'BLOCKED','slots':((0,'UNCERTAIN'),)})
        self.http.assert_not_called()
        rendered = once._render_observed(receipt.status)
        self.assertNotIn('Private HTTP success observed', rendered)
        self.assertNotIn('private success response', rendered)

    def test_reservation_is_committed_before_mocked_dispatch(self):
        self.mock_sends()
        ticket, lease = self.lease()
        original = self.http.side_effect
        def checked(session, prepared, **kwargs):
            states = self.reopen().snapshot(ticket.run_id)['slots']
            self.assertEqual(states[-1], (len(self.calls), 'UNCERTAIN'))
            return original(session, prepared, **kwargs)
        self.http.side_effect = checked
        once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)

    def test_timeout_no_retry_restart_or_replacement(self):
        self.mock_sends()
        self.http.side_effect = requests.Timeout('SECRET_EXCEPTION_SENTINEL')
        ticket, lease = self.lease()
        receipt = once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.assertEqual(self.http.call_count, 1)
        self.assertNotIn('SECRET_EXCEPTION_SENTINEL', receipt.render())
        self.assertFalse(receipt.status.collection_complete)
        with self.assertRaises(RunError):
            self.claim(ticket)
        self.assertEqual(self.store.snapshot(ticket.run_id)['slots'], ((0,'UNCERTAIN'),))

    def test_crash_after_reservation_leaves_uncertainty(self):
        self.mock_sends()
        self.http.side_effect = SystemExit('simulated crash')
        ticket, lease = self.lease()
        with self.assertRaises(SystemExit):
            once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.assertEqual(self.reopen().snapshot(ticket.run_id), {'state':'RUNNING','slots':((0,'UNCERTAIN'),)})
        with self.assertRaises(RunError):
            self.claim(ticket)

    def test_wrong_profile_before_any_dispatch(self):
        ticket, lease = self.lease()
        with self.assertRaises(RunError):
            once.collect_fixture_run(self.store, lease, replace(profile(),profile_id='c'*32))
        self.http.assert_not_called()
        self.assertEqual(self.store.snapshot(ticket.run_id)['slots'], ())

    def test_private_500_stops_and_never_marks_authentication_verified(self):
        self.mock_sends()
        self.data['/g-accounts/positions'] = {'code':10500,'msg':'retry_later'}
        ticket, lease = self.lease()
        receipt = once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(receipt.status.collection_complete)
        self.assertEqual(self.store.snapshot(ticket.run_id)['state'], 'BLOCKED')
        rows = {r.name:r for r in receipt.status.requirements}
        self.assertEqual(rows['authentication'].status.value, 'UNKNOWN')

    def test_storage_failure_never_dispatches_or_returns_success(self):
        self.mock_sends()
        _, lease = self.lease()
        with patch.object(ReadRunStore, 'reserve', side_effect=RunError('SECRET_DATABASE_SENTINEL')):
            receipt = once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.http.assert_not_called()
        self.assertFalse(receipt.status.collection_complete)
        self.assertNotIn('SECRET_DATABASE_SENTINEL', receipt.render())

    def test_environment_credentials_never_used(self):
        self.mock_sends()
        _, lease = self.lease()
        with patch.dict(os.environ, {'PHEMEX_API_KEY':'MAINNET_SENTINEL','PHEMEX_API_SECRET':'MAINNET_SENTINEL'}):
            receipt = once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.assertNotIn('MAINNET_SENTINEL', receipt.render())
        self.assertEqual(self.calls[2].headers['x-phemex-access-token'], KEY)

    def test_replayed_fixture_collect_cannot_add_requests(self):
        self.mock_sends()
        _, lease = self.lease()
        once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        with self.assertRaises(RunError):
            once.collect_fixture_run(self.store, lease, profile(), clock=lambda: NOW)
        self.assertEqual(len(self.calls),4)

    def test_consumption_failure_before_and_after_commit_never_sends(self):
        self.mock_sends()
        original = ReadRunStore.consume_dispatch
        for after in (False,True):
            ticket, lease = self.lease()
            context = runtime.ReadRunContext(self.store,lease,self.binding,self.store.storage_id,
                                            self.path,self.directory/'fixture-profile.json')
            duplicate = copy.copy(context)
            def failed(store, lease, entry, permit):
                if after:
                    original(store,lease,entry,permit)
                raise RunError('SECRET_DISPATCH_COMMIT_SENTINEL')
            with patch.object(ReadRunStore,'consume_dispatch',failed):
                receipt = once._collect_reserved(self.store,lease,profile(),clock=lambda: NOW,context=context)
            self.assertFalse(receipt.status.collection_complete)
            self.assertNotIn('SECRET_DISPATCH_COMMIT_SENTINEL',receipt.render())
            self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),((0,after),))
            with self.assertRaises(SafetyError):
                duplicate.check()
            with self.assertRaises(RunError):
                self.claim(ticket)
        self.http.assert_not_called()

    def test_dispatch_is_consumed_before_mocked_http(self):
        self.mock_sends()
        ticket, lease = self.lease()
        original = self.http.side_effect
        def checked(session,prepared,**options):
            consumed = self.store.dispatch_snapshot(ticket.run_id)
            self.assertEqual(consumed[-1],(len(self.calls),True))
            return original(session,prepared,**options)
        self.http.side_effect = checked
        once.collect_fixture_run(self.store,lease,profile(),clock=lambda: NOW)
        self.assertEqual(self.store.dispatch_snapshot(ticket.run_id),tuple((i,True) for i in range(4)))

    def test_complete_mocked_supervisor_worker_loader_runtime_path(self):
        self.mock_sends()
        ticket = self.issue()
        profile_path = self.directory/'testnet.json'
        profile_path.write_text(json.dumps({'version':1,'purpose':runtime.PROFILE_PURPOSE,
            'environment':'testnet','origin':HOST,'profile_id':self.binding.profile_id,
            'api_key':KEY,'api_secret':SECRET}))
        profile_path.chmod(0o600)
        config = once.InstalledReadConfiguration(self.binding,self.store.storage_id,self.path,
                    profile_path,once.INSTALLED_PYTHON,once.INSTALLED_ENTRY)
        original_fstat = os.fstat
        checkout = ROOT.stat()
        def protected(fd):
            info = original_fstat(fd)
            if (info.st_dev,info.st_ino) == (checkout.st_dev,checkout.st_ino):
                values = list(info)
                values[0] &= ~0o022
                return os.stat_result(values)
            return info
        def child(process,message):
            # Models the protected child boundary. Neither a real worker launch
            # nor an authenticated exchange observation occurs in this test.
            stdin = SimpleNamespace(buffer=io.BytesIO(message))
            with patch('os.geteuid',return_value=self.uid), patch('os.getpid',return_value=process.pid), \
                    patch('sys.stdin',stdin), patch.object(runtime.os,'fstat',side_effect=protected):
                return json.dumps(once._worker()).encode()
        with patch('os.geteuid',return_value=0), patch.object(once,'_configuration',return_value=config), \
                patch.object(once,'_store',return_value=self.store), \
                patch.object(once.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=self.uid,pw_gid=self.uid)), \
                patch.object(once.subprocess,'Popen') as popen, patch.object(once,'_read_child',side_effect=child):
            popen.return_value.pid = 123456
            reply = once.supervise_once(ticket)
        self.assertTrue(reply['complete'])  # Modeled branch, NOT real authentication.
        self.assertEqual(reply['run_id'],ticket.run_id)
        self.assertEqual(reply['release_id'],self.binding.release_id)
        self.assertEqual(len(self.calls),4)
        self.assertEqual(self.store.snapshot(ticket.run_id)['state'],'COMPLETE')
        self.assertIn('no execution authority',reply['report'])
        for secret in (KEY,SECRET,FAKE_ACCOUNT,FAKE_USER,ticket.token):
            self.assertNotIn(secret,reply['report'])


class SupervisorTests(OfflineTestCase):
    def configuration_fixture(self):
        manifest = b'SYNTHETIC_INSTALLATION_INVENTORY'
        return {'version':1,'purpose':stores.PURPOSE,'profile_id':'a'*32,
            'release_id':hashlib.sha256(manifest).hexdigest(),'worker_uid':2000,'storage_id':'c'*32,
            'store_path':'/var/lib/phemex-readonly-status/approvals.sqlite',
            'profile_path':'/var/lib/phemex-readonly-status/credentials/testnet.json',
            'python':'/usr/bin/python3.12',
            'entry':'/opt/phemex-readonly-status/launch.py'}, manifest

    def configured(self, value, manifest):
        def read(path):
            if path == once.CONFIG:
                return json.dumps(value).encode()
            if path == Path('/opt/phemex-readonly-status/release.json'):
                return manifest
            raise AssertionError('Unexpected configuration/profile read')
        with patch.object(once,'root_path'), patch.object(Path,'read_bytes',read), \
                patch.object(once.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=2000)):
            return once._configuration()

    def test_fixed_protected_configuration_has_no_credential_input(self):
        value, manifest = self.configuration_fixture()
        result = self.configured(value,manifest)
        self.assertEqual(result.binding.profile_id,'a'*32)
        self.assertEqual(result.profile_path,Path(value['profile_path']))

    def test_configuration_rejects_extra_keys_wrong_paths_and_uid(self):
        value, manifest = self.configuration_fixture()
        for changes in ({'api_key':'SECRET_SENTINEL'}, {'environment':'live'}, {'version':True},
                        {'profile_path':'/home/cli/mainnet.json'}, {'worker_uid':1000},
                        {'python':'/home/cli/python'}, {'entry':'/home/cli/worker.py'}):
            with self.subTest(changes=changes), self.assertRaises(RunError) as error:
                self.configured({**value,**changes},manifest)
            self.assertNotIn('SECRET_SENTINEL',str(error.exception))

    def test_configuration_release_mismatch_fails_closed(self):
        value, _ = self.configuration_fixture()
        with self.assertRaises(RunError):
            self.configured(value,b'REPLACED_RELEASE')

    def test_public_supervisor_and_child_stay_activation_blocked(self):
        with patch('os.geteuid', return_value=0), patch.object(once, '_configuration') as config, \
                patch.object(once.subprocess, 'Popen') as popen:
            with self.assertRaises(SafetyError):
                once.supervise_once(ReadTicket('a'*32,'b'*64))
            with self.assertRaises(SafetyError):
                once._worker()
            with self.assertRaises(SafetyError):
                once.issue_once()
        config.assert_not_called()
        popen.assert_not_called()

    def test_untrusted_uid_cannot_supervise(self):
        with patch('os.geteuid', return_value=1000), self.assertRaises(RunError):
            once.supervise_once(ReadTicket('a'*32,'b'*64))

    def test_message_rejects_duplicate_extra_encoding_and_size(self):
        for raw in (b'{"version":1,"version":1,"run_id":"a","token":"b"}',
                    b'{"version":1,"run_id":"a","token":"b","url":"https://api.phemex.com"}',
                    b'{"version":true,"run_id":"a","token":"b"}', b'\xff', b'{', b' '*2049):
            with self.assertRaises(RunError):
                once._decode_message(raw, ('version','run_id','token'))

    def test_reply_rejects_synthetic_or_incomplete_schema(self):
        for value in ({'version':1,'complete':True,'origin':'mocked_http','report':'fake'},
                      {'version':1,'complete':True,'origin':'protected_testnet_worker','report':'fake','extra':1},
                      {'version':1,'complete':1,'origin':'protected_testnet_worker','report':'fake'}):
            with self.assertRaises(RunError):
                once._decode_reply(json.dumps(value).encode())

    def _child(self, code):
        # Synthetic local process only. This is not a deployed worker or HTTP
        # client. It proves that a separate parent enforces timeout/output cap.
        return subprocess.Popen([sys.executable,'-I','-B','-c',code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)

    def test_external_deadline_kills_uncooperative_synthetic_child(self):
        child = self._child('import time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)')
        with self.assertRaises(RunError):
            once._read_child(child,b'{}',seconds=0.15)
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_bounded_reply_and_unexpected_exit(self):
        child = self._child('import sys; sys.stdin.buffer.read(); print("x"*200000)')
        with self.assertRaises(RunError):
            once._read_child(child,b'{}',seconds=5)
        child = self._child('raise SystemExit(1)')
        with self.assertRaises(RunError):
            once._read_child(child,b'{}',seconds=5)

    def test_external_supervisor_allows_small_success_without_restart(self):
        child = self._child('import sys; sys.stdin.buffer.read(); print("offline fixture")')
        self.assertEqual(once._read_child(child,b'{}',seconds=5), b'offline fixture\n')

    def test_help_has_no_sensitive_side_effects(self):
        with patch.object(once, '_configuration', side_effect=AssertionError('config')), \
                patch.object(runtime, 'load_testnet_profile', side_effect=AssertionError('profile')), \
                patch.object(once.subprocess, 'Popen', side_effect=AssertionError('child')), \
                patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(once.main(['--help']),0)

    def test_issuer_rejects_untrusted_uid(self):
        with patch('os.geteuid', return_value=1000), self.assertRaises(RunError):
            once.issue_once()


def load_tests(loader, tests, pattern):
    import unittest
    suite = unittest.TestSuite()
    for cls in (StoreTests, ContextTests, CollectionTests, SupervisorTests):
        names = [name for name in cls.__dict__ if name.startswith('test_')]
        suite.addTests(cls(name) for name in sorted(names))
    return suite
