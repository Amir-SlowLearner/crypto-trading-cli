"""Synthetic installed trees; no installer, credentials or network activation."""
from copy import deepcopy
from contextlib import ExitStack
import hashlib
import io
import json
import marshal
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from deploy.ubuntu24 import package as b1
from deploy.ubuntu24 import status_launch as boot


REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / 'deploy/ubuntu24'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class StatusBootstrapTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network denied'))
        guard.start()
        self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'installed'
        self.root.mkdir()
        self.root.chmod(0o755)
        self.patch = patch.object(boot, 'BASE', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, name, raw):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        for parent in (path.parent, *path.parent.parents):
            if parent == self.root or self.root in parent.parents:
                parent.chmod(0o755)  # synthetic tree only, never host ancestors
        path.write_bytes(raw)
        path.chmod(0o644)

    def fixture(self):
        # Only this synthetic fixture overrides the target-runtime assertion:
        # the development host is not Ubuntu 24.04/Python 3.12. All artifact,
        # ownership, inventory and dependency validation below stays active.
        preflight = (DEPLOY / 'package.py').read_bytes() + b'\ndef verify_runtime(lock): pass\nVERIFIED_SOURCE = True\n'
        self.write('preflight.py', preflight)
        self.write('launch.py', b'# synthetic installed launcher\n')
        requirements = b'ccxt==4.5.90\n'
        self.write('requirements.txt', requirements)
        vendor = {'ccxt/__init__.py': b'# synthetic dependency\n',
                  'ccxt-4.5.90.dist-info/METADATA': b'Name: ccxt\nVersion: 4.5.90\n'}
        for name, raw in vendor.items():
            self.write('vendor/' + name, raw)
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, 'w') as archive:
            for name, raw in vendor.items():
                archive.writestr(name, raw)
            archive.writestr('ccxt-4.5.90.dist-info/WHEEL', 'Tag: py3-none-any\n')
        wheel_name = 'ccxt-4.5.90-py3-none-any.whl'
        self.write('wheels/' + wheel_name, wheel.getvalue())
        lock = {'format': 1, 'target': dict(b1.TARGET), 'status': 'reviewed',
                'pins': {'ccxt': '4.5.90'}, 'python': {'version': '3.12.3', 'sha256': 'a'*64},
                'wheels': [{'name': 'ccxt', 'version': '4.5.90', 'filename': wheel_name,
                            'sha256': digest(wheel.getvalue())}],
                'vendor_files': {name: digest(raw) for name, raw in vendor.items()}}
        self.write('dependencies.json', json.dumps(lock).encode())
        for name in boot.APPS:
            self.write('app/' + name, b'# synthetic reviewed application\n')
        self.spec = {'format': 1, 'purpose': boot.PURPOSE, 'baseline': boot.BASELINE,
                     'target': dict(b1.TARGET), 'app': {n: digest((self.root/'app'/n).read_bytes())
                                                     for n in boot.APPS},
                     'tools': {n: digest((self.root/n).read_bytes()) for n in ('launch.py','preflight.py')},
                     'dependencies_sha256': digest((self.root/'dependencies.json').read_bytes()),
                     'requirements_sha256': digest(requirements)}
        return self.save_manifest()

    def save_manifest(self):
        self.write('release.json', json.dumps(self.spec).encode())
        return digest((self.root/'release.json').read_bytes())

    def permissions(self):
        original_lstat, original_fstat = os.lstat, os.fstat
        def protected(info, path=None):
            values = list(info)
            values[4] = values[5] = 0  # synthetic installed ownership only
            if path is not None and Path(path) not in (self.root, *self.root.parents) and self.root not in Path(path).parents:
                raise AssertionError('Unexpected path inspection')
            if path is not None and Path(path) in self.root.parents:
                values[0] = stat.S_IFDIR | 0o755
            return os.stat_result(values)
        return (patch.object(os, 'lstat', side_effect=lambda p: protected(original_lstat(p), p)),
                patch.object(os, 'fstat', side_effect=lambda fd: protected(original_fstat(fd))))

    def verify(self, release_id):
        a, b = self.permissions()
        # The test runner has already imported app modules. Model the pristine
        # subprocess's empty app cache here; test the cache rejection separately.
        with a, b, patch.object(boot, '_import_roots'):
            return boot.verify_release(release_id)

    def test_committed_source_inventory_matches_every_source_and_tool(self):
        spec = json.loads((DEPLOY/'status-release.json').read_bytes())
        self.assertEqual(set(spec['app']), boot.APPS)
        for name, expected in spec['app'].items():
            self.assertEqual(digest((REPO/name).read_bytes()), expected, name)
        for key, source in (('launch.py','status_launch.py'),('preflight.py','package.py')):
            self.assertEqual(spec['tools'][key], digest((DEPLOY/source).read_bytes()))
        self.assertEqual(spec['dependencies_sha256'], digest((DEPLOY/'status-dependencies.json').read_bytes()))
        self.assertEqual(spec['requirements_sha256'], digest((REPO/'requirements.txt').read_bytes()))

    def test_target_dependencies_remain_unapproved_even_if_status_flipped(self):
        lock = json.loads((DEPLOY/'status-dependencies.json').read_bytes())
        self.assertEqual(lock['status'], 'awaiting-target-artifact-review')
        for status in (lock['status'], 'reviewed'):
            lock['status'] = status
            with self.assertRaises(b1.PackageError): b1.dependency_lock(lock, lock['pins'])

    def test_complete_synthetic_installed_tree_verifies_before_app_import(self):
        release_id = self.fixture()
        self.assertEqual(self.verify(release_id), self.spec)
        self.assertFalse((self.root/'app/__pycache__').exists())

    def test_release_digest_mismatch_rejected_before_preflight_execution(self):
        self.fixture()
        with patch('builtins.exec') as execute, self.assertRaises(ValueError):
            self.verify('f'*64)
        execute.assert_not_called()

    def test_source_tamper_rejected(self):
        release_id = self.fixture()
        self.write('app/testnet_status_runtime.py', b'ATTACK\n')
        with self.assertRaises(ValueError): self.verify(release_id)

    def test_preflight_tamper_rejected_before_execution(self):
        release_id = self.fixture()
        self.write('preflight.py', b'raise RuntimeError("PRIVATE_SENTINEL")')
        with patch('builtins.exec') as execute, self.assertRaises(ValueError):
            self.verify(release_id)
        execute.assert_not_called()

    def test_installed_launcher_tamper_rejected(self):
        release_id = self.fixture()
        self.write('launch.py', b'ATTACK')
        with self.assertRaises(ValueError): self.verify(release_id)

    def test_malformed_missing_and_duplicate_manifest_fail_closed(self):
        release_id = self.fixture()
        for raw in (b'{', b'{"format":1,"format":1}', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.write('release.json', raw)
                self.verify(digest(raw))
        (self.root/'release.json').unlink()
        with self.assertRaises(OSError): self.verify(release_id)

    def test_extra_and_missing_inventory_members_rejected(self):
        self.fixture()
        for key, value in (('extra', 'a'*64), ('../escape', 'a'*64)):
            original = deepcopy(self.spec)
            self.spec['app'][key] = value
            with self.assertRaises(ValueError): self.verify(self.save_manifest())
            self.spec = original
        del self.spec['app']['safety.py']
        with self.assertRaises(ValueError): self.verify(self.save_manifest())

    def test_extra_files_and_empty_directories_rejected(self):
        release_id = self.fixture()
        for name in ('app/extra.py', 'vendor/extra.py', 'wheels/extra.whl', 'extra'):
            self.write(name, b'ATTACK')
            with self.subTest(name=name), self.assertRaises(ValueError): self.verify(release_id)
            (self.root/name).unlink()
        (self.root/'vendor/extra').mkdir()
        with self.assertRaises(ValueError): self.verify(release_id)

    def test_dependency_source_hash_mismatch_rejected(self):
        release_id = self.fixture()
        self.write('vendor/ccxt/__init__.py', b'ATTACK')
        with self.assertRaises(ValueError): self.verify(release_id)

    def test_dependency_version_mismatch_rejected_even_with_matching_file_hash(self):
        self.fixture()
        raw = b'Name: ccxt\nVersion: 4.5.91\n'
        self.write('vendor/ccxt-4.5.90.dist-info/METADATA', raw)
        lock = json.loads((self.root/'dependencies.json').read_bytes())
        lock['vendor_files']['ccxt-4.5.90.dist-info/METADATA'] = digest(raw)
        self.write('dependencies.json', json.dumps(lock).encode())
        self.spec['dependencies_sha256'] = digest((self.root/'dependencies.json').read_bytes())
        with self.assertRaises(ValueError): self.verify(self.save_manifest())

    def test_unapproved_dependencies_prevent_application_import(self):
        self.fixture()
        raw = (DEPLOY/'status-dependencies.json').read_bytes()
        self.write('dependencies.json', raw)
        self.spec['dependencies_sha256'] = digest(raw)
        with self.assertRaises(ValueError): self.verify(self.save_manifest())

    def test_wheel_tampering_rejected(self):
        release_id = self.fixture()
        self.write('wheels/ccxt-4.5.90-py3-none-any.whl', b'not the reviewed wheel')
        with self.assertRaises(ValueError): self.verify(release_id)

    def test_unreviewed_startup_hooks_and_bytecode_rejected(self):
        release_id = self.fixture()
        for name in ('sitecustomize.py','usercustomize.py','evil.pth','__pycache__/x.pyc'):
            self.write('vendor/'+name, b'ATTACK')
            with self.subTest(name=name), self.assertRaises(ValueError): self.verify(release_id)
            (self.root/'vendor'/name).unlink()
            if '/' in name: (self.root/'vendor/__pycache__').rmdir()

    def test_writable_dependency_file_and_directory_rejected(self):
        release_id = self.fixture()
        for name, mode, restore in (('vendor/ccxt/__init__.py',0o666,0o644),('vendor/ccxt',0o777,0o755)):
            path = self.root/name
            path.chmod(mode)
            with self.subTest(name=name), self.assertRaises(ValueError): self.verify(release_id)
            path.chmod(restore)

    def test_symlink_and_hardlink_rejected(self):
        release_id = self.fixture()
        path = self.root/'app/safety.py'
        raw = path.read_bytes()
        path.unlink()
        path.symlink_to(self.root/'app/config.py')
        with self.assertRaises(ValueError): self.verify(release_id)
        path.unlink()
        path.write_bytes(raw)
        os.link(path, self.root/'app/extra-link.py')
        with self.assertRaises(ValueError): self.verify(release_id)

    def test_preflight_executes_verified_bytes_not_timestamp_or_hash_pyc(self):
        self.fixture()
        marker = self.root/'EXECUTED'
        malicious = compile(f'open({str(marker)!r}, "w").write("ATTACK")', 'preflight.py', 'exec')
        import importlib.util
        source = (self.root/'preflight.py').read_bytes()
        stamp = int((self.root/'preflight.py').stat().st_mtime)
        for header in (importlib.util.MAGIC_NUMBER + struct.pack('<III',0,stamp,len(source)),
                       importlib.util.MAGIC_NUMBER + struct.pack('<I',3) + importlib.util.source_hash(source)):
            cache = self.root/'__pycache__'
            cache.mkdir(exist_ok=True)
            (cache/f'preflight.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc').write_bytes(header+marshal.dumps(malicious))
            release_id = self.save_manifest()
            # The extra directory rejects the release; source preflight still
            # executes, and no timestamp/hash cache payload is ever consulted.
            execute = exec
            verified = []
            def observe(code, namespace):
                execute(code, namespace)
                verified.append(namespace.get('VERIFIED_SOURCE'))
            with patch('builtins.exec', side_effect=observe), self.assertRaises(ValueError):
                self.verify(release_id)
            self.assertEqual(verified, [True])
            self.assertFalse(marker.exists())

    def startup_fixture(self, **changes):
        """Model target OS facts; never claims to test the actual target host."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        facts = {'argv':[str(self.root/'launch.py'),'_worker'], 'uid':5000, 'gid':5000,
                 'groups':[], 'env':{'LANG':'C.UTF-8'}, 'path':list(boot.STDLIB),
                 'flags':SimpleNamespace(isolated=True,no_site=True,dont_write_bytecode=True),
                 'file':str(self.root/'launch.py'), 'python':boot.PYTHON, 'version':(3,12,3)}
        facts.update(changes)
        for name, value in (('argv',facts['argv']),('path',facts['path']),('flags',facts['flags']),
                            ('executable',facts['python']),('version_info',facts['version']),
                            ('pycache_prefix',None),('_xoptions',{})):
            stack.enter_context(patch.object(sys,name,value))
        stack.enter_context(patch.object(boot,'__file__',facts['file']))
        stack.enter_context(patch.object(boot.platform,'machine',return_value='x86_64'))
        stack.enter_context(patch.object(os,'geteuid',return_value=facts['uid']))
        stack.enter_context(patch.object(os,'getegid',return_value=facts['gid']))
        stack.enter_context(patch.object(os,'getgroups',return_value=facts['groups']))
        stack.enter_context(patch.dict(os.environ,facts['env'],clear=True))
        stack.enter_context(patch.object(boot.pwd,'getpwnam',return_value=SimpleNamespace(
            pw_uid=5000,pw_gid=5000,pw_shell='/usr/sbin/nologin')))
        stack.enter_context(patch.object(boot,'_root_path'))
        stack.enter_context(patch.object(boot,'_read',return_value=b'{"worker_uid":5000,"release_id":"' + b'a'*64 + b'"}'))
        verifier = stack.enter_context(patch.object(boot,'verify_release',return_value={'synthetic':True}))
        return stack, verifier

    def test_fixed_startup_accepts_only_root_operator_or_dedicated_worker_role(self):
        for role, uid in (('issue',0),('run',0),('_worker',5000)):
            stack, verifier = self.startup_fixture(argv=[str(self.root/'launch.py'),role],uid=uid)
            with stack:
                self.assertEqual(boot._startup(),{'synthetic':True})
                verifier.assert_called_once_with('a'*64)

    def test_fixed_startup_rejects_arbitrary_commands_extra_arguments_and_wrong_identity(self):
        for changes in ({'argv':['launch.py','arbitrary.py']},
                        {'argv':['launch.py','_worker','--url=anything']},
                        {'argv':['launch.py','issue'],'uid':5000}, {'uid':0},
                        {'gid':0}, {'groups':[0]}, {'file':str(REPO/'testnet_status_once.py')}):
            stack, verifier = self.startup_fixture(**changes)
            with stack, self.subTest(changes=changes), self.assertRaises(ValueError): boot._startup()
            verifier.assert_not_called()

    def test_fixed_startup_rejects_poisoned_environment_paths_and_flags(self):
        for changes in ({'env':{'PYTHONPATH':'/cli'}}, {'env':{'LD_PRELOAD':'/cli.so'}},
                        {'env':{'HOME':'/cli'}}, {'path':list(boot.STDLIB)+['/cli']},
                        {'flags':SimpleNamespace(isolated=True,no_site=False,dont_write_bytecode=True)},
                        {'python':'/cli/python'}, {'version':(3,10,12)}):
            stack, verifier = self.startup_fixture(**changes)
            with stack, self.subTest(changes=changes), self.assertRaises(ValueError): boot._startup()
            verifier.assert_not_called()

    def test_reviewed_vendor_lock_still_forbids_pth_and_bytecode(self):
        self.fixture()
        lock = json.loads((self.root/'dependencies.json').read_bytes())
        for name in ('evil.pth','__pycache__/evil.pyc','sitecustomize.py'):
            value = deepcopy(lock)
            value['vendor_files'][name] = 'a'*64
            self.write('dependencies.json',json.dumps(value).encode())
            self.spec['dependencies_sha256'] = digest((self.root/'dependencies.json').read_bytes())
            with self.subTest(name=name), self.assertRaises(ValueError): self.verify(self.save_manifest())

    def test_fixed_startup_rejects_external_bytecode_cache_and_xoptions(self):
        for name, value in (('pycache_prefix','/cli/cache'),('_xoptions',{'pycache_prefix':'/cli/cache'})):
            stack, verifier = self.startup_fixture()
            with stack, patch.object(sys,name,value), self.subTest(name=name), self.assertRaises(ValueError):
                boot._startup()
            verifier.assert_not_called()

    def test_insecure_ancestor_acl_and_owner_rejected(self):
        self.fixture()
        a,b = self.permissions()
        with a,b:
            original = os.lstat
            for kind in ('writable','owner','acl'):
                def info(path):
                    value = original(path)
                    if Path(path) == self.root:
                        seq = list(value)
                        if kind == 'writable': seq[0] = stat.S_IFDIR | 0o777
                        if kind == 'owner': seq[4] = 5000
                        value = os.stat_result(seq)
                    return value
                with patch.object(os,'lstat',side_effect=info), \
                        patch.object(os,'listxattr',return_value=['system.posix_acl_access'] if kind=='acl' else []), \
                        self.subTest(kind=kind), self.assertRaises(ValueError):
                    boot._root_path(self.root/'release.json')

    def test_pristine_import_cache_required(self):
        with patch.dict(sys.modules, {'ccxt': SimpleNamespace()}), self.assertRaises(ValueError):
            boot._import_roots({'worker.py': 'a'*64}, {'ccxt/__init__.py': 'b'*64})

    def test_application_dependency_and_stdlib_shadowing_rejected(self):
        for app, vendor in (({'worker.py': ''}, {'worker/__init__.py': ''}),
                            ({'worker.py': ''}, {'json/__init__.py': ''})):
            with self.assertRaises(ValueError): boot._import_roots(app, vendor)

    def test_development_checkout_and_unsupported_flags_cannot_start(self):
        with patch.object(boot, '_read') as read, patch.object(boot, 'verify_release') as verify:
            with patch('sys.stderr', io.StringIO()): self.assertEqual(boot.main(), 1)
        read.assert_not_called()
        verify.assert_not_called()

    def test_isolated_python_ignores_poisoned_import_environment(self):
        poison = Path(self.temp.name)/'poison'
        poison.mkdir()
        marker = poison/'executed'
        for name in ('sitecustomize.py','usercustomize.py','json.py'):
            (poison/name).write_text(f'open({str(marker)!r}, "w").write("ATTACK")\n')
        env = {'PYTHONPATH':str(poison),'PYTHONHOME':str(poison),'PYTHONUSERBASE':str(poison),
               'PYTHONSTARTUP':str(poison/'sitecustomize.py')}
        result = subprocess.run([sys.executable,'-I','-S','-B','-c',
            'import json,sys; print(int(sys.flags.isolated),int(sys.flags.no_site),int(sys.flags.dont_write_bytecode)); print(json.__file__)'],
            env=env,cwd=poison,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(result.stdout.startswith('1 1 1\n'))
        self.assertNotIn(str(poison),result.stdout)
        self.assertFalse(marker.exists())

    def test_main_imports_only_the_fixed_entry_and_role_after_preflight(self):
        entry = Mock(return_value=1)
        with patch.object(boot,'_startup') as startup, patch.object(sys,'path',list(sys.path)), \
                patch.object(sys,'argv',['launch.py','_worker']), \
                patch.dict(sys.modules,{'testnet_status_once':SimpleNamespace(main=entry)}):
            self.assertEqual(boot.main(),1)
            self.assertEqual(sys.path[-2:],[str(self.root/'app'),str(self.root/'vendor')])
        startup.assert_called_once_with()
        entry.assert_called_once_with(['_worker'])

    def test_preflight_failure_is_sanitized_and_never_imports_application(self):
        with patch.object(boot,'_startup',side_effect=ValueError('SECRET_SENTINEL')), \
                patch('builtins.__import__', wraps=__import__) as imports, patch('sys.stderr',io.StringIO()) as error:
            self.assertEqual(boot.main(),1)
        self.assertNotIn('SECRET_SENTINEL',error.getvalue())
        self.assertFalse(any(c.args[0]=='testnet_status_once' for c in imports.call_args_list))


if __name__ == '__main__':
    unittest.main()
