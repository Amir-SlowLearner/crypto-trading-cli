"""Fixed installed entry point. No networking, credential loader or install mode."""
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import runpy
import stat
import sys


BASE = Path('/opt/phemex-readonly')


def root_path(path, directory=False, mode=0o644):
    for part in reversed((path, *path.parents)):
        value = os.lstat(part)
        final = part == path
        if (not (stat.S_ISDIR(value.st_mode) if not final or directory else stat.S_ISREG(value.st_mode))
                or value.st_uid != 0 or value.st_gid != 0
                or stat.S_IMODE(value.st_mode) != (mode if final else 0o755)
                or (final and not directory and value.st_nlink != 1)
                or any(name.startswith('system.posix_acl_') for name in os.listxattr(part, follow_symlinks=False))):
            raise ValueError('Unsafe installed bootstrap path')


def unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Ambiguous bootstrap metadata')
        result[key] = value
    return result


def main():
    try:
        if (Path(__file__) != BASE / 'launch.py' or len(sys.argv) != 1
                or not sys.flags.isolated or not sys.flags.no_site
                or not sys.flags.dont_write_bytecode or sys.version_info[:2] != (3, 12)
                or sys.executable != '/usr/bin/python3.12' or platform.machine() != 'x86_64'):
            raise ValueError('Unsupported isolated runtime')
        account = pwd.getpwnam('phemex-ro')
        if os.geteuid() != account.pw_uid or account.pw_uid == 0:
            raise ValueError('Run only as the dedicated worker')
        allowed = ['/usr/lib/python312.zip', '/usr/lib/python3.12', '/usr/lib/python3.12/lib-dynload']
        if sys.path != allowed:
            raise ValueError('Unexpected interpreter search path')
        for name in allowed:
            path = Path(name)
            if path.name.endswith('.zip') and not os.path.lexists(path):
                root_path(path.parent, directory=True, mode=0o755)
            else:
                root_path(path, directory=not name.endswith('.zip'), mode=0o644 if name.endswith('.zip') else 0o755)
        root_path(Path('/usr/lib/os-release'))
        os_release = dict(line.split('=', 1) for line in Path('/usr/lib/os-release').read_text().splitlines()
                          if '=' in line and not line.startswith('#'))
        os_release = {key: value.strip('"') for key, value in os_release.items()}
        if os_release.get('ID') != 'ubuntu' or os_release.get('VERSION_ID') != '24.04':
            raise ValueError('Unsupported operating system')
        root_path(BASE / 'active.json')
        selection = json.loads((BASE / 'active.json').read_bytes(), object_pairs_hook=unique)
        release = selection['release']
        if type(release) is not str or not release.startswith('blocked-') or any(
                char not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for char in release):
            raise ValueError('Invalid release')
        root = BASE / 'releases' / release
        root_path(root / 'manifest.json')
        spec = json.loads((root / 'manifest.json').read_bytes(), object_pairs_hook=unique)
        source_path = root / 'package.py'
        root_path(source_path)
        source = source_path.read_bytes()
        if hashlib.sha256(source).hexdigest() != spec['tools']['package.py']:
            raise ValueError('Unreviewed preflight tool')
        # The root-controlled manifest supplies the trusted expected digest.
        # Execute this exact buffer: an import loader could substitute .pyc even
        # with -B. Neither sys.modules nor an on-disk bytecode cache is consulted.
        preflight = {'__name__': '_phemex_b1_preflight', '__file__': str(source_path)}
        exec(compile(source, str(source_path), 'exec', dont_inherit=True), preflight)
        preflight['installed']()
        root_path(BASE / 'launch.py')
        if hashlib.sha256((BASE / 'launch.py').read_bytes()).hexdigest() != spec['tools']['launch.py']:
            raise ValueError('Installed launcher differs')
        # -S suppresses site/.pth/user-site activation. Only reviewed directories
        # are added, after every file and dependency version has been checked.
        sys.path[:] = allowed + [str(root / 'app'), str(root / 'vendor')]
        runpy.run_module('testnet_readonly_worker', run_name='__main__')
        return 0
    except Exception:
        print('Blocked worker installation validation failed; details withheld.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
