"""Offline B1 bundle builder and READ-ONLY installation preflight. No installer."""
import argparse
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import grp
import platform
import re
import stat
import struct
import sys
import tarfile
import zipfile


BASE = Path('/opt/phemex-readonly')
STATE = Path('/var/lib/phemex-readonly-worker')
DB = STATE / 'offline-approvals.sqlite3'
APPS = frozenset(('readonly_worker_store.py', 'testnet_readonly_worker.py',
                  'testnet_transport.py', 'testnet_compatibility.py', 'account_discovery.py',
                  'readonly_exchange.py', 'instrument_specs.py', 'diagnostics.py', 'safety.py'))
TARGET = {'os': 'ubuntu', 'version': '24.04', 'python': '3.12', 'arch': 'x86_64'}
BASELINE = '6a7acd4beebc729761d838905727b568381f87f8'
PURPOSE = 'offline-readonly-worker-v1'


class PackageError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PackageError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, 'Duplicate manifest field')
        result[key] = value
    return result


def decode(data):
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PackageError('Invalid deployment JSON') from None


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected), 'Invalid manifest fields')


def sha(value):
    return type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None


def relative(name):
    require(type(name) is str and re.fullmatch(r'[A-Za-z0-9_.+/-]+', name) is not None,
            'Invalid artifact path')
    require(all(part not in ('', '.', '..') for part in name.split('/')) and not name.startswith('/'),
            'Unsafe artifact path')
    return name


