#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""swfs-health: a tiny health endpoint per SeaweedFS box (python3 standard library only), polled by gatus.

Every box serves GET /health.json on one assigned port: the RAM of the account (cgroup v1 working set = usage - inactive file cache,
the account limit is 1250 MiB), the load, and the age of the data. With --cluster the box also runs the cluster-wide checks once per
--interval: the number of EC volumes with fewer than 14 shards (weed shell volume.list), the filers registered with the masters
(cluster.ps), and a write-via-filer-A / read-via-filer-B probe over every pair partner (the S3 gateways), giving the largest time a
marker object needed to become visible on a peer, or null when a peer did not answer. Run --cluster on two boxes so that the checks
survive the loss of one. Gatus turns the JSON into alerts (kubernetes/corbet-devops/gatus.nix); the weed shell needs the box's own
mutual-TLS security.toml, which is why this runs on the boxes and not on the monitoring side.
"""
import argparse
import hashlib
import json
import os
import re
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


def lag_probe(own, peers, bucket, access, secret, cacert, limit=90):
    """PUT a marker via `own`, poll HEAD on every peer until it is visible. Returns (max seconds, list of peers that never showed)."""
    key = "health/lag-%d" % int(time.time())
    t0 = time.time()
    if sigv4_curl("%s/%s/%s" % (own, bucket, key), "PUT", access, secret, body=key, cacert=cacert) != "200":
        return None, ["own gateway did not accept the marker"]
    seen = {}
    while time.time() - t0 < limit and len(seen) < len(peers):
        for p in peers:
            if p not in seen and sigv4_curl("%s/%s/%s" % (p, bucket, key), "HEAD", access, secret, cacert=cacert) == "200":
                seen[p] = time.time() - t0
        time.sleep(1)
    sigv4_curl("%s/%s/%s" % (own, bucket, key), "DELETE", access, secret, cacert=cacert)
    missing = [p for p in peers if p not in seen]
    return (round(max(seen.values()), 1) if seen else None), missing


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
        state.set(ram_ws_mib=ws, ram_usage_mib=usage, ram_limit_mib=limit, load1=os.getloadavg()[0])
        time.sleep(interval)


def cluster_loop(state, a):
    cfg = json.load(open(os.path.expanduser(a.s3_json)))
    ident = cfg["identities"][0]["credentials"][0]
    access, secret = ident["accessKey"], ident["secretKey"]
    own = a.own_s3
    peers = [p for p in a.peer_s3.split(",") if p and p != own]
    while True:
        try:
            vl = weed_shell(a.weed, a.masters, ["volume.list"])
            n_short, ids, n_ec = parse_ec_short(vl)
            filers = None
            for _ in range(4):  # the shell can list the filers a moment after it starts
                filers = parse_filers(weed_shell(a.weed, a.masters, ["cluster.ps", "cluster.ps"]))
                if filers:
                    break
                time.sleep(3)
            lag, missing = lag_probe(own, peers, a.bucket, access, secret, a.cacert)
            state.set(ec_volumes=n_ec, ec_volumes_short=n_short, ec_short_ids=ids[:20], filers_in_ring=filers, filer_lag_max_s=lag,
                      filer_peers_not_answering=len(missing), filer_peers_missing=missing, cluster_checked=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        except Exception as e:  # keep serving the last good values and say what failed
            state.set(cluster_error=repr(e)[:200])
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
    p.add_argument("--cacert")
    a = p.parse_args()
    state = State()
    threading.Thread(target=local_loop, args=(state, os.getuid(), a.interval), daemon=True).start()
    if a.cluster:
        threading.Thread(target=cluster_loop, args=(state, a), daemon=True).start()
    HTTPServer(("0.0.0.0", a.port), make_handler(state)).serve_forever()


if __name__ == "__main__":
    main()
