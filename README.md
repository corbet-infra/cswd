<!-- SPDX-License-Identifier: LGPL-3.0-or-later -->
# corbet.cswd - SeaweedFS on rented Pulsed Media seedboxes

An Ansible collection that runs [SeaweedFS](https://github.com/seaweedfs/seaweedfs) on rented seedbox accounts (Pulsed Media
"Storage Box" class: shared Debian VMs, **no root**, one unprivileged user). Nothing runs as root, nothing needs `become`,
nothing is installed outside the account's home directory. The box basics (inventory, fact sheets, debloat, hardening, TLS
leaf) come from the sibling collection [cpmd](https://git.corbet.ch/corbet-infra/cpmd), which this one depends on.

| Role | What it does |
|---|---|
| `seaweedfs_volume` | Pinned, checksummed `weed` release; `weed volume` as a `systemd --user` unit with mutual TLS; optionally a master (Raft), a filer with local leveldb2 store and an S3 gateway on the same box; Go memory limits and OOM order for the 1250 MiB account cap |

The role also ships two small stdlib-Python helpers, each optional and off by default:

* `swfs-filerwatch` - a filer that is cut off from most of its peers stops itself and its S3 gateway (and starts them again
  only after a majority was reachable for a while), because the filer lock ring has no filer-to-filer health check. Mode
  `observe` only logs what it would do, `enforce` stops and starts the units. The upstream problem is described in
  `docs-draft-seaweedfs-issue.md`.
* `swfs-health` - a health JSON per box over HTTPS with a required client certificate (RAM of the account, EC volumes with too
  few shards, filers in the ring, a write-via-one-filer / read-via-another probe), for a monitoring system to poll.

## Why SeaweedFS here

Volumes are packed files: a box holds a few hundred files however many objects it stores. That matters because the provider
counts every file against a file quota (soft limit 500 files per GiB of disk), which is the limit that bites first with small
objects. Replication `010` keeps two copies on different racks (one rack per box).

## Suitable for

Many objects on cheap, slow, shared disks, where a reader can wait: backups, archives, media originals behind a cache. Not
suitable for databases or latency-sensitive reads. The free SeaweedFS erasure coding is a fixed 10+4 and needs 14 volume
servers; below that use replication.

## How to use

1. Set up your inventory and box basics with cpmd (`corbet.cpmd.bootstrap`), and make a CA whose key lives in sops.
2. Install: `ansible-galaxy collection install git+https://git.corbet.ch/corbet-infra/cswd.git` (pulls cpmd).
3. Set the variables of `roles/seaweedfs_volume/defaults/main.yml` that have no default: `seaweed_master` (`host:httpPort`
   of the masters, comma separated), `seaweed_http_port` and `seaweed_grpc_port` (ports the provider assigned to the
   account; check the fact sheet), `seaweed_ca_cert_file` and `seaweed_ca_key_sops_file`. Roles of a box (master, filer, S3
   gateway) are switched on per host with `seaweed_master_enable` and `seaweed_filer_enable` plus their ports.
4. **Try it on one account first**, then the rest:

```sh
ansible-playbook corbet.cswd.seaweedfs_volume -e target=box01 --check --diff
ansible-playbook corbet.cswd.seaweedfs_volume -e target=box01                              # install and configure, start nothing
ansible-playbook corbet.cswd.seaweedfs_volume -e target=box01 -e seaweedfs_volume_start=true
```

## Limits and known issues

* Debian 12 userland, `systemd --user` and x86_64 are assumed; the account must have lingering sessions.
* cgroup v1 on the boxes: `MemoryMax` in a user unit is not enforced, so the role sets `GOMEMLIMIT` and `OOMScoreAdjust`
  (kill order: S3 gateway, filer, volume, master last).
* Filer metadata is aggregated between filers by SeaweedFS itself; the S3 gateway takes the distributed object-write lock for
  every write (see the issue draft).
* The `swfs-filerwatch` event log is written under `~/.local/state/crfs`, the name of the first collection this role shipped in,
  so that existing boxes keep their log.
* Changing the namespace of the three collections: `tools/set-namespace <new>`.

## Tests

* `tests/unit/test_swfs_health.py`, `tests/unit/test_swfs_filerwatch.py` - the parsers and the fence state machine (`python3 -m pytest tests/unit`).
* `tools/leak-scan.sh [patterns]` - run before every push of this public repository.

## License

LGPL-3.0-or-later (`LICENSE`, plus the GPL-3.0 it refers to in `COPYING`).
