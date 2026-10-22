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
| `harden` | Account modes, ONE restricted SSH key (`from=` + no forwarding), `umask 077`, "other" bits removed from our trees, listener audit, security fact sheet |
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
* Usable space = 8/10 of the raw: with ten accounts of 910 GiB that is about 7.1 TiB (keep headroom: the quota is soft).
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
| Unit | `Type=notify`, `Restart=always`, `RestartSec=10`, `TimeoutStopSec=30` | RustFS can abort under cancellation bursts; it must come back by itself. |

Every one of them is a role variable (`rustfs_*`), plus `rustfs_server_env_extra` for anything else. After the first
bring-up **verify 8+2**: `check_expect_drives=<n> check_expect_parity=2` in the `check` playbook (reads
`/rustfs/admin/v3/info`), or `rc admin info cluster`.

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
quota answers 503 "authoritative bucket usage is not available yet" for about a minute: set it up before clients connect.
Watch the per-account quota with `quotawatch` (`~/cold/status/quota.json`, also pushed into the cluster).

**Replacing a dead account.** A new account has an empty directory, and RustFS 1.0.3 does not heal it by itself:
automatic replacement is only admitted for a drive that is its own mount point on a different device, which a
directory never is (the escape hatch is `RUSTFS_UNSAFE_BYPASS_DISK_CHECK=true`; leave it off until you have tested it).
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

## Limits and known issues

* RustFS is young software (1.0.0 is from September 2026) and this deployment is far outside its documented envelope
  (1.25 GiB RAM, ext4, directory as drive, shared HDD). Everything here was tested at small scale; re-test before bumping the pin.
* A provider quota error is not understood by RustFS (see above); the bucket quota is the guard, not a replacement for watching `quotawatch`.
* The agent and the playbooks assume Debian 12 userland (python3.11, systemd --user, `ss`, `screen`) and x86_64.
* Connections use the SSH password only for the bootstrap; `harden` installs one key restricted by `from=`. If your
  egress address changes the key stops working until you bootstrap again by password and update the list.
* `debloat` cannot switch off what the provider restarts as root (Docker is the example); it stops the processes and tells you.
* Metrics are OTLP push only (no scrape endpoint); the roles do not set up a collector. Poll the health JSON instead.
* The collection ships no mc / rc client; `scripts/s3check.py` and `scripts/bucket.py` are stdlib SigV4 clients good enough for checks and quotas.
* Scanner and heal have open upstream issues (single-object auto-heal concurrency, scanner recovery after a peer restart): check
  scanner status after each roll.

## Tests

* `tests/fixture/test-own-procs.sh` - the PID-exact process stopper.
* `tests/fixture/test-inventory.sh` - lookup and inventory plugin end to end with a throw-away age key.
* `tools/leak-scan.sh [patterns]` - run before every push of this public repository.

## License

LGPL-3.0-or-later (`LICENSE`, plus the GPL-3.0 it refers to in `COPYING`). The `rustfs` role's variable names follow
[ricsanfre/ansible-role-minio](https://github.com/ricsanfre/ansible-role-minio) (MIT) where a counterpart exists; see
`roles/rustfs/README.md`.
