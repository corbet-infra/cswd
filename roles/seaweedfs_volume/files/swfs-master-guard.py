#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""swfs-master-guard: restart THIS box's SeaweedFS master when it is alive for systemd and dead for everyone else (python3 standard library only).

On 2026-11-12 the master on one box hung: systemd said `active`, /cluster/status never answered, raft kept electing around it, and all
seven volume servers stayed on its heartbeat stream, so the real leader saw no data node and no writable volume. Nothing noticed for
ten hours and one restart fixed it. This guard is that restart, taken box-locally and conservatively:

  * it looks at its own master only: GET http://127.0.0.1:<port>/cluster/status every --interval seconds (answer within --timeout);
  * it acts only on a master whose unit is `active` (a stopped, activating or failed unit belongs to systemd or to a person);
  * the master must have been silent for --down seconds (and --stagger seconds more per index in the master list, so two guards do not
    decide in the same second);
  * it asks the other masters first: it restarts only while at least min(len(masters) - 1, len(masters)//2 + 2) of the OTHERS still answer,
    i.e. after the restart a quorum is left and, from five masters on, one more master may be down. With seven masters at most two are ever
    down by its doing; with a cluster that is already degraded it does nothing and says so;
  * at most --max-restarts per 24 h and one per --cooldown seconds, remembered in --state across restarts of the guard itself;
  * the restart is SIGQUIT: the Go runtime writes every goroutine's stack to the master's log (the evidence for the upstream report),
    the process exits, `Restart=always` brings it back with its raft state; SIGKILL follows only if the process outlives --quit-wait.

It never starts a stopped unit, never touches another box, and everything it does is a line in its log. --observe logs the decision and
acts on nothing.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + msg, flush=True)


def parse_masters(spec):
    """'ip:http.grpc,ip:http.grpc,...' -> [(ip, http_port)] in list order."""
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        host, _, ports = item.rpartition(":")
        out.append((host, int(ports.split(".")[0])))
    return out


def probe(host, port, timeout):
    """GET /cluster/status with a hard overall deadline. Returns (True, '') when a JSON object came back with 200, else (False, why)."""
    deadline = time.monotonic() + timeout
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return False, "connect: %s" % e
    try:
        s.sendall(("GET /cluster/status HTTP/1.0\r\nHost: %s:%d\r\nConnection: close\r\n\r\n" % (host, port)).encode())
        data = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False, "no complete answer in %gs" % timeout
            s.settimeout(left)
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                return False, "no complete answer in %gs" % timeout
            if not chunk:
                break
            data += chunk
            if len(data) > 1 << 20:
                break
    except OSError as e:
        return False, "io: %s" % e
    finally:
        s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b" ", 2)[1:2]
    if status != [b"200"]:
        return False, "status %s" % (status[0].decode("latin-1") if status else "?")
    try:
        if not isinstance(json.loads(body.decode("utf-8", "replace")), dict):
            return False, "not a JSON object"
    except ValueError:
        return False, "not JSON"
    return True, ""


