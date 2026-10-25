"""DJS-1000 panel layout (panel-layout-djs-1.22.json) against the control names and the DJS candidate profile."""
import json
from pathlib import Path
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import panel_map
import profiles


class DJSLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = json.loads((TOOLS / "panel-layout-djs-1.22.json").read_text())
        cls.profile = profiles.load("djs-1.22")

    def test_ids_are_unique_and_named_like_the_shared_tables(self):
        ids = [c["id"] for c in self.layout["controls"]]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.layout["controls"]:
            table = panel_map.LAMP_NAMES if c["kind"] == "lamp" else panel_map.CONTROL_NAMES
            self.assertEqual(table.get(c["id"]), c["name"], c)

    def test_every_djs_button_has_a_profile_role(self):
        roles = self.profile["panel"]["button_roles"]
        for c in self.layout["controls"]:
            if c["kind"] in ("button", "step", "bar"):
                self.assertIn(c["name"], roles, c["name"])
        for name in ("selector", *(f"knob{i}" for i in range(1, 7))):
            self.assertIn(name, self.profile["panel"]["counters"])
        self.assertIn("fx_level_depth", self.profile["panel"]["analog_words"])

    def test_controls_fit_the_canvas(self):
        w, h = self.layout["canvas"]["width"], self.layout["canvas"]["height"]
        for c in self.layout["controls"]:
            self.assertTrue(0 < c["cx"] < w and 0 < c["cy"] < h, c["name"])
        self.assertEqual(self.layout["lcd"]["framebuffer"], {"width": 800, "height": 480})

    def test_the_bundle_ships_per_firmware_layouts(self):
        self.assertIn('TOOLS.glob("panel-layout-*.json")', (TOOLS / "native.py").read_text())
        self.assertIn('"panel-layout-\\($0)"', (TOOLS / "native-window.swift").read_text())


if __name__ == "__main__":
    unittest.main()
