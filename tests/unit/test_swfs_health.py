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


if __name__ == "__main__":
    unittest.main()
