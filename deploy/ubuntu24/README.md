# B1: offline Ubuntu 24.04 blocked-worker source package

**Nothing in this directory installs or activates anything.** There is no
privileged installer, service unit, credential loader, network backend, firewall
command, account-creation command or approval-database initializer.

This package provides reproducible source bundling, read-only preflight
validators, a fixed installed launcher and an operator installation contract.
The application payload is the nine reviewed worker/support modules from
6a7acd4beebc729761d838905727b568381f87f8. The source tree, legacy CLI and
execution/simulation modules are not deployed wholesale.

## Current readiness: SOURCE ONLY

[manifest.json](manifest.json) pins source/tool SHA-256 values, the requirements
file digest, the dependency-lock digest, the baseline and the offline purpose.
[dependencies.json](dependencies.json) preserves the existing exact version pins
but is explicitly **awaiting target artifact review**. The existing environment
was captured on Ubuntu 22.04/Python 3.10; it is not evidence of compatible Ubuntu
24.04/CPython 3.12 artifacts.

No target wheel or interpreter hashes have been invented. There are no downloaded
wheels in this package. A deployment cannot pass preflight until an administrator
obtains and independently reviews the complete CPython 3.12/x86-64 wheel set,
exact interpreter version/digest and complete installed vendor-file inventory.
Dependency closure and Ubuntu compatibility must be checked on that target,
using an offline wheelhouse. The pending lock cannot pass by changing its status
alone. Changes to the lock and its manifest digest need code review.

Hashes detect differences from reviewed input; they do not authenticate their own
origin. The administrator must obtain the package and manifest from a trusted,
reviewed revision. Never accept a caller-supplied manifest as approval, or derive
a new trusted inventory automatically from an existing installation.

## Offline tools

[package.py](package.py) uses only the standard library. From the checkout:

~~~sh
.venv/bin/python -I -S -B deploy/ubuntu24/package.py source-check --checkout .
.venv/bin/python -I -S -B deploy/ubuntu24/package.py source-bundle --checkout . --output /tmp/phemex-b1-source.tar
~~~

The output archive must not already exist. Bundling reads/hash-checks the exact
allowlist, excludes credentials and unrelated files, and creates regular archive
members with fixed ordering, ownership metadata, permissions and timestamps.
It neither extracts files nor touches an installation. Interrupted output is not
a successful package; do not reuse a partial archive.

After a separately reviewed complete target lock exists, wheels-check requires
--checkout and --wheelhouse. It rejects additional wheelhouse entries, hash
differences, wrong metadata/versions, incompatible target tags, duplicate ZIP
members, traversal and special files. Only wheels are accepted, not sdists.
The installed-tree checker also compares exact file inventories and dependency
METADATA versions, including rejecting unexpected startup hooks and .pth files.

check-installation performs a read-only check of the fixed installed selection.
There is no install, activate, reset, adopt or restore subcommand.
The library checks raise on unsafe inputs. The command reports a fixed failure
and nonzero exit without repairing paths or exposing their contents.

## Identity, layout and ownership

All paths below are proposed installation paths, not paths created by this task.
The trusted administrator must check every ancestor and refuse unexpected
existing paths; do not repair or chown them automatically.

| Installed path | Owner/group | Mode and restrictions |
| --- | --- | --- |
| /, /opt, /var, /var/lib, /usr, /usr/bin, /usr/lib | root:root | directories 0755; no symlinks or extended ACLs on checked paths |
| /opt/phemex-readonly/ and releases/ | root:root | 0755 |
| /opt/phemex-readonly/launch.py | root:root | regular 0644, one hard link |
| /opt/phemex-readonly/active.json | root:root | regular 0644, bounded release name, no symlink |
| /opt/phemex-readonly/state-binding.json | root:root | regular 0644, explicit offline state identity receipt |
| releases/RELEASE/ and all subdirectories | root:root | 0755 |
| all files inside a release | root:root | regular 0644, one hard link, exact reviewed inventory/hash |
| /var/lib/phemex-readonly-worker/ | phemex-ro:phemex-ro | 0700 |
| offline-approvals.sqlite3 | phemex-ro:phemex-ro | regular 0600, one hard link, bound device/inode |
| /usr/bin/python3.12 | root:root | regular 0755, reviewed exact version and SHA-256 |
| versioned standard-library directories | root:root | 0755; part of the trusted Ubuntu base |

The static account is described, but not created, by
[phemex-ro.sysusers](phemex-ro.sysusers). An administrator must separately review
the account creation: locked login/password, /usr/sbin/nologin, /nonexistent
home, stable nonzero UID/GID, no administrative groups, and no other workloads.
Do not place this proposal in an automatically processed system directory yet.
Preflight checks the passwd shell and primary UID/GID, not the password database
or all OS authorization policy; those remain administrator deployment gates.

The private environment is a reviewed app/ plus vendor/ tree rather than an
activated development venv. A target build must install wheels offline with
dependency resolution/network disabled and no bytecode compilation, using a
trusted toolchain, then review the installed inventory. Never copy the existing
Python 3.10 environment. Do not let pip, an updater or the worker change a release.
The interpreter, standard library, native loader and OS libraries remain trusted
Ubuntu components; this package is not a reproducible build of Ubuntu itself.

## Fixed runtime

The future fixed executable vector is:

~~~text
/usr/bin/python3.12 -I -S -B /opt/phemex-readonly/launch.py
~~~

[launch.py](launch.py) refuses execution from the checkout, extra arguments,
the wrong OS/interpreter/architecture/UID, non-isolated flags, altered initial
import roots, unreviewed files/dependencies and missing state binding. It does
not accept a caller-supplied release root or import path. -I ignores
PYTHONPATH/PYTHONHOME and excludes the current/script directory; -S prevents
site initialization, user site and executable .pth hooks. -B avoids bytecode
writes. Only after preflight does the launcher append the fixed reviewed app and
vendor directories to the versioned standard-library search path.