def regular_bytes(path, limit=None):
    """No following a final symlink; caller checks ancestor ownership separately."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Unsafe artifact file')
            return stream.read(limit)
    except OSError:
        raise PackageError('Artifact unavailable or unsafe') from None


def manifest(value):
    keys(value, ('format', 'baseline', 'release', 'target', 'purpose', 'app', 'tools',
                 'dependencies_sha256', 'requirements_sha256'))
    require(type(value['format']) is int and value['format'] == 1
            and value['baseline'] == BASELINE and value['target'] == TARGET
            and value['purpose'] == PURPOSE, 'Unreviewed manifest identity')
    require(type(value['release']) is str and re.fullmatch(r'blocked-[a-z0-9-]{1,48}', value['release']),
            'Unsafe release name')
    for section, names in (('app', APPS), ('tools', {'launch.py', 'package.py', 'phemex-ro.sysusers'})):
        keys(value[section], names)
        require(all(sha(v) for v in value[section].values()), 'Missing source digest')
    require(sha(value['dependencies_sha256']) and sha(value['requirements_sha256']),
            'Missing lock digest')
    return value


def pins(raw):
    result = {}
    for line in raw.decode('ascii').splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([a-z0-9][a-z0-9-]*)==([A-Za-z0-9.]+)', line)
        require(match is not None and match[1] not in result, 'Unpinned dependency')
        result[match[1]] = match[2]
    require(result.get('ccxt') == '4.5.90' and bool(result), 'Unexpected dependency closure')
    return result


def source_payload(checkout, directory):
    directory, checkout = Path(directory), Path(checkout)
    raw = regular_bytes(directory / 'manifest.json')
    spec = manifest(decode(raw))
    payload = {'manifest.json': raw, 'dependencies.json': regular_bytes(directory / 'dependencies.json'),
               'requirements.txt': regular_bytes(checkout / 'requirements.txt')}
    require(digest(payload['dependencies.json']) == spec['dependencies_sha256']
            and digest(payload['requirements.txt']) == spec['requirements_sha256'], 'Lock digest mismatch')
    dependency = decode(payload['dependencies.json'])
    require(dependency.get('pins') == pins(payload['requirements.txt']), 'Dependency version mismatch')
    for section, source in (('app', checkout), ('tools', directory)):
        for name, expected in spec[section].items():
            data = regular_bytes(source / name)
            require(digest(data) == expected, 'Reviewed source digest mismatch')
            payload[(('app/' if section == 'app' else '') + name)] = data
    return spec, payload


def bundle(checkout, directory, output):
    """Deterministic SOURCE bundle, not an executable/installation-ready runtime."""
    spec, payload = source_payload(checkout, directory)
    # Exclusive creation: never replace a reviewed archive or extract into /opt.
    with open(output, 'xb') as stream, tarfile.open(fileobj=stream, mode='w') as archive:
        for name, data in sorted(payload.items()):
            item = tarfile.TarInfo(spec['release'] + '/' + name)
            item.size, item.mode, item.mtime, item.uid, item.gid = len(data), 0o644, 0, 0, 0
            archive.addfile(item, io.BytesIO(data))


def dependency_lock(value, expected_pins):
    keys(value, ('format', 'target', 'status', 'pins', 'python', 'wheels', 'vendor_files'))
    require(type(value['format']) is int and value['format'] == 1 and value['target'] == TARGET
            and value['pins'] == expected_pins, 'Dependency target or versions differ')
    require(value['status'] == 'reviewed', 'Ubuntu 24.04 dependency artifacts are not reviewed')
    keys(value['python'], ('version', 'sha256'))
    require(type(value['python']['version']) is str
            and re.fullmatch(r'3\.12\.[0-9]+', value['python']['version'])
            and sha(value['python']['sha256']), 'Interpreter integrity not established')
    require(type(value['wheels']) is list and len(value['wheels']) == len(expected_pins),
            'Incomplete wheel lock')
    found = set()
    for wheel in value['wheels']:
        keys(wheel, ('name', 'version', 'filename', 'sha256'))
        name = wheel['name']
        require(type(name) is str and name in expected_pins and name not in found
                and wheel['version'] == expected_pins[name], 'Duplicate or mismatched wheel version')
        require(type(wheel['filename']) is str
                and re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', wheel['filename'])
                and sha(wheel['sha256']), 'Invalid wheel identity')
        fields = wheel['filename'][:-4].rsplit('-', 3)
        require(len(fields) == 4, 'Missing wheel compatibility tags')
        distribution = fields[0].split('-')
        require(len(distribution) in (2, 3)
                and re.sub(r'[-_.]+', '-', distribution[0]).lower() == name
                and distribution[1] == expected_pins[name], 'Wheel filename/version mismatch')
        py, abi, platform = fields[-3:]
        pure = 'py3' in py.split('.') and abi == 'none' and platform == 'any'
        native = ('cp312' in py.split('.') and abi == 'cp312') or (
            abi == 'abi3' and any(re.fullmatch(r'cp3[0-9]{1,2}', tag)
                                 and int(tag[2:]) <= 312 for tag in py.split('.')))
        platforms = platform.split('.')
        compatible = all(tag == 'linux_x86_64' or tag in ('manylinux1_x86_64', 'manylinux2010_x86_64',
                         'manylinux2014_x86_64') or (
                             re.fullmatch(r'manylinux_2_[0-9]+_x86_64', tag)
                             and int(tag.split('_')[2]) <= 39) for tag in platforms)
        require(pure or native and compatible, 'Wheel ABI/platform not approved for target')
        found.add(name)
    files = value['vendor_files']
    require(type(files) is dict and bool(files), 'Missing reviewed installed-file inventory')
    for name, expected in files.items():
        relative(name)
        require(sha(expected), 'Invalid installed-file digest')
        require(not name.endswith(('.pth', '.pyc', '.pyo')) and '__pycache__' not in Path(name).parts
                and Path(name).name not in ('sitecustomize.py', 'usercustomize.py'),
                'Import startup hooks forbidden')
    return value


def verify_wheels(lock, directory):
    directory = Path(directory)
    expected = {wheel['filename'] for wheel in lock['wheels']}
    require(len(expected) == len(lock['wheels']) and {p.name for p in directory.iterdir()} == expected,
            'Unexpected wheelhouse contents')
    for wheel in lock['wheels']:
        raw = regular_bytes(directory / wheel['filename'])
        require(digest(raw) == wheel['sha256'], 'Wheel digest mismatch')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            require(len(set(names)) == len(names), 'Duplicate wheel members')
            for item in archive.infolist():
                relative(item.filename.rstrip('/'))
                kind = stat.S_IFMT(item.external_attr >> 16)
                require(kind in (0, stat.S_IFDIR, stat.S_IFREG), 'Wheel special file forbidden')
            metadata = [name for name in names if name.endswith('.dist-info/METADATA')]
            require(len(metadata) == 1, 'Ambiguous wheel metadata')
            info = BytesParser().parsebytes(archive.read(metadata[0]))
            normal = lambda name: re.sub(r'[-_.]+', '-', name).lower()
            require(len(info.get_all('Name', [])) == len(info.get_all('Version', [])) == 1
                    and normal(info.get('Name', '')) == wheel['name']
                    and info.get('Version') == wheel['version'], 'Wheel metadata/version mismatch')
            headers = [name for name in names if name.endswith('.dist-info/WHEEL')]
            require(len(headers) == 1, 'Missing wheel compatibility metadata')
            tags = BytesParser().parsebytes(archive.read(headers[0])).get_all('Tag', [])
            py, abi, platform = wheel['filename'][:-4].rsplit('-', 3)[-3:]
            require(set(tags) == {p + '-' + a + '-' + s for p in py.split('.')
                                 for a in abi.split('.') for s in platform.split('.')},
                    'Wheel tags differ from reviewed filename')


def path_check(path, uid=0, gid=0, mode=0o755, directory=True):
    """Exact POSIX ownership/mode; rejects links, ACLs and unsafe ancestors."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'Absolute canonical path required')
    for part in reversed((path, *path.parents)):
        final = part == path
        info = os.lstat(part)
        expected_uid, expected_gid = (uid, gid) if final else (0, 0)
        expected_mode = mode if final else 0o755
        want_dir = not final or directory
        require((stat.S_ISDIR(info.st_mode) if want_dir else stat.S_ISREG(info.st_mode))
                and info.st_uid == expected_uid and info.st_gid == expected_gid
                and stat.S_IMODE(info.st_mode) == expected_mode
                and (want_dir or info.st_nlink == 1), 'Unsafe ownership, permissions or path type')
        require(not any(name.startswith('system.posix_acl_') for name in os.listxattr(part, follow_symlinks=False)),
                'Extended ACLs require separate review')


