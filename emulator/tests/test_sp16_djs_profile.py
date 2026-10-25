"""DJS-1000 candidate profile (djs-1.22): 2-bit lamp fields, unmarked TX frames, the tempo-slider wiper and
zero-point words and the PadCalibration.xml format. Each check runs in a child interpreter with SP16_PROFILE set, because
panel_map and panel read the profile when they are imported. All frames are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import profiles


def run_with_profile(code):
    env = {**os.environ, "SP16_PROFILE": "djs-1.22"}
    result = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(TOOLS)!r})\n"
                             + textwrap.dedent(code)], env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class DJSProfileTests(unittest.TestCase):
    def test_pair_lamps_decode_without_the_sp16_marker(self):
        out = run_with_profile("""
            import json, panel_map
            frame = bytearray(128); frame[0] = 2
            frame[0x66] = 0b00110111          # mute 11 (dim), stop 01 (full), rec 11 (dim)
            frame[0x68] = 0b00000001          # play full
            frame[0x6d] = 4                   # NORMAL brightness
            frame[0x31:0x34] = bytes([0x4a, 0, 0])   # step 1 full red
            raw = panel_map.decode_tx(bytes(frame))
            lamps = panel_map.decode_lamps(bytes(frame))
            names = {**panel_map.CONTROL_NAMES, **panel_map.LAMP_NAMES}
            print(json.dumps({"pair20": raw["pair"][20], "pair21": raw["pair"][21], "pair23": raw["pair"][23],
                              "lamps": {names[k]: v for k, v in lamps.items()}}))
        """)
        self.assertEqual(out["pair20"], [1, 1])
        self.assertEqual(out["pair21"], [1, 0])
        self.assertEqual(out["pair23"], [0, 0])
        lamps = out["lamps"]
        self.assertEqual(lamps["stop"], [255, 150, 40])
        self.assertEqual(lamps["play"], [60, 255, 90])
        self.assertTrue(0 < lamps["mute"][0] < 255)
        self.assertEqual(lamps["scene"], [0, 0, 0])
        self.assertEqual(lamps["step1"], [255, 0, 0])

    def test_usb_ring_dim_code_reads_dark_and_tempo_reset_lights(self):
        out = run_with_profile("""
            import json, panel_map
            names = {**panel_map.CONTROL_NAMES, **panel_map.LAMP_NAMES}
            result = {}
            for label, usb in (("mounted", 0b01), ("after_stop", 0b11)):
                frame = bytearray(128); frame[0] = 2; frame[0x6d] = 4
                frame[0x69] = usb                 # USB A port ring
                frame[0x68] = 0b01 << 6           # TEMPO RESET full
                lamps = panel_map.decode_lamps(bytes(frame))
                result[label] = {names[k]: v for k, v in lamps.items() if names[k] in ("usb_led", "tempo_reset")}
            print(json.dumps(result))
        """)
        self.assertEqual(out["mounted"]["usb_led"], [255, 255, 255])
        self.assertEqual(out["after_stop"]["usb_led"], [0, 0, 0])
        self.assertEqual(out["mounted"]["tempo_reset"], [60, 255, 90])

    def test_tempo_slider_writes_wiper_and_fixed_zero_point(self):
        out = run_with_profile("""
            import json, struct, panel
            words = {}
            for value in (0, 500, 1000):
                state = panel.PanelModel()
                state.apply(104, "value", value)
                words[value] = struct.unpack_from("<HH", state.raw, 0x56)
            print(json.dumps(words))
        """)
        self.assertEqual(out["0"], [512, 0])
        self.assertEqual(out["1000"], [512, 1023])
        self.assertEqual(out["500"], [512, 512])

    def test_pad_calibration_file_uses_the_ep113_keys(self):
        calibration = profiles.load("djs-1.22")["calibration"]
        self.assertEqual(calibration["path"], "/root/data/PadCalibration.xml")
        attributes = " ".join(calibration["attribute"].format(n=n, value=calibration["default"]) for n in (1, 16))
        self.assertIn('Pad01_Final="242"', attributes)
        self.assertIn('Pad16_Provisional3="242"', attributes)
        self.assertEqual(calibration["range"], [217, 454])

    def test_profile_rejects_bad_lamp_fields(self):
        profile = json.loads((TOOLS / "profiles" / "djs-1.22.json").read_text())
        profile["panel"]["lamp_roles"]["play"] = ["pair", 44]
        with self.assertRaises(ValueError):
            profiles.validate(profile)


if __name__ == "__main__":
    unittest.main()
