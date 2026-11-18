#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""swfs-health: a tiny health endpoint per SeaweedFS box (python3 standard library only), polled by gatus.

Every box serves GET /health.json on one assigned port: the RAM of the account (cgroup v1 working set = usage - inactive file cache,
the account limit is 1250 MiB), the load, the S3 gateway's open file descriptors / RSS / swap (the early warning for the
aborted-PUT queue during a stalled volume server), whether the provider's rootless Docker runs inside the account, the provider's
Docker policy for the account, and the age of the data. With --cluster the box also runs the cluster-wide checks once per
--interval: the number of EC volumes with fewer than 14 shards (weed shell volume.list), the filers registered with the masters
(cluster.ps), and a write-via-filer-A / read-via-filer-B probe over every pair partner (the S3 gateways), giving the largest time a
marker object needed to become visible on a peer, or null when a peer did not answer. Run --cluster on two boxes so that the checks
survive the loss of one. The same loop asks the raft LEADER whether the write path exists at all (`cluster.status`: the data nodes it sees and its
writable volumes): on 2026-11-12 a hung follower pinned every volume server, the leader saw 0 of 7 nodes, and every PUT failed while each box's GET
check stayed green. Whatever fails the write path (marker PUT or DELETE refused, a peer that never shows the marker, a leader with missing nodes or no
writable volume) is a line in `filer_peers_missing` and counts in `filer_peers_not_answering`; a loop that cannot run at all counts as failed too and
no longer leaves the last good values standing. Gatus turns the JSON into alerts; the weed shell needs the box's own
mutual-TLS security.toml, which is why this runs on the boxes and not on the monitoring side.
"""
import argparse
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def parse_ec_short(volume_list_text):
    """Number of EC volumes that have fewer than 14 distinct shards, and the ids."""
    shards = {}
    for line in volume_list_text.splitlines():
        m = re.search(r"ec volume id:(\d+) .*?shards:\[([\d ]*)\]", line)
        if m:
            shards.setdefault(m.group(1), set()).update(m.group(2).split())
    short = sorted(v for v, s in shards.items() if len(s) < 14)
    return len(short), short, len(shards)


def parse_filers(cluster_ps_text):
    """Number of filers registered with the masters (lines '  * ip:http.grpc (version) time' under '* filers N')."""
    m = re.search(r"^\* filers (\d+)", cluster_ps_text, re.M)
    return int(m.group(1)) if m else None


def provider_docker_processes(proc_root="/proc", uid=None):
    """Provider-owned rootless Docker processes inside this account: (count, summed RSS MiB).

    Pulsed Media's root cron starts dockerd-rootless.sh as the account user, outside the user
    systemd manager (no `systemctl --user` unit); the only switch is the root-owned account
    store. The check therefore reports, it does not try to stop anything.
    """
    uid = os.getuid() if uid is None else uid
    count = 0
    rss_kib = 0
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return 0, 0
    for name in entries:
        if not name.isdigit():
            continue
        p = os.path.join(proc_root, name)
        try:
            if os.stat(p).st_uid != uid:
                continue
            with open(os.path.join(p, "comm")) as f:
                comm = f.read().strip()
            with open(os.path.join(p, "cmdline"), "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if comm not in ("dockerd", "containerd", "rootlesskit", "slirp4netns") and "dockerd-rootless" not in cmdline:
                continue
            count += 1
            with open(os.path.join(p, "status")) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kib += int(line.split()[1])
                        break
        except (OSError, ValueError):
            continue
    return count, round(rss_kib / 1024)


def provider_docker_policy(uid=None, config_dir="/etc/seedbox/config/users"):
    """The provider's dockerEnabled policy for this account: True, False, or None when unreadable.

    PMSS keeps the authoritative value in the root-owned per-user store; ~/.config/pmss-user.json
    is only a display copy. The store is world-readable, so the health endpoint can report the
    policy even when the daemon is not running (a stopped daemon is not the same as an off policy:
    the provider's five-minute cron starts it again while the policy says true).
    """
    uid = os.getuid() if uid is None else uid
    try:
        import pwd
        user = pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return None
    try:
        with open(os.path.join(config_dir, user + ".json")) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    value = payload.get("dockerEnabled")
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return None if value is None else bool(value)


def s3_gateway_process(proc_root="/proc", uid=None):
    """The S3 gateway process of this account: (open fds, RSS MiB, swap MiB).

    The gateway is `weed ... s3 -filer=...` under the same uid. Its fd count is
    the early-warning number for the aborted-PUT pile-up: a gateway that queues
    write handlers behind a stalled volume server accumulates one socket per
    aborted request until the stall clears. RSS tracks the same queue, so both
    are reported; nothing here alerts or acts on them.
    """
    uid = os.getuid() if uid is None else uid
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None, None, None
    for name in entries:
        if not name.isdigit():
            continue
        p = os.path.join(proc_root, name)
        try:
            if os.stat(p).st_uid != uid:
                continue
            with open(os.path.join(p, "comm")) as f:
                if f.read().strip() != "weed":
                    continue
            with open(os.path.join(p, "cmdline"), "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if " s3 " not in cmdline or "-filer=" not in cmdline:
                continue
            fds = len(os.listdir(os.path.join(p, "fd")))
            rss_kib = 0
            swap_kib = 0
            with open(os.path.join(p, "status")) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kib = int(line.split()[1])
                    elif line.startswith("VmSwap:"):
                        swap_kib = int(line.split()[1])
            return fds, round(rss_kib / 1024), round(swap_kib / 1024)
        except (OSError, ValueError, IndexError):
            continue
    return None, None, None


def cgroup_working_set_mib(uid):
    base = "/sys/fs/cgroup/memory/user.slice/user-%d.slice" % uid
    try:
        usage = int(open(base + "/memory.usage_in_bytes").read())
        inactive = 0
        for line in open(base + "/memory.stat"):
            if line.startswith("total_inactive_file "):
                inactive = int(line.split()[1])
        limit = int(open(base + "/memory.limit_in_bytes").read())
        return round((usage - inactive) / 1048576), round(usage / 1048576), round(limit / 1048576)
    except OSError:
        return None, None, None


def cgroup_failcnt(uid):
    """Times the account hit its memory limit and had to reclaim (cgroup v1 memory.failcnt, a counter): the in-account pressure signal.
    The limit is the backstop; a rate above zero in normal operation means the configured budgets no longer fit."""
    try:
        return int(open("/sys/fs/cgroup/memory/user.slice/user-%d.slice/memory.failcnt" % uid).read())
    except (OSError, ValueError):
        return None


def host_psi(kind, path="/proc/pressure"):
    """avg60 of the host-wide PSI 'some' line for memory or io (the box is a slice of a shared host: this is the neighbourhood, not the account)."""
    try:
        for line in open("%s/%s" % (path, kind)):
            if line.startswith("some "):
                return float(dict(kv.split("=") for kv in line.split()[1:])["avg60"])
    except (OSError, ValueError, KeyError):
        pass
    return None


def weed_shell(weed, masters, commands, timeout=90):
    p = subprocess.run([weed, "shell", "-master=" + masters], input="\n".join(commands) + "\n", capture_output=True, text=True, timeout=timeout)
    return p.stdout


def sigv4_curl(url, method, access, secret, body=None, timeout=15, cacert=None):
    # the payload hash is given explicitly: curl 7.88 (the boxes) otherwise signs differently than the gateway expects
    digest = hashlib.sha256((body or "").encode()).hexdigest()
    cmd = ["curl", "-s", "-m", str(timeout), "-o", "/dev/null", "-w", "%{http_code}", "--aws-sigv4", "aws:amz:us-east-1:s3", "--user", "%s:%s" % (access, secret),
           "-H", "x-amz-content-sha256: " + digest, "-X", method]
    if cacert:
        cmd += ["--cacert", cacert]
    if body is not None:
        cmd += ["--data-binary", body]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    return r.stdout.strip()


def parse_cluster_status(text):
    """(data nodes, writable volumes) the leader reports in `weed shell cluster.status` ('nodes:    7', '... 729 writable (99.18%) ...'); None when absent."""
    nodes = re.search(r"^\s*nodes:\s+(\d+)", text, re.M)
    writable = re.search(r"([\d,]+) writable", text)
    return (int(nodes.group(1)) if nodes else None, int(writable.group(1).replace(",", "")) if writable else None)


def parse_raft_peers(cluster_raft_ps_text):
    """The number of raft servers the leader lists ('the raft cluster has 7 servers'); None when absent."""
    m = re.search(r"raft cluster has (\d+)", cluster_raft_ps_text)
    return int(m.group(1)) if m else None


def parse_volume_size_limit_mb(text):
    """The master's volumeSizeLimit from the topology line of volume.list / cluster.status ('volumeSizeLimit:8192 MB'); None when absent."""
    m = re.search(r"volumeSizeLimit:(\d+) MB", text)
    return int(m.group(1)) if m else None


def parse_readonly_volumes(volume_list_text, limit_mb):
    """(read-only volumes, of which abnormal): distinct volume ids marked ReadOnly:true in volume.list. A read-only volume that is at least half of the size limit is the normal kind
    (full, waiting for ec.encode); a smaller one was made read-only for another reason (an interrupted ec.encode, a write error) and is abnormal. Without a limit every one counts as abnormal."""
    ro = {}
    for line in volume_list_text.splitlines():
        m = re.search(r"volume Id:(\d+), Size:(\d+),.*ReadOnly:true", line)
        if m:
            ro[m.group(1)] = int(m.group(2))
    half = (limit_mb or 0) * 1048576 / 2.0
    abnormal = [v for v, size in ro.items() if not limit_mb or size < half]
    return len(ro), len(abnormal), sorted(abnormal, key=int)


def own_master_is_leader(masters, own_host, timeout=3):
    """True/False from this box's own master (GET http://127.0.0.1:<port>/cluster/status, field IsLeader); None when it cannot be asked. `masters` is the --masters list
    ('ip:httpPort.grpcPort,...'); our entry is the one with our own IP."""
    import urllib.request
    port = None
    for item in (masters or "").split(","):
        host, _, rest = item.partition(":")
        if host == own_host:
            port = rest.split(".")[0]
            break
    if not port:
        return None
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/cluster/status" % port, timeout=timeout) as r:
            return bool(json.load(r).get("IsLeader"))
    except Exception:
        return None


def s3_request_counters(port, timeout=3):
    """(all requests, 5xx answers) of this box's S3 gateway since it started, from its loopback Prometheus endpoint (SeaweedFS_s3_request_total{type,code,bucket});
    (None, None) when the endpoint is not up."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/metrics" % port, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return None, None
    total = errors = 0
    seen = False
    for line in text.splitlines():
        if not line.startswith("SeaweedFS_s3_request_total{"):
            continue
        m = re.search(r'code="(\d+)"[^}]*\}\s+([0-9.eE+]+)', line)
        if not m:
            continue
        seen = True
        n = int(float(m.group(2)))
        total += n
        if m.group(1).startswith("5"):
            errors += n
    return (total, errors) if seen else (0, 0)


def write_path_problems(nodes, writable, expected_nodes):
    """What the leader's view says is wrong with the write path (empty list = fine)."""
    problems = []
    if nodes is None or writable is None:
        problems.append("leader topology unreadable (cluster.status)")
        return problems
    if nodes < expected_nodes:
        problems.append("leader sees %d of %d data nodes" % (nodes, expected_nodes))
    if writable == 0:
        problems.append("leader has no writable volume")
    return problems


def guarded_curl(url, method, access, secret, t0, limit, **kw):
    """sigv4_curl for the marker write: a 503 is the gateway's deliberate upload guard (2 slots / 32 MB) saying "busy", and no answer (000)
    is a replica write stalled on a starved provider disk. Both are write-path degradation of the dirt tier. Retry every 5 s while the
    `limit` window (counted from t0) lasts; each request is cut off at the window's end (at most 60 s), so the window is a hard bound."""
    while True:
        left = limit - (time.time() - t0)
        code = sigv4_curl(url, method, access, secret, timeout=max(5, min(60, int(left))), **kw)
        if code not in ("503", "000", "") or time.time() - t0 >= limit:
            return code
        time.sleep(5)


def lag_probe(own, peers, bucket, access, secret, cacert, limit=60, poll=30):
    """PUT a marker via `own`, poll HEAD on every peer until it is visible, DELETE it.
    Returns (max seconds, list of what failed, busy reason or None, seconds the marker write took or None).
    A marker write that is refused (503) or unanswered for the whole `limit` window ends as busy, never as green and never as a silent peer.
    Hard bound of one probe: limit + poll + one HEAD round (5 s each) + 15 s for the DELETE, well inside two scrape intervals."""
    key = "health/lag-%d" % int(time.time())
    t0 = time.time()
    code = guarded_curl("%s/%s/%s" % (own, bucket, key), "PUT", access, secret, t0, limit, body=key, cacert=cacert)
    put_s = round(time.time() - t0, 1)
    if code in ("503", "000", ""):
        return None, [], "own gateway write %s for %ds" % ("refused (503 upload guard)" if code == "503" else "unanswered", limit), put_s
    if code != "200":
        return None, ["own gateway did not accept the marker (HTTP %s)" % code], None, put_s
    seen = {}
    t1 = time.time()
    while time.time() - t1 < poll and len(seen) < len(peers):
        for p in peers:
            if p not in seen and sigv4_curl("%s/%s/%s" % (p, bucket, key), "HEAD", access, secret, timeout=5, cacert=cacert) == "200":
                seen[p] = time.time() - t0
        time.sleep(1)
    deleted = sigv4_curl("%s/%s/%s" % (own, bucket, key), "DELETE", access, secret, timeout=15, cacert=cacert)
    missing = [p for p in peers if p not in seen]
    if deleted not in ("200", "204", "503", "000", ""):
        missing.append("own gateway did not delete the marker (HTTP %s)" % deleted)
    return (round(max(seen.values()), 1) if seen else None), missing, None, put_s


class State:
    def __init__(self):
        self.data = {"time": None}
        self.lock = threading.Lock()

    def set(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.data["time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def dump(self):
        with self.lock:
            return json.dumps(self.data)


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/health.json", "/"):
                self.send_error(404)
                return
            body = state.dump().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


def cgroup_memory_mib(uid, base=None):
    """The account's own memory-cgroup numbers (cgroup v1, readable without root), MiB: what the provider charges.
    usage/max_usage/limit/soft_limit/kmem come from the memory.* files, the split from memory.stat (hierarchical total_*).
    anon = total_rss ONLY: in cgroup v1 `rss` already includes the anonymous transparent hugepages that `rss_huge` counts again
    (the first version added them and overstated anon by up to 220 MiB). cache = total_cache (includes mapped files and tmpfs).
    soft_limit is -1 when unlimited (the kernel's 2^63 value)."""
    base = base or "/sys/fs/cgroup/memory/user.slice/user-%d.slice" % uid

    def rd(name):
        try:
            return int(open(base + "/memory." + name).read())
        except (OSError, ValueError):
            return None

    def mib(v):
        return None if v is None else round(v / 1048576)

    out = {}
    soft = rd("soft_limit_in_bytes")
    out["ram_usage_mib"] = mib(rd("usage_in_bytes"))
    out["ram_max_usage_mib"] = mib(rd("max_usage_in_bytes"))
    out["ram_limit_mib"] = mib(rd("limit_in_bytes"))
    out["ram_soft_limit_mib"] = None if soft is None else (-1 if soft >= 1 << 60 else mib(soft))
    out["ram_kmem_mib"] = mib(rd("kmem.usage_in_bytes"))
    names = {"total_rss": "ram_anon_mib", "total_cache": "ram_cache_mib", "total_shmem": "ram_shmem_mib", "total_mapped_file": "ram_mapped_file_mib",
             "total_active_file": "ram_active_file_mib", "total_inactive_file": "ram_inactive_file_mib", "total_rss_huge": "ram_rss_huge_mib", "total_swap": "ram_swap_mib"}
    try:
        for line in open(base + "/memory.stat"):
            k, _, v = line.partition(" ")
            if k in names:
                out[names[k]] = mib(int(v))
    except (OSError, ValueError):
        pass
    return out


S3_METRICS_PORT = 19327  # the S3 gateway's Prometheus endpoint on the loopback (-metricsPort/-metricsIp in its unit)
UNIT_NAMES = ("weed-master", "weed-volume", "weed-filer", "weed-s3", "weed-binary", "otelcol", "node_exporter", "swfs-health", "radar", "panel", "docker")
DOCKER_COMMS = ("dockerd", "containerd", "rootlesskit", "slirp4netns")


def classify_process(comm, cmdline):
    """The unit name a process of this account belongs to, or None. Exact, from comm and the command line."""
    if comm == "weed":
        for tok in cmdline.split()[1:]:
            if not tok.startswith("-"):
                return "weed-" + tok if tok in ("master", "volume", "filer", "s3") else None
        return None
    if comm.startswith("otelcol"):
        return "otelcol"
    if comm == "node_exporter":
        return "node_exporter"
    if comm in ("python3", "python") and "/bin/swfs-" in cmdline:
        return "swfs-health"
    if "radar-collector" in cmdline or comm == "radar-collector":
        return "radar"
    if comm in ("lighttpd", "php-cgi"):
        return "panel"
    if comm in DOCKER_COMMS or "dockerd-rootless" in cmdline:
        return "docker"
    return None


def unit_rows(proc_root="/proc", uid=None, budgets=None, spool_glob=None):
    """[{unit, rss_mib, budget_mib}] for every name in UNIT_NAMES (0 when it does not run). The radar unit adds the children of its collector
    (a curl per fetch) and the size of its tmpfs spool; docker counts the whole rootless tree."""
    uid = os.getuid() if uid is None else uid
    budgets = budgets or {}
    rss = {n: 0 for n in UNIT_NAMES}
    procs = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        entries = []
    for name in entries:
        if not name.isdigit():
            continue
        p = os.path.join(proc_root, name)
        try:
            if os.stat(p).st_uid != uid:
                continue
            with open(os.path.join(p, "stat")) as f:
                st = f.read()
            ppid = int(st[st.rindex(")") + 2:].split()[1])
            comm = st[st.index("(") + 1:st.rindex(")")]
            with open(os.path.join(p, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            kib = anon = filekib = 0
            have_split = False
            with open(os.path.join(p, "status")) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kib = int(line.split()[1])
                    elif line.startswith("RssAnon:"):
                        anon = int(line.split()[1])
                        have_split = True
                    elif line.startswith("RssFile:"):
                        filekib = int(line.split()[1])
            procs[int(name)] = (ppid, comm, cmd, kib, anon if have_split else kib, filekib)
        except (OSError, ValueError, IndexError):
            continue
    unit_of = {pid: classify_process(c, m) for pid, (_pp, c, m, _k, _a, _f) in procs.items()}
    weed_text_kib = 0
    for pid, (ppid, _c, _m, kib, anon, filekib) in procs.items():
        u = unit_of[pid]
        if u is None:  # a child of a radar collector (curl per fetch) counts for radar
            a = ppid
            seen = 0
            while a in procs and seen < 8:
                if unit_of.get(a) == "radar":
                    u = "radar"
                    break
                a = procs[a][0]
                seen += 1
        if u and u.startswith("weed-"):
            # the four weed processes map ONE binary: their file pages are charged to the account once. Each weed unit reports its anonymous memory,
            # the shared text is the unit "weed-binary" (the largest RssFile of the four), so the budgets add up.
            rss[u] += anon
            weed_text_kib = max(weed_text_kib, filekib)
        elif u:
            rss[u] += kib
    rss["weed-binary"] = weed_text_kib
    out = []
    for n in UNIT_NAMES:
        mib = rss[n] / 1024.0
        if n == "radar" and spool_glob:
            import glob
            for path in glob.glob(spool_glob):
                try:
                    if os.path.isdir(path):
                        for r, _d, fs in os.walk(path):
                            mib += sum(os.path.getsize(os.path.join(r, f)) for f in fs if os.path.exists(os.path.join(r, f))) / 1048576.0
                    else:
                        mib += os.path.getsize(path) / 1048576.0
                except OSError:
                    pass
        out.append({"unit": n, "rss_mib": round(mib, 1), "budget_mib": budgets.get(n, 0)})
    return out


def load_budgets(path):
    """The one declared budget file ({unit: MiB}); {} when absent or broken."""
    if not path:
        return {}
    try:
        with open(path) as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if isinstance(v, (int, float))}
    except (OSError, ValueError):
        return {}


def _quota_number(tok, files=False):
    """'297G' -> GiB (disk) or '621k' -> 621000 (files). The shop's quota(1) prints disk in K/M/G/T (1024) and files with k (1000)."""
    tok = tok.strip()
    if not tok or tok == "-":
        return None
    mult = {"k": 1000, "K": 1000} if files else {"k": 1.0 / 1048576, "K": 1.0 / 1048576, "M": 1.0 / 1024, "G": 1.0, "T": 1024.0}
    unit = tok[-1]
    try:
        if unit.isdigit():
            return float(tok) if files else float(tok) / 1048576  # bare numbers: files as is, disk in KiB
        return float(tok[:-1]) * mult[unit]
    except (ValueError, KeyError):
        return None


def provider_quota(path=None):
    """The account's disk and file quota from the text the provider keeps in ~/.quota (`quota -u`, refreshed by its root cron).
    Returns {} when unreadable. disk in GiB, files as counts; fractions are used/quota."""
    path = path or os.path.expanduser("~/.quota")
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        t = line.split()
        if len(t) >= 7 and t[0].startswith("/"):
            used, quota, limit = (_quota_number(x) for x in t[1:4])
            fused, fquota, flimit = (_quota_number(x, True) for x in t[4:7])
            out = {}
            if used is not None and quota:
                out.update(quota_disk_used_gib=round(used, 2), quota_disk_quota_gib=round(quota, 1), quota_disk_fraction=round(used / quota, 4))
            if fused is not None and fquota:
                out.update(quota_files_used=int(fused), quota_files_quota=int(fquota), quota_files_fraction=round(fused / fquota, 4))
            return out
    return {}


def provider_fs(path=None):
    """Fill of the provider's shared filesystem under the account home (df -P ~): a full disk on the provider's side makes writes fail even below our quota."""
    try:
        st = os.statvfs(path or os.path.expanduser("~"))
    except OSError:
        return {}
    size = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    if not size:
        return {}
    return {"provider_fs_size_gib": round(size / 1073741824), "provider_fs_used_gib": round(used / 1073741824), "provider_fs_fraction": round(used / size, 4)}


VOLUME_FILE_RE = re.compile(r"\.(dat|idx|ec\d\d|ecx|ecj)$")


def cgroup_usage_mib(uid):
    try:
        return int(open("/sys/fs/cgroup/memory/user.slice/user-%d.slice/memory.usage_in_bytes" % uid).read()) / 1048576.0
    except (OSError, ValueError):
        return None


def trim_candidates(data_dir, now, min_age_s):
    """Clean volume files of the volume directory, coldest first (max of atime and mtime), none changed in the last min_age_s seconds.
    Only the volume directory itself, only .dat/.idx/.ecNN/.ecx/.ecj: never binaries, leveldb or anything else."""
    out = []
    try:
        names = os.listdir(data_dir)
    except OSError:
        return out
    for name in names:
        if not VOLUME_FILE_RE.search(name):
            continue
        path = os.path.join(data_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat_is_regular(st) or now - st.st_mtime < min_age_s:
            continue
        out.append((max(st.st_atime, st.st_mtime), path))
    out.sort()
    return [p for _t, p in out]


LDB_FILE_RE = re.compile(r"\.ldb$")


def ldb_candidates(ldb_dir, now, min_age_s):
    """The immutable sorted tables (NNNNNN.ldb) of the filer's leveldb, coldest first, none changed in the last min_age_s seconds. Never MANIFEST, CURRENT, LOG or the
    write-ahead .log: only files whose name ends in .ldb (leveldb never rewrites a table in place)."""
    out = []
    for root, _dirs, names in os.walk(ldb_dir):
        for name in names:
            if not LDB_FILE_RE.search(name):
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat_is_regular(st) or now - st.st_mtime < min_age_s:
                continue
            out.append((max(st.st_atime, st.st_mtime), path))
    out.sort()
    return [p for _t, p in out]


def stat_is_regular(st):
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


def trim_once(data_dir, uid, high_mib, low_mib, min_age_s, now=None, usage=cgroup_usage_mib, advise=None, log=None, ldb_dir=None):
    """When the account's charged usage is above high_mib, drop the clean page cache of volume files (posix_fadvise DONTNEED), coldest first, until usage
    is below low_mib. Returns (files_advised, freed_mib, wall_s, cpu_s); freed is the usage drop, wall_s/cpu_s the cost of the pass itself.
    One log line per trim, on stderr (health.log), since stdout goes to a journal nobody can read."""
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    u0 = usage(uid)
    if u0 is None or u0 <= high_mib:
        return 0, 0.0, 0.0, 0.0
    advise = advise or (lambda path: _advise_dontneed(path))
    t0 = time.perf_counter()
    c0 = time.process_time()
    n = 0
    t_now = time.time() if now is None else now
    paths = trim_candidates(data_dir, t_now, min_age_s)
    if ldb_dir:  # the filer's leveldb tables are tried after the volume files
        paths = paths + ldb_candidates(ldb_dir, t_now, min_age_s)
    for path in paths:
        advise(path)
        n += 1
        u = usage(uid)
        if u is not None and u < low_mib:
            break
    u1 = usage(uid)
    freed = max(0.0, u0 - (u1 if u1 is not None else u0))
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    log("swfs-health: cache trim: usage %.0f -> %.0f MiB (high %d, low %d), %d volume files advised, %.0f MiB freed, %.3f s wall, %.3f s cpu" % (u0, u1 if u1 is not None else -1, high_mib, low_mib, n, freed, wall, cpu))
    return n, freed, wall, cpu


def _advise_dontneed(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOATIME", 0))
    except OSError:
        return
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def trim_wait(usage_mib, wall_s, fast_above_mib, fast_s, slow_s):
    """Seconds until the next governor sample: fast_s while charged usage is above fast_above_mib (0 = adaptive cadence off: always slow_s), slow_s below it.
    A pass that took more than 1 s (a slow disk) makes the next one wait at least 10x its own duration, so the governor never becomes the load."""
    wait = slow_s
    if fast_above_mib > 0 and usage_mib is not None and usage_mib > fast_above_mib:
        wait = fast_s
    if wall_s > 1.0:
        wait = max(wait, 10.0 * wall_s)
    return wait


def trim_loop(state, uid, data_dir, high_mib, low_mib, min_age_s, interval=30.0, ldb_dir=None, fast_above_mib=0, fast_s=5.0):
    trims = 0
    freed_total = 0.0
    wall_total = 0.0
    cpu_total = 0.0
    while True:
        n, freed, wall, cpu = trim_once(data_dir, uid, high_mib, low_mib, min_age_s, ldb_dir=ldb_dir)
        if n:
            trims += 1
            freed_total += freed
            wall_total += wall
            cpu_total += cpu
        wait = trim_wait(cgroup_usage_mib(uid), wall, fast_above_mib, fast_s, interval)
        state.set(cache_trims=trims, cache_trim_freed_mib=round(freed_total), cache_trim_seconds=round(wall_total, 3), cache_trim_cpu_seconds=round(cpu_total, 3), cache_trim_enabled=1, cache_trim_wait_s=wait)
        time.sleep(wait)


def local_loop(state, uid, interval, budgets_path=None):
    while True:
        ws, usage, limit = cgroup_working_set_mib(uid)
        mem = cgroup_memory_mib(uid)
        units = unit_rows(uid=uid, budgets=load_budgets(budgets_path), spool_glob="/run/user/%d/radar-collector*" % uid)
        docker_pids, docker_mib = provider_docker_processes()
        docker_policy = provider_docker_policy()
        s3_fds, s3_rss_mib, s3_swap_mib = s3_gateway_process()
        state.set(**provider_quota(), **provider_fs())
        s3_total, s3_5xx = s3_request_counters(S3_METRICS_PORT)
        state.set(**mem, s3_requests_total=s3_total, s3_5xx_total=s3_5xx, ram_ws_mib=ws, units=units, ram_failcnt=cgroup_failcnt(uid), host_mem_psi_some_avg60=host_psi("memory"), host_io_psi_some_avg60=host_psi("io"), load1=os.getloadavg()[0],
                  provider_docker=1 if docker_pids else 0, provider_docker_pids=docker_pids, provider_docker_mib=docker_mib,
                  provider_docker_enabled=-1 if docker_policy is None else (1 if docker_policy else 0),
                  s3_fds=s3_fds, s3_rss_mib=s3_rss_mib, s3_swap_mib=s3_swap_mib)
        time.sleep(interval)


def cluster_check(state, a, access, secret, own, peers):
    """One round of the checks. Every box runs the same code. The PER-BOX part (the marker write through this box's own gateway and its visibility on the peers: filer lag, own_gateway_busy,
    write_probe_s) runs everywhere. The CLUSTER part (volume.list, cluster.ps, cluster.status, cluster.raft.ps: EC shorts, filers in the ring, data nodes, writable and read-only volumes, raft peers)
    runs only on the box whose master is the raft leader right now, so one box asks the masters, not seven; elsewhere those fields are null. When the box cannot tell whether it leads, it
    runs them (a failing check must be loud). Any failure of the round itself is a failed check, not a reason to keep the previous result."""
    own_host = re.sub(r"^https?://", "", own).split(":")[0] if own else ""
    leader = own_master_is_leader(a.masters, own_host)
    cluster_fields = dict(ec_volumes=None, ec_volumes_short=None, ec_short_ids=None, filers_in_ring=None, topology_nodes=None, writable_volumes=None, raft_peers=None,
                          volumes_readonly=None, volumes_readonly_abnormal=None, volume_size_limit_mb=None)
    try:
        lag, missing, busy, put_s = lag_probe(own, peers, a.bucket, access, secret, a.cacert)
        if leader is not False:
            vl = weed_shell(a.weed, a.masters, ["volume.list"])
            n_short, ids, n_ec = parse_ec_short(vl)
            limit_mb = parse_volume_size_limit_mb(vl)
            n_ro, n_ro_bad, _ro_ids = parse_readonly_volumes(vl, limit_mb)
            filers = None
            for _ in range(4):  # the shell can list the filers a moment after it starts
                filers = parse_filers(weed_shell(a.weed, a.masters, ["cluster.ps", "cluster.ps"]))
                if filers:
                    break
                time.sleep(3)
            raft = parse_raft_peers(weed_shell(a.weed, a.masters, ["cluster.raft.ps"]))
            nodes, writable = parse_cluster_status(weed_shell(a.weed, a.masters, ["cluster.status"]))
            missing = missing + write_path_problems(nodes, writable, a.expected_nodes or len(peers) + 1)
            cluster_fields = dict(ec_volumes=n_ec, ec_volumes_short=n_short, ec_short_ids=ids[:20], filers_in_ring=filers, topology_nodes=nodes, writable_volumes=writable, raft_peers=raft,
                                  volumes_readonly=n_ro, volumes_readonly_abnormal=n_ro_bad, volume_size_limit_mb=limit_mb)
        state.set(filer_lag_max_s=lag, cluster_leader=(None if leader is None else (1 if leader else 0)), **cluster_fields,
                  filer_peers_not_answering=len(missing), filer_peers_missing=missing, own_gateway_busy=1 if busy else 0, write_probe_s=put_s, own_gateway_busy_reason=busy, cluster_error=None,
                  cluster_checked=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    except Exception as e:  # a check that cannot run counts as failed (a hung master makes the shell time out: exactly when the answer matters)
        why = "cluster check failed: " + repr(e)[:160]
        state.set(cluster_error=repr(e)[:200], filer_lag_max_s=None, topology_nodes=None, writable_volumes=None, raft_peers=None, volumes_readonly=None, volumes_readonly_abnormal=None,
                  filer_peers_not_answering=max(1, len(peers)), filer_peers_missing=[why])


def cluster_loop(state, a):
    cfg = json.load(open(os.path.expanduser(a.s3_json)))
    ident = cfg["identities"][0]["credentials"][0]
    access, secret = ident["accessKey"], ident["secretKey"]
    own = a.own_s3
    peers = [p for p in a.peer_s3.split(",") if p and p != own]
    while True:
        cluster_check(state, a, access, secret, own, peers)
        time.sleep(a.interval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--trim-dir", help="the volume directory whose clean files may be dropped from the page cache when the account's charged usage is high")
    p.add_argument("--trim-high", type=int, default=0, help="MiB of charged usage above which the cache governor trims (0 = off)")
    p.add_argument("--trim-low", type=int, default=0, help="MiB to trim down to")
    p.add_argument("--trim-ldb-dir", help="also drop the cache of the immutable .ldb tables of the filer's leveldb in this directory (after the volume files)")
    p.add_argument("--trim-fast-above", type=int, default=0, help="MiB of charged usage above which the governor samples every --trim-fast-interval seconds instead of every 30 (0 = off)")
    p.add_argument("--trim-fast-interval", type=float, default=5.0)
    p.add_argument("--trim-min-age", type=int, default=600, help="seconds a volume file must be unchanged before it is touched")
    p.add_argument("--budgets", help="JSON {unit: MiB}, the one declared RAM budget file; the units array reports each unit's RSS against it")
    p.add_argument("--cluster", action="store_true")
    p.add_argument("--weed")
    p.add_argument("--masters")
    p.add_argument("--s3-json", default="~/.seaweedfs/s3.json")
    p.add_argument("--own-s3")
    p.add_argument("--peer-s3", default="")
    p.add_argument("--bucket", default="health")
    p.add_argument("--expected-nodes", type=int, default=0, help="data nodes the leader must see (default: the number of S3 gateways in --peer-s3)")
    p.add_argument("--cacert")
    p.add_argument("--tls-cert", help="serve HTTPS with this certificate and REQUIRE a client certificate signed by --cacert (anonymous requests fail)")
    p.add_argument("--tls-key")
    a = p.parse_args()
    state = State()
    if a.trim_dir and a.trim_high > 0:
        threading.Thread(target=trim_loop, args=(state, os.getuid(), a.trim_dir, a.trim_high, a.trim_low or a.trim_high - 50, a.trim_min_age, 30.0, a.trim_ldb_dir, a.trim_fast_above, a.trim_fast_interval), daemon=True).start()
    threading.Thread(target=local_loop, args=(state, os.getuid(), a.interval, a.budgets), daemon=True).start()
    if a.cluster:
        threading.Thread(target=cluster_loop, args=(state, a), daemon=True).start()
    server = HTTPServer(("0.0.0.0", a.port), make_handler(state))
    if a.tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(a.tls_cert, a.tls_key)
        ctx.load_verify_locations(a.cacert)
        ctx.verify_mode = ssl.CERT_REQUIRED
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
