<!-- SPDX-License-Identifier: LGPL-3.0-or-later -->
# corbet.crfs - a RustFS cluster on rented Pulsed Media seedboxes

An Ansible collection that turns rented seedbox accounts (Pulsed Media "Storage Box" class: shared Debian VMs,
**no root**, one unprivileged user) into the nodes of one distributed, erasure-coded
[RustFS](https://github.com/rustfs/rustfs) S3 cluster. Nothing runs as root, nothing needs `become`, nothing is
installed outside the account's home directory.

| Role | What it does |
|---|---|
| `facts` | Installs a tiny stdlib-Python agent and writes a JSON fact sheet per account (limits, cgroup, quota, ports, memory) |
| `debloat` | Switches off the shop image's media stack, torrent clients and rootless Docker; stops leftovers by exact PID; reports what the provider restarts |
| `harden` | Account modes, ONE restricted SSH key (no forwarding; `from=` only if you give a source list), `umask 077`, "other" bits removed from our trees, listener audit, security fact sheet |
| `rustfs` | Pinned, checksummed RustFS binary, TLS leaf per node from your own CA, `systemd --user` unit, distributed mode over an inventory group, `/minio/health/ready` waits, rolling restarts |
| `quotawatch` | Timer that records disk / file / RAM usage against the account's limits (and pushes it into the cluster) |

Plugins: `corbet.crfs.sops_accounts` (inventory from sops files; passwords are never in inventory output) and
`corbet.crfs.sops` (lookup of ONE sops value).

## What the platform imposes (and how the roles deal with it)

| Constraint | Consequence |
|---|---|
| No root | Everything is `systemd --user`; the account must have lingering sessions (the shop enables it). Ansible connects as the user, no `become` anywhere. |
| **cgroup v1, about 1250 MiB RAM per account, page cache included** | `MemoryMax` / `MemoryHigh` in a user unit are *not enforced* on cgroup v1; the account as a whole is capped and the kernel may kill any process of it. The unit carries `OOMScoreAdjust=300` so the S3 node is the preferred victim. Remove the shop's media stack and Docker first: they eat 200-400 MiB. |
| 2 CPU cores per account | Fine for erasure coding of 4 MiB objects; this is a cold store, not a database. |
| ~910 GiB soft quota, ~455k files | `quotawatch` records both; RustFS stores small objects inline in `xl.meta`, large ones as parts, so watch the file count with many small objects. |
| Assigned ports | Only the ports the shop assigned are reachable from outside. Put yours in `rustfs_server_port`; `facts` tells you which ones you can bind. |
| Shared host, other tenants | TLS everywhere (own CA), console disabled, no second listener, host keys pinned (`StrictHostKeyChecking=yes`). |
| The shop restarts what you stop | A root cron of the provider restarts rootless Docker for every account with `dockerEnabled` in the account config, and rtorrent unless `~/.rtorrentDisable` exists. The role uses the marker where one exists and *reports* provider-owned starters; switching Docker off for good is a panel / support request. |

### Why `/minio/health/ready`

RustFS answers `GET /health` with 200 as soon as the process listens, **before** the object layer is ready (PUTs then
return 503). `/minio/health/ready` flips only when the node can serve. All waits and any load balancer in front must use it.

### Sizing 8+2 on ten accounts

* Erasure set: 10 drives, `RUSTFS_STORAGE_CLASS_STANDARD=EC:2` -> 8 data + 2 parity shards. Any 2 accounts may be down;
  reads and writes keep working (verified in a 4-node EC:2 test: 1 and 2 nodes stopped, objects still read, one write
  accepted with 1 node down).
* Disk space alone would give 8/10 of the raw: ten accounts of 910 GiB = about 7.1 TiB. **In practice the file quota
  is the limit, not the disk space** (next paragraph).
* **File-limit arithmetic.** The provider's soft file limit is 500 files per GiB of disk quota; the hard limit is
  25 % higher, but above the soft limit writes only work for a grace period, so size against the soft limit
  ([knowledge base article](https://pulsedmedia.com/clients/index.php/knowledgebase/113/Is-there-a-fixed-number-of-inodes-per-TB-How-file-limits-are-calculated.html);
  every file, directory and symlink counts). With 8+2 every
  account holds a piece of EVERY object, and RustFS uses about 3.7 files and directories per object per drive (measured
  2026-10-25: 446 objects = 1,632 entries; small objects stored inline in `xl.meta` use fewer). About 76k files are already
  taken by the account's own platform files (measured 2026-10-25 on one 1 TB account). So:

  `max objects = (disk_quota_GiB x 500 - platform_files) / 3.7`

  `quota -s` over SSH shows your own quota and usage. With 1 TB accounts (a soft limit of about 455k files) that is roughly
  100k objects, i.e. about 1.5 TiB at 16 MiB objects and less with smaller objects. Adding accounts to the same erasure
  set does not raise the limit (each account holds every object); more erasure sets or larger accounts do.
* RAM per node: roughly 100-150 MiB RSS idle; in a simulated 8+2 cluster under load about 450 MiB RSS and about 640 MiB
  peak for the whole account cgroup, which fits the 1250 MiB cap only after the debloat step.
* Each object write fans out to all ten accounts: the cluster's egress and ingress are Internet traffic of the
  accounts. Check your plan: Pulsed Media plans advertise a 10G port but some state a *burst-egress allowance of about
  500 GiB per month*; the S3 endpoint must never be on a read path that can exceed it. Light checks only (the bundled
  functional check moves about 800 MB).

## Quick start

1. Create sops files (see `examples/accounts.sample.yml`): the account list (host, ssh_user, ssh_password), the
   cluster secrets (`rustfs_access_key`, `rustfs_secret_key`, `ca_key`) and later the admin SSH key (`admin_ssh_key`).
   Make a CA (`openssl req -x509 ...`, EC P-256, keep the key only in sops) and put the *certificate* where Ansible can read it.
2. Copy `examples/` next to your playbooks, edit the paths and addresses (documentation addresses are used there),
   `ansible-galaxy collection install git+https://git.corbet.ch/corbet-infra/crfs.git`.
3. Pin host keys: `tools/scan-hostkeys known_hosts` (first contact is trust-on-first-use; verify from a second vantage point).
4. **Try everything on one account first**, then one production account, then the rest:

```sh
ansible-playbook corbet.crfs.facts     -e target=playground
ansible-playbook corbet.crfs.bootstrap -e target=playground --check --diff
ansible-playbook corbet.crfs.bootstrap -e target=playground
CRFS_AUTH=key ansible -m ping playground          # after the admin key is installed by `harden`
ansible-playbook corbet.crfs.cluster --check --diff   # dry run of the distributed bring-up
ansible-playbook corbet.crfs.cluster                  # install, start all nodes together, wait for ready
ansible-playbook corbet.crfs.check -e check_group=cluster -e check_expect_drives=10 -e check_expect_parity=2
```

`cluster` installs and configures everything first and starts nothing; nodes that are not running are started
together (a distributed set needs quorum), nodes that were running and whose configuration changed are restarted one
at a time with a readiness wait in between.

## What the `rustfs` role sets on every node (and why)

| Setting | Value | Reason |
|---|---|---|
| Release | 1.0.3 on **all** nodes, checksum verified | 1.0.1 has a runaway mmap read loop on spinning disks; 1.0.2 and later fix eight security advisories. Never mix versions except during a roll. |
| `RUSTFS_STORAGE_CLASS_STANDARD` | `EC:2` | With ten drives RustFS picks EC:4 (6+4) by itself; 8+2 exists only when this is set. Parity is stored per object and changing it later rewrites nothing. |
| `RUSTFS_ERASURE_SET_DRIVE_COUNT` | number of nodes | Pins one ten-wide set; cannot be changed for an initialised pool. Endpoint list, order and set width must never change afterwards. |
| `RUSTFS_OVERRIDE_CPU_CORES`, `RUSTFS_OVERRIDE_MEMORY_BYTES` | `2`, `1310720000` | RustFS reads cgroup limits at the root cgroup path, which under `systemd --user` shows the host. |
| `RUSTFS_TLS_PATH` + `RUSTFS_TLS_RELOAD_ENABLE` | node leaf, CA as `ca.crt` | TLS on the S3 listener, which also carries internode RPC. |
| `RUSTFS_CONSOLE_ENABLE` | false | No second listener. |
| `RUSTFS_DRIVE_TIMEOUT_PROFILE` | `high_latency` | A busy shared HDD can exceed the 5 s drive-liveness budget. |
| `RUSTFS_SCANNER_SPEED`, object data cache | `slow`, off | Scanner shares two cores and a shared disk; the body cache costs RAM and cannot help 4 MiB blocks. |
| `RUSTFS_OBS_LOG_MAX_TOTAL_SIZE_BYTES`, `RUSTFS_CHECK_UPDATE` | 256 MiB, false | Logs count against the disk quota; no phone-home. |
| `rustfs_binary_in_ram` (off by default) | binary runs from `/run/user/<uid>/crfs` (private tmpfs) | See "A node that stalls on a saturated host" below. Costs about 263 MiB of the account's RAM cap. |
| Unit | `Type=notify`, `Restart=always`, `RestartSec=10`, `TimeoutStopSec=30` | RustFS can abort under cancellation bursts; it must come back by itself. |

Every one of them is a role variable (`rustfs_*`), plus `rustfs_server_env_extra` for anything else. After the first
bring-up **verify 8+2**: `check_expect_drives=<n> check_expect_parity=2` in the `check` playbook (reads
`/rustfs/admin/v3/info`), or `rc admin info cluster`.

## Suitable for

Large objects that are written once and read rarely, where a reader can wait: backups, archives, media originals behind a
cache. Not suitable for many small objects (file quota), databases, or interactive reads of uncached data (see Disk I/O priority).

## Disk I/O priority

Storage accounts sit in the low disk-priority band (the plan shows e.g. "Archive (5)"). The host's BFQ scheduler shares disk
time by a per-account weight (`blkio.bfq.weight` of the user slice under `/sys/fs/cgroup/blkio/user.slice/`). Priority only
matters while the host's disks are busy; otherwise the account gets full speed. Measured on a ten-box cluster (2026-10-24/25):

* Through a client, 70-90 % of PUTs and 71-98 % of GETs succeeded on the first try; a 16 MiB PUT took 8-127 s.
* A write stalls in the `rename_data` commit stage while it waits for write quorum (8 of 10) when two or three hosts are busy.
* I/O pressure reached 97 % on a busy host; CPU pressure was 7-15 %, so CPU is not the bottleneck.
* The provider sells a higher disk-priority extra (see Upgrade Options in the shop).

Clients need long timeouts and retries; example for JuiceFS: `--io-retries 20 --get-timeout 300` with the writeback cache. The bundled
functional check uses a 20 s timeout, so it can fail on a busy cluster while the data is fine.

## Operating it

**Health and load balancing.** Use `GET /minio/health/ready` (200 only when the node can serve reads). A load
balancer should probe fast and retry: `inter 2s fall 2 rise 3`, `retries 3`, `option redispatch`,
`on-marked-down shutdown-sessions`, TLS verified against your CA, upstream idle keep-alive below 75 s (RustFS closes
idle HTTP/1.1 connections after 75 s). The docs' example (`inter 10s fall 3`) leaves a 30 s window in which a dead
node still receives traffic. With 8+2 and three nodes down all survivors answer 503 (reads are impossible then anyway).
Never expose `/rustfs/admin/`, `/minio/admin/`, `/rustfs/rpc/` or `/rustfs/peer/` on the public side of a proxy.

**Rolling restart.** RustFS 1.0.3 has no server-side gate for it. The `cluster` playbook restarts one node, waits for its
`/minio/health/ready` and then waits until no node of the cluster lists unavailable drives
(`details.storage.unavailableDrives`) before it touches the next one.

**Capacity and the provider quota.** RustFS sees the free space of the whole shared filesystem, not your account quota,
and a provider quota error (EDQUOT) is not mapped to "disk full" (it may look like a faulty drive). So set a **bucket
quota** (hard, byte based) at about 80 percent of the data capacity: 8+2 of ten 910 GiB accounts = 7280 GiB of data
capacity, so about 5.8 TiB (`playbooks/bucket.yml`, `scripts/bucket.py`). Bucket quota rejects PUTs with
`InvalidRequest: Bucket quota exceeded` (tested: 8 MiB quota accepts 2 x 3 MiB, refuses the third). A fresh bucket with a
quota answers 503 "authoritative bucket usage is not available yet" until the scanner's leader has finished a full cycle: about a
minute on a quiet single node, but much longer on ten saturated shared disks (see below; `GET /rustfs/admin/v3/scanner/status`
on the node whose `cycle_schedule.execution_role` is `leader`). Create quota'd buckets well before clients connect. **On our cluster the usage never became available**: five hours after the bucket was
created every node still answered 503, although the scanner leader completed cycles (16 recorded, the leader role moves between nodes) and
the cluster ran with one drive out all that time (the usage may only count as authoritative from a complete scan). A quota'd bucket that
is not writable is worse than none, so `iam.py --quota-bytes 0` / `quota_bytes: 0` clears the quota (`DELETE /rustfs/admin/v3/quota/<bucket>`);
the bucket quota as the provider-quota guard is NOT in force until a quota'd test bucket becomes writable on a 10-of-10 cluster.
Watch the per-account quota with `quotawatch` (`~/cold/status/quota.json`, also pushed into the cluster).

**A node that stalls on a saturated host (`rustfs_binary_in_ram`).** The shop hosts are shared with other tenants whose
reads saturate the disk queue and the page cache. The RustFS binary is a 263 MiB static executable that is *mapped*, not
read; every page fault pulls a read-around window from the disk (the hosts use `read_ahead_kb=4096`, about 1.3 MB per
fault on average). On two of our ten hosts the kernel kept evicting the text pages, so the two async workers sat in D
state on `blk_mq_get_tag` (`/proc/<pid>/task/*/wchan`) re-reading the binary: 44 GB read in 14 hours by a node whose data
directory held 1.2 MB, only ~50 `read()` syscalls (`rchar`), 33 000 major faults; the other eight hosts read about nothing.
Symptoms: TLS handshakes of 3 to 30 s (healthy: 0.1 s), `RemoteClientUnavailable: internode channel acquisition deadline
elapsed` and `storage format quorum not reached` in the node's own log, peers marking the drive suspect/faulty, and a
restart loop while the unit waits for quorum. It is **not** a configuration problem: environment, certificate (SANs
checked with `openssl s_client -verify_ip`), endpoint list and port were identical to the healthy nodes, and endpoints
given as IP addresses are "local" without any name resolution. How to recognise it: per node
`awk '/read_bytes/{print $2}' /proc/<pid>/io` against `awk '{print $12}' /proc/<pid>/stat` (major faults) and
`/proc/pressure/io`. The fix is to take the disk out of the code path: `rustfs_binary_in_ram: true` makes the unit copy the
binary to the account's private tmpfs (`ExecStartPre=install -D -m 0700`, mode 0700 directory, never `/dev/shm`, which
other tenants can pre-create) and run it from there. Measured on the playground box under the same kind of load: cold
start from disk 10 to 12 s and 183 MB read, from tmpfs 0.6 to 2.4 s and 0 bytes, no major faults, steady state
identical; kill -9 and restart come back from tmpfs. Set it per host (a host variable) only where the symptom shows up.
The copy is made at every start and checked against `rustfs_binary_sha256` (the pinned hash of the unpacked binary of
`rustfs_version`, written to `~/cold/etc/rustfs.sha256`): `ExecStartPre=sha256sum -c` fails the start on a mismatch (tested
by corrupting the pin: the unit stays down). The release binary is already stripped (203 MiB of code, 19 MiB rodata, no
debug sections), so there is nothing to shrink. Memory: the tmpfs copy is charged to the account's memory cgroup
(`memory.stat` `shmem`, 266 MiB). Under 64 parallel 4 MiB PUT+GET the node's RSS peaked at 483 MiB, so node plus copy is
about 750 MiB of the 1250 MiB cap before the other things of the account (shop panel, provider Docker); the page cache is
reclaimable. `RUSTFS_RUNTIME_WORKER_THREADS` above the CPU override did not change anything in a steady-state test and the
stall cannot be reproduced on a healthy host, so it is not set.

**A node whose own drive keeps dropping out.** Symptom: every node lists the same drive in
`details.storage.unavailableDrives` (`runtimeState: offline`, `hostOnline: true`), and the node's own log says
`Disk health check timed out` followed by `Disk health check rejected operation because disk is marked faulty`. RustFS probes
every drive (write, read and delete of a test object, every 15 s, timeout 5 s) and marks it suspect, then offline, when
probes time out; the `high_latency` profile does not raise that probe timeout. On a disk that other tenants keep 100 % busy
(`/proc/diskstats`: busy 100 %, 170-280 ms per read and 280-700 ms per write, against 14 ms and 44 % on a healthy host) a
probe exceeds 5 s now and then and the drive leaves the set for minutes. `rustfs_drive_active_check_timeout_secs: 30` (off by default) sets
`RUSTFS_DRIVE_ACTIVE_CHECK_TIMEOUT_SECS`. It helps against stalls of a few seconds only: on a host whose disk stalled for
more than 30 s (log: `Disk health check timed out ... timeout_secs: 30`, `operation still pending 17955 ms`) a 30 s probe timeout
did not keep the drive online. A raised timeout also keeps a very slow drive in the set, where it can slow writes down. A restart brings the drive back until the next
timeout. If a host stays this slow after that (disk 100 % busy, tens of seconds per probe), the account is the problem, not the settings: ask the provider for another
host (a dead drive costs one of the two parity shards).

**Running on busy shared disks (`rustfs_tolerance_tolerant`, `rustfs_durability_mode`).** On shared seedbox hosts the disk can be kept 100 % busy by
other tenants (reads of 50-260 ms, writes of 150-700 ms, stalls of 20-30 s) and some accounts flap in and out. It was reproduced in a VM lab
(ten RustFS 1.0.3 processes, 8+2, TLS, `dm-delay` latency, `dm-suspend` stalls, SIGKILL flaps, cold reads, a JuiceFS-like client with
4 MiB blocks, 30 s response-header timeout, 11 tries per PUT). What it showed, with the
settings of this role written today ("defaults") against the tolerance bundle (3 repetitions, identical load):

| | defaults | `rustfs_tolerance_tolerant` + `rustfs_durability_mode: relaxed` |
|---|---|---|
| PUT attempts that failed (503, reset, timeout) | 53 % | 36 % |
| single-try PUT with a 60 s timeout succeeds | 58 % | 89 % |
| drive-faulty rejections / operation timeouts per 4 min | 29 / 14 | 0 / 0 |
| PUT operations that exhaust 11 tries (EIO) | 3 of 60 | 3 of 80 |
| one 4 MiB PUT, one client thread (strace) | 17-24 s | 6.5 s |

* **Why a PUT is slow.** In RustFS's default `strict` durability one 4 MiB PUT is about eight dependent `fsync`s per drive (shard
  `fdatasync`, `xl.meta`, rollback copy, every parent directory), so 10-17 s on a drive that needs 1.6 s per sync; the PUT is acknowledged when 8 of 10 drives
  are done and seven drives are slow. `rustfs_durability_mode: relaxed` keeps the shard `fdatasync` and leaves `xl.meta` and the directory
  entries to the page cache: a power loss of ONE node can lose its newest versions (the other nodes hold them; the unclean-shutdown heal repairs it); a killed process loses
  nothing. Upstream wants independent power domains for it. It is a decision, hence a variable of its own and not part of the bundle.