def tree_files(root):
    result = set()
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path_check(Path(parent) / name)
        for name in files:
            path = Path(parent) / name
            path_check(path, mode=0o644, directory=False)
            result.add(path.relative_to(root).as_posix())
    return result


def vendor_versions(root, files, expected):
    found = {}
    for name in files:
        if name.endswith('.dist-info/METADATA'):
            value = BytesParser().parsebytes(regular_bytes(root / name))
            package = re.sub(r'[-_.]+', '-', value.get('Name', '')).lower()
            require(len(value.get_all('Name', [])) == len(value.get_all('Version', [])) == 1
                    and package not in found, 'Duplicate installed dependency metadata')
            found[package] = value.get('Version')
    require(found == expected, 'Installed dependency versions differ')


def state_binding(binding, worker_uid, worker_gid):
    keys(binding, ('format', 'purpose', 'device', 'inode'))
    require(type(binding['format']) is int and binding['format'] == 1 and binding['purpose'] == PURPOSE
            and all(type(binding[k]) is int and binding[k] > 0 for k in ('device', 'inode')),
            'Unreviewed state binding')
    path_check(STATE, worker_uid, worker_gid, 0o700)
    # The state directory is the only non-root-owned ancestor of DB.
    info = os.lstat(DB)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == worker_uid and info.st_gid == worker_gid
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (binding['device'], binding['inode']),
            'Approval database missing, replaced or incorrectly adopted')
    require(not os.listxattr(DB, follow_symlinks=False), 'Approval database attributes require review')
    require(not any(os.path.lexists(str(DB) + suffix) for suffix in ('-journal', '-wal', '-shm')),
            'Stop workers and resolve SQLite sidecars before deployment')
    header = regular_bytes(DB, 100)
    require(len(header) == 100 and header[:16] == b'SQLite format 3\0'
            and header[18:20] == b'\x01\x01' and struct.unpack('>I', header[60:64])[0] == 1
            and struct.unpack('>I', header[68:72])[0] == 0x524F4754, 'Wrong approval database format/purpose')
    # This is identity/header inspection only; never open SQLite or recover it.
    return info.st_dev, info.st_ino


def verify_release(root, worker_uid, worker_gid):
    root = Path(root)
    require(root.parent == BASE / 'releases', 'Release outside fixed installation prefix')
    path_check(root)
    for name in ('manifest.json', 'dependencies.json', 'requirements.txt', 'package.py', 'launch.py'):
        path_check(root / name, mode=0o644, directory=False)
    spec = manifest(decode(regular_bytes(root / 'manifest.json')))
    require(root.name == spec['release'], 'Release selection mismatch')
    deps = regular_bytes(root / 'dependencies.json')
    requirements = regular_bytes(root / 'requirements.txt')
    require(digest(deps) == spec['dependencies_sha256'] and digest(requirements) == spec['requirements_sha256'],
            'Dependency manifest changed')
    lock = dependency_lock(decode(deps), pins(requirements))
    for directory, expected in (('app', spec['app']), ('vendor', lock['vendor_files'])):
        path_check(root / directory)
        require(tree_files(root / directory) == set(expected), 'Unexpected or missing installed files')
        for name, expected_digest in expected.items():
            require(digest(regular_bytes(root / directory / name)) == expected_digest, 'Installed content differs')
    vendor_versions(root / 'vendor', lock['vendor_files'], lock['pins'])
    for name, expected in spec['tools'].items():
        require(digest(regular_bytes(root / name)) == expected, 'Deployment tool differs')
    for name in ('active.json', 'state-binding.json'):
        path_check(BASE / name, mode=0o644, directory=False)
    binding = decode(regular_bytes(BASE / 'state-binding.json'))
    identity = state_binding(binding, worker_uid, worker_gid)
    require(tree_files(root) == set(spec['tools']) | {'manifest.json', 'dependencies.json', 'requirements.txt'}
            | {'app/' + p for p in spec['app']} | {'vendor/' + p for p in lock['vendor_files']},
            'Unexpected release contents')
    return spec, lock, identity


