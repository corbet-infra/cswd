#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""swfs-docker-guard: keep the provider's rootless Docker from running in this account.

The provider's root cron starts `dockerd-rootless.sh` as the account user (outside the user systemd manager)
while its account policy says dockerEnabled=true; the daemon costs 27-103 MiB of the 1250 MiB account limit.
A user path unit fires when Docker's runtime files appear and starts this one-shot: it stops exactly the
rootless Docker process tree of THIS uid (verified by uid and by the rootless state directory in the command
line, never by a name pattern), and writes one line. Nothing of the provider's install is touched or deleted.
"""
import argparse
import os
import signal
import sys
import time

DOCKER_COMMS = ("rootlesskit", "dockerd", "containerd", "slirp4netns", "exe")


def read_procs(proc_root="/proc", uid=None):
    """{pid: (ppid, comm, cmdline, rss_kib)} of the processes owned by uid."""
    uid = os.getuid() if uid is None else uid
    out = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        p = os.path.join(proc_root, name)
        try:
            if os.stat(p).st_uid != uid:
                continue
            with open(os.path.join(p, "stat")) as f:
                stat = f.read()
            rest = stat[stat.rindex(")") + 2:].split()
            ppid = int(rest[1])
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            with open(os.path.join(p, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            rss = 0
            with open(os.path.join(p, "status")) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                        break
            out[int(name)] = (ppid, comm, cmd, rss)
        except (OSError, ValueError, IndexError):
            continue
    return out


def docker_tree(procs, uid):
    """PIDs of the rootless Docker tree: every process whose command line carries this uid's rootlesskit
    state directory (`--state-dir=/run/user/<uid>/dockerd-rootless`) or dockerd-rootless.sh, plus all their
    descendants (dockerd, containerd and the containers' processes run below rootlesskit's child)."""
    marker = "/run/user/%d/dockerd-rootless" % uid
    roots = {pid for pid, (_pp, comm, cmd, _r) in procs.items()
             if (comm == "rootlesskit" and marker in cmd) or "dockerd-rootless.sh" in cmd}
    children = {}
    for pid, (ppid, _c, _m, _r) in procs.items():
        children.setdefault(ppid, []).append(pid)
    tree = set()
    todo = list(roots)
    while todo:
        pid = todo.pop()
        if pid in tree or pid == os.getpid():
            continue
        tree.add(pid)
        todo.extend(children.get(pid, []))
    return tree


def stop(tree, procs, term_wait=10.0, kill=os.kill, alive=lambda pid: os.path.exists("/proc/%d" % pid), sleep=time.sleep):
    """SIGTERM the tree (exact PIDs), wait, SIGKILL what is left. Returns (stopped, killed)."""
    for pid in sorted(tree):
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    waited = 0.0
    left = set(tree)
    while left and waited < term_wait:
        sleep(0.5)
        waited += 0.5
        left = {p for p in left if alive(p)}
    killed = 0
    for pid in sorted(left):
        try:
            kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return len(tree), killed


def run(uid=None, proc_root="/proc", rounds=4, gap=2.0, log=print, **kw):
    """A daemon that is still starting can spawn children after the first sweep: sweep up to `rounds` times."""
    uid = os.getuid() if uid is None else uid
    total = 0
    pids = []
    mib = 0.0
    for i in range(rounds):
        procs = read_procs(proc_root, uid)
        tree = docker_tree(procs, uid)
        if not tree:
            if total:
                break
            if i == 0:
                return 0
            time.sleep(gap)
            continue
        mib += sum(procs[p][3] for p in tree) / 1024
        pids.extend(sorted(tree))
        n, _k = stop(tree, procs, **kw)
        total += n
        time.sleep(gap)
    if total:
        log("swfs-docker-guard: stopped %d rootless Docker processes (%.0f MiB RSS) of uid %d: pids %s" % (total, mib, uid, ",".join(str(p) for p in pids)))
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--observe", action="store_true", help="log what would be stopped, stop nothing")
    a = ap.parse_args()
    uid = os.getuid()
    if a.observe:
        procs = read_procs(uid=uid)
        tree = docker_tree(procs, uid)
        if tree:
            print("swfs-docker-guard: OBSERVE would stop %d processes (%.0f MiB RSS): pids %s" % (len(tree), sum(procs[p][3] for p in tree) / 1024, ",".join(str(p) for p in sorted(tree))))
        return 0
    run(uid=uid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
