# SPDX-License-Identifier: LGPL-3.0-or-later
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location("sh", os.path.join(os.path.dirname(__file__), "../../roles/seaweedfs_volume/files/swfs-health.py"))
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)

VL = "\n".join(
    "          ec volume id:%d collection:x shards:[%s] sizes:[...] total:1 MiB" % (v, " ".join(str(i) for i in s))
    for v, shards in {3: [[0, 7], [1, 8], [2, 9], [3, 10], [4, 11], [5, 12], [6, 13]], 4: [[0, 7], [1, 8], [2, 9]]}.items()
    for s in shards
)


class HealthTest(unittest.TestCase):
    def test_ec_short_counts_volumes_below_fourteen_shards(self):
        n, ids, total = sh.parse_ec_short(VL)
        self.assertEqual((n, ids, total), (1, ["4"], 2))

    def test_no_ec_volumes(self):
        self.assertEqual(sh.parse_ec_short("nothing here\n"), (0, [], 0))

    def test_filers(self):
        self.assertEqual(sh.parse_filers("* filers 7\n  * a\n"), 7)
        self.assertIsNone(sh.parse_filers("error: x"))

    def test_state_dump_is_json(self):
        s = sh.State()
        s.set(a=1)
        import json
        self.assertEqual(json.loads(s.dump())["a"], 1)

    def test_provider_docker_processes_counts_only_docker_and_mib(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            def pid(n, comm, cmdline, rss_kib):
                d = os.path.join(root, str(n))
                os.mkdir(d)
                with open(os.path.join(d, "comm"), "w") as f:
                    f.write(comm + "\n")
                with open(os.path.join(d, "cmdline"), "wb") as f:
                    f.write(cmdline.replace(" ", "\0").encode() + b"\0")
                with open(os.path.join(d, "status"), "w") as f:
                    f.write("Name:\t%s\nVmRSS:\t%d kB\n" % (comm, rss_kib))
            pid(10, "dockerd", "dockerd", 20480)
            pid(11, "bash", "bash -c sleep 1", 999999)
            pid(12, "exe", "/proc/self/exe --state-dir=/run/user/1000/dockerd-rootless", 3072)
            pid(13, "containerd", "/usr/bin/containerd --config /run/user/1000/docker/containerd/containerd.toml", 7168)
            os.mkdir(os.path.join(root, "notapid"))
            self.assertEqual(sh.provider_docker_processes(root, uid=os.getuid()), (3, 30))

    def test_provider_docker_processes_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(sh.provider_docker_processes(root, uid=os.getuid()), (0, 0))

    def test_s3_gateway_process_counts_fds_and_mib(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            def pid(n, comm, cmdline, rss_kib, swap_kib, fds):
                d = os.path.join(root, str(n))
                os.mkdir(d)
                with open(os.path.join(d, "comm"), "w") as f:
                    f.write(comm + "\n")
                with open(os.path.join(d, "cmdline"), "wb") as f:
                    f.write(cmdline.replace(" ", "\0").encode() + b"\0")
                with open(os.path.join(d, "status"), "w") as f:
                    f.write("Name:\t%s\nVmRSS:\t%d kB\nVmSwap:\t%d kB\n" % (comm, rss_kib, swap_kib))
                os.mkdir(os.path.join(d, "fd"))
                for i in range(fds):
                    os.symlink("/dev/null", os.path.join(d, "fd", str(i)))
            pid(20, "weed", "/home/u/cold/bin/weed-4.51/weed -logtostderr s3 -filer=1.2.3.4:1.2", 51200, 1024, 7)
            pid(21, "weed", "/home/u/cold/bin/weed-4.51/weed -logtostderr volume -mserver=1.2.3.4:1", 999999, 0, 99)
            pid(22, "python3", "python3 swfs-health.py --own-s3 https://1.2.3.4:1/health -filer=", 999999, 0, 99)
            self.assertEqual(sh.s3_gateway_process(root, uid=os.getuid()), (7, 50, 1))

    def test_s3_gateway_process_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(sh.s3_gateway_process(root, uid=os.getuid()), (None, None, None))

    def test_provider_docker_policy_reads_the_root_store(self):
        import json
        import pwd
        import tempfile
        user = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, user + ".json")
            with open(path, "w") as f:
                json.dump({"dockerEnabled": True}, f)
            self.assertIs(sh.provider_docker_policy(config_dir=root), True)
            with open(path, "w") as f:
                json.dump({"dockerEnabled": "false"}, f)
            self.assertIs(sh.provider_docker_policy(config_dir=root), False)
            with open(path, "w") as f:
                json.dump({}, f)
            self.assertIsNone(sh.provider_docker_policy(config_dir=root))
            os.unlink(path)
            self.assertIsNone(sh.provider_docker_policy(config_dir=root))

CS = """
cluster:
\tid:       topo
\tstatus:   LOCKED by 192.0.2.1
\tnodes:    7
\ttopology: 1 DC, 7 disks on 7 racks

volumes:
\ttotal:    362 volumes, 5 collections
\tmax size: 2.1 GB
\tregular:  354/1,050 volumes on 735 replicas, 1,729 writable (99.18%), 6 read-only (0.81%)
"""
EMPTY_CS = CS.replace("nodes:    7", "nodes:    0").replace("1,729 writable", "0 writable")


class WritePathTest(unittest.TestCase):
    def test_cluster_status_parse(self):
        self.assertEqual(sh.parse_cluster_status(CS), (7, 1729))
        self.assertEqual(sh.parse_cluster_status(EMPTY_CS), (0, 0))
        self.assertEqual(sh.parse_cluster_status("error: no master"), (None, None))

    def test_write_path_problems(self):
        self.assertEqual(sh.write_path_problems(7, 729, 7), [])
        self.assertEqual(sh.write_path_problems(0, 0, 7), ["leader sees 0 of 7 data nodes", "leader has no writable volume"])
        self.assertEqual(sh.write_path_problems(6, 10, 7), ["leader sees 6 of 7 data nodes"])
        self.assertEqual(len(sh.write_path_problems(None, None, 7)), 1)

    def _args(self):
        import argparse
        return argparse.Namespace(weed="weed", masters="m", bucket="health", cacert=None, expected_nodes=0)

    def _check(self, shell, probe):
        orig = (sh.weed_shell, sh.lag_probe)
        sh.weed_shell = shell
        sh.lag_probe = probe
        try:
            state = sh.State()
            peers = ["https://p%d" % i for i in range(6)]
            sh.cluster_check(state, self._args(), "a", "s", "https://own", peers)
            import json
            return json.loads(state.dump())
        finally:
            sh.weed_shell, sh.lag_probe = orig

    @staticmethod
    def _shell_ok(weed, masters, commands, timeout=90):
        if commands == ["cluster.status"]:
            return CS
        if commands == ["cluster.ps", "cluster.ps"]:
            return "* filers 7\n"
        return ""

    def test_healthy_round_is_green_and_clears_an_old_error(self):
        out = self._check(self._shell_ok, lambda *a, **k: (0.4, [], None, 0.3))
        self.assertEqual((out["filer_peers_not_answering"], out["topology_nodes"], out["writable_volumes"], out["cluster_error"]), (0, 7, 1729, None))

    def test_leader_without_nodes_is_red_even_when_the_marker_got_through(self):
        # the 2026-11-12 state seen from the leader: 0 of 7 nodes, 0 writable volumes
        def shell(weed, masters, commands, timeout=90):
            return EMPTY_CS if commands == ["cluster.status"] else self._shell_ok(weed, masters, commands)
        out = self._check(shell, lambda *a, **k: (0.4, [], None, 0.3))
        self.assertEqual(out["filer_peers_not_answering"], 2)
        self.assertIn("leader sees 0 of 7 data nodes", out["filer_peers_missing"])

    def test_refused_marker_is_red(self):
        out = self._check(self._shell_ok, lambda *a, **k: (None, ["own gateway did not accept the marker"], None, 0.3))
        self.assertEqual(out["filer_peers_not_answering"], 1)

    def test_a_check_that_cannot_run_is_red_and_drops_the_old_values(self):
        import subprocess
        def shell(weed, masters, commands, timeout=90):
            raise subprocess.TimeoutExpired("weed", 90)
        out = self._check(shell, lambda *a, **k: (0.4, [], None, 0.3))
        self.assertGreaterEqual(out["filer_peers_not_answering"], 1)
        self.assertIn("cluster check failed", out["filer_peers_missing"][0])
        self.assertIsNone(out["filer_lag_max_s"])
        self.assertIsNone(out["topology_nodes"])


class GuardedCurlTest(unittest.TestCase):
    def test_503_is_retried_then_accepted(self):
        codes = iter(["503", "503", "200"])
        orig = (sh.sigv4_curl, sh.time.sleep)
        sh.sigv4_curl = lambda *a, **k: next(codes)
        sh.time.sleep = lambda s: None
        try:
            self.assertEqual(sh.guarded_curl("u", "PUT", "a", "s", sh.time.time(), 90), "200")
        finally:
            sh.sigv4_curl, sh.time.sleep = orig

    def test_persistent_503_is_reported_busy_not_failed(self):
        orig = sh.sigv4_curl
        sh.sigv4_curl = lambda *a, **k: "503"
        try:
            self.assertEqual(sh.lag_probe("o", ["p"], "b", "a", "s", None, limit=0), (None, [], "own gateway write refused (503 upload guard) for 0s", 0.0))
        finally:
            sh.sigv4_curl = orig


class UnansweredWriteTest(unittest.TestCase):
    def test_unanswered_write_ends_busy_not_peers(self):
        orig = (sh.sigv4_curl, sh.time.sleep)
        sh.sigv4_curl = lambda *a, **k: "000"
        sh.time.sleep = lambda s: None
        try:
            lag, missing, busy, _ = sh.lag_probe("o", ["p"], "b", "a", "s", None, limit=0)
        finally:
            sh.sigv4_curl, sh.time.sleep = orig
        self.assertEqual((lag, missing), (None, []))
        self.assertIn("unanswered", busy)


class PsiTest(unittest.TestCase):
    def test_host_psi_reads_some_avg60(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        open(os.path.join(d, "memory"), "w").write("some avg10=0.46 avg60=0.79 avg300=1.55 total=1\nfull avg10=0.45 avg60=0.66 avg300=1.25 total=1\n")
        self.assertEqual(sh.host_psi("memory", d), 0.79)
        self.assertIsNone(sh.host_psi("io", d))


if __name__ == "__main__":
    unittest.main()
