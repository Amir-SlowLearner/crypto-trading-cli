"""Worker-owned, offline-purpose approvals; never an order/execution journal.

SQLite enforces one-use claims and request reservations across processes. The
deployment must keep this database and trusted worker code out of caller control.
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

from safety import SafetyError


URLS = (
    'https://testnet-api.phemex.com/public/products',
    'https://testnet-api.phemex.com/v1/exchange/public/products',
    'https://testnet-api.phemex.com/g-accounts/positions?currency=USDT',
    'https://testnet-api.phemex.com/g-accounts/accountPositions?currency=USDT',
)
PURPOSE = 'offline-readonly-worker-v1'
PLAN_HASH = hashlib.sha256(json.dumps((PURPOSE, 'GET', URLS)).encode()).hexdigest()
APPLICATION_ID = 0x524F4754
CONSENT = 'APPROVE OFFLINE FOUR TESTNET GETS'
SCHEMA = (
    """CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
        plan_hash TEXT NOT NULL, purpose TEXT NOT NULL,
        issued INTEGER NOT NULL, expires INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('APPROVED','RUNNING','COMPLETE','BLOCKED')),
        next_slot INTEGER NOT NULL DEFAULT 0 CHECK(next_slot BETWEEN 0 AND 4),
        owner_hash TEXT, owner_pid INTEGER)""",
    """CREATE TABLE attempts (
        approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
        slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 3),
        state TEXT NOT NULL CHECK(state IN ('UNCERTAIN','OBSERVED','FAILED')),
        reserved INTEGER NOT NULL, finished INTEGER,
        PRIMARY KEY(approval_id, slot))""",
)


class WorkerStoreError(SafetyError):
    pass


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _hex(value, length):
    return type(value) is str and len(value) == length and all(c in '0123456789abcdef' for c in value)


@dataclass(frozen=True)
class OfflineApproval:
    approval_id: str
    token: str = field(repr=False)

    def message(self):
        return json.dumps({'version': 1, 'approval_id': self.approval_id, 'token': self.token}).encode()


@dataclass(frozen=True)
class _Owner:
    approval_id: str
    nonce: str = field(repr=False)


class ApprovalStore:
    def __init__(self, path, *, clock=None):
        self.path = Path(path).absolute()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._transaction():
            pass

    def _now(self):
        value = self.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise WorkerStoreError('Invalid worker clock')
        return int(value.timestamp())

    def _secure_files(self):
        try:
            parent, db = self.path.parent.lstat(), self.path.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077
                    or not stat.S_ISREG(db.st_mode) or db.st_uid != os.geteuid()
                    or db.st_mode & 0o077 or db.st_nlink != 1):
                raise WorkerStoreError('Worker store requires private, owned regular files')
            return db.st_dev, db.st_ino
        except OSError:
            raise WorkerStoreError('Worker store unavailable') from None

    @classmethod
    def create(cls, path, *, clock=None):
        path = Path(path).absolute()
        connection = None
        try:
            parent = path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise WorkerStoreError('Worker store directory must be private and owned')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            connection = sqlite3.connect(path, isolation_level=None)
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=EXTRA')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute('PRAGMA user_version=1')
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute('COMMIT')
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, sqlite3.Error):
            # Never replace/delete a partial or existing approval store to recover.
            raise WorkerStoreError('Cannot initialize worker store') from None
        finally:
            if connection is not None:
                connection.close()
        return cls(path, clock=clock)

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            identity = self._secure_files()
            connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True,
                                         isolation_level=None, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=EXTRA')
            if (self._secure_files() != identity
                    or connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or connection.execute('PRAGMA user_version').fetchone()[0] != 1
                    or connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'):
                raise WorkerStoreError('Invalid worker store identity or purpose')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            if self._secure_files() != identity:
                raise WorkerStoreError('Worker store changed during transaction')
            connection.execute('COMMIT')
        except (OSError, sqlite3.Error):
            raise WorkerStoreError('Worker store transaction failed; authorization remains unavailable') from None
        finally:
            if connection is not None:
                connection.close()  # Any uncommitted transaction rolls back.

    def issue(self, consent):
        """Trusted operator API only. No approval-issuance command exists in IPC."""
        if consent != CONSENT:
            raise WorkerStoreError('Explicit offline approval consent required')
        approval = OfflineApproval(secrets.token_hex(16), secrets.token_hex(32))
        now = self._now()
        with self._transaction() as db:
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,0,NULL,NULL)',
                       (approval.approval_id, _digest(approval.token), PLAN_HASH, PURPOSE, now, now + 300, 'APPROVED'))
        return approval

    def claim(self, approval_id, token):
        if not _hex(approval_id, 32) or not _hex(token, 64):
            raise WorkerStoreError('Invalid offline approval')
        owner = None
        with self._transaction() as db:
            row = db.execute('SELECT * FROM approvals WHERE approval_id=?', (approval_id,)).fetchone()
            if (row is None or not hmac.compare_digest(row['token_hash'], _digest(token))
                    or row['state'] != 'APPROVED' or row['next_slot'] != 0
                    or row['plan_hash'] != PLAN_HASH or row['purpose'] != PURPOSE
                    or db.execute('SELECT 1 FROM attempts WHERE approval_id=?', (approval_id,)).fetchone()):
                raise WorkerStoreError('Approval unavailable or already consumed')
            if row['issued'] <= self._now() < row['expires']:
                owner = _Owner(approval_id, secrets.token_hex(32))
                db.execute("UPDATE approvals SET state='RUNNING',owner_hash=?,owner_pid=? WHERE approval_id=?",
                           (_digest(owner.nonce), os.getpid(), approval_id))
            else:
                db.execute("UPDATE approvals SET state='BLOCKED' WHERE approval_id=?", (approval_id,))
        if owner is None:
            raise WorkerStoreError('Approval expired or clock-inconsistent')
        return owner

    def _owned(self, db, owner):
        if type(owner) is not _Owner:
            raise WorkerStoreError('Invalid worker ownership')
        row = db.execute('SELECT * FROM approvals WHERE approval_id=?', (owner.approval_id,)).fetchone()
        if (row is None or row['owner_pid'] != os.getpid() or row['owner_hash'] != _digest(owner.nonce)
                or row['plan_hash'] != PLAN_HASH or row['purpose'] != PURPOSE):
            raise WorkerStoreError('Worker ownership or scope mismatch')
        return row

    def reserve(self, owner, slot):
        """Commit UNCERTAIN before dispatch. No method can reclaim that slot."""
        if type(slot) is not int or slot not in range(4):
            raise WorkerStoreError('Unapproved worker request slot')
        with self._transaction() as db:
            row = self._owned(db, owner)
            previous = db.execute('SELECT slot,state FROM attempts WHERE approval_id=? ORDER BY slot',
                                  (owner.approval_id,)).fetchall()
            if (row['state'] != 'RUNNING' or row['next_slot'] != slot
                    or not row['issued'] <= self._now() < row['expires']
                    or [(r['slot'], r['state']) for r in previous] != [(i, 'OBSERVED') for i in range(slot)]):
                raise WorkerStoreError('Run blocked, stale, uncertain, duplicated or out of order')
            db.execute('INSERT INTO attempts VALUES (?,?,\'UNCERTAIN\',?,NULL)',
                       (owner.approval_id, slot, self._now()))
            db.execute('UPDATE approvals SET next_slot=next_slot+1 WHERE approval_id=?', (owner.approval_id,))

    def record(self, owner, slot, success):
        if type(success) is not bool:
            raise WorkerStoreError('Invalid observation result')
        with self._transaction() as db:
            row = self._owned(db, owner)
            if row['state'] != 'RUNNING' or row['next_slot'] != slot + 1:
                raise WorkerStoreError('Run is not awaiting this observation')
            changed = db.execute("UPDATE attempts SET state=?,finished=? WHERE approval_id=? AND slot=? AND state='UNCERTAIN'",
                                 ('OBSERVED' if success else 'FAILED', self._now(), owner.approval_id, slot)).rowcount
            if changed != 1:
                raise WorkerStoreError('Observation replay or missing reservation')
            if not success:
                db.execute("UPDATE approvals SET state='BLOCKED' WHERE approval_id=?", (owner.approval_id,))

    def finish(self, owner, complete):
        with self._transaction() as db:
            row = self._owned(db, owner)
            states = db.execute('SELECT slot,state FROM attempts WHERE approval_id=? ORDER BY slot',
                                (owner.approval_id,)).fetchall()
            success = (complete is True and row['state'] == 'RUNNING' and row['next_slot'] == 4
                       and row['issued'] <= self._now() < row['expires']
                       and [(r['slot'], r['state']) for r in states] == [(i, 'OBSERVED') for i in range(4)])
            if row['state'] not in ('RUNNING', 'BLOCKED'):
                raise WorkerStoreError('Run already terminal')
            db.execute('UPDATE approvals SET state=? WHERE approval_id=?',
                       ('COMPLETE' if success else 'BLOCKED', owner.approval_id))
        return success

    def snapshot(self, approval_id):
        """Trusted audit API; never returns approval hashes, tokens or account data."""
        with self._transaction() as db:
            row = db.execute('SELECT state,next_slot FROM approvals WHERE approval_id=?', (approval_id,)).fetchone()
            if row is None:
                raise WorkerStoreError('Unknown approval')
            attempts = db.execute('SELECT slot,state FROM attempts WHERE approval_id=? ORDER BY slot', (approval_id,)).fetchall()
            return {'state': row['state'], 'attempts': tuple((r['slot'], r['state']) for r in attempts)}
