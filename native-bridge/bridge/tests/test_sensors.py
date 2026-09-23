"""Unit tests for the camera room-sensor sanity gate (no camera, no container).

Run:  python3 -m unittest discover -s native-bridge/bridge/tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tutk_client as tc  # noqa: E402  (module-level import loads no TUTK libs)
import vitals_poller as vp  # noqa: E402
import config_store as cs  # noqa: E402
import tempfile  # noqa: E402


class RealtimeSentinelTests(unittest.TestCase):
    """A cam without room sensors answers 0xFF in both GET_REALTIME_DATA fields.
    Published raw that became "491°F   255% RH" on the HUD and in Home Assistant."""

    def test_ff_sentinels_become_none(self):
        self.assertEqual(tc._gate_realtime(255, 255), (None, None))

    def test_real_readings_pass_through(self):
        self.assertEqual(tc._gate_realtime(22, 45), (22, 45))
        self.assertEqual(tc._gate_realtime(-20, 0), (-20, 0))
        self.assertEqual(tc._gate_realtime(60, 100), (60, 100))

    def test_each_field_gated_independently(self):
        self.assertEqual(tc._gate_realtime(-5, 101), (-5, None))
        self.assertEqual(tc._gate_realtime(999, 40), (None, 40))

    def test_none_is_preserved(self):
        self.assertEqual(tc._gate_realtime(None, None), (None, None))


class OverlayTextTests(unittest.TestCase):
    def _run(self, devices):
        with tempfile.TemporaryDirectory() as d:
            saved = cs.VITALS_DIR
            cs.VITALS_DIR = d
            try:
                path = os.path.join(d, "overlay-owlet.txt")
                with open(path, "w") as f:
                    f.write("491\u00b0F   255% RH")   # stale text from before the gate
                vp._write_overlays(devices, lambda m: None)
                with open(path) as f:
                    return f.read()
            finally:
                cs.VITALS_DIR = saved

    def test_no_readings_blanks_the_hud_instead_of_keeping_stale_text(self):
        cam = {"kind": "cam", "name": "owlet", "sensors": {"temperature": None, "humidity": None}}
        self.assertEqual(self._run([cam]), " ")

    def test_readings_are_rendered(self):
        cam = {"kind": "cam", "name": "owlet", "sensors": {"temperature": 22, "humidity": 45}}
        out = self._run([cam])
        self.assertIn("\u00b0F", out)
        self.assertIn("45% RH", out)
        self.assertNotIn("491", out)


if __name__ == "__main__":
    unittest.main()
