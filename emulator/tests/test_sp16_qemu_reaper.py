"""The QEMU reaper: a killed or crashed app must end its QEMU, and the helper must never signal anything else.

The tests run the real helper with a fake app (its parent) and a fake QEMU (its child, carrying the session marker on
its command line). No firmware, Docker or Hypervisor is involved. Every process a test starts is killed by exact PID.
"""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

TOOLS = Path(__file__).resolve().parents[1]
REAPER = TOOLS / "qemu_reaper.py"

FAKE_QEMU = """
import signal, sys, time
mode = sys.argv[2]
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.2)
"""

# argv: marker mode control fake_qemu reaper log. Starts a QEMU and a helper, reports both PIDs, then waits to be killed.
FAKE_APP = """
import subprocess, sys, time
marker, mode, control, fake_qemu, reaper, log = sys.argv[1:7]
qemu = subprocess.Popen([sys.executable, fake_qemu, marker, mode])
args = [sys.executable, reaper, "--qemu", str(qemu.pid), "--match", marker]
if control != "-":
    args += ["--control", control]
with open(log, "w") as out:
    helper = subprocess.Popen(args, stdout=out, stderr=out)
    print(qemu.pid, helper.pid, flush=True)
    time.sleep(600)
"""

# argv: marker reaper log target. Starts only the helper against an arbitrary PID, reports it, then waits.
FAKE_PARENT = """
import subprocess, sys, time
marker, reaper, log, target = sys.argv[1:5]
with open(log, "w") as out:
    helper = subprocess.Popen([sys.executable, reaper, "--qemu", target, "--match", marker], stdout=out, stderr=out)
    print(helper.pid, flush=True)
    time.sleep(600)
"""

# Same argv as FAKE_APP. Starts QEMU and the helper, reports both PIDs, then exits at once.
FAKE_APP_EXITS = """
import subprocess, sys
marker, mode, control, fake_qemu, reaper, log = sys.argv[1:7]
qemu = subprocess.Popen([sys.executable, fake_qemu, marker, "normal"])
out = open(log, "w")
helper = subprocess.Popen([sys.executable, reaper, "--qemu", str(qemu.pid), "--match", marker], stdout=out, stderr=out)
print(qemu.pid, helper.pid, flush=True)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_gone(pid, seconds):
    """Seconds until pid is gone, or None when it is still there after the limit."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if not alive(pid):
            return time.monotonic() - start
        time.sleep(0.1)
    return None


class ReaperTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir="/tmp", prefix="sp16-reaper-")
        self.marker = os.path.join(self.folder, "ui.sock")  # stands in for the session socket: unique per test
        self.fake_qemu = os.path.join(self.folder, "fake_qemu.py")
        Path(self.fake_qemu).write_text(FAKE_QEMU)
        self.log = os.path.join(self.folder, "reaper.log")
        self.pids = []  # every process this test started; killed by exact PID in tearDown
        self.popens = []  # the Popen objects behind them, reaped in tearDown

    def tearDown(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for popen in self.popens:
            if popen.stdout:
                popen.stdout.close()
            popen.wait(timeout=10)
        for name in os.listdir(self.folder):
            os.unlink(os.path.join(self.folder, name))
        os.rmdir(self.folder)

    def start_app(self, script, *extra, mode="normal", control="-"):
        """Start a fake app; returns its Popen and the (qemu, helper) PIDs it reports."""
        app = subprocess.Popen([sys.executable, "-c", script, self.marker, mode, control, self.fake_qemu,
                                str(TOOLS / "qemu_reaper.py"), self.log, *extra],
                               stdout=subprocess.PIPE, text=True)
        self.popens.append(app)
        self.pids.append(app.pid)
        qemu, helper = (int(value) for value in app.stdout.readline().split())
        self.pids += [qemu, helper]
        return app, qemu, helper

    def outcome(self):
        text = Path(self.log).read_text() if os.path.exists(self.log) else ""
        for line in text.splitlines():
            if line.startswith("qemu_reaper outcome="):
                return line.split("=", 1)[1]
        return None

    def test_qemu_ends_within_limit_after_app_is_killed_hard(self):
        app, qemu, helper = self.start_app(FAKE_APP)
        time.sleep(0.5)
        os.kill(app.pid, signal.SIGKILL)  # kill -9: the app runs no cleanup
        self.assertIsNotNone(wait_gone(qemu, 15), "QEMU survived its app by more than 15 s")
        self.assertIsNotNone(wait_gone(helper, 5), "the helper outlived its QEMU")
        self.assertEqual(self.outcome(), "signal-term")

    def test_qemu_that_ignores_sigterm_is_killed(self):
        app, qemu, helper = self.start_app(FAKE_APP, mode="ignore-term")
        time.sleep(0.5)
        os.kill(app.pid, signal.SIGKILL)
        self.assertIsNotNone(wait_gone(qemu, 15), "a QEMU that ignores SIGTERM was not killed")
        self.assertIsNotNone(wait_gone(helper, 5))
        self.assertEqual(self.outcome(), "signal-kill")

    def test_clean_stop_reaches_the_guest_before_qemu_is_signalled(self):
        control = os.path.join(self.folder, "ui.sock.ctl")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(control)
        server.listen(1)
        seen = {}

        def guest(qemu_pid):
            connection, _ = server.accept()
            seen["request"] = connection.recv(64)
            connection.sendall(b"done\n")
            connection.close()
            os.kill(qemu_pid, signal.SIGTERM)  # the guest powering off ends QEMU (-no-reboot)

        app, qemu, helper = self.start_app(FAKE_APP, control=control)
        thread = threading.Thread(target=guest, args=(qemu,))
        thread.start()
        time.sleep(0.5)
        os.kill(app.pid, signal.SIGKILL)
        thread.join(15)
        server.close()
        self.assertEqual(seen.get("request"), b"shutdown\n")
        self.assertIsNotNone(wait_gone(qemu, 15))
        self.assertIsNotNone(wait_gone(helper, 5))
        self.assertEqual(self.outcome(), "exited")

    def test_helper_leaves_when_qemu_quits_while_the_app_lives(self):
        qemu = subprocess.Popen([sys.executable, self.fake_qemu, self.marker, "normal"])
        self.popens.append(qemu)
        self.pids.append(qemu.pid)
        helper = subprocess.Popen([sys.executable, str(REAPER), "--qemu", str(qemu.pid), "--match", self.marker],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.popens.append(helper)
        self.pids.append(helper.pid)
        time.sleep(0.5)
        self.assertIsNone(helper.poll(), "the helper left while QEMU and its app were both running")
        qemu.kill()  # a clean quit ends QEMU first; the helper leaves without signalling anything
        qemu.wait()
        self.assertEqual(helper.wait(timeout=10), 0)

    def test_pid_that_is_not_our_qemu_is_never_signalled(self):
        unrelated = subprocess.Popen(["sleep", "600"])
        self.popens.append(unrelated)
        self.pids.append(unrelated.pid)
        parent = subprocess.Popen([sys.executable, "-c", FAKE_PARENT, self.marker, str(REAPER), self.log,
                                   str(unrelated.pid)], stdout=subprocess.PIPE, text=True)
        self.popens.append(parent)
        self.pids.append(parent.pid)
        helper = int(parent.stdout.readline())
        self.pids.append(helper)
        time.sleep(0.5)
        os.kill(parent.pid, signal.SIGKILL)  # the helper's parent dies; the target is not a QEMU of this session
        self.assertIsNotNone(wait_gone(helper, 20), "the helper did not finish")
        self.assertIsNone(unrelated.poll(), "the helper signalled a process that is not the session's QEMU")
        self.assertEqual(self.outcome(), "not-ours")

    def test_app_gone_before_the_helper_starts_still_ends_qemu(self):
        app, qemu, helper = self.start_app(FAKE_APP_EXITS)
        app.wait(timeout=10)  # the app is already gone when the helper begins its checks
        self.assertIsNotNone(wait_gone(qemu, 15), "QEMU survived when its app was gone before the helper started")
        self.assertIsNotNone(wait_gone(helper, 5))

    def test_helper_is_standard_library_only_and_is_bundled(self):
        text = REAPER.read_text()
        self.assertNotIn("import psutil", text)
        native = (TOOLS / "native.py").read_text()
        self.assertIn('"qemu_reaper.py"', native)
        window = (TOOLS / "native-window.swift").read_text()
        self.assertIn("startQEMUReaper()", window)
        self.assertLess(window.index("QEMU started pid="), window.index("startQEMUReaper()"))


if __name__ == "__main__":
    unittest.main()
