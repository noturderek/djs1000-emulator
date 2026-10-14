"""Diagnostics collection (bug reports): redaction, exclusion rules, the zip contents, the key check, and the
app's log, clean-exit marker and crash check (the real Swift code compiled into a small harness)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
import zipfile

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import diagnostics  # noqa: E402
import native  # noqa: E402

KEY_TEXT = b"unit-test-key-material-01234567"


class Fixture(unittest.TestCase):
    """A synthetic decoupled build with two sessions, a library entry, build logs and a fake crash-report folder."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        root = Path(self.work.name)
        self.home = root / "fakehome"
        self.home.mkdir()
        self.user, self.host = "alice", "alices-mac"
        self.redactor = diagnostics.Redactor(home=str(self.home), user=self.user, host=self.host, full_name="Alice Example")
        self.build = self.home / "build/sp16"
        (self.build / "sessions").mkdir(parents=True)
        (self.build / "library/fw-1.60").mkdir(parents=True)
        (self.build / "assets.json").write_text(json.dumps({
            "decoupled": True, "qemu_version": "10.2.1", "profile": "fullpanel", "research": False,
            "library": str(self.build / "library"), "qemu": str(self.build / "missing-qemu")}))
        for name, age, clean in (("native-aaaa", 300, True), ("native-bbbb", 200, False), ("native-cccc", 100, True), ("native-dddd", 50, False)):
            folder = self.build / "sessions" / name
            folder.mkdir()
            runtime = folder / "runtime.json"
            runtime.write_text(json.dumps({"session": str(folder), "firmware": "fw-1.60"}))
            stamp = time.time() - age
            os.utime(runtime, (stamp, stamp))
            (folder / "console.txt").write_text(f"boot in {self.home}/x for {self.user}@{self.host}\nSP16_NATIVE_READY\n")
            (folder / "metrics.json").write_text("{}")
            (folder / "app.log").write_text(f"2026-10-13T10:00:00Z opened {self.home}/lib mail bob@example.com\n")
            (folder / "qemu-stderr.txt").write_text("[QEMU exit status 0 (exited)]\n")
            (folder / "screen.png").write_bytes(b"\x89PNG-synthetic")
            (folder / "output.wav").write_bytes(b"RIFF-synthetic")
            # Things that must never be collected, whatever their place:
            (folder / "firmware.sqfs").write_bytes(b"sqfs")
            (folder / "emmc.img").write_bytes(b"img")
            (folder / "device.key").write_bytes(KEY_TEXT)
            (folder / "update.UPD").write_bytes(b"upd")
            if clean:
                (folder / "clean-exit").write_text("quit\n")
        (self.build / "logs").mkdir()
        (self.build / "logs/app-20261013-100000-1.log").write_text(f"import from {self.home}/Downloads\n")
        (self.build / "qemu-build.log").write_text("".join(f"line {i}\n" for i in range(2000)))
        (self.build / "swift-build.log").write_text("ok\n")
        (self.build / "library/fw-1.60/manifest.json").write_text(json.dumps({
            "schema": 1, "id": "fw-1.60", "kind": "stock", "app_sha256": "a" * 64,
            "update": {"trailer_version": "1.60", "sha256": "b" * 64}, "image": {"sha256": "c" * 64, "bytes": 4096, "file": "firmware.sqfs"},
            "compat": {"devices_unknown": []}}))
        (self.build / "library/fw-1.60/entry.json").write_text(json.dumps({"name": "My sampler"}))
        (self.build / "library/fw-1.60/firmware.sqfs").write_bytes(b"sqfs-image")
        (self.build / "library/fw-1.60/update.upd").write_bytes(b"upd")
        self.reports = root / "reports"
        self.reports.mkdir()
        (self.reports / "SP16Emulator-2026-10-13-100000.ips").write_text(f'{{"app_name":"SP16Emulator"}}\n{{"path":"{self.home}/x"}}\n')
        (self.reports / "qemu-system-aarch64-2026-10-13-100000.ips").write_text("{}\n")
        old = self.reports / "SP16Emulator-2026-01-01-100000.ips"
        old.write_text("{}\n")
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        (self.reports / "OtherApp-2026-10-13-100000.ips").write_text("{}\n")

    def tearDown(self):
        self.work.cleanup()

    def collect(self, **kwargs):
        out = Path(self.work.name) / "out.zip"
        result = diagnostics.collect(self.build, out=out, redactor=self.redactor, crash_root=self.reports, **kwargs)
        return result, zipfile.ZipFile(out)