* **What the bundle sets** (`rustfs_tolerance_env`; every key overridable per host through `rustfs_tolerance_env_override`): a drive goes offline only after
  1000 consecutive timeouts (default 2), probe and operation timeouts of 120-300 s instead of 5-30 s, RPC deadline 300 s, connect timeout 10 s, HTTP/2 keep-alive timeouts 60 s, lock
  timeouts 20 s / 10 s, readiness not withdrawn after 30 s without a completed object stage. It makes RustFS wait for a slow drive instead of
  dropping it, which with 8+2 matters because two more slow or flapping nodes already take the set below write quorum.
* **What it does not fix.** With ten accounts and 8+2, three impaired nodes at the same moment (flapping or stalled) leave neither reads nor writes with quorum;
  in the lab plan that was 12-20 % of the time. A client that gives up after 30 s per request (JuiceFS: hard-coded response-header timeout) and after
  11 tries / 5 minutes still sees about 4 % of its PUT operations fail. Two ways out, both outside RustFS settings: JuiceFS `--writeback --max-uploads 2` (the application never sees a
  PUT error; blocks wait minutes on the client disk) and more parity (`rustfs_storage_class_standard: "EC:4"`, 6+4, 60 % usable; applies to new objects only).
* **Never enable** `RUSTFS_GET_LOCKSTEP_DATA_SHARDS_ONLY_ENABLE` (1.0.3: permanent HTTP 500 on objects that have exactly 8 live shards), nor
  `RUSTFS_OBJECT_DISK_WRITE_STALL_TIMEOUT=0` (waits for all ten drives forever) nor `RUSTFS_DRIVE_ACTIVE_MONITORING=false` (nothing re-admits a drive).
