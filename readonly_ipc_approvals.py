"""B2 offline approval foundation. No authenticated collection authority.

This is a separate namespace from the synthetic worker's approvals. Synthetic
peers stand in for a FUTURE independently authenticated IPC boundary in tests;
constructing a Python object is not OS authentication or a security sandbox.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from readonly_worker_store import ApprovalStore, URLS, _digest, _hex
from safety import SafetyError


VERSION = 2
PURPOSE = 'authenticated-readonly-intent-v2'
MODE = 'OFFLINE_FOUNDATION_ONLY'
APPLICATION_ID = 0x52495032
PLAN_HASH = hashlib.sha256(json.dumps((PURPOSE, 'testnet', 'GET', URLS),
                                    separators=(',', ':')).encode()).hexdigest()
MAX_LIFETIME = 300


class ApprovalError(SafetyError):
    pass


@dataclass(frozen=True)
class SyntheticPeer:
    """Trusted test injection ONLY. Never decode this identity from an IPC body."""
    uid: int

    def __post_init__(self):
        if type(self.uid) is not int or not 0 <= self.uid < 2**32 - 1:
            raise ApprovalError('Invalid synthetic peer')


def _admin(peer):
    if type(peer) is not SyntheticPeer or peer.uid != 0:
        raise ApprovalError('Independent administrator authentication required')


@dataclass(frozen=True)
class Binding:
    caller_uid: int
    profile_id: str  # Opaque identifier, never a credential or filesystem path.
    release_id: str  # Independently approved deployment-manifest SHA-256.
    environment: str = 'testnet'
    plan_hash: str = PLAN_HASH

    def __post_init__(self):
        if (type(self.caller_uid) is not int or not 0 < self.caller_uid < 2**32 - 1
                or not _hex(self.profile_id, 32) or not _hex(self.release_id, 64)
                or type(self.environment) is not str or self.environment != 'testnet'
                or type(self.plan_hash) is not str or self.plan_hash != PLAN_HASH):
            raise ApprovalError('Invalid read-only approval binding')

    def values(self):
        return self.caller_uid, self.profile_id, self.release_id, self.environment, self.plan_hash


@dataclass(frozen=True)
class Ticket:
    run_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class _Lease:
    run_id: str
    nonce: str = field(repr=False)
    binding: Binding = field(repr=False)
    storage_id: str


class BoundApprovalStore:
    """Trusted bootstrap supplies path and storage ID; neither is an IPC input.

    Reuses the existing secure-file guard, endpoint literals and token primitives.
    The old store's transaction/issuance methods hard-code its offline purpose,
    so they cannot be inherited for this distinct database namespace.
    """
    def __init__(self, path, storage_id, *, clock=None):
        if not _hex(storage_id, 32):
            raise ApprovalError('Expected approval storage identity required')
        self.path = Path(path).absolute()
        self.storage_id = storage_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._file_identity = ApprovalStore._secure_files(self)
        with self._transaction():
            pass

    def _now(self):
        value = self.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalError('Invalid approval clock')
        return int(value.timestamp())

    @classmethod
    def initialize(cls, path, *, administrator, clock=None):
        """Explicit offline fixture/admin provisioning; never called by IPC."""
        _admin(administrator)
        path = Path(path).absolute()
        connection = None
        storage_id = secrets.token_hex(16)
        try:
            parent = path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise ApprovalError('Private administrator-provisioned storage required')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            connection = sqlite3.connect(path, isolation_level=None)
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=EXTRA')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={VERSION}')
            connection.execute('CREATE TABLE identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                               'purpose TEXT NOT NULL, mode TEXT NOT NULL, storage_id TEXT NOT NULL)')
            connection.execute('INSERT INTO identity VALUES (1,?,?,?)', (PURPOSE, MODE, storage_id))
            connection.execute("""CREATE TABLE approvals (
                run_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                caller_uid INTEGER NOT NULL, profile_id TEXT NOT NULL, release_id TEXT NOT NULL,
                environment TEXT NOT NULL CHECK(environment='testnet'), plan_hash TEXT NOT NULL,
                issued INTEGER NOT NULL, expires INTEGER NOT NULL CHECK(expires>issued),
                state TEXT NOT NULL CHECK(state IN ('APPROVED','CLAIMED','BLOCKED')),
                owner_hash TEXT, owner_pid INTEGER)""")
            connection.execute("""CREATE TABLE reservations (
                run_id TEXT NOT NULL REFERENCES approvals(run_id),
                slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 3),
                reserved INTEGER NOT NULL, PRIMARY KEY(run_id,slot))""")
            connection.execute('COMMIT')
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, sqlite3.Error):
            raise ApprovalError('Approval storage initialization failed; no reset permitted') from None
        finally:
            if connection is not None:
                connection.close()
        return cls(path, storage_id, clock=clock)

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            if ApprovalStore._secure_files(self) != self._file_identity:
                raise ApprovalError('Approval storage replaced')
            connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True,
                                         isolation_level=None, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=EXTRA')
            connection.execute('BEGIN IMMEDIATE')
            if (ApprovalStore._secure_files(self) != self._file_identity
                    or connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or connection.execute('PRAGMA user_version').fetchone()[0] != VERSION
                    or connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                    or [tuple(row) for row in connection.execute('SELECT * FROM identity')] !=
                       [(1, PURPOSE, MODE, self.storage_id)]):
                raise ApprovalError('Approval storage identity or purpose mismatch')
            yield connection
            if ApprovalStore._secure_files(self) != self._file_identity:
                raise ApprovalError('Approval storage changed during transaction')
            connection.execute('COMMIT')
        except (OSError, sqlite3.Error):
            raise ApprovalError('Approval transaction failed; no automatic retry permitted') from None
        finally:
            if connection is not None:
                connection.close()

    def _issue(self, binding, lifetime):
        if type(binding) is not Binding or type(lifetime) is not int or not 0 < lifetime <= MAX_LIFETIME:
            raise ApprovalError('Invalid approval scope or lifetime')
        ticket = Ticket(secrets.token_hex(16), secrets.token_hex(32))
        now = self._now()
        with self._transaction() as db:
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                       (ticket.run_id, _digest(ticket.token), *binding.values(), now, now + lifetime, 'APPROVED'))
        return ticket

    def claim(self, run_id, token, *, peer, binding):
        if (type(peer) is not SyntheticPeer or type(binding) is not Binding
                or peer.uid != binding.caller_uid or not _hex(run_id, 32) or not _hex(token, 64)):
            raise ApprovalError('Read-only approval unavailable')
        lease = None
        with self._transaction() as db:
            row = db.execute('SELECT * FROM approvals WHERE run_id=?', (run_id,)).fetchone()
            if (row is None or row['state'] != 'APPROVED'
                    or not hmac.compare_digest(row['token_hash'], _digest(token))
                    or tuple(row[name] for name in ('caller_uid', 'profile_id', 'release_id',
                                                    'environment', 'plan_hash')) != binding.values()
                    or db.execute('SELECT 1 FROM reservations WHERE run_id=?', (run_id,)).fetchone()):
                raise ApprovalError('Read-only approval unavailable')
            now = self._now()
            if row['issued'] <= now < row['expires'] and 0 < row['expires'] - row['issued'] <= MAX_LIFETIME:
                lease = _Lease(run_id, secrets.token_hex(32), binding, self.storage_id)
                db.execute("UPDATE approvals SET state='CLAIMED',owner_hash=?,owner_pid=? WHERE run_id=?",
                           (_digest(lease.nonce), os.getpid(), run_id))
            else:
                db.execute("UPDATE approvals SET state='BLOCKED' WHERE run_id=?", (run_id,))
        if lease is None:
            raise ApprovalError('Read-only approval expired or clock inconsistent')
        return lease

    def _owned(self, db, lease):
        if type(lease) is not _Lease or lease.storage_id != self.storage_id:
            raise ApprovalError('Invalid read-only run ownership')
        row = db.execute('SELECT * FROM approvals WHERE run_id=?', (lease.run_id,)).fetchone()
        if (row is None or row['owner_pid'] != os.getpid()
                or row['owner_hash'] != _digest(lease.nonce)
                or tuple(row[name] for name in ('caller_uid', 'profile_id', 'release_id',
                                                'environment', 'plan_hash')) != lease.binding.values()):
            raise ApprovalError('Invalid read-only run ownership')
        return row

    def reserve_plan(self, lease):
        """Irrevocably reserve ALL four slots, atomically, without dispatching.

        This is budget accounting, not four executable permits or observations.
        No completion, send, retry, renew or reset API exists in this foundation.
        """
        with self._transaction() as db:
            row = self._owned(db, lease)
            now = self._now()
            if (row['state'] != 'CLAIMED' or not row['issued'] <= now < row['expires']
                    or db.execute('SELECT 1 FROM reservations WHERE run_id=?', (lease.run_id,)).fetchone()):
                raise ApprovalError('Read-only plan unavailable or already reserved')
            db.executemany('INSERT INTO reservations VALUES (?,?,?)',
                           ((lease.run_id, slot, now) for slot in range(len(URLS))))

    def block(self, lease):
        with self._transaction() as db:
            row = self._owned(db, lease)
            if row['state'] not in ('CLAIMED', 'BLOCKED'):
                raise ApprovalError('Invalid read-only run state')
            db.execute("UPDATE approvals SET state='BLOCKED' WHERE run_id=?", (lease.run_id,))

    def snapshot(self, run_id):
        """Trusted audit only: never returns tokens, profile or account data."""
        if not _hex(run_id, 32):
            raise ApprovalError('Invalid read-only run')
        with self._transaction() as db:
            row = db.execute('SELECT state FROM approvals WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise ApprovalError('Read-only run unavailable')
            slots = tuple(row['slot'] for row in db.execute(
                'SELECT slot FROM reservations WHERE run_id=? ORDER BY slot', (run_id,)))
            return {'state': row['state'], 'reserved_slots': slots}


class OfflineAdministratorIssuer:
    """Separate trusted test/admin API. Not callable through the IPC protocol."""
    def __init__(self, store):
        if type(store) is not BoundApprovalStore:
            raise ApprovalError('Bound approval storage required')
        self._store = store

    def issue(self, *, administrator, binding, lifetime=MAX_LIFETIME):
        _admin(administrator)  # A consent string or caller UID is not authentication.
        return self._store._issue(binding, lifetime)
