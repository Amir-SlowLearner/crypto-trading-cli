# T1C Step 2D: protected bootstrap, source-only and still blocked

This checkpoint adds a separate installed-entry design for the prepared status
runtime. It does **not** install it or enable credentials, approvals or HTTP.
The unconditional `_require_protected_bootstrap()` exception in
[the runtime](../testnet_status_runtime.py) remains unchanged, including every
credential/send boundary. Passing the new preflight never bypasses that exception.
The original [B1 launcher](../deploy/ubuntu24/launch.py), manifest, permission
policy and synthetic worker are unchanged.

## Fixed invocation and layout

The future independently reviewed installation would use this literal vector:

```text
/usr/bin/python3.12 -I -S -B /opt/phemex-readonly-status/launch.py _worker
```

The supervisor uses that exact vector, a minimal environment, cwd `/`, closed
descriptors, empty supplementary groups and the dedicated non-root identity.
It rejects a configuration naming any different interpreter or entry point.
The root operator roles are exactly `issue` and `run`; `_worker` requires the
`phemex-ro` UID/GID, `/usr/sbin/nologin` and no extra groups. Neither role accepts
an arbitrary Python module, command, URL, release path or import directory.
This replaces the earlier direct application-file/venv invocation described in
[the Step 2A/2C notes](testnet-status-once.md); those older vectors are no longer
accepted by the prepared supervisor.

No command below is an instruction to install or activate this checkpoint.
The proposed installed layout is:

| Installed path | Source or content | Required owner/mode |
|---|---|---|
| `/`, `/opt`, `/etc`, `/etc/phemex-readonly`, `/usr`, `/usr/bin`, `/usr/lib` | Trusted Ubuntu ancestors | root:root directories 0755 |
| `/opt/phemex-readonly-status` and all release subdirectories | Separate, fixed status prefix | root:root 0755 |
| `launch.py` | [status_launch.py](../deploy/ubuntu24/status_launch.py) | root:root regular 0644, one link |
| `preflight.py` | Unmodified [B1 package.py](../deploy/ubuntu24/package.py) | root:root regular 0644, one link |
| `release.json` | [status-release.json](../deploy/ubuntu24/status-release.json) | root:root regular 0644, one link |
| `dependencies.json` | [status-dependencies.json](../deploy/ubuntu24/status-dependencies.json) | root:root regular 0644, one link |
| `requirements.txt` | Existing pinned source requirements | root:root regular 0644, one link |
| `app/` | Exactly the 18 source files in the release inventory | root:root directories 0755, files 0644, one link |
| `vendor/` | Exactly the future reviewed Python 3.12 installed dependency inventory | Same |
| `wheels/` | Exactly the future reviewed wheel artifacts | Same |
| `/usr/bin/python3.12` | Independently reviewed Ubuntu interpreter | root:root regular 0755, one link, pinned SHA-256/version |
| `/usr/lib/python3.12`, `lib-dynload`, optional `/usr/lib/python312.zip` | Trusted versioned Ubuntu standard library | root:root directories 0755; archive 0644 if present |
| `/etc/phemex-readonly/testnet-status-once.json` | Separately approved fixed run configuration | root:root regular 0644, one link |
| `/var/lib/phemex-readonly-status` | Existing proposed actual-read state prefix, separate from offline approvals | phemex-ro private 0700; root-controlled ancestors |
| `approvals.sqlite`, `credentials/testnet.json` under that private prefix | Future separately provisioned state/profile | phemex-ro private regular 0600, one link; credentials directory 0700 |

Paths in the table with relative names are beneath the fixed status prefix.
No directory is a checkout, editable dependency environment or user site. All
checked ancestors reject symlinks, incorrect ownership/modes and POSIX ACLs;
regular files reject additional hard links. The existing run store and credential
loader retain their own storage checks. The bootstrap does not open SQLite or
credentials, create/adopt approval state, or change filesystem permissions.

## Integrity and import sequence

1. Refuse a checkout path, extra arguments, wrong role/identity, interpreter,
   architecture, flags, initial search path or unexpected environment. Reject
   external bytecode-cache prefixes and additional interpreter `-X` options.
2. Read the small root-controlled run config using no-follow file access. Its
   `release_id` pins SHA-256 of the exact installed `release.json` bytes. Its
   worker UID must match the dedicated account. The application subsequently
   validates the entire existing configuration schema and durable run binding.
3. Validate exact release identity, purpose, target, application filenames and
   hash formats. This is a new blocked-status purpose, not the old B1 manifest.
4. Read the preflight helper source once; check its SHA-256 before compiling and
   executing **that same byte buffer**. Do not use an import loader or cached
   bytecode. Check the installed launcher's digest too.
5. Reuse verified B1 dependency validation: requirements/lock hashes, exact
   versions, reviewed target interpreter version/hash, Ubuntu identity,
   compatible wheel names/tags and contents, wheel hashes, installed-file
   hashes and installed distribution versions. The preflight's installer/bundle
   functions are never invoked.
