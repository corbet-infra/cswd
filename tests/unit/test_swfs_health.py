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


class UnitRowsTest(unittest.TestCase):
    def _proc(self, root, pid, ppid, comm, cmd, kib, uid=None):
        import os
        d = os.path.join(root, str(pid))
        os.makedirs(d)
        open(os.path.join(d, "stat"), "w").write("%d (%s) S %d 0 0 0\n" % (pid, comm, ppid))
        open(os.path.join(d, "cmdline"), "wb").write(cmd.replace(" ", "\0").encode())
        open(os.path.join(d, "status"), "w").write("VmRSS:\t%d kB\nRssAnon:\t%d kB\nRssFile:\t%d kB\n" % (kib, kib - 4096 if comm == "weed" else kib, 4096 if comm == "weed" else 0))

    def test_classify(self):
        c = sh.classify_process
        self.assertEqual(c("weed", "/x/weed -logtostderr filer -port=1"), "weed-filer")
        self.assertEqual(c("weed", "/x/weed -logtostderr s3 -filer=a"), "weed-s3")
        self.assertEqual(c("otelcol-corbet", "otelcol-corbet --config=x"), "otelcol")
        self.assertEqual(c("python3", "/usr/bin/python3 /h/cold/bin/swfs-master-guard.py"), "swfs-health")
        self.assertEqual(c("php-cgi", "/usr/bin/php-cgi -c x"), "panel")
        self.assertEqual(c("exe", "/proc/self/exe --state-dir=/run/user/1/dockerd-rootless"), "docker")
        self.assertIsNone(c("bash", "bash -s"))

    def test_rows_sum_children_of_radar_and_report_zero_for_absent_units(self):
        import os, tempfile
        uid = os.getuid()
        with tempfile.TemporaryDirectory() as root:
            self._proc(root, 10, 1, "weed", "/x/weed -logtostderr master", 36 * 1024)
            self._proc(root, 20, 1, "python3", "/usr/bin/python3 /h/radar-collector.py", 8 * 1024)
            self._proc(root, 21, 20, "curl", "curl https://x", 6 * 1024)
            self._proc(root, 30, 1, "bash", "bash", 3 * 1024)
            rows = {r["unit"]: r for r in sh.unit_rows(root, uid, {"weed-master": 80, "radar": 20})}
        self.assertEqual(rows["weed-master"], {"unit": "weed-master", "rss_mib": 32.0, "budget_mib": 80})  # anonymous part only: 36 MiB rss minus 4 MiB of shared binary
        self.assertEqual(rows["weed-binary"]["rss_mib"], 4.0)
        self.assertEqual(rows["radar"]["rss_mib"], 14.0)
        self.assertEqual(rows["radar"]["budget_mib"], 20)
        self.assertEqual(rows["docker"]["rss_mib"], 0)
        self.assertEqual(rows["panel"]["budget_mib"], 0)
        self.assertEqual(len(rows), len(sh.UNIT_NAMES))

    def test_budgets_file(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "b.json")
        open(p, "w").write('{"weed-filer": 200, "note": "x"}')
        self.assertEqual(sh.load_budgets(p), {"weed-filer": 200})
        self.assertEqual(sh.load_budgets(os.path.join(d, "none")), {})


class QuotaTest(unittest.TestCase):
    def test_provider_quota_text(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "q")
        open(p, "w").write("Disk quotas for user richc (uid 1001):\n     Filesystem   space   quota   limit   grace   files   quota   limit   grace\n       /dev/vda    297G   1241G   1552G           66437    621k    776k\n")
        q = sh.provider_quota(p)
        self.assertEqual(q["quota_disk_used_gib"], 297.0)
        self.assertEqual(q["quota_disk_quota_gib"], 1241.0)
        self.assertEqual(q["quota_files_used"], 66437)
        self.assertEqual(q["quota_files_quota"], 621000)
        self.assertAlmostEqual(q["quota_disk_fraction"], 297 / 1241, places=4)
        self.assertEqual(sh.provider_quota(os.path.join(d, "none")), {})


class ProviderFsTest(unittest.TestCase):
    def test_fraction_and_sizes(self):
        import tempfile
        r = sh.provider_fs(tempfile.gettempdir())
        self.assertTrue(0 < r["provider_fs_fraction"] <= 1)
        self.assertEqual(sh.provider_fs("/nonexistent/path/x"), {})


