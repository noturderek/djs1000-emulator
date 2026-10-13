"""Jitter buffer, drift controller and device-state checks for the host audio output (M4b2), on synthetic input.

Compiles tools/sp16-emulator/audio-selftest.swift (never part of the app) and runs it in virtual time; no
audio device is opened and nothing is played.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("swiftc"), "swiftc not available")
class HostAudioOutputTests(unittest.TestCase):
    def test_synthetic_buffer_and_drift_scenarios(self):
        with tempfile.TemporaryDirectory() as work:
            binary = Path(work) / "selftest"
            build = subprocess.run(
                ["swiftc", "-O", "-parse-as-library", "-framework", "AVFoundation", "-framework", "CoreAudio",
                 str(TOOLS / "audio-selftest.swift"), str(TOOLS / "audio-output.swift"), str(TOOLS / "audio-sink.swift"),
                 "-o", str(binary)], capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr[-2000:])
            run = subprocess.run([str(binary), "test"], capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-1000:])
            for name in ("equal-rates", "drift-learned", "48k-512", "overflow-drop-oldest", "prefill", "flush",
                         "stall-recovers", "device-unavailable"):
                self.assertIn("PASS " + name, run.stdout)
            self.assertIn("ALL PASS", run.stdout)


if __name__ == "__main__":
    unittest.main()
