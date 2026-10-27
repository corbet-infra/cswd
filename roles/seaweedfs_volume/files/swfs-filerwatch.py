#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""swfs-filerwatch: self-fence of one SeaweedFS filer + S3 gateway that is cut off from the other filers (python3 stdlib only).

Why: the filer lock ring is fed by the masters (a filer leaves it only when its connection to the masters ends), and there is no
filer-to-filer health check. A box that is cut off from most peers but still connected to a master stays in the ring, and every S3
write whose lock key it owns then fails after several 60 s timeouts for the filers that cannot reach it.

What it does: every --interval seconds it opens a TCP connection to the gRPC port of every filer (itself counts as reachable). When
fewer than a majority (total // 2 + 1) are reachable for --fence-seconds in a row, it stops the S3 gateway and the filer
(--stop-cmd), which ends the filer's master stream so the ring drops it. It starts them again (--start-cmd) only after a majority
was reachable for --unfence-seconds in a row; every re-fence within --flap-window seconds of the last unfence doubles that wait (up
to --max-unfence-seconds). It only starts what it stopped itself (flag file), never what an operator stopped. The mirror case
(this box sees the majority, a few peers do not see this box) never fences: the box's own view decides.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time


class Fence:
    """The decision logic, no I/O. step() returns 'fence', 'unfence' or None."""

    def __init__(self, total, fence_seconds=60, unfence_seconds=300, flap_window=1800, max_unfence_seconds=3600, fenced=False):
        self.total = total
        self.majority = total // 2 + 1
        self.fence_seconds = fence_seconds
        self.unfence_seconds = unfence_seconds
        self.flap_window = flap_window
        self.max_unfence_seconds = max_unfence_seconds
        self.fenced = fenced
        self.bad_since = None
        self.good_since = None
        self.last_unfence = None
        self.wait = unfence_seconds

    def step(self, now, reachable):
        good = reachable >= self.majority
        if not self.fenced:
            if good:
                self.bad_since = None
                return None
            if self.bad_since is None:
                self.bad_since = now
            if now - self.bad_since >= self.fence_seconds:
                self.fenced = True
                self.good_since = None
                if self.last_unfence is not None and now - self.last_unfence < self.flap_window:
                    self.wait = min(self.wait * 2, self.max_unfence_seconds)
                else:
                    self.wait = self.unfence_seconds
                return "fence"
            return None
        if not good:
            self.good_since = None
            return None
        if self.good_since is None:
            self.good_since = now
        if now - self.good_since >= self.wait:
            self.fenced = False
            self.bad_since = None
            self.last_unfence = now
            return "unfence"
        return None


class Strikes:
    """A peer is unreachable only after N consecutive failed connects, so a busy neighbour box does not count as a partition."""

    def __init__(self, n=3):
        self.n = n
        self.fails = {}

    def seen(self, peer, ok):
        if ok:
            self.fails[peer] = 0
            return True
        self.fails[peer] = self.fails.get(peer, 0) + 1
        return self.fails[peer] < self.n


def probe(addr, timeout=3.0):
    host, port = addr.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def log(path, text):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), text)
    sys.stderr.write(line)
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--filers", required=True, help="comma separated host:grpcport of ALL filers, this box included")
    p.add_argument("--self", dest="me", required=True, help="host:grpcport of this box's filer")
    p.add_argument("--masters", default="", help="host:grpcport list, probed for the event log only")
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--fence-seconds", type=float, default=60)
    p.add_argument("--unfence-seconds", type=float, default=300)
    p.add_argument("--flap-window", type=float, default=1800)
    p.add_argument("--max-unfence-seconds", type=float, default=3600)
    p.add_argument("--strikes", type=int, default=3, help="a peer counts as unreachable only after this many consecutive failed connects")
    p.add_argument("--observe", action="store_true", help="compute and log decisions as WOULD FENCE / WOULD UNFENCE, touch no unit and no flag file")
    p.add_argument("--stop-cmd", required=True)
    p.add_argument("--start-cmd", required=True)
    p.add_argument("--flag-file", required=True)
    p.add_argument("--status-file")
    p.add_argument("--event-log")
    a = p.parse_args()
    filers = [x for x in a.filers.split(",") if x]
    masters = [x for x in a.masters.split(",") if x]
    fence = Fence(len(filers), a.fence_seconds, a.unfence_seconds, a.flap_window, a.max_unfence_seconds, fenced=(not a.observe and os.path.exists(a.flag_file)))
    strikes = Strikes(a.strikes)
    while True:
        now = time.monotonic()
        table = {f: (True if f == a.me else strikes.seen(f, probe(f))) for f in filers}
        reachable = sum(table.values())
        action = fence.step(now, reachable)
        if action and a.observe:
            log(a.event_log, "WOULD %s: %d of %d filers reachable (majority %d); table=%s masters=%s"
                % (action.upper(), reachable, fence.total, fence.majority, json.dumps(table), json.dumps({m: probe(m) for m in masters})))
        elif action == "fence":
            log(a.event_log, "FENCE: %d of %d filers reachable (< %d) for %ds; stopping s3 + filer; table=%s masters=%s"
                % (reachable, fence.total, fence.majority, a.fence_seconds, json.dumps(table), json.dumps({m: probe(m) for m in masters})))
            open(a.flag_file, "w").write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
            subprocess.run(a.stop_cmd, shell=True, check=False)
        elif action == "unfence":
            log(a.event_log, "UNFENCE: %d of %d filers reachable for %ds; starting filer + s3; table=%s" % (reachable, fence.total, fence.wait, json.dumps(table)))
            subprocess.run(a.start_cmd, shell=True, check=False)
            try:
                os.unlink(a.flag_file)
            except FileNotFoundError:
                pass
        if a.status_file:
            tmp = a.status_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mode": "observe" if a.observe else "enforce", "fenced": fence.fenced, "reachable": reachable, "total": fence.total,
                           "majority": fence.majority, "unfence_wait": fence.wait, "table": table}, f)
            os.replace(tmp, a.status_file)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
