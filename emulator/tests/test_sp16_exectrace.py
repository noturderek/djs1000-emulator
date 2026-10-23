"""Host-side reader of a --trace exec session's console.txt (executed translation-block addresses)."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import exectrace

CONSOLE = (
    "SP16_APP_STARTED pid=41\r\n"
    "SP16_TB 000116f8 00011700 00011704\r\n"
    "SP16_STRACE 41 openat(1)\r\n"
    "SP16_TB 00011700 00831ca4 00831ca8\r\n"
    "SP16_TB 00011700 zz 00011708\r\n"              # a damaged token ends the batch there
    "SP16_TB 00011710 00011714[sp16-ui-stub] open /dev/x\r\n"   # another console writer cut into the line
    "[sp16-ui-stub] ioctl okSP16_TB 00011720 00011\r\n"          # a batch that starts mid-line and is cut off itself
    "SP16_TB_END distinct=8 lines=9\r\n"
)


class ExecTraceReaderTests(unittest.TestCase):
    def test_addresses_are_distinct_in_first_seen_order(self):
        self.assertEqual(exectrace.addresses(CONSOLE),
                         [0x116f8, 0x11700, 0x11704, 0x831ca4, 0x831ca8,
                          0x11710, 0x11714, 0x11720])

    def test_summary_checks_the_range_and_the_guest_count(self):
        summary = exectrace.summarize(exectrace.addresses(CONSOLE), CONSOLE, (0x116f8, 0x831ca8))
        self.assertEqual(summary["distinct"], 8)
        self.assertEqual(summary["first"], ["0x000116f8", "0x00011700", "0x00011704", "0x00831ca4", "0x00831ca8"])
        self.assertEqual(summary["outside_range"], 0)
        self.assertEqual(summary["guest_reported_distinct"], 8)
        narrow = exectrace.summarize(exectrace.addresses(CONSOLE), CONSOLE, (0x116f8, 0x11704))
        self.assertEqual(narrow["outside_range"], 5)

    def test_main_writes_one_address_per_line_and_prints_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.txt"
            console.write_text(CONSOLE)
            out = Path(directory) / "tb.txt"
            stdout = io.StringIO()
            code = exectrace.main([str(console), "--out", str(out), "--range", "0x116f8..0x831ca8"], stdout)
            self.assertEqual(code, 0)
            self.assertEqual(out.read_text().split(), ["0x000116f8", "0x00011700", "0x00011704", "0x00831ca4",
                                                       "0x00831ca8", "0x00011710", "0x00011714",
                                                       "0x00011720"])
            self.assertEqual(json.loads(stdout.getvalue())["distinct"], 8)

    def test_main_fails_when_the_console_has_no_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.txt"
            console.write_text("SP16_APP_STARTED pid=1\n")
            self.assertEqual(exectrace.main([str(console)], io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