def installed():
    path_check(BASE / 'active.json', mode=0o644, directory=False)
    selected = decode(regular_bytes(BASE / 'active.json'))
    keys(selected, ('format', 'release'))
    require(type(selected['format']) is int and selected['format'] == 1
            and type(selected['release']) is str
            and re.fullmatch(r'blocked-[a-z0-9-]{1,48}', selected['release']), 'Invalid release selection')
    account, group = pwd.getpwnam('phemex-ro'), grp.getgrnam('phemex-ro')
    require(account.pw_uid > 0 and group.gr_gid > 0 and account.pw_gid == group.gr_gid
            and account.pw_shell == '/usr/sbin/nologin', 'Unexpected worker identity')
    result = verify_release(BASE / 'releases' / selected['release'], account.pw_uid, group.gr_gid)
    verify_runtime(result[1])
    return result


def verify_runtime(lock):
    require(sys.executable == '/usr/bin/python3.12' and sys.version_info[:2] == (3, 12)
            and sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
            and platform.machine() == 'x86_64', 'Wrong isolated target runtime')
    path_check(Path('/usr/lib/os-release'), mode=0o644, directory=False)
    values = dict(line.split('=', 1) for line in regular_bytes('/usr/lib/os-release').decode().splitlines()
                  if '=' in line and not line.startswith('#'))
    require(values.get('ID', '').strip('"') == 'ubuntu'
            and values.get('VERSION_ID', '').strip('"') == '24.04', 'Wrong target operating system')
    path_check(Path(sys.executable), mode=0o755, directory=False)
    require(platform.python_version() == lock['python']['version']
            and digest(regular_bytes(sys.executable)) == lock['python']['sha256'], 'Interpreter differs')


def switch_plan(current, target, binding_before, binding_after):
    """Pure upgrade/rollback plan. No database copy, reset, creation or execution."""
    for spec in (current, target):
        manifest(spec)
    require(binding_before == binding_after and binding_before is not None, 'State identity changed')
    require(current['release'] != target['release'], 'Release already selected')
    return {'replace': str(BASE / 'active.json'), 'from': current['release'], 'to': target['release'],
            'state_action': 'preserve', 'requires': 'stopped workers and fresh preflight'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('source-check', 'source-bundle', 'wheels-check', 'check-installation'))
    parser.add_argument('--checkout', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--wheelhouse', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'check-installation':
            installed()
        elif args.command == 'wheels-check':
            require(args.checkout is not None and args.wheelhouse is not None, 'Source and wheelhouse required')
            _, payload = source_payload(args.checkout, Path(__file__).parent)
            lock = dependency_lock(decode(payload['dependencies.json']), pins(payload['requirements.txt']))
            verify_wheels(lock, args.wheelhouse)
        elif args.command == 'source-bundle':
            require(args.checkout is not None, 'Checkout required')
            require(args.output is not None, 'Output required')
            bundle(args.checkout, Path(__file__).parent, args.output)
        else:
            require(args.checkout is not None, 'Checkout required')
            source_payload(args.checkout, Path(__file__).parent)
        print({'source-check': 'SOURCE ONLY: hashes valid; target installation readiness is not established.',
               'source-bundle': 'SOURCE ONLY: deterministic bundle created; no installation performed.',
               'wheels-check': 'Target wheel integrity checked; installed preflight still required.',
               'check-installation': 'B1 read-only installation preflight passed; real networking remains disabled.'}
              [args.command])
        return 0
    except Exception:
        print('B1 validation failed; no installation or state changes performed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
