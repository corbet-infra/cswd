# SPDX-License-Identifier: LGPL-3.0-or-later
import importlib.util
import os
import signal
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("dg", os.path.join(os.path.dirname(__file__), "../../roles/seaweedfs_volume/files/swfs-docker-guard.py"))
dg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dg)

UID = os.getuid()
MARK = "--state-dir=/run/user/%d/dockerd-rootless" % UID


def fake_proc(root, procs):
    """procs: {pid: (ppid, comm, cmdline, rss_kib)}"""
    for pid, (ppid, comm, cmd, rss) in procs.items():
        d = os.path.join(root, str(pid))
        os.makedirs(d)
        open(os.path.join(d, "stat"), "w").write("%d (%s) S %d 0 0 0 0\n" % (pid, comm, ppid))
        open(os.path.join(d, "cmdline"), "wb").write(cmd.replace(" ", "\0").encode())
        open(os.path.join(d, "status"), "w").write("Name:\t%s\nVmRSS:\t%d kB\n" % (comm, rss))


PROCS = {
    100: (1, "rootlesskit", "rootlesskit " + MARK + " --net=slirp4netns", 1200),
    101: (100, "exe", "/proc/self/exe " + MARK, 900),
    102: (101, "dockerd", "dockerd", 20000),
    103: (102, "containerd", "/usr/bin/containerd --config /run/user/%d/docker/containerd/containerd.toml" % UID, 14000),
    104: (100, "slirp4netns", "slirp4netns --mtu 65520", 2000),
    200: (1, "weed", "/home/x/cold/bin/weed volume", 120000),
    201: (1, "dockerd", "dockerd --mine-is-not-rootless", 500),  # same name, not below rootlesskit: must NOT be touched
}


class GuardTest(unittest.TestCase):
    def test_tree_is_exactly_the_rootless_docker_below_rootlesskit(self):
        with tempfile.TemporaryDirectory() as root:
            fake_proc(root, PROCS)
            procs = dg.read_procs(root, UID)
            self.assertEqual(dg.docker_tree(procs, UID), {100, 101, 102, 103, 104})

    def test_other_uid_state_dir_is_not_ours(self):
        with tempfile.TemporaryDirectory() as root:
            fake_proc(root, {100: (1, "rootlesskit", "rootlesskit --state-dir=/run/user/%d/dockerd-rootless" % (UID + 1), 10)})
            self.assertEqual(dg.docker_tree(dg.read_procs(root, UID), UID), set())

    def test_stop_signals_only_the_tree_and_escalates(self):
        sent = []
        tree = {100, 101}
        n, k = dg.stop(tree, {}, term_wait=1.0, kill=lambda pid, sig: sent.append((pid, sig)), alive=lambda pid: pid == 101, sleep=lambda s: None)
        self.assertEqual((n, k), (2, 1))
        self.assertEqual([p for p, s in sent if s == signal.SIGTERM], [100, 101])
        self.assertEqual([p for p, s in sent if s == signal.SIGKILL], [101])
        self.assertNotIn(200, [p for p, s in sent])

    def test_nothing_to_do_returns_zero_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            fake_proc(root, {200: PROCS[200]})
            lines = []
            self.assertEqual(dg.run(uid=UID, proc_root=root, log=lines.append), 0)
            self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
