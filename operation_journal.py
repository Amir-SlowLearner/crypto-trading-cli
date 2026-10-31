"""Durable OFFLINE lifecycle model. No exchange client or submission gateway.

Opening starts a new exclusive session: disarm, invalidate unsent authorization,
and recover possibly submitted operations as uncertain. Never retry a placement.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from uuid import uuid4

from execution_policy import ValidatedIntent, canonical_payload, fingerprint, utc_time
from safety import SafetyError


class JournalError(SafetyError):
    """Static messages only; never echo database values, tokens or SQL errors."""


class State(str, Enum):
    PREPARED = 'PREPARED'
    AUTHORIZED = 'AUTHORIZED'
    SUBMITTING = 'SUBMITTING'
    UNCERTAIN = 'UNCERTAIN'
    ACKNOWLEDGED = 'ACKNOWLEDGED'
    RECONCILING = 'RECONCILING'
    FILLED = 'FILLED'
    CANCELED = 'CANCELED'
    REJECTED = 'REJECTED'
    ABORTED = 'ABORTED'
    EXPIRED = 'EXPIRED'
    BLOCKED = 'BLOCKED'


TERMINAL = frozenset({State.FILLED, State.CANCELED, State.REJECTED, State.ABORTED, State.EXPIRED, State.BLOCKED})
UNSENT = frozenset({State.PREPARED, State.AUTHORIZED})
PAYLOAD_KEYS = frozenset({'symbol', 'market_id', 'side', 'action', 'position_side', 'quantity',
                          'requested_quantity', 'contracts', 'position_quantity', 'price',
                          'order_type', 'time_in_force', 'reduce_only'})
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Irreversible within this process; it is not a cross-process notification channel.
_STOP_REQUESTS = set()


def _time(value):
    delta = utc_time(value) - EPOCH
    result = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    if not 0 <= result < 2 ** 63:
        raise JournalError('Unsupported journal clock')
    return result


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _secure_path(path):
    path = Path(path).absolute()
    parent = path.parent
    info = parent.stat()
    if (parent.resolve() != parent or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700):
        raise JournalError('Journal requires an owner-only directory')
    return path


def _secure_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o777 != 0o600 or info.st_nlink != 1):
        raise JournalError('Journal files must be private regular files')


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _connect(path):
    _secure_file(path)
    connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA synchronous=FULL')
    return connection


@dataclass(frozen=True)
class Operation:
    operation_id: str
    client_order_id: str
    account_binding: str = field(repr=False)
    environment: str
    payload: str = field(repr=False)
    payload_fingerprint: str
    evidence_fingerprint: str
    state: State
    version: int
    expires_at_us: int
    exchange_order_id: str = None
    filled_quantity: str = None


@dataclass(frozen=True)
class Authorization:
    operation_id: str
    token: str = field(repr=False)
    payload_fingerprint: str
    expires_at_us: int
    version: int


@dataclass(frozen=True)
class StopResult:
    filesystem_revoked: bool
    database_revoked: bool
    marker_installed: bool

    @property
    def durable(self):
        return self.filesystem_revoked or self.database_revoked


class OperationJournal:
    def __init__(self, path, *, create=False, clock=None):
        self._connection = None
        self._lock = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            self.path = _secure_path(path)
            self.stop_path = self.path.with_name(self.path.name + '.stop')
            if not create:
                _secure_file(self.path)  # Missing journals are never silently recreated.
            lock_path = self.path.with_name(self.path.name + '.lock')
            self._lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            _secure_file(lock_path)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if create:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            self._connection = _connect(self.path)
            identity = self.path.stat()
            self._identity = identity.st_dev, identity.st_ino
            if create:
                self._initialize()
            if self._connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise JournalError('Unsupported journal durability mode')
            if self._connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise JournalError('Unsupported journal schema')
            if self._connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise JournalError('Journal integrity check failed')
            self._recover()
        except BaseException as error:
            self.close()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise JournalError('Cannot open a private, intact, exclusive journal') from None

    def _initialize(self):
        self._connection.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE control (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                mode TEXT NOT NULL CHECK(mode IN ('DISARMED','ARMED','STOPPED')),
                generation INTEGER NOT NULL CHECK(generation>=0),
                last_clock INTEGER NOT NULL CHECK(last_clock>=0));
            INSERT INTO control VALUES(1,'DISARMED',0,0);
            CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                client_order_id TEXT NOT NULL UNIQUE,
                account_binding TEXT NOT NULL,
                environment TEXT NOT NULL CHECK(environment='testnet'),
                payload TEXT NOT NULL,
                payload_fingerprint TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('PREPARED','AUTHORIZED','SUBMITTING','UNCERTAIN',
                    'ACKNOWLEDGED','RECONCILING','FILLED','CANCELED','REJECTED','ABORTED','EXPIRED','BLOCKED')),
                version INTEGER NOT NULL CHECK(version>=0),
                auth_hash TEXT, auth_generation INTEGER,
                exchange_order_id TEXT, filled_quantity TEXT);
            CREATE UNIQUE INDEX one_active_account ON operations(account_binding,environment)
                WHERE state NOT IN ('FILLED','CANCELED','REJECTED','ABORTED','EXPIRED','BLOCKED');
            CREATE TRIGGER immutable_intent BEFORE UPDATE OF operation_id,client_order_id,account_binding,
                environment,payload,payload_fingerprint,evidence_fingerprint,expires_at ON operations
                BEGIN SELECT RAISE(ABORT,'immutable intent'); END;
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                version INTEGER NOT NULL,
                state TEXT NOT NULL,
                at_us INTEGER NOT NULL,
                UNIQUE(operation_id,version));
            PRAGMA user_version=1;
            COMMIT;
        ''')
        descriptor = os.open(self.stop_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, b'READY\n')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(self.path.parent)

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _control(self):
        rows = self._connection.execute('SELECT * FROM control').fetchall()
        if (len(rows) != 1 or rows[0]['singleton'] != 1 or rows[0]['mode'] not in ('DISARMED', 'ARMED', 'STOPPED')
                or type(rows[0]['generation']) is not int or rows[0]['generation'] < 0
                or type(rows[0]['last_clock']) is not int or rows[0]['last_clock'] < 0):
            raise JournalError('Emergency control state is invalid')
        return rows[0]

    @contextmanager
    def _transaction(self):
        try:
            if self._connection is None:
                raise JournalError('Journal session is closed')
            _secure_path(self.path)
            _secure_file(self.path)
            identity = self.path.stat()
            if (identity.st_dev, identity.st_ino) != self._identity:
                raise JournalError('Journal file was replaced')
            self._connection.execute('BEGIN IMMEDIATE')
            control = self._control()
            now = _time(self._clock())
            if now < control['last_clock']:
                raise JournalError('Journal clock moved backwards')
            self._connection.execute('UPDATE control SET last_clock=? WHERE singleton=1', (now,))
            yield now, control
            self._connection.execute('COMMIT')
        except BaseException as error:
            try:
                if self._connection is not None and self._connection.in_transaction:
                    self._connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass  # Preserve a sanitized failure; recovery must inspect durable state.
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise JournalError('Journal operation refused; no authorization to submit') from None

    def _recover(self):
        with self._transaction() as (now, _):
            self._connection.execute("""UPDATE control SET
                mode=CASE WHEN mode='STOPPED' THEN 'STOPPED' ELSE 'DISARMED' END,
                generation=generation+1 WHERE singleton=1""")
            rows = self._connection.execute('SELECT operation_id FROM operations').fetchall()
            for row in rows:
                operation = self._get(row['operation_id'])
                if operation.state in UNSENT:
                    self._change(operation, State.ABORTED, now, auth_hash=None, auth_generation=None)
                elif operation.state not in TERMINAL and operation.state is not State.UNCERTAIN:
                    self._change(operation, State.UNCERTAIN, now)

    def is_armed(self):
        if self.path in _STOP_REQUESTS:
            return False
        try:
            _secure_path(self.path)
            _secure_file(self.path)
            identity = self.path.stat()
            if (identity.st_dev, identity.st_ino) != self._identity or not self._stop_ready():
                return False
            return self._control()['mode'] == 'ARMED'
        except (OSError, sqlite3.Error, JournalError, AttributeError):
            return False

    def _stop_ready(self):
        try:
            _secure_file(self.stop_path)
            with self.stop_path.open('rb') as stream:
                return stream.read(7) == b'READY\n'
        except (OSError, JournalError):
            return False

    def arm(self, confirmation):
        if confirmation != 'ARM OFFLINE MODEL':
            raise JournalError('Explicit offline arm confirmation required')
        with self._transaction() as (_, control):
            if self.path in _STOP_REQUESTS or control['mode'] == 'STOPPED' or not self._stop_ready():
                raise JournalError('Persistent stop is latched or its state is unavailable')
            self._connection.execute("UPDATE control SET mode='ARMED',generation=generation+1 WHERE singleton=1")

    @staticmethod
    def _persist_database_stop(path):
        """One independent SQLite attempt; never changes operation lifecycle states."""
        connection = None
        try:
            _secure_path(path)
            connection = _connect(path)
            if (connection.execute('PRAGMA user_version').fetchone()[0] != 1
                    or connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'):
                return False
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('SELECT * FROM control').fetchall()
            if (len(rows) != 1 or rows[0]['singleton'] != 1
                    or rows[0]['mode'] not in ('ARMED', 'DISARMED', 'STOPPED')
                    or type(rows[0]['generation']) is not int or not 0 <= rows[0]['generation'] < 2 ** 63 - 1
                    or type(rows[0]['last_clock']) is not int or rows[0]['last_clock'] < 0):
                return False
            connection.execute("UPDATE control SET mode='STOPPED',generation=generation+1 WHERE singleton=1")
            connection.execute("""UPDATE operations SET auth_hash=NULL,auth_generation=NULL
                                  WHERE state IN ('PREPARED','AUTHORIZED')""")
            connection.execute('COMMIT')
            return True
        except (OSError, sqlite3.Error, SafetyError, ValueError, TypeError, IndexError):
            return False
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                except (sqlite3.Error, OSError):
                    pass
                try:
                    connection.close()
                except (sqlite3.Error, OSError):
                    pass

    @staticmethod
    def emergency_stop(path):
        """Revoke locally first, then attempt independent durable stop channels.

        A helper process cannot notify an owner if BOTH persistence channels fail.
        Such a result always raises; it must never be reported as a durable stop.
        """
        try:
            path = Path(path).absolute()
        except (TypeError, ValueError, OSError):
            raise JournalError('Invalid stop path; durable revocation is unconfirmed') from None
        _STOP_REQUESTS.add(path)
        database_revoked = OperationJournal._persist_database_stop(path)
        filesystem_revoked = marker_installed = False
        try:
            _secure_path(path)
            marker = path.with_name(path.name + '.stop')
            # Remove READY first: a crash or failed replacement now leaves a
            # missing control file, which also blocks arming and consumption.
            marker.unlink(missing_ok=True)
            _sync_directory(path.parent)
            filesystem_revoked = True  # Durable absence is itself fail-closed.
            temporary = marker.with_name(marker.name + '.' + uuid4().hex)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(descriptor, b'STOPPED\n')
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, marker)
            _sync_directory(path.parent)
            marker_installed = True
        except (OSError, SafetyError, TypeError, ValueError):
            pass
        result = StopResult(filesystem_revoked, database_revoked, marker_installed)
        if not result.durable:
            raise JournalError('Emergency stop is not durable; only this process is revoked. '
                               'Other processes may remain armed; durable revocation is unconfirmed') from None
        return result

    def reserve(self, intent, *, operation_id=None, client_order_id=None):
        if (type(intent) is not ValidatedIntent or intent.environment != 'testnet'
                or intent.policy_version != 'offline-reduction-v1' or not _digest(intent.account_binding)
                or not _digest(intent.evidence_fingerprint) or type(intent.payload) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2 or any(type(x) is not str for x in pair)
                       for pair in intent.payload)
                or len(intent.payload) != len(PAYLOAD_KEYS) or set(dict(intent.payload)) != PAYLOAD_KEYS
                or any(len(value) > 100 for _, value in intent.payload)):
            raise JournalError('Only immutable validated testnet intents may be reserved')
        payload = canonical_payload(intent.payload)
        if fingerprint(payload) != intent.payload_fingerprint:
            raise JournalError('Intent fingerprint mismatch')
        operation_id = 'op_' + uuid4().hex if operation_id is None else operation_id
        client_order_id = 'tn_' + uuid4().hex if client_order_id is None else client_order_id
        if (not isinstance(operation_id, str) or not re.fullmatch('op_[0-9a-f]{32}', operation_id)
                or not isinstance(client_order_id, str) or not re.fullmatch('tn_[0-9a-f]{32}', client_order_id)):
            raise JournalError('Invalid local operation identifier')
        with self._transaction() as (now, _):
            created, expires = _time(intent.validated_at), _time(intent.expires_at)
            if not created <= now < expires <= created + 5000000:
                raise JournalError('Intent evidence expired or has an invalid validity interval')
            self._connection.execute('''INSERT INTO operations
                (operation_id,client_order_id,account_binding,environment,payload,payload_fingerprint,
                 evidence_fingerprint,expires_at,state,version) VALUES(?,?,?,?,?,?,?,?,?,0)''',
                (operation_id, client_order_id, intent.account_binding, 'testnet', payload,
                 intent.payload_fingerprint, intent.evidence_fingerprint, expires, State.PREPARED.value))
            self._event(operation_id, 0, State.PREPARED, now)
        return self.get(operation_id)

    def _get(self, operation_id):
        row = self._connection.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
        if row is None:
            raise JournalError('Operation is unavailable')
        try:
            payload = json.loads(row['payload'])
            if (not _digest(row['account_binding']) or row['environment'] != 'testnet'
                    or not _digest(row['evidence_fingerprint']) or set(payload) != PAYLOAD_KEYS
                    or any(type(value) is not str for value in payload.values())
                    or fingerprint(row['payload']) != row['payload_fingerprint']
                    or row['payload'] != canonical_payload(tuple(payload.items()))):
                raise ValueError
            state = State(row['state'])
            if type(row['version']) is not int or row['version'] < 0:
                raise ValueError
            event = self._connection.execute('SELECT version,state FROM events WHERE operation_id=? ORDER BY version DESC LIMIT 1',
                                             (operation_id,)).fetchone()
            if event is None or event['version'] != row['version'] or event['state'] != row['state']:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise JournalError('Stored operation is inconsistent') from None
        return Operation(row['operation_id'], row['client_order_id'], row['account_binding'], row['environment'],
                         row['payload'], row['payload_fingerprint'], row['evidence_fingerprint'], state,
                         row['version'], row['expires_at'], row['exchange_order_id'], row['filled_quantity'])

    def get(self, operation_id):
        with self._transaction():
            return self._get(operation_id)

    def unresolved(self):
        with self._transaction():
            rows = self._connection.execute('SELECT operation_id FROM operations ORDER BY operation_id').fetchall()
            return tuple(op for row in rows if (op := self._get(row['operation_id'])).state not in TERMINAL)

    def _event(self, operation_id, version, state, now):
        self._connection.execute('INSERT INTO events(operation_id,version,state,at_us) VALUES(?,?,?,?)',
                                 (operation_id, version, state.value, now))

    def _change(self, operation, target, now, **extra):
        # Callers restrict transitions; compare-and-swap prevents stale actions.
        columns = {'state': target.value, 'version': operation.version + 1, **extra}
        assignments = ','.join(key + '=?' for key in columns)
        result = self._connection.execute('UPDATE operations SET ' + assignments + ' WHERE operation_id=? AND version=?',
                                          (*columns.values(), operation.operation_id, operation.version))
        if result.rowcount != 1:
            raise JournalError('Concurrent or stale operation state')
        self._event(operation.operation_id, operation.version + 1, target, now)

    def _expected(self, operation_id, version, states):
        operation = self._get(operation_id)
        if type(version) is not int or operation.version != version or operation.state not in states:
            raise JournalError('Transition is unavailable or stale')
        return operation

    def authorize(self, operation_id, version, payload_fingerprint, confirmation):
        with self._transaction() as (now, control):
            operation = self._expected(operation_id, version, {State.PREPARED})
            if (not self.is_armed() or now >= operation.expires_at_us
                    or payload_fingerprint != operation.payload_fingerprint
                    or confirmation != 'AUTHORIZE OFFLINE ' + operation_id):
                raise JournalError('Offline authorization refused')
            token = secrets.token_hex(32)
            self._change(operation, State.AUTHORIZED, now,
                         auth_hash=fingerprint(token), auth_generation=control['generation'])
            return Authorization(operation_id, token, operation.payload_fingerprint,
                                 operation.expires_at_us, operation.version + 1)

    def consume_authorization(self, authorization, payload_fingerprint):
        """Record SUBMITTING in the offline model. This cannot send an order."""
        if type(authorization) is not Authorization or not _digest(authorization.token):
            raise JournalError('Invalid offline authorization')
        with self._transaction() as (now, control):
            operation = self._expected(authorization.operation_id, authorization.version, {State.AUTHORIZED})
            row = self._connection.execute('SELECT auth_hash,auth_generation FROM operations WHERE operation_id=?',
                                            (operation.operation_id,)).fetchone()
            if (not self.is_armed() or now >= operation.expires_at_us
                    or payload_fingerprint != operation.payload_fingerprint
                    or authorization.payload_fingerprint != operation.payload_fingerprint
                    or authorization.expires_at_us != operation.expires_at_us
                    or row['auth_generation'] != control['generation']
                    or not isinstance(row['auth_hash'], str)
                    or not hmac.compare_digest(row['auth_hash'], fingerprint(authorization.token))):
                raise JournalError('Offline authorization expired, changed or revoked')
            self._change(operation, State.SUBMITTING, now, auth_hash=None, auth_generation=None)
            if not self.is_armed():
                raise JournalError('Stop observed during authorization consumption')
        return self.get(operation.operation_id)

    def finish_unsent(self, operation_id, version, target):
        if type(target) is not State or target not in {State.ABORTED, State.BLOCKED, State.EXPIRED}:
            raise JournalError('Invalid unsent outcome')
        with self._transaction() as (now, _):
            operation = self._expected(operation_id, version, UNSENT)
            if target is State.EXPIRED and now < operation.expires_at_us:
                raise JournalError('Unsent operation has not expired')
            self._change(operation, target, now, auth_hash=None, auth_generation=None)

    def mark_uncertain(self, operation_id, version):
        with self._transaction() as (now, _):
            operation = self._expected(operation_id, version, {State.SUBMITTING, State.ACKNOWLEDGED, State.RECONCILING})
            self._change(operation, State.UNCERTAIN, now)

    def acknowledge(self, operation_id, version, exchange_order_id):
        if not isinstance(exchange_order_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}', exchange_order_id):
            raise JournalError('Invalid modeled exchange order identifier')
        with self._transaction() as (now, _):
            operation = self._expected(operation_id, version, {State.SUBMITTING})
            self._change(operation, State.ACKNOWLEDGED, now, exchange_order_id=exchange_order_id)

    def start_reconciliation(self, operation_id, version):
        with self._transaction() as (now, _):
            operation = self._expected(operation_id, version, {State.SUBMITTING, State.UNCERTAIN, State.ACKNOWLEDGED})
            self._change(operation, State.RECONCILING, now)

    def resolve(self, operation_id, version, target, *, filled_quantity):
        """Record a modeled observation, not proof of an authenticated result."""
        if type(target) is not State or target not in {State.FILLED, State.CANCELED, State.REJECTED}:
            raise JournalError('Invalid reconciliation outcome')
        if (type(filled_quantity) is not Decimal or not filled_quantity.is_finite() or filled_quantity < 0
                or len(filled_quantity.as_tuple().digits) > 40 or abs(filled_quantity.adjusted()) > 40):
            raise JournalError('Invalid reconciled quantity')
        with self._transaction() as (now, _):
            operation = self._expected(operation_id, version, {State.RECONCILING})
            quantity = Decimal(json.loads(operation.payload)['quantity'])
            if (filled_quantity > quantity or (target is State.FILLED and filled_quantity != quantity)
                    or (target is State.REJECTED and filled_quantity != 0)):
                raise JournalError('Reconciled quantity conflicts with outcome')
            self._change(operation, target, now, filled_quantity=str(filled_quantity))