class RedactorTests(unittest.TestCase):
    def test_home_user_host_name_and_email_are_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            home = str(Path(folder) / "people/alice")
            redactor = diagnostics.Redactor(home=home, user="alice", host="alices-mac.local", full_name="Alice Example")
            text = (f"path {home}/Library/x and {home}\nuser alice owns alices-mac (alices-mac.local); Alice Example wrote "
                    "to Alice.Example+bug@mail.example.org and bob@example.com\nmalice alice2 stay")
            result = redactor.text(text)
        self.assertNotIn("alice@", result)
        self.assertIn("path ~/Library/x and ~\n", result)
        self.assertIn("user <user> owns <host> (<host>); <name> wrote to <email> and <email>", result)
        self.assertIn("malice alice2 stay", result)  # whole words only
        self.assertEqual(redactor.counts["email"], 2)

    def test_crash_report_escaped_paths_and_machine_ids_are_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            home = str(Path(folder) / "people/alice")
            redactor = diagnostics.Redactor(home=home, user="", host="", full_name="")
            escaped = home.replace("/", "\\/")
            text = (f'{{"procPath":"{escaped}\\/src\\/build","crashReporterKey" : "1A2B-3C4D",'
                    '"bootSessionUUID":"0F1E2D3C","sleepWakeUUID":"ABCD","bundleID":"local.sp16"}')
            result = redactor.text(text)
        self.assertIn('"procPath":"~\\/src\\/build"', result)
        for value in ("1A2B-3C4D", "0F1E2D3C", "ABCD", "people"):
            self.assertNotIn(value, result)
        self.assertIn('"bundleID":"local.sp16"', result)
        self.assertEqual((redactor.counts["home"], redactor.counts["machine_id"]), (1, 3))

    def test_short_or_generic_names_are_not_replaced(self):
        redactor = diagnostics.Redactor(home="/nonexistent-home-x", user="root", host="mac", full_name="")
        self.assertEqual(redactor.text("root mac /root/pdj"), "root mac /root/pdj")
        redactor = diagnostics.Redactor(home="/nonexistent-home-x", user="al", host="", full_name="")
        self.assertEqual(redactor.text("al"), "al")

    def test_symlinked_home_is_redacted_in_both_spellings(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder).resolve() / "real"
            real.mkdir()
            link = Path(folder) / "link"
            link.symlink_to(real)
            redactor = diagnostics.Redactor(home=str(link), user="", host="", full_name="")
            self.assertEqual(redactor.text(f"{link}/a {real}/b"), "~/a ~/b")

    def test_exclusion_rules(self):
        for name in ("device.key", "SECRET.KEY", "SAMPLER.UPD", "update.upd", "firmware.sqfs", "data/emmc.img", "x.iso",
                     "a.sfs", "b.mot"):
            self.assertTrue(diagnostics.is_excluded(name), name)
        for name in ("console.txt", "report.json", "screen.png", "crash.ips", "output.wav", "app.log.1", "clean-exit"):
            self.assertFalse(diagnostics.is_excluded(name), name)

    def test_tail_helpers_keep_the_end(self):
        data = "".join(f"{i}\n" for i in range(1000)).encode()
        cut = diagnostics.tail_lines(data, 10)
        self.assertTrue(cut.startswith(b"[diagnostics: truncated"))
        self.assertTrue(cut.endswith(b"999\n"))
        self.assertEqual(len(cut.splitlines()), 11)
        self.assertEqual(diagnostics.tail_bytes(b"short"), b"short")
        self.assertTrue(diagnostics.tail_bytes(data, 100).endswith(b"999\n"))