class CgroupMemoryTest(unittest.TestCase):
    def test_anon_is_total_rss_only_and_soft_limit_unlimited_is_minus_one(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        MiB = 1048576
        for name, val in {"usage_in_bytes": 1301 * MiB, "max_usage_in_bytes": 1310 * MiB, "limit_in_bytes": 1250 * MiB, "soft_limit_in_bytes": 9223372036854771712, "kmem.usage_in_bytes": 69 * MiB}.items():
            open(os.path.join(d, "memory." + name), "w").write(str(val))
        open(os.path.join(d, "memory.stat"), "w").write("cache 0\ntotal_cache %d\ntotal_rss %d\ntotal_rss_huge %d\ntotal_shmem 0\ntotal_mapped_file %d\ntotal_active_file %d\ntotal_inactive_file %d\ntotal_swap %d\n" % (651 * MiB, 458 * MiB, 222 * MiB, 123 * MiB, 400 * MiB, 249 * MiB, 16 * MiB))
        m = sh.cgroup_memory_mib(1000, base=d)
        self.assertEqual(m["ram_anon_mib"], 458)  # not 458 + 222: rss already includes the transparent hugepages
        self.assertEqual(m["ram_rss_huge_mib"], 222)
        self.assertEqual(m["ram_cache_mib"], 651)
        self.assertEqual(m["ram_usage_mib"], 1301)
        self.assertEqual(m["ram_soft_limit_mib"], -1)
        self.assertEqual(m["ram_kmem_mib"], 69)
        self.assertEqual(m["ram_active_file_mib"] + m["ram_inactive_file_mib"], 649)


class CacheGovernorTest(unittest.TestCase):
    def _files(self, d, spec, now):
        import os
        for name, age in spec.items():
            p = os.path.join(d, name)
            open(p, "w").write("x")
            os.utime(p, (now - age, now - age))

    def test_candidates_are_old_clean_volume_files_coldest_first(self):
        import tempfile
        now = 1_000_000.0
        d = tempfile.mkdtemp()
        self._files(d, {"a_1.dat": 5000, "a_2.dat": 9000, "a_3.dat": 100, "a_2.idx": 8000, "a_4.ec03": 7000, "weed.log": 99999, "x.ldb": 99999, "a_5.vif": 99999}, now)
        got = [__import__("os").path.basename(p) for p in sh.trim_candidates(d, now, 600)]
        self.assertEqual(got, ["a_2.dat", "a_2.idx", "a_4.ec03", "a_1.dat"])  # a_3.dat is too fresh; log, leveldb and .vif are never touched

    def test_trim_stops_below_low_and_does_nothing_below_high(self):
        import tempfile
        now = 1_000_000.0
        d = tempfile.mkdtemp()
        self._files(d, {"a_%d.dat" % i: 5000 + i for i in range(10)}, now)
        level = {"u": 950.0}
        advised = []

        def usage(_uid):
            return level["u"]

        def advise(p):
            advised.append(p)
            level["u"] -= 30.0
        lines = []
        n, freed, wall, cpu = sh.trim_once(d, 0, 900, 850, 600, now=now, usage=usage, advise=advise, log=lines.append)
        self.assertGreaterEqual(wall, 0.0)
        self.assertGreaterEqual(cpu, 0.0)
        self.assertEqual(n, 4)  # 950 -> 920 -> 890 -> 860 -> 830 (< 850) stops after the fourth
        self.assertEqual(freed, 120.0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(sh.trim_once(d, 0, 900, 850, 600, now=now, usage=lambda _u: 800.0, advise=advise, log=lines.append), (0, 0.0, 0.0, 0.0))


class LdbGovernorTest(unittest.TestCase):
    def test_only_old_ldb_tables_are_candidates(self):
        import os, tempfile
        now = 1_000_000.0
        d = tempfile.mkdtemp()
        sub = os.path.join(d, "01")
        os.makedirs(sub)
        for name, age in {"001221.ldb": 5000, "001222.ldb": 100, "MANIFEST-000005": 99999, "CURRENT": 99999, "LOG": 99999, "000009.log": 99999, "LOCK": 99999}.items():
            p = os.path.join(sub, name)
            open(p, "w").write("x")
            os.utime(p, (now - age, now - age))
        got = [os.path.basename(p) for p in sh.ldb_candidates(d, now, 120)]
        self.assertEqual(got, ["001221.ldb"])

    def test_trim_once_tries_volume_files_then_ldb_tables(self):
        import os, tempfile
        now = 1_000_000.0
        vd = tempfile.mkdtemp()
        ld = tempfile.mkdtemp()
        for d, name in ((vd, "a_1.dat"), (ld, "000001.ldb")):
            p = os.path.join(d, name)
            open(p, "w").write("x")
            os.utime(p, (now - 5000, now - 5000))
        seen = []
        sh.trim_once(vd, 0, 900, 850, 120, now=now, usage=lambda _u: 950.0, advise=seen.append, log=lambda m: None, ldb_dir=ld)
        self.assertEqual([os.path.basename(p) for p in seen], ["a_1.dat", "000001.ldb"])


class TrimCadenceTest(unittest.TestCase):
    def test_fast_above_the_threshold_slow_below_and_off_by_default(self):
        self.assertEqual(sh.trim_wait(900, 0.03, 800, 5, 30), 5)
        self.assertEqual(sh.trim_wait(700, 0.03, 800, 5, 30), 30)
        self.assertEqual(sh.trim_wait(900, 0.03, 0, 5, 30), 30)   # off
        self.assertEqual(sh.trim_wait(None, 0.03, 800, 5, 30), 30)  # unreadable usage

    def test_a_slow_pass_backs_off_ten_times_its_duration(self):
        self.assertEqual(sh.trim_wait(900, 1.5, 800, 5, 30), 15)   # fast 5 s, but 10 x 1.5 s = 15 s wins
        self.assertEqual(sh.trim_wait(900, 4.0, 800, 5, 30), 40)
        self.assertEqual(sh.trim_wait(900, 0.9, 800, 5, 30), 5)    # under 1 s: no back-off


class ClusterSeriesTest(unittest.TestCase):
    VL = (
        "Topology volumeSizeLimit:8192 MB hdd(volume:1012/1050)\n"
        "          volume Id:485, Size:9650176, ReplicaPlacement:010, Collection:, Version:3, Ttl:, FileCount:504, DeleteCount:0, DeletedByteCount:0, ReadOnly:true, ModifiedAtSecond:1\n"
        "          volume Id:595, Size:675049472, ReplicaPlacement:010, Collection:cold-jfs-c, Version:3, Ttl:, FileCount:4114, DeleteCount:21, DeletedByteCount:15, ReadOnly:true, ModifiedAtSecond:1\n"
        "          volume Id:600, Size:6000000000, ReplicaPlacement:010, Collection:cold-jfs-a, Version:3, Ttl:, FileCount:9, DeleteCount:0, DeletedByteCount:0, ReadOnly:true, ModifiedAtSecond:1\n"
        "          volume Id:600, Size:6000000000, ReplicaPlacement:010, Collection:cold-jfs-a, Version:3, Ttl:, FileCount:9, DeleteCount:0, DeletedByteCount:0, ReadOnly:true, ModifiedAtSecond:1\n"
        "          volume Id:601, Size:100, ReplicaPlacement:010, Collection:x, Version:3, Ttl:, FileCount:1, DeleteCount:0, DeletedByteCount:0, ReadOnly:false, ModifiedAtSecond:1\n"
    )

    def test_readonly_volumes_full_ones_are_normal_small_ones_abnormal(self):
        limit = sh.parse_volume_size_limit_mb(self.VL)
        self.assertEqual(limit, 8192)
        self.assertEqual(sh.parse_readonly_volumes(self.VL, limit), (3, 2, ["485", "595"]))  # 600 is over half of 8 GB: waiting for ec.encode
        self.assertEqual(sh.parse_readonly_volumes(self.VL, None)[1], 3)  # no limit known: all abnormal

    def test_raft_peers(self):
        self.assertEqual(sh.parse_raft_peers("the raft cluster has 7 servers\n  * a (Leader)\n"), 7)
        self.assertIsNone(sh.parse_raft_peers("nothing"))

    def test_s3_counters_parse_and_unreachable_endpoint(self):
        import http.server, threading
        body = (b'# HELP x\nSeaweedFS_s3_request_total{bucket="a",code="200",type="GET"} 10\nSeaweedFS_s3_request_total{bucket="a",code="503",type="PUT"} 3\n'
                b'SeaweedFS_s3_request_total{bucket="b",code="500",type="GET"} 2\nSeaweedFS_s3_request_total{bucket="b",code="404",type="GET"} 5\n')

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertEqual(sh.s3_request_counters(srv.server_address[1]), (20, 5))
        finally:
            srv.shutdown()
        self.assertEqual(sh.s3_request_counters(1), (None, None))

    def test_leader_lookup(self):
        import http.server, threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"IsLeader":true,"Leader":"x"}')

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            masters = "9.9.9.9:1.2,10.0.0.5:%d.77,8.8.8.8:3.4" % srv.server_address[1]
            self.assertTrue(sh.own_master_is_leader(masters, "10.0.0.5"))
            self.assertIsNone(sh.own_master_is_leader(masters, "10.0.0.6"))  # not in the list: cannot tell
        finally:
            srv.shutdown()


class FailureSignalsTest(unittest.TestCase):
    def test_unit_states_parse_active_restarts_and_failed(self):
        out = ("Id=seaweed-master.service\nActiveState=active\nResult=success\nNRestarts=0\n\n"
               "Id=seaweed-volume.service\nActiveState=activating\nResult=exit-code\nNRestarts=3\n\n"
               "Id=seaweed-filer.service\nActiveState=failed\nResult=signal\nNRestarts=1\n")
        st = sh.unit_states(run=lambda c: out)
        self.assertEqual(st["weed-master"], (1, 0, 0))
        self.assertEqual(st["weed-volume"], (0, 3, 1))
        self.assertEqual(st["weed-filer"], (0, 1, 1))
        self.assertNotIn("weed-s3", st)
        self.assertEqual(sh.unit_states(run=lambda c: (_ for _ in ()).throw(OSError("no systemctl"))), {})

    def test_rows_carry_the_states_and_oom_kill_is_read(self):
        import os, tempfile
        rows = {r["unit"]: r for r in sh.unit_rows(tempfile.mkdtemp(), os.getuid(), {}, None, {"weed-s3": (1, 2, 0)})}
        self.assertEqual((rows["weed-s3"]["active"], rows["weed-s3"]["restarts"], rows["weed-s3"]["failed"]), (1, 2, 0))
        self.assertNotIn("restarts", rows["panel"])
        d = tempfile.mkdtemp()
        for n in ("usage_in_bytes", "max_usage_in_bytes", "limit_in_bytes", "soft_limit_in_bytes", "kmem.usage_in_bytes"):
            open(os.path.join(d, "memory." + n), "w").write("1048576")
        open(os.path.join(d, "memory.oom_control"), "w").write("oom_kill_disable 0\nunder_oom 0\noom_kill 4\n")
        open(os.path.join(d, "memory.stat"), "w").write("total_rss 1048576\n")
        m = sh.cgroup_memory_mib(1, base=d)
        self.assertEqual((m["ram_oom_kill"], m["ram_under_oom"]), (4, 0))


class ReplicationAndLocalReadonlyTest(unittest.TestCase):
    def test_replica_counts_against_the_placement(self):
        vl = "".join("          volume Id:%d, Size:1, ReplicaPlacement:010, Collection:c, Version:3\n" % v for v in (1, 1, 2, 3, 3, 3))
        vl += "          volume Id:4, Size:1, ReplicaPlacement:000, Collection:c, Version:3\n"
        under, over, u, o = sh.parse_replica_counts(vl)
        self.assertEqual((under, over, u, o), (1, 1, ["2"], ["3"]))

    def test_local_volume_readonly_from_status(self):
        import http.server, json, threading
        body = json.dumps({"Volumes": [{"Id": 1, "Size": 100, "ReadOnly": True}, {"Id": 2, "Size": 6 * 1024 ** 3, "ReadOnly": True}, {"Id": 3, "Size": 5, "ReadOnly": False}]}).encode()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/status" % srv.server_address[1]
            self.assertEqual(sh.local_volume_readonly(url, None, None, None, 8192), (2, 1))  # 6 GiB is over half of 8192 MB: normal; 100 B is abnormal
        finally:
            srv.shutdown()
        self.assertEqual(sh.local_volume_readonly("http://127.0.0.1:1/status", None, None, None, 8192), (None, None))

    def test_units_not_loaded_are_not_reported(self):
        out = ("Id=seaweed-master.service\nLoadState=loaded\nActiveState=active\nResult=success\nNRestarts=0\n\n"
               "Id=seaweed-s3.service\nLoadState=not-found\nActiveState=inactive\nResult=success\nNRestarts=0\n")
        st = sh.unit_states(run=lambda c: out)
        self.assertIn("weed-master", st)
        self.assertNotIn("weed-s3", st)


if __name__ == "__main__":
    unittest.main()
