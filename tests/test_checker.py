"""The checker passes the reference controller, and catches every deliberately broken one.

A checker that passes everything proves nothing, so each broken mode of reference_hub.py must make its own check fail.
Run from the repository root:  python3 -m unittest discover -s tests -t .
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pipehub_check import CHECKS, run_checks  # noqa: E402
from reference_hub import BROKEN_MODES, PipeHubEmulator  # noqa: E402

PIPES, OUTLETS = ["kitchen1", "toilet"], ["tub"]
FAST = dict(lease_s=1.0, travel_max_s=1.0, slack_s=0.6)        # short timings so the whole suite runs in a few minutes

# which check must catch each broken mode
EXPECTED = {
    "ignore_lease": {"C09", "C10"},
    "outlet_open_unleased": {"C06"},
    "no_cap": {"C08"},
    "no_auth": {"C01"},
    "wrong_rest": {"C10"},
    "rest_keeps_lease": {"C07"},
    "unknown_is_200": {"C04"},
    "sloppy_lease": {"C05"},
    "no_renew": {"C11"},
}


def run_against(broken="", token="t0k", url_token=None, travel=0.2, **kw):
    hub = PipeHubEmulator(PIPES, token, travel_s=travel, outlets=OUTLETS, broken=broken).start()
    try:
        return run_checks(hub.base_url, url_token or token, PIPES, OUTLETS, out=lambda *a: None, **dict(FAST, **kw))
    finally:
        hub.stop()


class Checker(unittest.TestCase):
    def test_the_reference_controller_passes_every_check(self):
        rep = run_against()
        self.assertTrue(rep["passed"], [(c["id"], c["detail"]) for c in rep["checks"] if c["status"] != "PASS"])
        self.assertEqual([c["id"] for c in rep["checks"]], [c[0] for c in CHECKS])
        self.assertTrue(all(c["status"] == "PASS" for c in rep["checks"]))

    def test_every_broken_mode_is_caught_by_its_check(self):
        self.assertEqual(set(EXPECTED), set(BROKEN_MODES))
        for mode, want in EXPECTED.items():
            with self.subTest(mode=mode):
                rep = run_against(mode)
                self.assertFalse(rep["passed"], mode)
                self.assertEqual(set(rep["failed"]), want, f"{mode}: expected exactly {sorted(want)} to fail")

    def test_slow_valves_pass_when_the_travel_limit_allows_them(self):
        rep = run_against(travel=0.8, lease_s=1.5, travel_max_s=2.0)
        self.assertTrue(rep["passed"], [(c["id"], c["detail"]) for c in rep["checks"] if c["status"] != "PASS"])

    def test_a_valve_slower_than_the_limit_fails(self):
        rep = run_against(travel=1.5, travel_max_s=0.5, only=["C07"])
        self.assertEqual(rep["failed"], ["C07"])

    def test_without_outlets_the_outlet_checks_are_skipped(self):
        hub = PipeHubEmulator(PIPES, "t", travel_s=0.1).start()
        try:
            rep = run_checks(hub.base_url, "t", PIPES, [], out=lambda *a: None, **FAST)
        finally:
            hub.stop()
        st = {c["id"]: c["status"] for c in rep["checks"]}
        self.assertEqual((st["C06"], st["C10"]), ("SKIP", "SKIP"))
        self.assertTrue(rep["passed"])

    def test_a_controller_left_away_from_rest_stops_the_run(self):
        hub = PipeHubEmulator(PIPES, "t", travel_s=0.0, outlets=OUTLETS).start()
        try:
            hub.handle("PUT", "/api/v1/pipes/toilet", {"want": "closed", "lease_s": None})
            rep = run_checks(hub.base_url, "t", PIPES, OUTLETS, out=lambda *a: None, **FAST)
        finally:
            hub.stop()
        self.assertEqual(rep["failed"], ["C03"])
        self.assertEqual(rep["checks"][-1]["id"], "C03")

    def test_plain_http_to_another_machine_fails_the_transport_check(self):
        from pipehub_check import Hub, Run, c13, Fail
        r = Run(Hub("http://192.168.1.20:8099", "t"), PIPES, OUTLETS, 1, 1, 1)
        r.allow_insecure_lan = False
        with self.assertRaises(Fail):
            c13(r)
        r.allow_insecure_lan = True
        self.assertIn("insecure", c13(r))
        self.assertEqual(c13(Run(Hub("https://hub.example", "t"), PIPES, OUTLETS, 1, 1, 1)), "https")


if __name__ == "__main__":
    unittest.main()
