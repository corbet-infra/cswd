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


if __name__ == "__main__":
    unittest.main()