6. Check every app/vendor/wheel file and ancestor and the exact directory
   inventory. Missing/extra files, empty extra directories, `.pth`, bytecode,
   `__pycache__`, startup hooks, dependency mismatches and unsafe paths fail.
   Reject already-cached app/vendor module roots or collisions with each other
   or standard-library module names.
7. Only now append fixed `app/` and `vendor/` to the exact initial standard-library
   roots. Import only `testnet_status_once` and pass the one checked literal role.
   Its existing protected-bootstrap exception still prevents real activation.

`-I` ignores Python environment overrides and removes caller/script directories;
`-S` prevents site initialization, user sites and `.pth` processing; `-B`
prevents cache writes, **not cache reads**. Source-buffer execution and exact
file inventories provide the cache-substitution protection here. Dependencies
must be installed without generating bytecode. App imports may transitively
define existing policy/compiler or legacy configuration functions, but the fixed
entry does not call generic environment credential loaders or trading methods.

The config digest is a trust anchor only because a separately authenticated
administrator installs the config and immutable code into protected paths. A
self-consistent, attacker-modifiable manifest is not authenticity. Root can
replace the config, interpreter, manifest and files together. There is no claim
to resist root, compromised installation tools, worker arbitrary-code execution
or same-privilege debugging/injection. Python objects/provenance labels are not
execution authority.

## Ubuntu 24.04 / Python 3.12 artifact gate

The dedicated dependency file deliberately retains
`awaiting-target-artifact-review`, null interpreter version/hash and empty
wheel/vendor inventories. It records candidate pins from the existing Python
3.10 lock; it is **not** an approved Ubuntu 24.04 closure. Flipping its status
alone still fails validation. No interpreter, wheel or installed dependency
artifact has been downloaded, installed or approved by this checkpoint.

Before installation readiness can be claimed, independently review on Ubuntu
24.04 x86_64:

- Exact `/usr/bin/python3.12` microversion/hash and trusted Ubuntu standard
  library/native libraries. The standard library is an OS trust dependency,
  not a recursively attested reproducible build in this package.
- The entire Python 3.12 dependency closure and environment markers, pinned
  versions, target-compatible wheels and SHA-256 values, including native ABI
  dependencies. Resolve offline using separately obtained reviewed artifacts;
  do not copy the development virtual environment.
- Exact installed vendor inventory, metadata, every file digest, absence of
  hooks/bytecode, and conformity with the strict ownership/mode contract.
  Wheel checks establish artifact consistency, not semantic dependency safety
  or target compatibility by themselves.
- Actual isolated interpreter startup paths on the target. A different Ubuntu
  path layout fails rather than falling back to broader imports.
- Current application inventory, bootstrap and requirements hashes, then a new
  independently approved release digest in the protected config. Do not update
  files while a run is active. Revalidation after any source/dependency change
  must precede a future separately authorized run.

The 18-source inventory includes unchanged transitive dependencies, not newly
granted capabilities. No deployment permission, existing B1 manifest or
dependency lock is changed.

## Authorization, timing and remaining deployment boundaries

The fixed launcher does not mint a capability. The supervisor still claims the
one-use run before launching, attaches its child PID before sending the lease,
and the runtime still reserves and atomically consumes each of four fixed
request slots before dispatch. A changed command fails after claim and blocks
that run; it cannot replenish it. Failed, replayed, uncertain and incomplete
runs retain existing conservative recovery. There are no new retries, routes,
observations, journal transitions or trading permissions.

The existing parent enforces its 30-second child deadline. Independent cgroup
containment and a hard deadline surviving supervisor loss, no-restart policy,
separate identity, private storage provisioning, restricted egress, sanitized
pre-exec environment, TLS/DNS trust and protection of credentials remain
deployment prerequisites. No OS isolation/egress rule is installed here.
Checking `LD_PRELOAD` or other environment variables inside Python is too late
to neutralize native-loader injection. A future trusted root/systemd parent
must construct the clean environment **before** interpreter execution; the
supervisor's child vector already specifies a minimal environment.

Path/hash checks assume no concurrent trusted administrator updates. They are
not an fd-only filesystem sandbox; privileged bind mounts, path replacement,
storage rollback and OS compromise remain outside this preflight guarantee.
Approval storage must never be restored/reset during upgrades or rollback.

Finally, a separately reviewed and explicitly authorized change to the runtime
protected-bootstrap gate is still necessary after target validation and OS
confinement. This checkpoint makes no such change. Do not treat a passing
synthetic test or a matching manifest as permission for credential access or
the first real four-GET collection.

## Offline verification

[Bootstrap tests](../tests/test_testnet_status_bootstrap.py) use synthetic
installed trees, fake root ownership, mocked target facts and explicit network
denial. They exercise integrity/import/cache/permission/dependency rejection and
fixed dispatch. Cached-bytecode attacks never execute, while the verified
preflight source does. Their synthetic helper overrides only the target-runtime
assertion; these fixtures cannot be used as a deployment inventory or approval.
Isolated subprocess tests prove local Python startup isolation, not Ubuntu
3.12 runtime compatibility. Existing supervisor integration tests continue to
mock only the independent bootstrap block and final HTTP boundary for modeled
flows; no real credentials or exchange observations are obtained.
