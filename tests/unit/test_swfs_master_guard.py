# SPDX-License-Identifier: LGPL-3.0-or-later
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

spec = importlib.util.spec_from_file_location("mg", os.path.join(os.path.dirname(__file__), "../../roles/seaweedfs_volume/files/swfs-master-guard.py"))
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)

SPEC = ",".join("192.0.2.%d:%d.%d" % (i + 1, 9300 + i, 19300 + i) for i in range(7))


def serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Answer(BaseHTTPRequestHandler):
    code, body = 200, b'{"Leader":"a:1.2","Peers":[],"MaxVolumeId":5}'

    def do_GET(self):
        self.send_response(self.code)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


class GuardTest(unittest.TestCase):
    def test_parse_masters_keeps_order_and_http_port(self):
        m = mg.parse_masters(SPEC)
        self.assertEqual(len(m), 7)
        self.assertEqual(m[0], ("192.0.2.1", 9300))
        self.assertEqual(m[6], ("192.0.2.7", 9306))

    def test_quorum_margin(self):
        self.assertEqual(mg.need_healthy_others(7), 5)  # a quorum of 4 survives the restart and one more master may be down
        self.assertEqual(mg.need_healthy_others(5), 4)
        self.assertEqual(mg.need_healthy_others(3), 2)  # three masters: both others must answer
        self.assertEqual(mg.need_healthy_others(1), 0)

    def test_decide_waits_for_the_grace_time(self):
        self.assertEqual(mg.decide(100, 180, 6, 7, 1000, [], 1800, 3)[0], "wait")

    def test_decide_restarts_a_silent_master_in_a_healthy_cluster(self):
        self.assertEqual(mg.decide(200, 180, 6, 7, 1000, [], 1800, 3)[0], "restart")
        self.assertEqual(mg.decide(200, 180, 5, 7, 1000, [], 1800, 3)[0], "restart")  # one other master is down as well: still allowed

    def test_decide_never_restarts_a_degraded_cluster(self):
        action, why = mg.decide(10000, 180, 4, 7, 1000, [], 1800, 3)
        self.assertEqual(action, "skip")
        self.assertIn("quorum", why)

    def test_decide_cooldown_and_daily_limit(self):
        self.assertEqual(mg.decide(200, 180, 6, 7, 5000, [4000], 1800, 3)[0], "skip")     # 1000 s ago
        self.assertEqual(mg.decide(200, 180, 6, 7, 5000, [1000], 1800, 3)[0], "restart")  # 4000 s ago
        self.assertEqual(mg.decide(200, 180, 6, 7, 90000, [10000, 30000, 60000], 1800, 3)[0], "skip")  # 3 in 24 h
        self.assertEqual(mg.decide(200, 180, 6, 7, 100000, [10000, 30000, 60000], 1800, 3)[0], "restart")  # the oldest is past 24 h

    def test_probe_accepts_a_json_object_with_200(self):
        srv = serve(Answer)
        self.assertEqual(mg.probe("127.0.0.1", srv.server_address[1], 2), (True, ""))
        srv.shutdown()

    def test_probe_rejects_errors_and_junk(self):
        class Bad(Answer):
            code = 500
        class Junk(Answer):
            body = b"<html>not json</html>"
        for cls in (Bad, Junk):
            srv = serve(cls)
            ok, why = mg.probe("127.0.0.1", srv.server_address[1], 2)
            self.assertFalse(ok, why)
            srv.shutdown()

    def test_probe_gives_up_on_a_master_that_accepts_and_never_answers(self):
        # the hung master of 2026-11-12: the connection is accepted, nothing comes back
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(5)
        t0 = time.monotonic()
        ok, why = mg.probe("127.0.0.1", ls.getsockname()[1], 0.5)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertIn("no complete answer", why)
        ls.close()

    def test_probe_connection_refused(self):
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        port = ls.getsockname()[1]
        ls.close()
        self.assertFalse(mg.probe("127.0.0.1", port, 1)[0])

    def test_probe_many_is_parallel_and_bounded(self):
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(5)
        srv = serve(Answer)
        targets = [("127.0.0.1", ls.getsockname()[1]), ("127.0.0.1", srv.server_address[1])] * 3
        t0 = time.monotonic()
        res = mg.probe_many(list(dict.fromkeys(targets)), 0.5)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(sorted(v[0] for v in res.values()), [False, True])
        srv.shutdown()
        ls.close()

    def test_state_roundtrip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            self.assertEqual(mg.load_state(p), [])
            mg.save_state(p, [1.0, 2.5])
            self.assertEqual(mg.load_state(p), [1.0, 2.5])
            with open(p, "w") as f:
                f.write("{broken")
            self.assertEqual(mg.load_state(p), [])


if __name__ == "__main__":
    unittest.main()