The root launcher reads the release preflight source once, verifies its SHA-256
against the manifest in the checked root-controlled release, and compiles/executes
exactly that verified buffer. It does not use an import loader, sys.modules or
timestamp/hash-based bytecode caches for this bootstrap. -B alone would not
prevent reading cached bytecode. Unexpected cache files still fail the subsequent
release inventory check. The launcher also verifies its own installed digest.
The expected source digest comes from the administrator-authenticated manifest;
changing both the manifest and source defeats internal consistency checks and
requires control of trusted installation inputs. These checks do not establish
manifest authenticity independently. The launcher and manifests are trusted root
inputs, not an in-process security sandbox. System-library integrity, privileged
Python build tools and administrative shell startup files require independent
OS/package checks. Directories with unusual modes/ACLs fail instead of falling
back to relaxed policy.

Python isolation does not neutralize native-loader variables such as LD_PRELOAD,
LD_LIBRARY_PATH or LD_AUDIT before the interpreter starts. A future root-controlled
launcher/systemd unit must construct a clean environment before exec; never
forward the caller's environment. Checking those variables inside Python would
be too late to establish that guarantee. B1 does not supply a privileged pre-exec
launcher, and its import tests cover Python startup/search-path isolation only.

The payload's HTTP send still always raises. It retains fictitious credentials,
synthetic-only reports and no execution authority. This launcher does not select
a real backend, enable a service, issue approvals or load environment credentials.

## Initial installation contract — not automated by B1

For a later, separately authorized root installation:

1. Authenticate the reviewed source/manifests and obtain the complete approved
   target artifacts. Stage only verified regular files in a new release.
2. Verify the Ubuntu base, account, protected ancestors, exact ownership/modes,
   target interpreter, wheel hashes/tags, installed metadata and file inventory.
   Abort on any unexpected owner, permission, ACL, link or extra file.
3. Provision the private state directory explicitly. **Do not create or adopt an
   approval database as a side effect of installation.** Approval initialization
   and operator-controlled receipt issuance require a separate reviewed step.
4. An administrator-issued state-binding.json has exactly format: 1,
   purpose: "offline-readonly-worker-v1", and positive integer device/inode
   fields for that explicitly initialized database. No receipt means no launch.
   Existing unbound databases are refused, not silently enrolled.
5. A root-controlled regular active.json has exactly format: 1 and the reviewed
   release name. Select only a fully validated release; keep services disabled.
6. Run read-only installed preflight. Do not execute the worker until that check
   and the next deployment review have passed.

The checker reads only the first 100 SQLite header bytes, not rows or credentials.
It checks the offline application ID, schema version, rollback-journal format and
bound device/inode. It never connects to SQLite, changes data, performs recovery
or claims that header checks prove a valid schema. The existing worker remains
responsible for its own database validation. Missing state fails closed.
Unresolved journal/WAL/SHM sidecars fail deployment preflight; never delete them
as a deployment repair. A separately reviewed recovery procedure is required.

## Upgrades and rollback

Stop all workers and keep them stopped for maintenance. B1 does not install a
service or prove quiescence, so no concurrent-maintenance guarantee is claimed.
Use new immutable release directories; never update the active tree in place.
Validate both current and candidate release, account, ancestors, dependencies
and the same root-issued state binding before selection. switch_plan is a
pure plan that rejects a changed state identity; it is not an executor.

A future administrator selection change must recheck preconditions immediately
before writing, use a new root-owned regular file in the same protected directory,
atomically replace only active.json, and fsync the file and parent directory.
Do not use a current symlink. B1 supplies no privileged copy/selection executor;
the runbook and validators alone are not a transactional installer.

Rollback changes only the code selection, after the previous release passes
current preflight. Never restore, copy, reset, migrate, recreate or replace the
approval database or its binding as part of code rollback. If either release is
incompatible with preserved state, remain stopped for explicit review.
Partial staging leaves the active release untouched. Missing/corrupt state or
interrupted maintenance blocks launch rather than generating fresh state.

## Trust boundaries and remaining gates

The worker is trusted and can modify its own SQLite storage. An attacker running
as the worker UID can edit bytes in place; root can restore old storage/receipts.
Device/inode/header checks do not prevent either attack or privileged snapshot
rollback. No external anti-rollback anchor is implemented.

Read-only preflight checks do not eliminate filesystem TOCTOU races. Protected
ancestors, trusted administrators, stopped workers and root-controlled installation
are assumptions. They do not protect against arbitrary worker/root code execution.
ACLs are rejected rather than attempting incomplete effective-permission analysis.

Remaining B1 readiness gates include target artifact approval, actual Ubuntu 24.04
installation/permissions/import validation, target dependency closure checks,
explicit state provisioning, and review of a privileged installation executor if
automation is desired. No deployable binary runtime is claimed by source-check.

B2 approval issuance/IPC, B3 systemd hard deadlines and network confinement,
B4 real HTTP/credentials, B5 deployed adversarial validation and B6 authenticated
GET authorization remain outside this package. The worker's existing 30-second
signal is only a soft timeout; no external deadline or OS confinement is installed.

## Tests

~~~sh
.venv/bin/python -B -m unittest tests.test_ubuntu24_package -v
.venv/bin/python -B -m unittest discover -q
.venv/bin/python -B -m pip check
~~~

Tests use synthetic metadata, temporary files, mocked ownership and inherited
network-denial guards. Import-isolation probes run isolated subprocesses; they
are not evidence of a successful privileged installation on Ubuntu 24.04.
