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
    "SP16_TB c0 000116f8 00011700 00011704\r\n"
    "SP16_STRACE 41 openat(1)\r\n"
    "SP16_TB c1 00011700 00831ca4\r\n"               # the same pc on another guest thread is a block of its own
    "SP16_TB c0 00011700 zz 00011708\r\n"              # a damaged token ends the batch there
    "SP16_TB c0 00011710 00011714[sp16-ui-stub] open /dev/x\r\n"   # another console writer cut into the line
    "[sp16-ui-stub] ioctl okSP16_TB c2 00011720 00011\r\n"         # a batch that starts mid-line and is cut off itself
    "SP16_TB c1 00831ca8\r\n"
    "SP16_TB_END distinct=9 lines=12 t=41.5 cpus=c0:5@0.0,c1:3@3.2,c2:1@9.9\r\n"
)

BLOCKS = [(0, 0x116f8), (0, 0x11700), (0, 0x11704), (1, 0x11700), (1, 0x831ca4), (0, 0x11710), (0, 0x11714),
          (2, 0x11720), (1, 0x831ca8)]


class ExecTraceReaderTests(unittest.TestCase):
    def test_blocks_are_distinct_per_cpu_in_first_seen_order(self):
        self.assertEqual(exectrace.blocks(CONSOLE), BLOCKS)

    def test_a_build_without_the_cpu_tag_still_reads_as_cpu_none(self):
        old = "SP16_TB 000116f8 00011700\r\nSP16_TB 00011700 00011704\r\n"
        self.assertEqual(exectrace.blocks(old), [(None, 0x116f8), (None, 0x11700), (None, 0x11704)])

    def test_summary_checks_the_range_and_the_guest_counts_per_cpu(self):
        summary = exectrace.summarize(exectrace.blocks(CONSOLE), CONSOLE, (0x116f8, 0x831ca8))
        self.assertEqual(summary["distinct"], 9)
        self.assertEqual(summary["distinct_addresses"], 8)
        self.assertEqual(summary["first"], ["c0 0x000116f8", "c0 0x00011700", "c0 0x00011704", "c1 0x00011700",
                                            "c1 0x00831ca4"])
        self.assertEqual(summary["outside_range"], 0)
        self.assertEqual(summary["guest_reported_distinct"], 9)
        self.assertEqual(summary["cpus"], {"c0": 5, "c1": 3, "c2": 1})
        self.assertEqual(summary["guest_reported_cpus"], {"c0": 5, "c1": 3, "c2": 1})
        self.assertEqual(summary["cpu_first_seen_seconds"], {"c0": 0.0, "c1": 3.2, "c2": 9.9})
        self.assertEqual(summary["guest_reported_seconds"], 41.5)
        self.assertEqual(summary["cpu_count_mismatch"], [])
        narrow = exectrace.summarize(exectrace.blocks(CONSOLE), CONSOLE, (0x116f8, 0x11704))
        self.assertEqual(narrow["outside_range"], 5)

    def test_a_cpu_whose_count_differs_from_the_guest_is_named(self):
        text = CONSOLE.replace("c1:3@3.2", "c1:4@3.2")
        summary = exectrace.summarize(exectrace.blocks(text), text)
        self.assertEqual(summary["cpu_count_mismatch"], ["c1"])

    def test_main_writes_one_cpu_and_address_per_line_and_prints_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.txt"
            console.write_text(CONSOLE)
            out = Path(directory) / "tb.txt"
            stdout = io.StringIO()
            code = exectrace.main([str(console), "--out", str(out), "--range", "0x116f8..0x831ca8"], stdout)
            self.assertEqual(code, 0)
            self.assertEqual(out.read_text().splitlines(),
                             ["c0 0x000116f8", "c0 0x00011700", "c0 0x00011704", "c1 0x00011700", "c1 0x00831ca4",
                              "c0 0x00011710", "c0 0x00011714", "c2 0x00011720", "c1 0x00831ca8"])
            self.assertEqual(json.loads(stdout.getvalue())["distinct"], 9)

    def test_main_fails_when_the_console_has_no_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.txt"
            console.write_text("SP16_APP_STARTED pid=1\n")
            self.assertEqual(exectrace.main([str(console)], io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
