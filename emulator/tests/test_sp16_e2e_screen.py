"""The LCD title oracle must reject unstable output and preserve failure evidence."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("sp16_e2e_screen", TOOLS / "e2e-screen.py")
SCREEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCREEN)


class ScreenOracleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session_dir = Path(self.tmp.name)
        self.runtime = self.session_dir / "runtime.json"
        self.runtime.write_text(json.dumps({"session": str(self.session_dir)}))

        def snapshot(name):
            path = self.session_dir / name
            path.write_bytes(b"capture")
            return path

        self.session = SimpleNamespace(snapshot=snapshot, reader=Mock(), socket=Mock())

    def test_one_matching_frame_does_not_pass(self):
        outputs = ["HOME\n", "MIXER\n", "HOME\n", "UTILITY\n"]
        with patch.object(SCREEN.shutil, "which", return_value="/usr/bin/tesseract"), \
             patch.object(SCREEN, "Session", return_value=self.session), \
             patch.object(SCREEN.subprocess, "run",
                          side_effect=[SimpleNamespace(stdout=value) for value in outputs]):
            result = SCREEN.inspect(self.runtime, "HOME")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["observed"]["titles"], ["HOME", "MIXER", "HOME", "UTILITY"])
        self.assertTrue(Path(result["observed"]["failure_capture"]).exists())
        self.assertEqual(len(list(self.session_dir.glob("e2e-screen-*.png"))), 1)

    def test_ocr_error_keeps_capture_and_reports_error(self):
        with patch.object(SCREEN.shutil, "which", return_value="/usr/bin/tesseract"), \
             patch.object(SCREEN, "Session", return_value=self.session), \
             patch.object(SCREEN.subprocess, "run", side_effect=OSError("OCR failed")):
            result = SCREEN.inspect(self.runtime, "HOME")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["observed"]["error"], "OCR failed")
        self.assertTrue(Path(result["observed"]["failure_capture"]).exists())
        self.session.reader.close.assert_called_once()
        self.session.socket.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
