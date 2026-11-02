"""Fixed T1C installed preflight. No installer, activation switch or I/O backend.

Installed as /opt/phemex-readonly-status/launch.py by a future reviewed installer.
Root ownership and an independently approved config anchor the release digest;
matching a source file to a self-modifiable manifest is not authentication.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import stat
import sys


BASE = Path('/opt/phemex-readonly-status')
CONFIG = Path('/etc/phemex-readonly/testnet-status-once.json')
PYTHON = '/usr/bin/python3.12'
STDLIB = ['/usr/lib/python312.zip', '/usr/lib/python3.12', '/usr/lib/python3.12/lib-dynload']
PURPOSE = 'blocked-testnet-status-bootstrap-v1'
BASELINE = '57eafd3b934db5f1a726801d4293ad5a64b7134c'
APPS = frozenset((
    'account_discovery.py', 'command_parser.py', 'config.py', 'deploy/ubuntu24/launch.py',
    'diagnostics.py', 'execution_policy.py', 'instrument_specs.py', 'order_preparation.py',
    'phemex_request_compiler.py', 'readonly_exchange.py', 'readonly_worker_store.py',
    'safety.py', 'testnet_compatibility.py', 'testnet_read_run_store.py',
    'testnet_status_collection.py', 'testnet_status_once.py', 'testnet_status_runtime.py',
    'testnet_transport.py'))


def _require(condition):
    if not condition:
        raise ValueError('Blocked bootstrap validation failed')


def _unique(items):
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _sha(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _root_path(path, *, directory=False, mode=0o644):
    # Bootstrap cannot import a preflight helper before checking its source.
    _require(path.is_absolute() and '..' not in path.parts)
    for part in reversed((path, *path.parents)):
        info = os.lstat(part)
        final = part == path
        _require((stat.S_ISDIR(info.st_mode) if not final or directory else stat.S_ISREG(info.st_mode))
                 and info.st_uid == info.st_gid == 0
                 and stat.S_IMODE(info.st_mode) == (mode if final else 0o755)
                 and (not final or directory or info.st_nlink == 1)
                 and not any(n.startswith('system.posix_acl_') for n in
                             os.listxattr(part, follow_symlinks=False)))


def _read(path, limit=4 * 1024 * 1024):
    _root_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0
                 and stat.S_IMODE(info.st_mode) == 0o644 and info.st_nlink == 1)
        raw = stream.read(limit + 1)
        _require(len(raw) <= limit)
        return raw


def _tree(preflight, directory, expected):
    """Exact files AND directories; caches, hooks, links and extra dirs fail."""
    preflight['path_check'](directory)
    expected_dirs = {p.as_posix() for name in expected for p in Path(name).parents
                     if p != Path('.')}
    files, dirs = set(), set()
    def fail(_):
        raise ValueError('Unavailable inventory')
    for parent, children, names in os.walk(directory, followlinks=False, onerror=fail):
        for child in children:
            path = Path(parent) / child
            preflight['path_check'](path)
            dirs.add(path.relative_to(directory).as_posix())
        for name in names:
            path = Path(parent) / name
            preflight['path_check'](path, directory=False, mode=0o644)
            relative = path.relative_to(directory).as_posix()
            files.add(relative)
            _require(relative in expected and
                     hashlib.sha256(preflight['regular_bytes'](path)).hexdigest() == expected[relative])
    _require(files == set(expected) and dirs == expected_dirs)


def _import_roots(app, vendor):
    roots = set()
    for name in set(app) | set(vendor):
        first = name.split('/')[0]
        if first.endswith('.dist-info') or first.endswith('.data'):
            continue
        roots.add(first.split('.')[0])
    # A cached module could bypass the reviewed search paths. The pristine
    # isolated process must not have imported any application/vendor root yet.
    _require(not roots.intersection(sys.modules) and not roots.intersection(sys.stdlib_module_names))
    app_roots = {n.split('/')[0].split('.')[0] for n in app}
    vendor_roots = {n.split('/')[0].split('.')[0] for n in vendor
                    if not n.split('/')[0].endswith(('.dist-info', '.data'))}
    _require(not app_roots.intersection(vendor_roots))


def verify_release(expected_digest):
    """Read-only preflight, no application imports, SQLite opens or writes.

    expected_digest is supplied ONLY by the checked root config in main(). This
    function is not an activation capability, including when called in tests.
    """
    _require(_sha(expected_digest))
    raw = _read(BASE / 'release.json')
    _require(hashlib.sha256(raw).hexdigest() == expected_digest)
    spec = _decode(raw)
    _require(type(spec) is dict and set(spec) == {
        'format', 'purpose', 'baseline', 'target', 'app', 'tools',
        'dependencies_sha256', 'requirements_sha256'})
    _require(type(spec['format']) is int and spec['format'] == 1
             and spec['purpose'] == PURPOSE and spec['baseline'] == BASELINE
             and spec['target'] == {'os': 'ubuntu', 'version': '24.04', 'python': '3.12', 'arch': 'x86_64'})
    _require(type(spec['app']) is dict and set(spec['app']) == APPS
             and type(spec['tools']) is dict and set(spec['tools']) == {'launch.py', 'preflight.py'}
             and all(_sha(v) for v in (*spec['app'].values(), *spec['tools'].values(),
                                      spec['dependencies_sha256'], spec['requirements_sha256'])))
    # Like B1, execute precisely the verified source buffer. No importlib loader,
    # .pyc selection, sys.modules lookup or caller import path is involved.
    source = _read(BASE / 'preflight.py')
    _require(hashlib.sha256(source).hexdigest() == spec['tools']['preflight.py'])
    _require(hashlib.sha256(_read(BASE / 'launch.py')).hexdigest() == spec['tools']['launch.py'])
    preflight = {'__name__': '_t1c_verified_preflight', '__file__': str(BASE / 'preflight.py')}
    exec(compile(source, str(BASE / 'preflight.py'), 'exec', dont_inherit=True), preflight)
    deps, requirements = _read(BASE / 'dependencies.json'), _read(BASE / 'requirements.txt')
    _require(hashlib.sha256(deps).hexdigest() == spec['dependencies_sha256']
             and hashlib.sha256(requirements).hexdigest() == spec['requirements_sha256'])
    lock = preflight['dependency_lock'](preflight['decode'](deps), preflight['pins'](requirements))
    preflight['verify_runtime'](lock)
    _tree(preflight, BASE / 'app', spec['app'])
    _tree(preflight, BASE / 'vendor', lock['vendor_files'])
    wheel_files = {w['filename']: w['sha256'] for w in lock['wheels']}
    _tree(preflight, BASE / 'wheels', wheel_files)
    preflight['verify_wheels'](lock, BASE / 'wheels')
    preflight['vendor_versions'](BASE / 'vendor', lock['vendor_files'], lock['pins'])
    _require({p.name for p in BASE.iterdir()} == {
        'launch.py', 'preflight.py', 'release.json', 'requirements.txt', 'dependencies.json',
        'app', 'vendor', 'wheels'})
    _import_roots(spec['app'], lock['vendor_files'])
    return spec


def _startup():
    _require(Path(__file__) == BASE / 'launch.py' and
             len(sys.argv) == 2 and sys.argv[1] in ('issue', 'run', '_worker')
             and sys.executable == PYTHON and sys.version_info[:2] == (3, 12)
             and platform.machine() == 'x86_64' and sys.flags.isolated
             and sys.flags.no_site and sys.flags.dont_write_bytecode and sys.path == STDLIB
             and sys.pycache_prefix is None and not sys._xoptions)
    # Python isolation alone cannot neutralize native loader variables before
    # startup. A trusted parent must already have created this minimal env.
    _require(set(os.environ) <= {'LANG', 'LC_CTYPE'}
             and all(v in ('C.UTF-8', 'C.utf8') for v in os.environ.values()))
    for name in STDLIB:
        path = Path(name)
        if name.endswith('.zip') and not os.path.lexists(path):
            _root_path(path.parent, directory=True, mode=0o755)
        else:
            _root_path(path, directory=not name.endswith('.zip'),
                       mode=0o644 if name.endswith('.zip') else 0o755)
    _root_path(Path(PYTHON), mode=0o755)
    account = pwd.getpwnam('phemex-ro')
    _require(account.pw_uid > 0 and account.pw_gid > 0
             and account.pw_shell == '/usr/sbin/nologin'
             and (os.geteuid() == account.pw_uid if sys.argv[1] == '_worker' else os.geteuid() == 0))
    if sys.argv[1] == '_worker':
        _require(os.getegid() == account.pw_gid and set(os.getgroups()) <= {account.pw_gid})
    config = _decode(_read(CONFIG, 2048))
    _require(type(config) is dict and type(config.get('worker_uid')) is int
             and config['worker_uid'] == account.pw_uid)
    spec = verify_release(config.get('release_id'))
    return spec


def main():
    try:
        _startup()
        sys.path[:] = STDLIB + [str(BASE / 'app'), str(BASE / 'vendor')]
        # One literal entry module and three literal roles, not a general loader.
        from testnet_status_once import main as status_main
        return status_main([sys.argv[1]])
    except Exception:
        print('Protected Testnet bootstrap unavailable; details withheld; no activation.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