def probe_many(targets, timeout):
    """Probe several masters in parallel; returns {(host, port): (ok, why)}."""
    results = {}

    def one(t):
        results[t] = probe(t[0], t[1], timeout)

    threads = [threading.Thread(target=one, args=(t,), daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 5)
    return {t: results.get(t, (False, "probe did not finish")) for t in targets}


def need_healthy_others(total):
    """Masters that must still answer (not counting this one) before it may be restarted: a quorum survives the restart and, from five masters on,
    one more may be down (7 -> 5 of the other 6, 5 -> 4 of 4, 3 -> both others)."""
    return min(total - 1, total // 2 + 2)


def decide(down_for, grace, healthy_others, total, now, restarts, cooldown, max_per_day):
    """('wait'|'skip'|'restart', reason). restarts = timestamps of earlier restarts by this guard."""
    if down_for < grace:
        return "wait", "silent for %ds of %ds" % (down_for, grace)
    need = need_healthy_others(total)
    if healthy_others < need:
        return "skip", "only %d of the other %d masters answer, %d are needed to keep a quorum" % (healthy_others, total - 1, need)
    recent = [t for t in restarts if now - t < 86400]
    if recent and now - max(recent) < cooldown:
        return "skip", "restarted %ds ago (cooldown %ds)" % (now - max(recent), cooldown)
    if len(recent) >= max_per_day:
        return "skip", "%d restarts in 24 h already (limit %d)" % (len(recent), max_per_day)
    return "restart", "silent for %ds, %d of the other %d masters answer" % (down_for, healthy_others, total - 1)


def systemctl(*args):
    p = subprocess.run(["systemctl", "--user"] + list(args), capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout.strip()


def unit_state(unit):
    """(ActiveState, SubState, MainPID) of the unit."""
    rc, out = systemctl("show", "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", unit)
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return kv.get("ActiveState", "?"), kv.get("SubState", "?"), int(kv.get("MainPID", "0") or 0)


def load_state(path):
    try:
        with open(path) as f:
            return [float(t) for t in json.load(f).get("restarts", [])]
    except (OSError, ValueError, AttributeError):
        return []


def save_state(path, restarts):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"restarts": restarts}, f)
    os.replace(tmp, path)


def restart_master(a, own, pid):
    """SIGQUIT (goroutine dump into the master's log, exit, Restart=always), SIGKILL if it outlives --quit-wait. Returns True when it answers again."""
    rc, out = systemctl("kill", "--signal=QUIT", "--kill-whom=main", a.unit)
    log("sent SIGQUIT to pid %d of %s (rc=%d): the goroutine dump is in the master's log" % (pid, a.unit, rc))
    end = time.monotonic() + a.quit_wait
    while time.monotonic() < end:
        time.sleep(1)
        active, sub, now_pid = unit_state(a.unit)
        if now_pid != pid:
            break
    else:
        rc, out = systemctl("kill", "--signal=KILL", "--kill-whom=main", a.unit)
        log("pid %d outlived %ds after SIGQUIT: sent SIGKILL (rc=%d)" % (pid, a.quit_wait, rc))
    end = time.monotonic() + a.start_wait
    while time.monotonic() < end:
        time.sleep(2)
        ok, why = probe(own[0], own[1], a.timeout)
        if ok:
            return True
    return False


def run(a):
    masters = parse_masters(a.masters)
    me = lambda h, p: a.self_ip in (h, "%s:%d" % (h, p))
    index = next(i for i, (h, p) in enumerate(masters) if me(h, p))
    own = ("127.0.0.1", masters[index][1])
    others = [(h, p) for h, p in masters if not me(h, p)]
    grace = a.down + a.stagger * index
    log("guard up: unit %s, own master 127.0.0.1:%d (index %d of %d), %d s silence + stagger -> %d s, need %d of %d others, %s" % (
        a.unit, own[1], index, len(masters), a.down, grace, need_healthy_others(len(masters)), len(others), "OBSERVE ONLY" if a.observe else "acting"))
    first_fail = None
    last_pid = 0
    last_reason = None
    while True:
        active, sub, pid = unit_state(a.unit)
        ok, why = probe(own[0], own[1], a.timeout)
        now = time.time()
        if pid != last_pid:
            last_pid = pid
            first_fail = None  # a new process gets its own grace
        if ok or active != "active" or sub != "running":
            if first_fail is not None:
                log("own master %s again (unit %s/%s)" % ("answers" if ok else "is not running", active, sub))
            first_fail = None
            last_reason = None
        else:
            if first_fail is None:
                first_fail = now
                log("own master does not answer /cluster/status (%s) while its unit is %s/%s" % (why, active, sub))
            peers = probe_many(others, a.timeout)
            healthy = sum(1 for v in peers.values() if v[0])
            action, reason = decide(int(now - first_fail), grace, healthy, len(masters), now, load_state(a.state), a.cooldown, a.max_restarts)
            if action != "wait" and reason != last_reason:
                log("%s: %s" % (action, reason))
                last_reason = reason
            if action == "restart":
                if a.observe:
                    log("observe only: would restart %s now" % a.unit)
                else:
                    restarts = [t for t in load_state(a.state) if now - t < 86400] + [now]
                    save_state(a.state, restarts)
                    answered = restart_master(a, own, pid)
                    log("restart %s: own master %s" % (a.unit, "answers again" if answered else "does NOT answer yet (the guard keeps watching; limits apply)"))
                first_fail = None
                last_reason = None
        time.sleep(a.interval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--unit", default="seaweed-master.service")
    p.add_argument("--masters", required=True, help="the -peers list: ip:httpPort.grpcPort,... (all masters, this one included)")
    p.add_argument("--self-ip", required=True, help="this box's address in --masters (host, or host:httpPort when several masters share a host)")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--down", type=int, default=180, help="seconds of silence before the guard may act")
    p.add_argument("--stagger", type=int, default=20, help="extra seconds per index in the master list")
    p.add_argument("--cooldown", type=int, default=1800)
    p.add_argument("--max-restarts", type=int, default=3, help="per 24 hours")
    p.add_argument("--quit-wait", type=int, default=30)
    p.add_argument("--start-wait", type=int, default=120)
    p.add_argument("--state", default=os.path.expanduser("~/cold/status/swfs-master-guard.json"))
    p.add_argument("--observe", action="store_true")
    a = p.parse_args()
    try:
        run(a)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
