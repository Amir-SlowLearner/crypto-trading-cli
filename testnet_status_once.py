"""Prepared, blocked, operator-supervised Testnet read. No listener or retry.

Run-bound checks are active; the separate protected-bootstrap boundary still
unconditionally blocks production activation. Nothing here enables networking.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import pwd
import selectors
import signal
import subprocess
import sys
import time

from deploy.ubuntu24.launch import root_path
from safety import SafetyError
from testnet_read_run_store import ReadBinding, ReadLease, ReadRunStore, ReadTicket, RunError
from testnet_status_collection import StatusReport
import testnet_status_runtime as runtime_module
from testnet_transport import PLAN, decode_response_json
from readonly_worker_store import _hex


CONFIG = Path('/etc/phemex-readonly/testnet-status-once.json')
INSTALLED_BASE = Path('/opt/phemex-readonly-status')
INSTALLED_PYTHON = Path('/usr/bin/python3.12')
INSTALLED_ENTRY = INSTALLED_BASE / 'launch.py'
HARD_SECONDS = 30
MAX_MESSAGE = 2048
MAX_REPLY = 128 * 1024


@dataclass(frozen=True)
class InstalledReadConfiguration:
    """Trusted bootstrap configuration, never decoded from CLI/run IPC bytes."""
    binding: ReadBinding
    storage_id: str
    store_path: Path = field(repr=False)
    profile_path: Path = field(repr=False)
    python: Path = field(repr=False)
    entry: Path = field(repr=False)


def _configuration():
    # The B1 launcher intentionally targets the old synthetic worker. Do not
    # modify/repurpose it, its manifest, or its permissions here.
    try:
        root_path(CONFIG)
        raw = CONFIG.read_bytes()
        if len(raw) > MAX_MESSAGE:
            raise ValueError
        value = decode_response_json(raw, exact_numbers=True)
        keys = {'version','purpose','profile_id','release_id','worker_uid','storage_id',
                'store_path','profile_path','python','entry'}
        if (type(value) is not dict or set(value) != keys or type(value['version']) is not int
                or value['version'] != 1 or value['purpose'] != 'prepared-testnet-status-read-v1'):
            raise ValueError
        binding = ReadBinding(value['profile_id'], value['release_id'], value['worker_uid'])
        account = pwd.getpwnam('phemex-ro')
        if account.pw_uid != binding.worker_uid or account.pw_uid == 0:
            raise ValueError
        paths = [Path(value[key]) for key in ('store_path','profile_path','python','entry')]
        if any(not p.is_absolute() or '..' in p.parts for p in paths):
            raise ValueError
        store, profile, python, entry = paths
        if (store != Path('/var/lib/phemex-readonly-status/approvals.sqlite')
                or profile != Path('/var/lib/phemex-readonly-status/credentials/testnet.json')
                or python != INSTALLED_PYTHON or entry != INSTALLED_ENTRY):
            raise ValueError
        root_path(python, mode=0o755)
        root_path(entry)
        manifest = INSTALLED_BASE / 'release.json'
        root_path(manifest)
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != binding.release_id:
            raise ValueError
        # Merely hashing a mutable manifest is not release authentication. The
        # future independently approved installer must establish this boundary.
        return InstalledReadConfiguration(binding, value['storage_id'], store, profile, python, entry)
    except Exception:
        raise RunError('Protected Testnet read installation unavailable; details withheld') from None


def _store(config):
    return ReadRunStore(config.store_path, storage_id=config.storage_id, worker_uid=config.binding.worker_uid)


def _run_context(config, store, lease):
    return runtime_module.ReadRunContext(store, lease, config.binding, config.storage_id,
                                         config.store_path, config.profile_path)


@dataclass(frozen=True)
class ReadRunReceipt:
    status: StatusReport
    origin: str = field(default='mocked_http', init=False)

    @property
    def execution_authorized(self):
        return False

    @property
    def may_resolve_journal(self):
        return False

    @property
    def evidence_trusted(self):
        # Even the protected collector is display-only, not policy evidence.
        return False

    def render(self):
        return self.status.render() + '\nRun provenance: ' + self.origin + '; display only; no execution authority.'


def collect_fixture_run(store, lease, profile, *, clock=None):
    """Offline test entry. Always synthetic; no provenance selector is accepted.

    Tests mock only the protected-bootstrap boundary and the final HTTP send;
    durable authorization checks stay active. Production I/O remains blocked.
    """
    return _collect_reserved(store, lease, profile, clock=clock)


def _collect_reserved(store, lease, profile, *, clock=None, context=None):
    if (type(store) is not ReadRunStore or type(lease) is not ReadLease
            or type(lease.binding) is not ReadBinding
            or type(profile) is not runtime_module.TestnetCredentialProfile
            or profile.profile_id != lease.binding.profile_id):
        raise RunError('Read profile or operation binding mismatch')
    if context is None:
        # Fixture-only default expectations. They do not satisfy the independent
        # production bootstrap block. Real worker scope comes from config below.
        context = runtime_module.ReadRunContext(store, lease, lease.binding, store.storage_id,
                                                store.path, store.path.parent / 'fixture-profile.json')
    if (type(context) is not runtime_module.ReadRunContext or context.store is not store
            or context.lease != lease):
        raise RunError('Read collection context mismatch')
    runtime = runtime_module.TestnetReadOnlyRuntime(profile, context=context)
    try:
        report = runtime.collect(clock=clock)
        completed = store.finish(lease, report.collection_complete)
        if report.collection_complete and not completed:
            raise RunError('Read completion could not be durably established')
        return ReadRunReceipt(report)
    except Exception:
        # Do not overwrite an uncertain slot with a success, retry, replacement
        # ID or fresh approval. If blocking persistence fails, RUNNING remains
        # consumed and the UNCERTAIN reservation still prevents continuation.
        try:
            store.finish(lease, False)
        except Exception:
            pass
        raise RunError('Testnet read incomplete or uncertain; no retry; details withheld') from None


def _decode_message(raw, keys):
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_MESSAGE:
            raise ValueError
        value = decode_response_json(raw, exact_numbers=True)
        if type(value) is not dict or set(value) != set(keys):
            raise ValueError
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError
        return value
    except Exception:
        raise RunError('Invalid one-shot read message; details withheld') from None


def _child_message(lease):
    return json.dumps({'version': 1, 'run_id': lease.run_id, 'nonce': lease.nonce}).encode()


def _kill(process):
    # The child cannot change UID or start a replacement through this API. A
    # compromised worker can escape a process group; cgroup containment remains
    # a deployment gate. This never retries or restarts a child.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        process.wait()


def _read_child(process, message, *, seconds=HARD_SECONDS):
    """Parent-enforced SIGKILL deadline, independent of child Python clocks.

    Bounded pipe reads avoid communicate()'s unbounded output buffering. The
    parent must itself remain alive; root/systemd containment is still required.
    """
    deadline = time.monotonic() + seconds
    try:
        if len(message) > MAX_MESSAGE:
            raise RunError('Oversized worker request')
        process.stdin.write(message)  # <= PIPE_BUF; child cannot make it huge.
        process.stdin.close()
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RunError('Hard worker deadline exceeded; no retry')
                ready = selector.select(remaining)
                if not ready:
                    raise RunError('Hard worker deadline exceeded; no retry')
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_REPLY:
                    raise RunError('Worker reply exceeded the fixed limit')
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise RunError('Worker exited unexpectedly; no retry')
        return bytes(data)
    except Exception:
        raise RunError('Worker failed, timed out or returned invalid output; no retry') from None
    finally:
        _kill(process)
        process.stdout.close()


def supervise_once(ticket):
    """Future root operator entry, NOT an untrusted CLI or arbitrary command API."""
    config = _operator_configuration()
    return _supervise(config, ticket)


def issue_once():
    """Separate root operator issuance, disabled in this checkpoint too."""
    config = _operator_configuration()
    return _store(config).issue(config.binding)


def _operator_configuration():
    # Issuance cannot require a lease that does not exist yet. Kernel root
    # identity and the future protected installation are the independent gate;
    # only then may issue()/claim() operate on the configured durable binding.
    if os.geteuid() != 0:
        raise RunError('Independent root operator required')
    runtime_module._require_protected_bootstrap()
    return _configuration()


def _supervise(config, ticket):
    store = _store(config)
    lease = store.claim(ticket, config.binding)  # Durable, BEFORE Popen.
    process = None
    try:
        account = pwd.getpwnam('phemex-ro')
        if account.pw_uid != config.binding.worker_uid:
            raise RunError('Dedicated worker identity mismatch')
        if config.python != INSTALLED_PYTHON or config.entry != INSTALLED_ENTRY:
            raise RunError('Fixed installed bootstrap required')
        process = subprocess.Popen([str(INSTALLED_PYTHON), '-I', '-S', '-B', str(INSTALLED_ENTRY), '_worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd='/', env={'LANG': 'C.UTF-8'}, start_new_session=True, close_fds=True,
            user=account.pw_uid, group=account.pw_gid, extra_groups=())
        store.attach(lease, process.pid)  # No stdin/dispatch until this COMMIT.
        raw = _read_child(process, _child_message(lease))
        reply = _decode_reply(raw)
        if reply['run_id'] != lease.run_id or reply['release_id'] != lease.binding.release_id:
            raise RunError('Worker receipt binding mismatch')
        # Receipt loss, corruption, storage mismatch, unfinished slots or an
        # unexpected process exit must never appear as a successful collection.
        state = store.snapshot(lease.run_id)
        complete = state == {'state': 'COMPLETE', 'slots': tuple((i, 'OBSERVED') for i in range(4))}
        if reply['complete'] is not complete:
            raise RunError('Worker completion and durable accounting disagree')
        return reply
    except Exception:
        if process is not None:
            _kill(process)
        try:
            store.finish(lease, False)
        except Exception:
            pass
        raise RunError('One-shot read failed or uncertain; approval consumed; details withheld') from None


def _decode_reply(raw):
    try:
        if type(raw) is not bytes or len(raw) > MAX_REPLY:
            raise ValueError
        reply = decode_response_json(raw, exact_numbers=True)
        if (type(reply) is not dict or set(reply) != {'version','run_id','release_id','complete','origin','report'}
                or type(reply['version']) is not int or reply['version'] != 1
                or not _hex(reply['run_id'], 32) or not _hex(reply['release_id'], 64)
                or type(reply['complete']) is not bool or reply['origin'] != 'protected_testnet_worker'
                or type(reply['report']) is not str):
            raise ValueError
        return reply
    except Exception:
        raise RunError('Invalid protected worker response; details withheld') from None


def _worker():
    runtime_module._require_protected_bootstrap()  # No deployment/bootstrap yet.
    config = _configuration()
    if os.geteuid() != config.binding.worker_uid or os.geteuid() == 0:
        raise RunError('Dedicated non-root worker identity required')
    value = _decode_message(sys.stdin.buffer.read(MAX_MESSAGE + 1), ('version','run_id','nonce'))
    if not _hex(value['run_id'], 32) or not _hex(value['nonce'], 64):
        raise RunError('Invalid protected worker lease')
    store = _store(config)
    lease = ReadLease(value['run_id'], value['nonce'], config.binding, config.storage_id)
    context = _run_context(config, store, lease)
    runtime_module._require_step2_activation(context, stage='worker')
    # Loader checks this same lease/path/profile and the separate bootstrap
    # boundary again. The caller never supplies its path or credentials.
    profile = runtime_module.load_testnet_profile(config.profile_path, context=context)
    receipt = _collect_reserved(store, lease, profile, context=context)
    # Actual provenance is assigned ONLY at this protected subprocess boundary,
    # never by converting a fixture receipt or accepting a caller provenance flag.
    # It is descriptive, not cryptographic authentication or execution evidence.
    report = _render_observed(receipt.status)
    return {'version': 1, 'run_id': lease.run_id, 'release_id': lease.binding.release_id,
            'complete': receipt.status.collection_complete,
            'origin': 'protected_testnet_worker', 'report': report}


def _render_observed(report):
    lines = ['PHEMEX TESTNET STATUS — DISPLAY ONLY',
             'Protected runtime observations; sequential snapshots, not stable account binding.']
    for row in report.endpoints:
        source = 'authenticated_testnet_http' if row.label in ('account_positions','identity_crosscheck') else 'public_testnet_http'
        lines.append(f'{row.label}: {"OBSERVED" if row.success else "UNKNOWN"}; {row.source}; '
                     f'provenance={source if row.success else "unavailable"}; '
                     f'collected={row.started_at} -> {row.completed_at}; http={row.http_status}')
    for row in report.requirements:
        # Existing interpreter stays explicitly synthetic. Reuse only its
        # checked display values; do not change its models or promote to policy.
        reason = row.reason
        if row.name == 'authentication':
            reason = ('Private HTTP success observed; stable account identity UNKNOWN.'
                      if row.status.value == 'VERIFIED' else
                      'No accepted private success observation establishes authentication.')
        value = ('private success response' if row.name == 'authentication' and row.status.value == 'VERIFIED'
                 else row.value)
        lines.append(f'{row.status.value} {row.name}: {value}; {reason}')
    lines += ['Account classification, stable binding, account-wide funds and complete orders: UNKNOWN.',
              'Read-only display; no execution authority; no journal resolution; no resubmission.']
    return '\n'.join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv in ([], ['--help']):
        print('Prepared Testnet status one-shot worker. Activation is disabled. No real-read command is enabled.')
        return 0
    try:
        if argv == ['_worker']:
            reply = _worker()
        elif argv == ['issue']:
            ticket = issue_once()
            # Deliberate root-operator output, not a diagnostic or log. Future
            # use must send this directly to a protected descriptor, never argv.
            print(json.dumps({'version': 1, 'run_id': ticket.run_id, 'token': ticket.token}))
            return 0
        elif argv == ['run']:
            value = _decode_message(sys.stdin.buffer.read(MAX_MESSAGE + 1), ('version','run_id','token'))
            reply = supervise_once(ReadTicket(value['run_id'], value['token']))
        else:
            raise RunError('Unsupported one-shot operation')
        print(json.dumps(reply))
        return 0 if reply['complete'] else 2
    except Exception:
        print('One-shot Testnet read unavailable or uncertain; details withheld; no retry.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
