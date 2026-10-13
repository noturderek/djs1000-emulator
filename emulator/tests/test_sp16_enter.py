"""File-size policy of enter.py: 1 GiB bound; an over-limit write fails with EFBIG, not SIGXFSZ termination.

The writer is /bin/dd, not Python: CPython ignores SIGXFSZ itself, so a Python writer would prove nothing.
"""
import os
import resource
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import enter

LIMIT = 64 * 1024
DD = "/bin/dd"

PREAMBLE = (
    "import os, sys\n"
    "sys.path.insert(0, %r)\n"
    "import enter\n"
) % str(TOOLS)


def run_python(body, *args):
    return subprocess.run([sys.executable, "-I", "-c", PREAMBLE + body, *args],
                          capture_output=True, text=True, timeout=60)


class FileSizePolicyTest(unittest.TestCase):
    def test_bound_is_one_gibibyte_and_sigxfsz_is_ignored(self):
        self.assertEqual(enter.FSIZE_LIMIT, 1024 ** 3)
        result = run_python(
            "enter.apply_file_size_limit()\n"
            "import resource, signal\n"
            "print(*resource.getrlimit(resource.RLIMIT_FSIZE), signal.getsignal(signal.SIGXFSZ) == signal.SIG_IGN)\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [str(1024 ** 3), str(1024 ** 3), "True"])

    def test_over_limit_write_fails_with_efbig_and_dd_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "big.bin")
            # Fork, then exec in the child: the way an app's helper runs, with SIG_IGN inherited.
            result = run_python(
                "enter.apply_file_size_limit(%d)\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    os.execv(%r, [%r, 'if=/dev/zero', 'of=' + sys.argv[1], 'bs=4096', 'count=64'])\n"
                "_, status = os.waitpid(pid, 0)\n"
                "print('exit', os.waitstatus_to_exitcode(status))\n" % (LIMIT, DD, DD), target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("exit 1", result.stdout)
            self.assertLessEqual(os.path.getsize(target), LIMIT)

    def test_ignore_survives_execve_into_the_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "big.bin")
            # execv replaces this process with dd; the SIG_IGN disposition must carry over.
            result = run_python(
                "enter.apply_file_size_limit(%d)\n"
                "os.execv(%r, [%r, 'if=/dev/zero', 'of=' + sys.argv[1], 'bs=4096', 'count=64'])\n"
                % (LIMIT, DD, DD), target)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertLessEqual(os.path.getsize(target), LIMIT)

    def test_control_without_the_ignore_dd_is_terminated(self):
        # Proves the survival above comes from the SIG_IGN, not from the limit alone: the same limit with the
        # default SIGXFSZ disposition (subprocess restores it in the child) kills dd with SIGXFSZ.
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "big.bin")
            result = subprocess.run([DD, "if=/dev/zero", "of=" + target, "bs=4096", "count=64"],
                                    capture_output=True, timeout=60,
                                    preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT)))
            self.assertEqual(result.returncode, -signal.SIGXFSZ)


if __name__ == "__main__":
    unittest.main()