* **Caveats.** Hung or black-holed peers are tried on every operation with the bundle (10 s connect timeout each); a killed process is refused instantly. The lab has no queueing
  at the disk and uses one noisy host; read the table as direction and size, not as a promise.

**Endpoints by DNS name: what happens when DNS misbehaves** (`rustfs_cluster_domain`; RustFS 1.0.3, measured with four nodes in
private network namespaces on the playground box, a controllable DNS server, `Restart=always RestartSec=10` emulated; the
resolver of the static musl binary has no cache and honours `/etc/resolv.conf` only):

| Situation | Behaviour |
|---|---|
| Name of a peer or of the node itself gives SERVFAIL, timeout or an unreachable resolver at **start** | The node exits within 2-4 s with `FATAL ... local-host detection failed for host '<name>': failed to lookup address information: Try again`; the supervisor restarts it every ~12 s (9 restarts in 100 s), it needs no hands: **full cluster 10 s after DNS returned**. |
| A peer name gives NXDOMAIN at start (record missing) | The node stays alive, listens on nothing, retries; **ready 12-22 s after the record appeared**. No crash loop. |
| Resolver unreachable (refused) or black-holed **while the cluster runs** | Running nodes keep serving, but they cannot re-resolve when a pooled connection breaks: peers mark the affected drives faulty one by one (in the black-hole run two of four drives were gone from two nodes' view within 10-30 s). A long DNS outage therefore costs quorum even though every node is up. Recovery ~10 s after DNS returns. |
| Half of the queries lost | A restarting node needs one extra restart cycle (~12 s); nothing else. The boxes have two resolvers in `resolv.conf`, which is much better than the single resolver of the test. |
| Name repointed to a new, empty account (TTL 30 s) | Peers reach the new address within seconds, no restart of the others (the heal itself could not be judged in the test: its plain directories shared one filesystem, which the replacement admission refuses). |

So the names add one dependency: the boxes must reach their resolvers. The unit's restart policy absorbs short outages; plan
rolling restarts when DNS is healthy (a roll during a resolver outage just loops until it returns).

**Application users (`playbooks/iam.yml`, `scripts/iam.py`).** One RustFS IAM user per bucket, never the cluster admin
key for an application. For every entry of `iam_users` (a variable of your cluster group; secrets stay in sops) the
playbook creates the bucket with a hard quota, adds the user, adds the canned policy `bucket-<bucket>` (`s3:*` on
`arn:aws:s3:::<bucket>` and `<bucket>/*`, nothing else) and attaches it; it then proves the pattern as that user: PUT, GET
and DELETE in its own bucket work, a PUT into another bucket is refused with 403, the bucket listing shows only its own.
Plain JSON admin API (`add-user`, `add-canned-policy`, `idp/builtin/policy/attach`), idempotent.
The default quota of every bucket is `iam_default_quota_bytes` (80 percent of the 8+2 data capacity, see above); the quota
is per bucket, so the sum over all buckets must stay below it.

**Replacing a dead account.** A new account has an empty directory, and RustFS 1.0.3 does not heal it by itself:
automatic replacement is only admitted for a drive that is its own mount point on a different device, which a
directory never is (the escape hatch is `RUSTFS_UNSAFE_BYPASS_DISK_CHECK=true`; leave it off until you have tested it).
Conflicting lab results: one lab run found no self-heal, a later 10-process lab on 1.0.3 healed an empty replacement drive
by itself within 90 s (with and without `RUSTFS_UNSAFE_BYPASS_DISK_CHECK`). `playbooks/replace.yml` therefore waits for the
self-heal first and starts the erasure-set heal from a survivor only if the slot is still out (prepared, **not yet run
against a real replacement**; `scripts/replace.py` preflight and status were run read-only against a live cluster).
**Endpoints by IP address:** a replacement account has another address. If `RUSTFS_VOLUMES` names the nodes by IP, the
list changes and all other nodes need the new list and a rolling restart (the persisted layout checks counts and set width,
and the slot is tied to the position in the list and the drive's UUID, but this was not tested). Name the nodes by host
names from the start and repoint the name to the new account; each name must resolve on its own node to an address of a local interface.
The procedure that matches the source and docs (check each step on a lab cluster first):

1. Make sure the cluster is otherwise healthy (all other nodes ready; EC:2 survives two losses, not three).
2. Record pool, set and slot of the lost drive (`rc admin info storage`).
3. Provision the new account with the same roles; same binary version, same env file, same TLS bundle, same host name
   (point the old name or address at the new account), **empty** data directory. Never copy files from the old one.
4. Start the unit; its slot stays offline (`UnformattedDisk`) until a heal task writes the format.
5. From a **surviving** node start the erasure-set heal through the admin API (`POST /rustfs/admin/v3/heal/` with
   `pool` and `set`, SigV4); a heal started on the replaced node fails with "No available disk found for set".
6. Watch `GET /rustfs/admin/v4/heal/replacement-recovery` and heal status. Healing is one object at a time; a 910 GiB
   account takes hours to a day. Verify physically that shards exist on the new drive; a readable object proves nothing.
7. Only then take the next node through maintenance.

### Wedge watchdog (`rustfs_hostwatch`)

On a saturated shared host a node can wedge: the TCP port accepts, but the process never completes a TLS handshake. Peers and the balancer then hang on it until their timeouts instead of getting an instant refusal, which is worse than a dead node. `crfs-hostwatch` is an optional user unit (python3 standard library only) beside the RustFS unit:

- it probes the node's own TLS endpoint; when the probe fails for `wedge_seconds` (10) in a row it kills the process and keeps it down (fence file);
- the RustFS unit has an `ExecStartPre` gate, so `Restart=always` cannot restart it into the saturated disk; the node is re-admitted after `healthy_probes` (6) consecutive fast disk probes (a repeat of one PUT's sync chain, `healthy_disk_s` 2.0), with a doubling fence time if it wedges again soon;
- optionally it answers HAProxy's `agent-check` (`rustfs_hostwatch_agent_port`) with `up 100%`, `up 40%`, `drain` or `down`.

Enable with `rustfs_hostwatch: true`; options as a map in `rustfs_hostwatch_options` (names of `crfs-hostwatch.py --help` with underscores). In the lab it is a safety net: with the tolerance bundle, relaxed durability, early acknowledgement off and 8 worker threads it never fired and changed nothing measurable. 
**Event log.** With `--event-log` (the role sets `~/.local/state/crfs/hostwatch-events.log`) every decision and every change of the RustFS unit is appended as one line: an ISO-8601 timestamp with time zone, then the message. Messages are the decisions (`WEDGE: ...`, `re-admit: ...`), failed commands or steps, and `unit: ...` lines carrying systemd's own view of the RustFS unit (InvocationID, ActiveState, Result, ExecMainStatus, NRestarts) plus the status JSON, so the exit reason survives a restart. The file is size-capped: at `--event-log-max` bytes (default 1,000,000) it is rotated to `hostwatch-events.log.1`, one generation kept. Per-cycle status lines are not persisted. Example of why a node was fenced and when it came back:

```
2026-10-25T08:54:09+0200 WEDGE: TLS probe failing for 41 s (disk probe 3.0 s): fencing for at least 30 s
...
2026-10-25T08:55:16+0200 re-admit: disk probes fast (333 ms), starting the node
```

Read it as: the node's TLS endpoint did not answer for 41 s while a disk probe took 3 s, so the watcher fenced the node; about 70 s later it was started again once the disk probes were fast again.

Tests: `tests/fixture/test-hostwatch.py` (state machine) and the env-file render test.

## Limits and known issues

* RustFS is young software (1.0.0 is from September 2026) and this deployment is far outside its documented envelope
  (1.25 GiB RAM, ext4, directory as drive, shared HDD). Everything here was tested at small scale; re-test before bumping the pin.
* A provider quota error is not understood by RustFS (see above); the bucket quota is the guard, not a replacement for watching `quotawatch`.
* The agent and the playbooks assume Debian 12 userland (python3.11, systemd --user, `ss`, `screen`) and x86_64.
* Connections use the SSH password only for the bootstrap; `harden` installs one key with the options of `harden_authorized_options`. `harden_ssh_from` is optional: an empty list means no `from=` (use that when your egress address is not yours to control; a `from=` list that misses the controller locks the key out and leaves the password login as the way back). `playbooks/sshkey.yml` rewrites only the key line, one account at a time, and proves a fresh key login.
* `debloat` cannot switch off what the provider restarts as root (Docker is the example); it stops the processes and tells you.
* Metrics are OTLP push only (no scrape endpoint); the roles do not set up a collector. Poll the health JSON instead.
* The collection ships no mc / rc client; `scripts/s3check.py`, `scripts/bucket.py` and `scripts/iam.py` are stdlib SigV4 clients good enough for checks, quotas and application users.
* On a saturated host every process of the account is slow, not only RustFS (SSH logins and Ansible tasks take minutes); a restart of such a node can take up to ten minutes to become ready.
* Scanner and heal have open upstream issues (single-object auto-heal concurrency, scanner recovery after a peer restart): check
  scanner status after each roll.

## Tests

* `tests/fixture/test-own-procs.sh` - the PID-exact process stopper.
* `tests/fixture/test-envfile.py` - renders the RustFS env file template with plain jinja2 and checks the tolerance bundle, its overrides and the durability switch.
* `tests/fixture/test-inventory.sh` - lookup and inventory plugin end to end with a throw-away age key.
* `tools/leak-scan.sh [patterns]` - run before every push of this public repository.

## License

LGPL-3.0-or-later (`LICENSE`, plus the GPL-3.0 it refers to in `COPYING`). The `rustfs` role's variable names follow
[ricsanfre/ansible-role-minio](https://github.com/ricsanfre/ansible-role-minio) (MIT) where a counterpart exists; see
`roles/rustfs/README.md`.
