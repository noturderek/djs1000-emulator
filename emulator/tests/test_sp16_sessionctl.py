"""The research control socket client: a call that gets no usable reply ends in one clear error.

A heavily loaded host can take more than ten seconds to answer a call; the client used to give up after a hard-coded
10 s with a traceback ending inside `readline`, which read like the app had hung up. The tests serve the socket from a
thread in this process (a fake app that answers, hangs up, stays silent or answers garbage); no emulator runs.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import sessionctl


class FakeApp:
    """A one-client control socket: `behaviour` is "answer", "hang-up", "silent" or "garbage"."""

    def __init__(self, folder, behaviour):
        self.path = str(folder / "c.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.behaviour = behaviour
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        client, _ = self.server.accept()
        with client:
            reader = client.makefile("r")
            while reader.readline():
                if self.behaviour == "answer":
                    client.sendall(b'{"ok": true}\n')
                elif self.behaviour == "garbage":
                    client.sendall(b"not json\n")
                elif self.behaviour == "hang-up":
                    break
                else:
                    self.done.wait(10)          # silent: never answers while the test runs
                    break

    def close(self):
        self.done.set()
        self.server.close()


class SessionctlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sctl-", dir="/tmp"))   # a short path: AF_UNIX allows 104 bytes
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def runtime(self, app, pid=None):
        (self.tmp / "app.pid").write_text(f"{pid if pid is not None else os.getpid()}\n")
        path = self.tmp / "runtime.json"
        path.write_text(json.dumps({"control_socket": app.path, "session": str(self.tmp)}))
        return path

    def app(self, behaviour):
        app = FakeApp(self.tmp, behaviour)
        self.addCleanup(app.close)
        return app

    def session(self, runtime, **kw):
        session = sessionctl.Session(runtime, **kw)
        self.addCleanup(session.close)
        return session

    def test_a_reply_is_returned(self):
        session = self.session(self.runtime(self.app("answer")))
        self.assertEqual(session.metrics(), {"ok": True})

    def test_the_default_timeout_is_a_minute_and_can_be_set(self):
        runtime = self.runtime(self.app("answer"))
        with mock.patch.dict(os.environ, {"SP16_CONTROL_TIMEOUT": ""}):
            self.assertEqual(self.session(runtime).socket.gettimeout(), 60.0)
        with mock.patch.dict(os.environ, {"SP16_CONTROL_TIMEOUT": "120"}):
            self.assertEqual(sessionctl.reply_timeout(), 120.0)
        self.assertEqual(sessionctl.reply_timeout(7), 7.0)

    def test_an_app_that_hangs_up_is_a_clear_error(self):
        session = self.session(self.runtime(self.app("hang-up")))
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.snapshot("obs003")
        message = str(caught.exception)
        self.assertIn("the app closed the control socket during snapshot", message)
        self.assertIn(f"app pid {os.getpid()} running", message)

    def test_no_reply_in_time_is_a_clear_error_and_closes_the_connection(self):
        session = self.session(self.runtime(self.app("silent")), timeout=0.3)
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.event("home", "press", 100)
        self.assertIn("no reply to event within 0.3 s", str(caught.exception))
        self.assertEqual(session.socket.fileno(), -1)            # a late reply cannot answer the next call

    def test_the_error_says_when_the_app_is_gone(self):
        gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        session = self.session(self.runtime(self.app("hang-up"), pid=int(gone.stdout)))
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.metrics()
        self.assertIn(f"app pid {int(gone.stdout)} is not running", str(caught.exception))

    def test_an_unreadable_reply_is_a_clear_error(self):
        session = self.session(self.runtime(self.app("garbage")))
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.metrics()
        self.assertIn("unreadable reply to metrics: 'not json", str(caught.exception))

    def test_a_socket_that_is_gone_is_a_clear_error(self):
        app = self.app("answer")
        runtime = self.runtime(app)
        app.close()
        Path(app.path).unlink()
        with self.assertRaises(sessionctl.ControlError) as caught:
            sessionctl.Session(runtime)
        self.assertIn("cannot connect to the control socket", str(caught.exception))

    def test_the_command_line_prints_one_line_and_exits_1(self):
        runtime = self.runtime(self.app("silent"))
        result = subprocess.run([sys.executable, str(TOOLS / "sessionctl.py"), "--runtime", str(runtime), "--timeout", "0.3",
                                 "snapshot", "obs003"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.count("\n"), 1, result.stderr)
        self.assertTrue(result.stderr.startswith("sessionctl: no reply to snapshot within 0.3 s"), result.stderr)


if __name__ == "__main__":
    unittest.main()
