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
    """sigv4_curl, retried every 5 s while the gateway answers 503: that is its deliberate upload guard (2 slots / 32 MB) saying
    "busy" while JuiceFS writes drain through a slow replica, which is load on the dirt tier, not a silent peer."""
    while True:
        code = sigv4_curl(url, method, access, secret, **kw)
        if code != "503" or time.time() - t0 >= limit:
            return code
        time.sleep(5)


def lag_probe(own, peers, bucket, access, secret, cacert, limit=90):
    """PUT a marker via `own`, poll HEAD on every peer until it is visible, DELETE it.
    Returns (max seconds, list of what failed, busy reason or None); a gateway still busy after `limit` is reported as busy, not as a failure."""
    key = "health/lag-%d" % int(time.time())
    t0 = time.time()
    code = guarded_curl("%s/%s/%s" % (own, bucket, key), "PUT", access, secret, t0, limit, body=key, cacert=cacert)
    if code == "503":
        return None, [], "own gateway busy (503 upload guard) for %ds" % limit
    if code != "200":
        return None, ["own gateway did not accept the marker (HTTP %s)" % (code or "none")], None
    seen = {}
    while time.time() - t0 < limit and len(seen) < len(peers):
        for p in peers:
            if p not in seen and sigv4_curl("%s/%s/%s" % (p, bucket, key), "HEAD", access, secret, cacert=cacert) == "200":
                seen[p] = time.time() - t0
        time.sleep(1)
    deleted = guarded_curl("%s/%s/%s" % (own, bucket, key), "DELETE", access, secret, time.time(), 30, cacert=cacert)
    missing = [p for p in peers if p not in seen]
    if deleted not in ("200", "204", "503"):
        missing.append("own gateway did not delete the marker (HTTP %s)" % (deleted or "none"))
    return (round(max(seen.values()), 1) if seen else None), missing, None


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


def local_loop(state, uid, interval):
    while True:
        ws, usage, limit = cgroup_working_set_mib(uid)
        docker_pids, docker_mib = provider_docker_processes()
        docker_policy = provider_docker_policy()
        s3_fds, s3_rss_mib, s3_swap_mib = s3_gateway_process()
        state.set(ram_ws_mib=ws, ram_usage_mib=usage, ram_limit_mib=limit, load1=os.getloadavg()[0],
                  provider_docker=1 if docker_pids else 0, provider_docker_pids=docker_pids, provider_docker_mib=docker_mib,
                  provider_docker_enabled=-1 if docker_policy is None else (1 if docker_policy else 0),
                  s3_fds=s3_fds, s3_rss_mib=s3_rss_mib, s3_swap_mib=s3_swap_mib)
        time.sleep(interval)


def cluster_check(state, a, access, secret, own, peers):
    """One round of the cluster-wide checks. Any failure of the round itself is a failed check, not a reason to keep the previous result."""
    try:
        vl = weed_shell(a.weed, a.masters, ["volume.list"])
        n_short, ids, n_ec = parse_ec_short(vl)
        filers = None
        for _ in range(4):  # the shell can list the filers a moment after it starts
            filers = parse_filers(weed_shell(a.weed, a.masters, ["cluster.ps", "cluster.ps"]))
            if filers:
                break
            time.sleep(3)
        nodes, writable = parse_cluster_status(weed_shell(a.weed, a.masters, ["cluster.status"]))
        lag, missing, busy = lag_probe(own, peers, a.bucket, access, secret, a.cacert)
        missing = missing + write_path_problems(nodes, writable, a.expected_nodes or len(peers) + 1)
        state.set(ec_volumes=n_ec, ec_volumes_short=n_short, ec_short_ids=ids[:20], filers_in_ring=filers, filer_lag_max_s=lag,
                  topology_nodes=nodes, writable_volumes=writable,
                  filer_peers_not_answering=len(missing), filer_peers_missing=missing, own_gateway_busy=1 if busy else 0, own_gateway_busy_reason=busy, cluster_error=None,
                  cluster_checked=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    except Exception as e:  # a check that cannot run counts as failed (a hung master makes the shell time out: exactly when the answer matters)
        why = "cluster check failed: " + repr(e)[:160]
        state.set(cluster_error=repr(e)[:200], filer_lag_max_s=None, topology_nodes=None, writable_volumes=None,
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
    threading.Thread(target=local_loop, args=(state, os.getuid(), a.interval), daemon=True).start()
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