class CollectTests(Fixture):
    def test_zip_contents_and_exclusions(self):
        result, archive = self.collect()
        names = set(archive.namelist())
        self.assertEqual(result["files"], len(names))
        self.assertIn("report.json", names)
        self.assertIn("README.txt", names)
        # The newest three sessions, only the listed files, no audio by default.
        sessions = {n.split("/")[1] for n in names if n.startswith("sessions/")}
        self.assertEqual(sessions, {"native-bbbb", "native-cccc", "native-dddd"})
        for session in sessions:
            for name in ("console.txt", "metrics.json", "runtime.json", "app.log", "qemu-stderr.txt", "screen.png"):
                self.assertIn(f"sessions/{session}/{name}", names)
            self.assertNotIn(f"sessions/{session}/output.wav", names)
        self.assertFalse([n for n in names if diagnostics.is_excluded(n)], names)
        self.assertFalse([n for n in names if "firmware" in n.lower() or "emmc" in n or "UPD" in n])
        self.assertIn("logs/app-20261013-100000-1.log", names)
        # Build logs keep their last 500 lines.
        build_log = archive.read("logs/build/qemu-build.log").decode()
        self.assertIn("truncated to the last 500 of 2000 lines", build_log)
        self.assertTrue(build_log.rstrip().endswith("line 1999"))
        self.assertEqual(len(build_log.splitlines()), 501)
        # Crash reports of the last 14 days, for the app and QEMU only.
        crashes = {n for n in names if n.startswith("crash-reports/")}
        self.assertEqual(crashes, {"crash-reports/SP16Emulator-2026-10-13-100000.ips",
                                   "crash-reports/qemu-system-aarch64-2026-10-13-100000.ips"})
        zip_names = [n.lower() for n in names]
        self.assertFalse([n for n in zip_names if n.endswith((".key", ".upd", ".sqfs", ".img"))])

    def test_session_count_and_audio_option(self):
        _, archive = self.collect(sessions=1, include_audio=True)
        names = archive.namelist()
        self.assertEqual({n.split("/")[1] for n in names if n.startswith("sessions/")}, {"native-dddd"})
        self.assertIn("sessions/native-dddd/output.wav", names)
        self.assertFalse([n for n in names if n.endswith("emmc.img")])

    def test_everything_is_redacted(self):
        _, archive = self.collect()
        combined = b"".join(archive.read(n) for n in archive.namelist() if not n.endswith(".png")).decode(errors="replace")
        for secret in (str(self.home), self.user, self.host, "bob@example.com", "Alice Example"):
            self.assertNotIn(secret, combined)
        self.assertIn("~/lib", combined)
        self.assertIn("<user>@<host>", combined)
        self.assertIn("<email>", combined)
        self.assertIn("Redactions made", archive.read("README.txt").decode())

    def test_report_json(self):
        _, archive = self.collect()
        report = json.loads(archive.read("report.json"))
        for key in ("emulator", "build_options", "system", "firmware_library", "sessions", "included"):
            self.assertIn(key, report)
        system = report["system"]
        for key in ("macos", "hardware_model", "cpu", "free_disk_bytes", "python", "docker_present", "qemu_version"):
            self.assertIn(key, system)
        self.assertIsInstance(system["docker_present"], bool)
        entry = report["firmware_library"]["entries"][0]
        self.assertEqual((entry["id"], entry["name"], entry["version"], entry["kind"]), ("fw-1.60", "My sampler", "1.60", "stock"))
        self.assertEqual(entry["update_sha256"], "b" * 64)
        self.assertIn(entry["status"], ("verified", "unverified", "unknown"))
        self.assertIn("reason", entry)
        self.assertIn("compat", entry)
        sessions = {s["id"]: s for s in report["sessions"]}
        self.assertTrue(sessions["native-cccc"]["clean_exit"])
        self.assertFalse(sessions["native-dddd"]["clean_exit"])
        self.assertTrue(sessions["native-dddd"]["ready_seen"])
        self.assertNotIn("sqfs", json.dumps(report["build_options"]))

    def test_audio_device_uid_is_not_reported(self):
        assets = json.loads((self.build / "assets.json").read_text())
        (self.build / "assets.json").write_text(json.dumps({**assets, "audio": "coreaudio",
                                                            "audio_device": "AppleUSBAudioEngine:Acme:SN12345:1"}))
        options, _ = diagnostics.build_options(self.build)
        self.assertEqual(options["assets"]["audio_device"], "<set>")
        self.assertNotIn("SN12345", json.dumps(options))

    def test_key_in_contents_refuses_the_whole_zip(self):
        key_file = Path(self.work.name) / "the.key"
        key_file.write_bytes(KEY_TEXT + b"\n")
        out = Path(self.work.name) / "refused.zip"
        # Clean contents pass the check.
        diagnostics.collect(self.build, out=out, redactor=self.redactor, crash_root=self.reports, key_file=key_file)
        self.assertTrue(out.exists())
        out.unlink()
        # The key text anywhere in a collected file refuses everything and leaves no partial file behind.
        (self.build / "sessions/native-dddd/console.txt").write_bytes(b"leak " + KEY_TEXT + b"\n")
        with self.assertRaises(diagnostics.KeyFound) as caught:
            diagnostics.collect(self.build, out=out, redactor=self.redactor, crash_root=self.reports, key_file=key_file)
        self.assertNotIn(KEY_TEXT.decode(), str(caught.exception))
        self.assertFalse(out.exists())
        self.assertFalse([p for p in Path(self.work.name).iterdir() if p.name.endswith(".partial")])

    def test_hex_and_base64_forms_of_the_key_are_found_too(self):
        import base64
        key_file = Path(self.work.name) / "the.key"
        key_file.write_bytes(KEY_TEXT + b"\n")
        for leak in (KEY_TEXT.hex().encode(), base64.b64encode(KEY_TEXT)):
            (self.build / "sessions/native-dddd/metrics.json").write_bytes(b'{"x":"' + leak + b'"}')
            with self.assertRaises(diagnostics.KeyFound):
                diagnostics.collect(self.build, out=Path(self.work.name) / "z.zip", redactor=self.redactor,
                                    crash_root=self.reports, key_file=key_file)

    def test_not_a_build_folder_is_refused(self):
        with self.assertRaises(diagnostics.DiagnosticsError):
            diagnostics.collect(Path(self.work.name) / "nope", out=Path(self.work.name) / "z.zip")

    def test_desktop_unavailable_falls_back_to_the_build_folder(self):
        with mock.patch.object(diagnostics, "default_zip", return_value=Path("/nonexistent-desktop/x.zip")):
            result = diagnostics.collect(self.build, redactor=self.redactor, crash_root=self.reports)
        self.assertTrue(Path(result["zip"]).is_relative_to(self.build))
        self.assertRegex(Path(result["zip"]).name, r"^diagnostics-\d{8}-\d{6}\.zip$")
        self.assertEqual(oct(Path(result["zip"]).stat().st_mode & 0o777), "0o600")

    def test_default_zip_prefers_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "Desktop").mkdir()
            with mock.patch.object(diagnostics.Path, "home", return_value=Path(folder)):
                self.assertEqual(diagnostics.default_zip(self.build, "20261013-100000"),
                                 Path(folder) / "Desktop/sp16-diagnostics-20261013-100000.zip")
            with mock.patch.object(diagnostics.Path, "home", return_value=Path(folder) / "none"):
                self.assertEqual(diagnostics.default_zip(self.build, "20261013-100000"),
                                 self.build / "diagnostics-20261013-100000.zip")

    def test_cli_wrapper(self):
        args = types.SimpleNamespace(output=str(self.build), sessions=2, include_audio=False,
                                     out=str(Path(self.work.name) / "cli.zip"), key_file=None)
        with mock.patch.object(diagnostics, "Redactor", return_value=self.redactor), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(native.diagnostics_command(args), 0)
        document = json.loads(printed.call_args.args[0])
        self.assertEqual(document["status"], "diagnostics_written")
        self.assertTrue(Path(document["zip"]).is_file())

    def test_native_registers_the_command_and_bundles_the_tool(self):
        self.assertIn("diagnostics.py", native.FWLIB_FILES)
        self.assertIn("app-diagnostics.swift", native.SWIFT_SOURCES)
        with mock.patch("sys.argv", ["native.py", "diagnostics", "--output", "x", "--sessions", "5", "--include-audio"]), \
                mock.patch.object(native, "diagnostics_command", return_value=0) as command:
            native.main()
        arguments = command.call_args.args[0]
        self.assertEqual((arguments.sessions, arguments.include_audio, arguments.out), (5, True, None))


