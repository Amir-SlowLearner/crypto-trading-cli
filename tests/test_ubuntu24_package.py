"""B1 synthetic filesystem/artifact tests. Nothing is installed or activated."""
from copy import deepcopy
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from deploy.ubuntu24 import package as b1
from tests.helpers import OfflineTestCase


REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / 'deploy/ubuntu24'


def ready_lock():
    metadata = b'Name: ccxt\nVersion: 4.5.90\n'
    return {'format': 1, 'target': dict(b1.TARGET), 'status': 'reviewed',
            'pins': {'ccxt': '4.5.90'}, 'python': {'version': '3.12.3', 'sha256': 'a' * 64},
            'wheels': [{'name': 'ccxt', 'version': '4.5.90',
                        'filename': 'ccxt-4.5.90-py3-none-any.whl', 'sha256': 'b' * 64}],
            'vendor_files': {'ccxt/__init__.py': b1.digest(b''),
                             'ccxt-4.5.90.dist-info/METADATA': b1.digest(metadata)}}


class UbuntuPackageTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source_copy(self):
        checkout, package = self.root / 'checkout', self.root / 'package'
        checkout.mkdir()
        package.mkdir()
        for name in b1.APPS | {'requirements.txt'}:
            shutil.copyfile(REPO / name, checkout / name)
        for name in ('manifest.json', 'dependencies.json', 'launch.py', 'package.py', 'phemex-ro.sysusers'):
            shutil.copyfile(PACKAGE / name, package / name)
        return checkout, package

    def fake_paths(self, changes=None):
        changes = changes or {}
        def lstat(path):
            values = dict(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755, st_nlink=1)
            values.update(changes.get(str(path), {}))
            return SimpleNamespace(**values)
        return patch.object(b1.os, 'lstat', side_effect=lstat)

    def test_manifest_pins_exact_reviewed_blocked_worker_sources(self):
        spec, payload = b1.source_payload(REPO, PACKAGE)
        self.assertEqual(set(spec['app']), b1.APPS)
        self.assertEqual(spec['baseline'], b1.BASELINE)
        self.assertEqual(spec['purpose'], 'offline-readonly-worker-v1')
        for forbidden in ('config.py', 'exchange_client.py', 'execution_policy.py',
                          'operation_journal.py', 'offline_simulation.py', 'test.env'):
            self.assertFalse(any(name.endswith(forbidden) for name in payload))
        self.assertIn(b"Phase A network transport is disabled", payload['app/testnet_transport.py'])

    def test_source_hash_mismatch_fails_without_creating_bundle(self):
        checkout, directory = self.source_copy()
        (checkout / 'testnet_transport.py').write_text('unexpected backend')
        output = self.root / 'source.tar'
        with self.assertRaises(b1.PackageError): b1.bundle(checkout, directory, output)
        self.assertFalse(output.exists())

    def test_source_bundle_is_deterministic_and_contains_only_regular_sources(self):
        first, second = self.root / 'one.tar', self.root / 'two.tar'
        b1.bundle(REPO, PACKAGE, first)
        b1.bundle(REPO, PACKAGE, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            self.assertEqual(len(archive.getmembers()), 15)
            self.assertTrue(all(item.isfile() and item.mode == 0o644 and item.uid == item.gid == item.mtime == 0
                                for item in archive.getmembers()))
        with self.assertRaises(FileExistsError): b1.bundle(REPO, PACKAGE, first)

    def test_duplicate_manifest_members_rejected(self):
        with self.assertRaises(b1.PackageError): b1.decode(b'{"format":1,"format":1}')
        with self.assertRaises(b1.PackageError): b1.decode(b'{"nested":[{"x":1,"x":2}]}')

    def test_manifest_extra_missing_and_unsafe_release_rejected(self):
        original = b1.decode((PACKAGE / 'manifest.json').read_bytes())
        for kind in ('extra', 'missing', 'path', 'hash', 'purpose', 'boolean'):
            spec = deepcopy(original)
            if kind == 'extra': spec['backend'] = 'HTTP'
            if kind == 'missing': del spec['app']['safety.py']
            if kind == 'path': spec['release'] = '../checkout'
            if kind == 'hash': spec['app']['safety.py'] = ''
            if kind == 'purpose': spec['purpose'] = 'authenticated'
            if kind == 'boolean': spec['format'] = True
            with self.subTest(kind=kind), self.assertRaises(b1.PackageError): b1.manifest(spec)

    def test_unpinned_or_duplicate_requirements_rejected(self):
        for raw in (b'ccxt>=4', b'ccxt==4.5.90\nccxt==4.5.90', b'ccxt==4.5.91', b'-r other.txt'):
            with self.assertRaises(b1.PackageError): b1.pins(raw)

    def test_unreviewed_target_lock_cannot_be_made_ready_by_flipping_status(self):
        lock = b1.decode((PACKAGE / 'dependencies.json').read_bytes())
        for status in (lock['status'], 'reviewed'):
            lock['status'] = status
            with self.assertRaises(b1.PackageError): b1.dependency_lock(lock, lock['pins'])

    def test_ready_synthetic_lock_requires_every_hash_and_exact_version(self):
        b1.dependency_lock(ready_lock(), {'ccxt': '4.5.90'})
        for kind in ('wheel_hash', 'version', 'python', 'inventory', 'extra', 'duplicate'):
            value = ready_lock()
            if kind == 'wheel_hash': value['wheels'][0]['sha256'] = None
            if kind == 'version': value['wheels'][0]['version'] = '4.5.91'
            if kind == 'python': value['python']['version'] = '3.10.12'
            if kind == 'inventory': value['vendor_files'] = {}
            if kind == 'extra': value['allow_network'] = True
            if kind == 'duplicate': value['wheels'] *= 2
            with self.subTest(kind=kind), self.assertRaises(b1.PackageError):
                b1.dependency_lock(value, {'ccxt': '4.5.90'})

    def test_wrong_architecture_abi_or_unsafe_wheel_paths_rejected(self):
        for filename in ('ccxt-4.5.90-cp310-cp310-manylinux_2_28_x86_64.whl',
                         'ccxt-4.5.90-cp312-cp312-win_amd64.whl',
                         'ccxt-4.5.90-cp312-cp312-manylinux_2_40_x86_64.whl',
                         '../ccxt.whl', 'ccxt.whl', 'ccxt.tar.gz'):
            value = ready_lock()
            value['wheels'][0]['filename'] = filename
            with self.subTest(filename=filename), self.assertRaises(b1.PackageError):
                b1.dependency_lock(value, value['pins'])

    def test_startup_hooks_and_traversal_in_vendor_inventory_rejected(self):
        for name in ('../escape', '/absolute', 'a//b', 'module.pth', 'sitecustomize.py',
                     'x/usercustomize.py', 'module.pyc', '__pycache__/module.pyc'):
            value = ready_lock()
            value['vendor_files'][name] = 'a' * 64
            with self.assertRaises(b1.PackageError): b1.dependency_lock(value, value['pins'])

    def wheel(self, metadata=b'Name: ccxt\nVersion: 4.5.90\n', extra=None):
        lock = ready_lock()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('ccxt-4.5.90.dist-info/METADATA', metadata)
            archive.writestr('ccxt-4.5.90.dist-info/WHEEL', b'Wheel-Version: 1.0\nTag: py3-none-any\n')
            if extra: archive.writestr(extra, b'unsafe')
        raw = stream.getvalue()
        lock['wheels'][0]['sha256'] = b1.digest(raw)
        path = self.root / lock['wheels'][0]['filename']
        path.write_bytes(raw)
        return lock, path

    def test_wheel_hash_and_internal_version_verified_offline(self):
        lock, path = self.wheel()
        b1.verify_wheels(lock, self.root)
        path.write_bytes(b'changed')
        with self.assertRaises(b1.PackageError): b1.verify_wheels(lock, self.root)

    def test_matching_wheel_hash_does_not_hide_wrong_version_or_zip_traversal(self):
        lock, path = self.wheel(metadata=b'Name: ccxt\nVersion: 0.0.0\n')
        with self.assertRaises(b1.PackageError): b1.verify_wheels(lock, self.root)
        path.unlink()
        lock, _ = self.wheel(extra='../escape')
        with self.assertRaises(b1.PackageError): b1.verify_wheels(lock, self.root)

    def test_extra_wheelhouse_files_rejected(self):
        lock, _ = self.wheel()
        (self.root / 'extra').write_text('unexpected')
        with self.assertRaises(b1.PackageError): b1.verify_wheels(lock, self.root)

    def release_copy(self):
        base = self.root / ('deployment-' + str(len(list(self.root.iterdir()))))
        spec = b1.decode((PACKAGE / 'manifest.json').read_bytes())
        release = base / 'releases' / spec['release']
        (release / 'app').mkdir(parents=True)
        (release / 'vendor/ccxt').mkdir(parents=True)
        for name in b1.APPS:
            shutil.copyfile(REPO / name, release / 'app' / name)
        for name in spec['tools']:
            shutil.copyfile(PACKAGE / name, release / name)
            spec['tools'][name] = b1.digest((release / name).read_bytes())
        lock = ready_lock()
        (release / 'vendor/ccxt/__init__.py').write_bytes(b'')
        metadata = release / 'vendor/ccxt-4.5.90.dist-info/METADATA'
        metadata.parent.mkdir()
        metadata.write_bytes(b'Name: ccxt\nVersion: 4.5.90\n')
        requirements = b'ccxt==4.5.90\n'
        (release / 'requirements.txt').write_bytes(requirements)
        spec['requirements_sha256'] = b1.digest(requirements)
        self.write_lock(release, spec, lock)
        (base / 'active.json').write_text(json.dumps({'format': 1, 'release': spec['release']}))
        (base / 'state-binding.json').write_text('{}')  # state_binding is mocked, tested separately.
        return base, release, spec, lock

    def write_lock(self, release, spec, lock):
        raw = json.dumps(lock).encode()
        (release / 'dependencies.json').write_bytes(raw)
        spec['dependencies_sha256'] = b1.digest(raw)
        (release / 'manifest.json').write_text(json.dumps(spec))

    def test_complete_synthetic_release_inventory_and_versions_pass_preflight(self):
        base, release, _, _ = self.release_copy()
        with patch.object(b1, 'BASE', base), patch.object(b1, 'path_check'), \
             patch.object(b1, 'state_binding', return_value=(1, 2)):
            _, _, identity = b1.verify_release(release, 77, 78)
        self.assertEqual(identity, (1, 2))

    def test_missing_extra_or_modified_installed_files_fail_preflight(self):
        for kind in ('missing', 'extra', 'app', 'vendor', 'tool'):
            base, release, _, _ = self.release_copy()
            if kind == 'missing': (release / 'app/safety.py').unlink()
            if kind == 'extra': (release / 'unreviewed.py').write_text('extra')
            if kind == 'app': (release / 'app/testnet_transport.py').write_text('changed')
            if kind == 'vendor': (release / 'vendor/ccxt/__init__.py').write_text('changed')
            if kind == 'tool': (release / 'package.py').write_text('changed')
            with patch.object(b1, 'BASE', base), patch.object(b1, 'path_check'), \
                 patch.object(b1, 'state_binding', return_value=(1, 2)):
                with self.subTest(kind=kind), self.assertRaises(b1.PackageError):
                    b1.verify_release(release, 77, 78)

    def test_reviewed_file_hash_cannot_hide_installed_version_mismatch(self):
        base, release, spec, lock = self.release_copy()
        name = 'ccxt-4.5.90.dist-info/METADATA'
        raw = b'Name: ccxt\nVersion: 4.5.91\n'
        (release / 'vendor' / name).write_bytes(raw)
        lock['vendor_files'][name] = b1.digest(raw)
        self.write_lock(release, spec, lock)
        with patch.object(b1, 'BASE', base), patch.object(b1, 'path_check'), \
             patch.object(b1, 'state_binding', return_value=(1, 2)):
            with self.assertRaises(b1.PackageError): b1.verify_release(release, 77, 78)

    def test_release_outside_fixed_prefix_never_accepted(self):
        with self.assertRaises(b1.PackageError): b1.verify_release(self.root / 'checkout', 77, 78)

    def test_runtime_preflight_checks_os_interpreter_hash_version_and_flags(self):
        lock = ready_lock()
        lock['python']['sha256'] = b1.digest(b'INTERPRETER_FIXTURE')
        flags = SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=1)
        for kind in ('valid', 'hash', 'os', 'version', 'flags'):
            def read(path):
                if str(path) == '/usr/lib/os-release':
                    return b'ID=ubuntu\nVERSION_ID="22.04"\n' if kind == 'os' else b'ID=ubuntu\nVERSION_ID="24.04"\n'
                return b'CHANGED' if kind == 'hash' else b'INTERPRETER_FIXTURE'
            with patch.object(b1, 'path_check'), patch.object(b1, 'regular_bytes', side_effect=read), \
                 patch.object(b1.platform, 'machine', return_value='x86_64'), \
                 patch.object(b1.platform, 'python_version', return_value='3.12.4' if kind == 'version' else '3.12.3'), \
                 patch.object(b1.sys, 'executable', '/usr/bin/python3.12'), \
                 patch.object(b1.sys, 'version_info', (3, 12, 3)), \
                 patch.object(b1.sys, 'flags', flags if kind != 'flags' else
                              SimpleNamespace(isolated=0, no_site=1, dont_write_bytecode=1)):
                if kind == 'valid':
                    b1.verify_runtime(lock)
                else:
                    with self.subTest(kind=kind), self.assertRaises(b1.PackageError): b1.verify_runtime(lock)

    def test_vendor_metadata_version_checked_even_when_content_digest_reviewed(self):
        metadata = self.root / 'ccxt-4.5.90.dist-info/METADATA'
        metadata.parent.mkdir()
        metadata.write_bytes(b'Name: ccxt\nVersion: 4.5.91\n')
        with self.assertRaises(b1.PackageError):
            b1.vendor_versions(self.root, [metadata.relative_to(self.root).as_posix()], {'ccxt': '4.5.90'})

    def test_root_owned_ancestor_chain_accepted_without_changing_permissions(self):
        with self.fake_paths(), patch.object(b1.os, 'listxattr', return_value=[]):
            b1.path_check(Path('/opt/phemex-readonly/releases'))

    def test_writable_wrong_owner_mode_link_and_acl_ancestors_rejected(self):
        for change in ({'st_uid': 1000}, {'st_gid': 1000}, {'st_mode': stat.S_IFDIR | 0o775},
                       {'st_mode': stat.S_IFLNK | 0o777}):
            with self.fake_paths({'/opt': change}), patch.object(b1.os, 'listxattr', return_value=[]):
                with self.assertRaises(b1.PackageError): b1.path_check(Path('/opt/phemex-readonly'))
        with self.fake_paths(), patch.object(b1.os, 'listxattr', return_value=['system.posix_acl_access']):
            with self.assertRaises(b1.PackageError): b1.path_check(Path('/opt'))

    def test_required_files_reject_hardlinks_or_executable_mode(self):
        for change in ({'st_mode': stat.S_IFREG | 0o644, 'st_nlink': 2},
                       {'st_mode': stat.S_IFREG | 0o755}):
            with self.fake_paths({'/opt/active.json': change}), patch.object(b1.os, 'listxattr', return_value=[]):
                with self.assertRaises(b1.PackageError):
                    b1.path_check(Path('/opt/active.json'), mode=0o644, directory=False)

    def test_real_temporary_symlink_and_hardlink_artifacts_rejected(self):
        source = self.root / 'regular'
        source.write_bytes(b'fixture')
        alias = self.root / 'alias'
        alias.symlink_to(source)
        with self.assertRaises(b1.PackageError): b1.regular_bytes(alias)
        alias.unlink()
        os.link(source, alias)
        with self.assertRaises(b1.PackageError): b1.regular_bytes(source)

    def state(self):
        header = bytearray(100)
        header[:16] = b'SQLite format 3\0'
        header[18:20] = b'\x01\x01'
        header[60:64] = struct.pack('>I', 1)
        header[68:72] = struct.pack('>I', 0x524F4754)
        binding = {'format': 1, 'purpose': b1.PURPOSE, 'device': 1, 'inode': 2}
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=77, st_gid=78,
                               st_nlink=1, st_dev=1, st_ino=2)
        return binding, info, bytes(header)

    def test_state_binding_accepts_only_explicit_identity_and_never_opens_sqlite(self):
        binding, info, header = self.state()
        with patch.object(b1, 'path_check'), patch.object(b1.os, 'lstat', return_value=info), \
             patch.object(b1.os, 'listxattr', return_value=[]), patch.object(b1.os.path, 'lexists', return_value=False), \
             patch.object(b1, 'regular_bytes', return_value=header), patch('sqlite3.connect') as sqlite:
            self.assertEqual(b1.state_binding(binding, 77, 78), (1, 2))
            sqlite.assert_not_called()

    def test_replaced_wrong_purpose_insecure_or_unrecovered_database_rejected(self):
        for kind in ('inode', 'permission', 'hardlink', 'purpose', 'sidecar', 'header'):
            binding, info, header = self.state()
            if kind == 'inode': info.st_ino += 1
            if kind == 'permission': info.st_mode = stat.S_IFREG | 0o644
            if kind == 'hardlink': info.st_nlink = 2
            if kind == 'purpose': binding['purpose'] = 'execution'
            if kind == 'header': header = b'not SQLite'
            with patch.object(b1, 'path_check'), patch.object(b1.os, 'lstat', return_value=info), \
                 patch.object(b1.os, 'listxattr', return_value=[]), \
                 patch.object(b1.os.path, 'lexists', return_value=kind == 'sidecar'), \
                 patch.object(b1, 'regular_bytes', return_value=header):
                with self.subTest(kind=kind), self.assertRaises(b1.PackageError):
                    b1.state_binding(binding, 77, 78)

    def test_missing_state_never_created_or_adopted(self):
        binding, _, _ = self.state()
        with patch.object(b1, 'path_check'), patch.object(b1.os, 'lstat', side_effect=FileNotFoundError), \
             patch('sqlite3.connect') as sqlite:
            with self.assertRaises(FileNotFoundError): b1.state_binding(binding, 77, 78)
            sqlite.assert_not_called()

    def test_upgrade_and_rollback_preserve_identical_state(self):
        current = b1.decode((PACKAGE / 'manifest.json').read_bytes())
        target = dict(current, release='blocked-previous-review')
        for old, new in ((current, target), (target, current)):
            plan = b1.switch_plan(old, new, (1, 2), (1, 2))
            self.assertEqual(plan['state_action'], 'preserve')
            self.assertEqual(plan['replace'], '/opt/phemex-readonly/active.json')
            self.assertNotIn('database', plan)
        with self.assertRaises(b1.PackageError): b1.switch_plan(current, target, (1, 2), (1, 3))
        with self.assertRaises(b1.PackageError): b1.switch_plan(current, target, None, None)

    def test_isolated_source_checker_ignores_caller_imports_and_startup_hooks(self):
        poison = self.root / 'poison'
        poison.mkdir()
        marker = self.root / 'executed'
        for name in ('sitecustomize.py', 'usercustomize.py', 'json.py'):
            (poison / name).write_text('raise RuntimeError("CALLER_IMPORT_EXECUTED")')
        result = subprocess.run([sys.executable, '-I', '-S', '-B', str(PACKAGE / 'package.py'),
                                 'source-check', '--checkout', str(REPO)],
                                cwd=poison, env=dict(os.environ, PYTHONPATH=str(poison),
                                PYTHONHOME=str(poison), PYTHONSTARTUP=str(poison / 'sitecustomize.py')),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('CALLER_IMPORT_EXECUTED', result.stderr)
        self.assertFalse(marker.exists())

    def test_launcher_refuses_development_checkout_or_unisolated_runtime(self):
        for flags in ([], ['-I', '-S', '-B']):
            result = subprocess.run([sys.executable, *flags, str(PACKAGE / 'launch.py')],
                                    capture_output=True, text=True, cwd=self.root)
            self.assertEqual(result.returncode, 1)
            self.assertIn('validation failed', result.stderr)

    def launcher_fixture(self):
        """Synthetic installed selection; no real installation or state access."""
        base = self.root / 'installed'
        release = base / 'releases' / 'blocked-bootstrap-fixture'
        release.mkdir(parents=True)
        shutil.copyfile(PACKAGE / 'launch.py', base / 'launch.py')
        marker = self.root / 'verified-source'
        source = release / 'package.py'
        source.write_text(
            'from pathlib import Path\n'
            f'Path({str(marker)!r}).write_text("source executed")\n'
            'def installed():\n'
            f'    Path({str(marker)!r}).write_text("verified source and preflight executed")\n')
        spec = b1.decode((PACKAGE / 'manifest.json').read_bytes())
        spec['release'] = release.name
        spec['tools']['package.py'] = b1.digest(source.read_bytes())
        spec['tools']['launch.py'] = b1.digest((base / 'launch.py').read_bytes())
        (release / 'manifest.json').write_text(json.dumps(spec))
        (base / 'active.json').write_text(json.dumps({'format': 1, 'release': release.name}))
        return base, source, marker

    def run_launcher_fixture(self, base, *, env=None):
        # Exercise main() in a genuinely isolated subprocess. Only target OS,
        # identity and root-owned path facts are mocked; the bootstrap source,
        # manifest, caches, digest check and compile/exec path are real.
        script = '''
import json, os, socket, sys, types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
base, launcher_file = map(Path, sys.argv[1:])
module = types.ModuleType('_launcher_fixture')
module.__file__ = str(base / 'launch.py')
exec(compile(launcher_file.read_bytes(), str(launcher_file), 'exec'), module.__dict__)
module.BASE = base
allowed = ['/usr/lib/python312.zip', '/usr/lib/python3.12', '/usr/lib/python3.12/lib-dynload']
# A previously imported module must not replace the verified source either.
def reject_cached_module():
    raise AssertionError('Unverified module cache used')
cached = types.ModuleType('_phemex_b1_preflight')
cached.installed = reject_cached_module
sys.modules[cached.__name__] = cached
with patch.object(module, 'root_path') as paths, \\
     patch.object(module.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=7777)), \\
     patch.object(module.os, 'geteuid', return_value=7777), \\
     patch.object(module.platform, 'machine', return_value='x86_64'), \\
     patch.object(module.sys, 'argv', [str(base / 'launch.py')]), \\
     patch.object(module.sys, 'path', allowed.copy()), \\
     patch.object(module.sys, 'version_info', (3, 12, 0)), \\
     patch.object(module.sys, 'executable', '/usr/bin/python3.12'), \\
     patch.object(Path, 'read_text', return_value='ID=ubuntu\\nVERSION_ID="24.04"\\n'), \\
     patch.object(module.runpy, 'run_module') as worker, \\
     patch.object(socket.socket, 'connect', side_effect=AssertionError('Network prohibited')):
    code = module.main()
    print(json.dumps({'worker_called': worker.called,
                      'checked_paths': [str(call.args[0]) for call in paths.call_args_list],
                      'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site,
                      'no_bytecode_writes': sys.flags.dont_write_bytecode}))
sys.exit(code)
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script,
                                 str(base), str(PACKAGE / 'launch.py')],
                                cwd=self.root, env=env, capture_output=True, text=True)
        info = json.loads(result.stdout)
        self.assertEqual((info['isolated'], info['no_site'], info['no_bytecode_writes']), (1, 1, 1))
        return result, info

    def malicious_cache(self, source, kind, *, external=False):
        marker = self.root / 'unverified-bytecode'
        code = compile('from pathlib import Path\n'
                       f'Path({str(marker)!r}).write_text("unverified bytecode executed")\n'
                       'def installed():\n    pass\n', str(source), 'exec')
        if external:
            target = self.root / 'attacker-cache'
            target.mkdir()
            (source.parent / '__pycache__').symlink_to(target, target_is_directory=True)
        cache = Path(importlib.util.cache_from_source(str(source)))
        cache.parent.mkdir(exist_ok=True)
        if kind == 'timestamp':
            info = source.stat()
            header = struct.pack('<III', 0, int(info.st_mtime) & 0xffffffff, info.st_size)
        else:
            flags = 3 if kind == 'checked-hash' else 1
            header = struct.pack('<I', flags) + importlib.util.source_hash(source.read_bytes())
        cache.write_bytes(importlib.util.MAGIC_NUMBER + header + marshal.dumps(code))
        return marker

    def assert_cache_cannot_replace_source(self, kind, *, external=False):
        base, source, verified = self.launcher_fixture()
        marker = self.malicious_cache(source, kind, external=external)
        # Control: prove these are valid malicious caches, accepted by precisely
        # the import-loader sequence removed from the production launcher.
        old = importlib.util.spec_from_file_location('_phemex_b1_preflight', source)
        old.loader.exec_module(importlib.util.module_from_spec(old))
        self.assertTrue(marker.exists())
        self.assertFalse(verified.exists())
        marker.unlink()
        result, info = self.run_launcher_fixture(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(info['worker_called'])
        self.assertEqual(verified.read_text(), 'verified source and preflight executed')
        self.assertFalse(marker.exists())
        manifest = str(source.parent / 'manifest.json')
        self.assertLess(info['checked_paths'].index(manifest), info['checked_paths'].index(str(source)))

    def test_bootstrap_ignores_valid_malicious_timestamp_bytecode(self):
        self.assert_cache_cannot_replace_source('timestamp')

    def test_bootstrap_ignores_valid_malicious_checked_hash_bytecode(self):
        self.assert_cache_cannot_replace_source('checked-hash')

    def test_bootstrap_ignores_valid_malicious_unchecked_hash_bytecode(self):
        self.assert_cache_cannot_replace_source('unchecked-hash')

    def test_bootstrap_ignores_attacker_controlled_cache_directory(self):
        self.assert_cache_cannot_replace_source('timestamp', external=True)

    def test_bootstrap_executes_verified_source_without_cache(self):
        base, _, marker = self.launcher_fixture()
        result, info = self.run_launcher_fixture(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(info['worker_called'])
        self.assertEqual(marker.read_text(), 'verified source and preflight executed')

    def test_bootstrap_source_hash_mismatch_blocks_source_and_bytecode(self):
        base, source, verified = self.launcher_fixture()
        source.write_bytes(source.read_bytes() + b'\n# unapproved change\n')
        marker = self.malicious_cache(source, 'checked-hash')
        result, info = self.run_launcher_fixture(base)
        self.assertEqual(result.returncode, 1)
        self.assertIn('details withheld', result.stderr)
        self.assertFalse(info['worker_called'])
        self.assertFalse(verified.exists())
        self.assertFalse(marker.exists())

    def test_bootstrap_missing_manifest_fails_closed(self):
        base, source, verified = self.launcher_fixture()
        (source.parent / 'manifest.json').unlink()
        marker = self.malicious_cache(source, 'timestamp')
        result, info = self.run_launcher_fixture(base)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(info['worker_called'])
        self.assertFalse(verified.exists())
        self.assertFalse(marker.exists())

    def test_bootstrap_malformed_or_ambiguous_manifest_fails_closed(self):
        base, source, verified = self.launcher_fixture()
        marker = self.malicious_cache(source, 'timestamp')
        for raw in (b'{', b'[]', b'{}', b'{"tools":{"package.py":null}}',
                    b'{"tools":{},"tools":{}}'):
            with self.subTest(raw=raw):
                (source.parent / 'manifest.json').write_bytes(raw)
                result, info = self.run_launcher_fixture(base)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(info['worker_called'])
                self.assertFalse(verified.exists())
                self.assertFalse(marker.exists())

    def test_bootstrap_ignores_poisoned_python_environment(self):
        base, source, verified = self.launcher_fixture()
        marker = self.malicious_cache(source, 'unchecked-hash')
        poison = self.root / 'poison'
        poison.mkdir()
        for name in ('sitecustomize.py', 'usercustomize.py', 'json.py', '_phemex_b1_preflight.py'):
            (poison / name).write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'
                                       'raise RuntimeError("POISON_EXECUTED")\n')
        env = dict(os.environ, PYTHONPATH=str(poison), PYTHONHOME=str(poison),
                   PYTHONSTARTUP=str(poison / 'sitecustomize.py'), PYTHONPYCACHEPREFIX=str(poison))
        result, info = self.run_launcher_fixture(base, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(info['worker_called'])
        self.assertTrue(verified.exists())
        self.assertFalse(marker.exists())
        self.assertNotIn('POISON_EXECUTED', result.stderr)

    def test_no_install_activate_or_network_mode_exists(self):
        result = subprocess.run([sys.executable, '-I', '-S', '-B', str(PACKAGE / 'package.py'), 'install'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(hasattr(b1, 'install'))
        self.assertFalse(hasattr(b1, 'reset_state'))
