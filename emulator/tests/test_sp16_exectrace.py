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
    "SP16_TB_INSTANCE i0\r\n"
    "SP16_TB i0 c0 000116f8 00011700 00011704\r\n"
    "SP16_STRACE 41 openat(1)\r\n"
    "SP16_TB i0 c1 00011700 00831ca4\r\n"               # the same pc on another guest thread is a block of its own
    "SP16_TB i0 c0 00011700 zz 00011708\r\n"              # a damaged token ends the batch there
    "SP16_TB i0 c0 00011710 00011714[sp16-ui-stub] open /dev/x\r\n"   # another console writer cut into the line
    "[sp16-ui-stub] ioctl okSP16_TB i0 c2 00011720 00011\r\n"         # a batch that starts mid-line and is cut off itself
    "SP16_TB i0 c1 00831ca8\r\n"
    "SP16_TB_END i0 distinct=9 lines=12 t=41.5 cpus=c0:5@0.0,c1:3@3.2,c2:1@9.9\r\n"
    "SP16_APP_STARTED pid=77\r\n"                         # app-restart: a new qemu-user, cpu labels start from c0
    "SP16_TB_INSTANCE i1\r\n"
    "SP16_TB i1 c0 000116f8 00011700\r\n"                 # blocks the first instance already ran
    "SP16_TB_END i1 distinct=2 lines=4 t=2.5 cpus=c0:2@0.0\r\n"
)

BLOCKS = [(0, 0, 0x116f8), (0, 0, 0x11700), (0, 0, 0x11704), (0, 1, 0x11700), (0, 1, 0x831ca4), (0, 0, 0x11710),
          (0, 0, 0x11714), (0, 2, 0x11720), (0, 1, 0x831ca8), (1, 0, 0x116f8), (1, 0, 0x11700)]


class ExecTraceReaderTests(unittest.TestCase):
    def test_blocks_are_distinct_per_instance_and_cpu_in_first_seen_order(self):
        self.assertEqual(exectrace.blocks(CONSOLE), BLOCKS)

    def test_a_restarted_instance_reusing_cpu_labels_is_not_merged_with_the_one_before(self):
        text = "SP16_TB i0 c0 000116f8\r\nSP16_TB i1 c0 000116f8\r\n"
        self.assertEqual(exectrace.blocks(text), [(0, 0, 0x116f8), (1, 0, 0x116f8)])
        self.assertEqual(exectrace.addresses(text), [0x116f8])

    def test_older_builds_still_read_as_instance_zero(self):
        no_instance = "SP16_TB c0 000116f8 00011700\r\nSP16_TB c1 00011700\r\n"
        self.assertEqual(exectrace.blocks(no_instance), [(0, 0, 0x116f8), (0, 0, 0x11700), (0, 1, 0x11700)])
        no_cpu = "SP16_TB 000116f8 00011700\r\nSP16_TB 00011700 00011704\r\n"
        self.assertEqual(exectrace.blocks(no_cpu), [(0, None, 0x116f8), (0, None, 0x11700), (0, None, 0x11704)])
        old_end = no_instance + "SP16_TB_END distinct=3 lines=5 t=1.0 cpus=c0:2@0.0,c1:1@0.5\r\n"
        summary = exectrace.summarize(exectrace.blocks(old_end), old_end)
        self.assertEqual(summary["guest_reported_cpus"], {"i0 c0": 2, "i0 c1": 1})
        self.assertEqual(summary["cpu_count_mismatch"], [])

    def test_summary_checks_the_range_and_the_guest_counts_per_instance_and_cpu(self):
        summary = exectrace.summarize(exectrace.blocks(CONSOLE), CONSOLE, (0x116f8, 0x831ca8))
        self.assertEqual(summary["distinct"], 11)
        self.assertEqual(summary["distinct_addresses"], 8)
        self.assertEqual(summary["instances"], {"i0": 9, "i1": 2})
        self.assertEqual(summary["first"], ["i0 c0 0x000116f8", "i0 c0 0x00011700", "i0 c0 0x00011704",
                                            "i0 c1 0x00011700", "i0 c1 0x00831ca4"])
        self.assertEqual(summary["outside_range"], 0)
        self.assertEqual(summary["guest_reported_distinct"], 11)
        self.assertEqual(summary["cpus"], {"i0 c0": 5, "i0 c1": 3, "i0 c2": 1, "i1 c0": 2})
        self.assertEqual(summary["guest_reported_cpus"], {"i0 c0": 5, "i0 c1": 3, "i0 c2": 1, "i1 c0": 2})
        self.assertEqual(summary["cpu_first_seen_seconds"], {"i0 c0": 0.0, "i0 c1": 3.2, "i0 c2": 9.9, "i1 c0": 0.0})
        self.assertEqual(summary["guest_reported_seconds"], 2.5)
        self.assertEqual(summary["cpu_count_mismatch"], [])
        narrow = exectrace.summarize(exectrace.blocks(CONSOLE), CONSOLE, (0x116f8, 0x11704))
        self.assertEqual(narrow["outside_range"], 5)

    def test_a_thread_whose_count_differs_from_the_guest_is_named(self):
        text = CONSOLE.replace("c1:3@3.2", "c1:4@3.2")
        summary = exectrace.summarize(exectrace.blocks(text), text)
        self.assertEqual(summary["cpu_count_mismatch"], ["i0 c1"])
        # The second instance's c0 has the same label and count as the first's but is checked on its own.
        text = CONSOLE.replace("cpus=c0:2@0.0\r\n", "cpus=c0:3@0.0\r\n")
        self.assertEqual(exectrace.summarize(exectrace.blocks(text), text)["cpu_count_mismatch"], ["i1 c0"])

    def test_an_instance_without_an_end_line_is_not_checked(self):
        text = CONSOLE.rsplit("SP16_TB_END i1", 1)[0]
        summary = exectrace.summarize(exectrace.blocks(text), text)
        self.assertEqual(summary["cpu_count_mismatch"], [])
        self.assertEqual(summary["instances"], {"i0": 9, "i1": 2})

    def test_main_writes_one_instance_cpu_and_address_per_line_and_prints_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.txt"
            console.write_text(CONSOLE)
            out = Path(directory) / "tb.txt"
            stdout = io.StringIO()
            code = exectrace.main([str(console), "--out", str(out), "--range", "0x116f8..0x831ca8"], stdout)
            self.assertEqual(code, 0)
            self.assertEqual(out.read_text().splitlines(),
                             ["i0 c0 0x000116f8", "i0 c0 0x00011700", "i0 c0 0x00011704", "i0 c1 0x00011700",
                              "i0 c1 0x00831ca4", "i0 c0 0x00011710", "i0 c0 0x00011714", "i0 c2 0x00011720",
                              "i0 c1 0x00831ca8", "i1 c0 0x000116f8", "i1 c0 0x00011700"])
            self.assertEqual(json.loads(stdout.getvalue())["distinct"], 11)

    def test_main_fails_when_the_console_has_no_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.txt"
            console.write_text("SP16_APP_STARTED pid=1\n")
            self.assertEqual(exectrace.main([str(console)], io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
