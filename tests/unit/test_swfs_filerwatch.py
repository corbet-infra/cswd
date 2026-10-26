# SPDX-License-Identifier: LGPL-3.0-or-later
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location("fw", os.path.join(os.path.dirname(__file__), "../../roles/seaweedfs_volume/files/swfs-filerwatch.py"))
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)


def run(f, samples):
    """samples: list of (t, reachable); returns list of (t, action)"""
    out = []
    for t, r in samples:
        a = f.step(t, r)
        if a:
            out.append((t, a))
    return out


class FenceTest(unittest.TestCase):
    def test_majority_is_four_of_seven(self):
        self.assertEqual(fw.Fence(7).majority, 4)

    def test_never_fences_with_majority(self):
        f = fw.Fence(7)
        self.assertEqual(run(f, [(t, 4) for t in range(0, 600, 10)]), [])

    def test_mirror_case_unreachable_from_peers_does_not_fence(self):
        # this box sees 6 of 7 (itself and five peers); that peers cannot see this box is invisible here by design
        f = fw.Fence(7)
        self.assertEqual(run(f, [(t, 6) for t in range(0, 600, 10)]), [])

    def test_fences_after_sixty_seconds_below_majority(self):
        f = fw.Fence(7)
        self.assertEqual(run(f, [(t, 2) for t in range(0, 100, 10)]), [(60, "fence")])

    def test_short_dip_does_not_fence(self):
        f = fw.Fence(7)
        samples = [(t, 2) for t in range(0, 50, 10)] + [(t, 7) for t in range(50, 200, 10)]
        self.assertEqual(run(f, samples), [])

    def test_unfence_needs_five_minutes_of_majority(self):
        f = fw.Fence(7)
        acts = run(f, [(t, 2) for t in range(0, 70, 10)] + [(t, 7) for t in range(70, 500, 10)])
        self.assertEqual(acts, [(60, "fence"), (370, "unfence")])

    def test_dip_during_fence_restarts_the_good_timer(self):
        f = fw.Fence(7)
        samples = [(t, 2) for t in range(0, 70, 10)] + [(t, 7) for t in range(70, 300, 10)] + [(300, 1)] + [(t, 7) for t in range(310, 700, 10)]
        self.assertEqual(run(f, samples), [(60, "fence"), (610, "unfence")])

    def test_refence_soon_after_unfence_doubles_the_wait(self):
        f = fw.Fence(7)
        run(f, [(t, 2) for t in range(0, 70, 10)] + [(t, 7) for t in range(70, 400, 10)])
        self.assertFalse(f.fenced)
        run(f, [(t, 2) for t in range(400, 470, 10)])
        self.assertTrue(f.fenced)
        self.assertEqual(f.wait, 600)

    def test_wait_is_capped(self):
        f = fw.Fence(7, max_unfence_seconds=500)
        f.wait = 400
        f.last_unfence = 0
        run(f, [(t, 2) for t in range(10, 80, 10)])
        self.assertEqual(f.wait, 500)

    def test_started_fenced_from_flag_file(self):
        f = fw.Fence(7, fenced=True)
        self.assertEqual(run(f, [(t, 7) for t in range(0, 400, 10)]), [(300, "unfence")])


if __name__ == "__main__":
    unittest.main()