class SwiftSourceTests(unittest.TestCase):
    def test_menu_and_notice_wording(self):
        source = (TOOLS / "app-diagnostics.swift").read_text()
        self.assertIn('NSMenuItem(title: "Collect Diagnostics…"', source)
        self.assertIn('NSMenu(title: "Help")', source)
        self.assertIn("The emulator did not shut down cleanly last time. Collect diagnostics?", source)
        self.assertIn('static let limit = 2 * 1024 * 1024', source)
        window = (TOOLS / "native-window.swift").read_text()
        self.assertIn("helpMenuItem()", window.split("if config.firmware != nil {", 1)[1].split("let inputItem", 1)[0])
        self.assertEqual((TOOLS / "firmware-window.swift").read_text().count("menu.addItem(helpMenuItem())"), 2)

    def test_app_log_starts_only_for_decoupled_builds(self):
        window = (TOOLS / "native-window.swift").read_text()
        self.assertIn("if decoded.firmware != nil {\n        // Decoupled builds: the app log", window)
        self.assertIn("if config.firmware == nil {\n            process.standardError = log\n        } else {", window)


HARNESS = r'''
import Foundation
import AppKit

enum FirmwareTool {
    static func run(_ arguments: [String], tool: String = "firmware.py", progress: ((String) -> Void)? = nil) -> [String: Any] { [:] }
}

func check(_ condition: Bool, _ what: String) {
    if !condition { print("FAIL: \(what)"); exit(1) }
}
func read(_ path: String) -> String { (try? String(contentsOfFile: path, encoding: .utf8)) ?? "" }
func exists(_ path: String) -> Bool { FileManager.default.fileExists(atPath: path) }
func touch(_ path: String, content: String = "x\n", age: TimeInterval = 0) {
    try? content.write(toFile: path, atomically: true, encoding: .utf8)
    if age > 0 { try? FileManager.default.setAttributes([.modificationDate: Date().addingTimeInterval(-age)], ofItemAtPath: path) }
}

let build = CommandLine.arguments[1]
let sessions = build + "/sessions"
try! FileManager.default.createDirectory(atPath: sessions, withIntermediateDirectories: true)

// --- app log: build-level file, replay into the session log, redaction, rotation, clean-exit marker
AppLog.start(buildDirectory: build)
AppLog.log("early line in " + NSHomeDirectory() + "/Library for " + NSUserName() + " mail me@example.com")
let session = sessions + "/native-current"
try! FileManager.default.createDirectory(atPath: session, withIntermediateDirectories: true)
AppLog.attach(session: session)
AppLog.log("late line")
AppLog.queue.sync {}
let buildLogs = (try? FileManager.default.contentsOfDirectory(atPath: build + "/logs"))?.filter { $0.hasPrefix("app-") } ?? []
check(buildLogs.count == 1, "one build-level log, got \(buildLogs)")
let early = read(build + "/logs/" + buildLogs[0])
check(early.contains("app start pid="), "build log has the start line")
check(early.contains("early line in ~/Library"), "home folder redacted: \(early)")
check(!early.contains(NSHomeDirectory() + "/"), "no home path in the build log")
check(!early.contains("me@example.com") && early.contains("<email>"), "email redacted")
check(early.contains("session folder native-current created"), "pointer to the session log")
let appLog = read(session + "/app.log")
check(appLog.contains("early line in ~/Library"), "early lines replayed into app.log")
check(appLog.contains("late line"), "later lines go to app.log")
check(!read(build + "/logs/" + buildLogs[0]).contains("late line"), "later lines are not in the build log")
check(exists(session + "/app.pid"), "pid file written")
// Rotation at 2 MB, one rotated file.
let filler = String(repeating: "x", count: 1000)
for _ in 0..<2600 { AppLog.log(filler) }
AppLog.queue.sync {}
check(exists(session + "/app.log.1"), "rotated once")
let size = (try! FileManager.default.attributesOfItem(atPath: session + "/app.log"))[.size] as! Int
check(size <= AppLog.limit, "app.log stays under the cap, \(size)")
check(!exists(session + "/app.log.2"), "only one rotation")
check(!exists(session + "/clean-exit"), "no clean-exit marker yet")
AppLog.markCleanExit("test quit")
AppLog.markCleanExit("again")
check(read(session + "/clean-exit").contains("test quit") && !read(session + "/clean-exit").contains("again"), "clean-exit written once")

// --- crash check
let reports = build + "/reports"
try! FileManager.default.createDirectory(atPath: reports, withIntermediateDirectories: true)
CrashCheck.reportsDirectory = reports
CrashCheck.bundleID = "local.test.app"
func session(_ name: String, age: TimeInterval, log: Bool = true, clean: Bool = false, pid: Int32? = nil) -> String {
    let folder = sessions + "/" + name
    try? FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
    touch(folder + "/runtime.json", content: "{}", age: age)
    if log { touch(folder + "/app.log") }
    if clean { touch(folder + "/clean-exit") }
    if let pid = pid { touch(folder + "/app.pid", content: "\(pid)\n") }
    return folder
}
// The harness's own session counts as the previous one here: give it a marker, as the real app would have.
check(CrashCheck.findProblem(build: build, current: nil) == nil, "a cleanly ended previous session is fine")

let crashed = session("native-crashed", age: 0, pid: 999999)   // newest, no marker, process gone
let found = CrashCheck.findProblem(build: build, current: nil)
check(found != nil && found!.text.contains("without a clean exit") && found!.session == crashed, "unclean exit found")
check(CrashCheck.findProblem(build: build, current: crashed) == nil, "the current session itself is never checked")
CrashCheck.acknowledge(build: build, session: crashed)
check(CrashCheck.findProblem(build: build, current: nil) == nil, "announced once only")

let running = session("native-running", age: 0, pid: getpid())  // marker missing but the process is alive (and is not the app)
check(CrashCheck.findProblem(build: build, current: nil) != nil, "a live process that is not the app does not hide a crash")
try? FileManager.default.removeItem(atPath: running)
let legacy = session("native-legacy", age: 0, log: false)       // written by an older app: no app.log
check(CrashCheck.findProblem(build: build, current: nil) == nil, "sessions without app.log are ignored")
try? FileManager.default.removeItem(atPath: legacy)

// A macOS crash report newer than the last session and naming this app; one for another app; an old one.
touch(reports + "/SP16Emulator-new.ips", content: "{\"bundleID\":\"local.test.app\"}\n")
touch(reports + "/SP16Emulator-other.ips", content: "{\"bundleID\":\"local.test.app.other\"}\n")
let ips = CrashCheck.findProblem(build: build, current: nil)
check(ips != nil && ips!.text.contains("SP16Emulator-new.ips") && !ips!.text.contains("other"), "crash report found: \(String(describing: ips))")
CrashCheck.acknowledge(build: build, session: nil)
check(CrashCheck.findProblem(build: build, current: nil) == nil, "crash report announced once only")
print("harness ok")
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "swiftc required")
class SwiftHarnessTests(unittest.TestCase):
    def test_app_log_clean_exit_and_crash_check(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "main.swift").write_text(HARNESS)
            # The binary's path must contain the app's executable name for the "process is the app" test to be meaningful
            # in reverse: this harness is deliberately not named like the app.
            binary = work / "diag-harness"
            subprocess.run(["swiftc", "-O", "-framework", "AppKit", str(work / "main.swift"), str(TOOLS / "app-diagnostics.swift"),
                            "-o", str(binary)], check=True, capture_output=True, text=True)
            build = work / "build"
            result = subprocess.run([str(binary), str(build)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("harness ok", result.stdout)
            for log in (build / "sessions/native-current").glob("app.log*"):
                self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
