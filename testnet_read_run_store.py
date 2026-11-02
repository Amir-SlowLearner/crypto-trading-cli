"""Separate durable *prepared* actual-read approvals. No network authority.

Only the future protected supervisor may issue/claim records. Existing offline
worker/IPC purposes cannot be used here. Python objects are not authentication.
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

from readonly_worker_store import _digest, _hex, URLS
from safety import SafetyError
from testnet_transport import PLAN


PURPOSE = 'prepared-testnet-status-read-v1'
APPLICATION_ID = 0x54535231
PLAN_HASH = hashlib.sha256(json.dumps((PURPOSE, 'testnet', 'GET', URLS),
                                    separators=(',', ':')).encode()).hexdigest()
MAX_LIFETIME = 300
SCHEMA_VERSION = 2  # Explicit dispatch consumption; v1 stores are not adopted.


class RunError(SafetyError):
    pass


@dataclass(frozen=True)
class ReadBinding:
    profile_id: str = field(repr=False)
    release_id: str
    worker_uid: int
    environment: str = 'testnet'
    plan_hash: str = PLAN_HASH

    def __post_init__(self):
        if (not _hex(self.profile_id, 32) or not _hex(self.release_id, 64)
                or type(self.worker_uid) is not int or not 0 < self.worker_uid < 2**32 - 1
                or self.environment != 'testnet' or self.plan_hash != PLAN_HASH):
            raise RunError('Invalid fixed Testnet read binding')

    def values(self):
        return self.profile_id, self.release_id, self.worker_uid, self.environment, self.plan_hash


@dataclass(frozen=True)
class ReadTicket:
    run_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class ReadLease:
    run_id: str
    nonce: str = field(repr=False)
    binding: ReadBinding
    storage_id: str


@dataclass(frozen=True)
class DispatchPermit:
    """Returned once after reservation COMMIT; never recoverable from storage."""
    run_id: str
    slot: int
    nonce: str = field(repr=False)


class ReadRunStore:
    """Private SQLite store, no reset/resume/replenish API.

    A root supervisor issues and claims; the dedicated UID reserves and records.
    Root can always tamper/rollback storage. That is outside this local guarantee.
    """
    def __init__(self, path, *, storage_id, worker_uid, clock=None):
        if not _hex(storage_id, 32) or type(worker_uid) is not int or worker_uid <= 0:
            raise RunError('Invalid protected read storage configuration')
        self.path = Path(path).absolute()
        self.storage_id, self.worker_uid = storage_id, worker_uid
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._identity = self._files()
        with self._transaction():
            pass

    def _now(self):
        value = self.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise RunError('Invalid read approval clock')
        return int(value.timestamp())

    def _files(self):
        try:
            # Reject symlinks/ACLs on ALL components. Trusted mounts and the
            # worker identity remain deployment prerequisites, not Python claims.
            for part in reversed((self.path.parent, *self.path.parent.parents)):
                info = part.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, self.worker_uid)
                        or info.st_mode & 0o022
                        or any(x.startswith('system.posix_acl_') for x in os.listxattr(part, follow_symlinks=False))):
                    raise RunError('Unsafe read storage ancestry')
            parent, info = self.path.parent.lstat(), self.path.lstat()
            if (parent.st_uid != self.worker_uid or stat.S_IMODE(parent.st_mode) != 0o700
                    or not stat.S_ISREG(info.st_mode) or info.st_uid != self.worker_uid
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or any(x.startswith('system.posix_acl_') for x in os.listxattr(self.path, follow_symlinks=False))):
                raise RunError('Read storage must be private and worker owned')
            return info.st_dev, info.st_ino
        except OSError:
            raise RunError('Read approval storage unavailable') from None

    @classmethod
    def initialize(cls, path, *, worker_uid, clock=None):
        """Explicit worker-identity initialization ONLY; never recreate on open."""
        if type(worker_uid) is not int or worker_uid <= 0 or os.geteuid() != worker_uid:
            raise RunError('Dedicated worker identity required for initialization')
        path = Path(path).absolute()
        identity = secrets.token_hex(16)
        connection = None
        try:
            parent = path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != worker_uid
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise RunError('Private pre-provisioned read directory required')
            # Check all ancestors before creating anything. A failed partial
            # initialization is never exposed as a usable store or recreated.
            for part in reversed((path.parent, *path.parent.parents)):
                info = part.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, worker_uid)
                        or info.st_mode & 0o022
                        or any(x.startswith('system.posix_acl_') for x in os.listxattr(part, follow_symlinks=False))):
                    raise RunError('Unsafe read storage ancestry')
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            connection = sqlite3.connect(path, isolation_level=None)
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=EXTRA')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            connection.execute('CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK(id=1), purpose TEXT, storage TEXT)')
            connection.execute('INSERT INTO identity VALUES (1,?,?)', (PURPOSE, identity))
            connection.execute('''CREATE TABLE runs (
                run TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, profile TEXT NOT NULL,
                release TEXT NOT NULL, uid INTEGER NOT NULL, environment TEXT NOT NULL, plan TEXT NOT NULL,
                issued INTEGER NOT NULL, expires INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('APPROVED','CLAIMED','RUNNING','COMPLETE','BLOCKED')),
                nonce TEXT, pid INTEGER)''')
            connection.execute('''CREATE TABLE slots (
                run TEXT REFERENCES runs(run), slot INTEGER CHECK(slot BETWEEN 0 AND 3),
                state TEXT NOT NULL CHECK(state IN ('UNCERTAIN','OBSERVED','FAILED')),
                reserved INTEGER NOT NULL, permit TEXT NOT NULL UNIQUE,
                consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
                PRIMARY KEY(run,slot))''')
            connection.execute('COMMIT')
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, sqlite3.Error):
            raise RunError('Read storage initialization failed; no automatic repair') from None
        finally:
            if connection is not None:
                connection.close()
        return cls(path, storage_id=identity, worker_uid=worker_uid, clock=clock)

    @contextmanager
    def _transaction(self):
        db = None
        try:
            if self._files() != self._identity:
                raise RunError('Read storage replaced')
            db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=1)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=EXTRA')
            db.execute('BEGIN IMMEDIATE')
            if (self._files() != self._identity
                    or db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                    or db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                    or [tuple(row) for row in db.execute('SELECT * FROM identity')] != [(1, PURPOSE, self.storage_id)]):
                raise RunError('Read storage identity or purpose mismatch')
            yield db
            if self._files() != self._identity:
                raise RunError('Read storage changed')
            db.execute('COMMIT')
        except (OSError, sqlite3.Error):
            raise RunError('Read storage transaction uncertain; no retry or repair') from None
        finally:
            if db is not None:
                db.close()

    def issue(self, binding, *, lifetime=MAX_LIFETIME):
        # Real OS identity, not a caller-supplied UID or public consent phrase.
        if os.geteuid() != 0:
            raise RunError('Independent root operator required')
        if (type(binding) is not ReadBinding or binding.worker_uid != self.worker_uid
                or type(lifetime) is not int or not 0 < lifetime <= MAX_LIFETIME):
            raise RunError('Invalid read approval binding or lifetime')
        ticket, now = ReadTicket(secrets.token_hex(16), secrets.token_hex(32)), self._now()
        with self._transaction() as db:
            # A crash/timeout is still an outstanding run. Root must review it;
            # this API cannot mint a replacement while an unresolved run exists.
            if db.execute("SELECT 1 FROM runs WHERE state IN ('APPROVED','CLAIMED','RUNNING')").fetchone():
                raise RunError('An outstanding read approval requires operator review')
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                       (ticket.run_id, _digest(ticket.token), *binding.values(), now, now + lifetime, 'APPROVED'))
        return ticket

    def claim(self, ticket, binding):
        if (os.geteuid() != 0 or type(ticket) is not ReadTicket or type(binding) is not ReadBinding
                or not _hex(ticket.run_id, 32) or not _hex(ticket.token, 64)):
            raise RunError('Protected supervisor approval required')
        lease = None
        with self._transaction() as db:
            row = db.execute('SELECT * FROM runs WHERE run=?', (ticket.run_id,)).fetchone()
            if (row is None or row['state'] != 'APPROVED'
                    or not hmac.compare_digest(row['token'], _digest(ticket.token))
                    or tuple(row[k] for k in ('profile','release','uid','environment','plan')) != binding.values()
                    or db.execute('SELECT 1 FROM slots WHERE run=?', (ticket.run_id,)).fetchone()):
                raise RunError('Read approval unavailable or already consumed')
            now = self._now()
            if row['issued'] <= now < row['expires'] and 0 < row['expires'] - row['issued'] <= MAX_LIFETIME:
                lease = ReadLease(ticket.run_id, secrets.token_hex(32), binding, self.storage_id)
                db.execute("UPDATE runs SET state='CLAIMED',nonce=? WHERE run=?", (_digest(lease.nonce), ticket.run_id))
            else:
                db.execute("UPDATE runs SET state='BLOCKED' WHERE run=?", (ticket.run_id,))
        if lease is None:
            raise RunError('Read approval expired or clock inconsistent')
        return lease

    def _owned(self, db, lease):
        if (type(lease) is not ReadLease or type(lease.binding) is not ReadBinding
                or not _hex(lease.run_id, 32) or not _hex(lease.nonce, 64)
                or lease.storage_id != self.storage_id):
            raise RunError('Invalid read run ownership')
        row = db.execute('SELECT * FROM runs WHERE run=?', (lease.run_id,)).fetchone()
        if (row is None or row['nonce'] != _digest(lease.nonce)
                or tuple(row[k] for k in ('profile','release','uid','environment','plan')) != lease.binding.values()):
            raise RunError('Read run binding mismatch')
        return row

    def attach(self, lease, pid):
        if os.geteuid() != 0 or type(pid) is not int or pid <= 0:
            raise RunError('Protected supervisor must attach one worker')
        with self._transaction() as db:
            row = self._owned(db, lease)
            if row['state'] != 'CLAIMED' or row['pid'] is not None:
                raise RunError('Worker already attached or run unavailable')
            db.execute("UPDATE runs SET state='RUNNING',pid=? WHERE run=?", (pid, lease.run_id))

    def _active(self, db, lease):
        row = self._owned(db, lease)
        if (os.geteuid() != self.worker_uid or row['pid'] != os.getpid() or row['state'] != 'RUNNING'
                or not row['issued'] <= self._now() < row['expires']):
            raise RunError('Read worker inactive, expired or mismatched')
        return row

    def reserve(self, lease, entry):
        if type(lease) is not ReadLease or type(entry) is not type(PLAN[0]) or entry not in PLAN:
            raise RunError('Unapproved read request')
        slot = PLAN.index(entry)
        permit = DispatchPermit(lease.run_id, slot, secrets.token_hex(32))
        with self._transaction() as db:
            self._active(db, lease)
            prior = [tuple(r) for r in db.execute('SELECT slot,state FROM slots WHERE run=? ORDER BY slot', (lease.run_id,))]
            if prior != [(i, 'OBSERVED') for i in range(slot)]:
                raise RunError('Read request duplicated, out of order or uncertain')
            db.execute("INSERT INTO slots VALUES (?,?,'UNCERTAIN',?,?,0)",
                       (lease.run_id, slot, self._now(), _digest(permit.nonce)))
        return permit  # Never returned on a failed/uncertain COMMIT.

    def consume_dispatch(self, lease, entry, permit):
        """Commit one irreversible send claim. No retry or permit recovery API."""
        if (type(entry) is not type(PLAN[0]) or entry not in PLAN or type(permit) is not DispatchPermit
                or type(lease) is not ReadLease or permit.run_id != lease.run_id
                or type(permit.slot) is not int or permit.slot != PLAN.index(entry)
                or not _hex(permit.nonce, 64)):
            raise RunError('Invalid single-use dispatch permission')
        with self._transaction() as db:
            self._active(db, lease)
            changed = db.execute("""UPDATE slots SET consumed=1 WHERE run=? AND slot=?
                AND state='UNCERTAIN' AND consumed=0 AND permit=?""",
                (lease.run_id, permit.slot, _digest(permit.nonce))).rowcount
            if changed != 1:
                raise RunError('Dispatch permission missing, consumed or mismatched')

    def check_active(self, lease):
        """Validate durable ownership before even attempting a profile read."""
        with self._transaction() as db:
            self._active(db, lease)

    def record(self, lease, entry, success):
        if type(success) is not bool or type(entry) is not type(PLAN[0]) or entry not in PLAN:
            raise RunError('Invalid read response accounting')
        with self._transaction() as db:
            self._active(db, lease)
            changed = db.execute("UPDATE slots SET state=? WHERE run=? AND slot=? AND state='UNCERTAIN' AND consumed=1",
                ('OBSERVED' if success else 'FAILED', lease.run_id, PLAN.index(entry))).rowcount
            if changed != 1:
                raise RunError('Read response missing or duplicated')
            if not success:
                db.execute("UPDATE runs SET state='BLOCKED' WHERE run=?", (lease.run_id,))

    def finish(self, lease, complete):
        if type(complete) is not bool:
            raise RunError('Invalid read completion')
        with self._transaction() as db:
            row = self._owned(db, lease)
            if os.geteuid() not in (0, self.worker_uid) or row['state'] not in ('CLAIMED','RUNNING','BLOCKED'):
                raise RunError('Read run cannot be finalized')
            slots = [tuple(r) for r in db.execute('SELECT slot,state FROM slots WHERE run=? ORDER BY slot', (lease.run_id,))]
            success = (complete and row['state'] == 'RUNNING' and row['pid'] == os.getpid()
                       and os.geteuid() == self.worker_uid and row['issued'] <= self._now() < row['expires']
                       and slots == [(i, 'OBSERVED') for i in range(4)])
            success = bool(success and db.execute(
                'SELECT COUNT(*) FROM slots WHERE run=? AND consumed=1', (lease.run_id,)).fetchone()[0] == 4)
            db.execute('UPDATE runs SET state=? WHERE run=?', ('COMPLETE' if success else 'BLOCKED', lease.run_id))
        return success

    def snapshot(self, run_id):
        if not _hex(run_id, 32) or os.geteuid() not in (0, self.worker_uid):
            raise RunError('Invalid read audit request')
        with self._transaction() as db:
            row = db.execute('SELECT state FROM runs WHERE run=?', (run_id,)).fetchone()
            if row is None:
                raise RunError('Read run unavailable')
            return {'state': row['state'], 'slots': tuple(tuple(r) for r in db.execute(
                'SELECT slot,state FROM slots WHERE run=? ORDER BY slot', (run_id,)))}

    def dispatch_snapshot(self, run_id):
        """Trusted audit: consumed means attempted/uncertain, not HTTP success."""
        if not _hex(run_id, 32) or os.geteuid() not in (0, self.worker_uid):
            raise RunError('Invalid read audit request')
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM runs WHERE run=?', (run_id,)).fetchone() is None:
                raise RunError('Read run unavailable')
            return tuple((row['slot'], bool(row['consumed'])) for row in db.execute(
                'SELECT slot,consumed FROM slots WHERE run=? ORDER BY slot', (run_id,)))
