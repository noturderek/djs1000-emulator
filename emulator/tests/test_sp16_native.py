"""Native transport bounds and offline VM arguments, without firmware fixtures."""
import importlib.util
import io
from pathlib import Path
import struct
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import protocol
import native
import panel
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time


class Fragmented(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3))


def framebuffer(**changes):
    name = b"native-fixture\0"
    fields = [100 + len(name), 7, 2, 24, 800, 480, 0, 0, 32, 0, 32, 32,
              3200, 4, 0xff0000, 0xff00, 0xff, 8, 256, 1, 800, 480, 0, 0, 0]
    for key, value in changes.items():
        fields[int(key[1:])] = value
    pixels = bytes([0x12, 0x34, 0x56, 0]) * (800 * 480)
    return struct.pack(">25I", *fields) + name + bytes(12) + pixels, pixels


class NativeTransportTests(unittest.TestCase):
    def test_host_app_survives_peer_close_sigpipe(self):
        # PC-REL-3: a research client closing before the reply write raised SIGPIPE and silently killed the app.
        source = (TOOLS / "native-window.swift").read_text()
        self.assertIn("signal(SIGPIPE, SIG_IGN)", source)
        self.assertGreaterEqual(source.count("SO_NOSIGPIPE"), 2)

    def test_trace_is_research_only_and_default_off(self):
        base = native.arguments(Path("/q"), Path("/g"), "/s")
        self.assertNotIn("sp16.trace", " ".join(base))
        traced = native.arguments(Path("/q"), Path("/g"), "/s", None, "strace")
        self.assertIn("sp16.trace=strace", " ".join(traced))
        with self.assertRaises(ValueError):
            native.arguments(Path("/q"), Path("/g"), "/s", None, "bogus")
        with self.assertRaises(ValueError):
            native.build("x", "y", research=False, trace="strace")

    def test_pcm_tap_is_a_research_trace_mode_off_by_default(self):
        self.assertIn("sp16.trace=pcmtap", " ".join(native.arguments(Path("/q"), Path("/g"), "/s", None, "pcmtap")))
        self.assertNotIn("pcmtap", " ".join(native.arguments(Path("/q"), Path("/g"), "/s")))
        with self.assertRaises(ValueError):
            native.build("x", "y", research=False, trace="pcmtap")

    def test_audio_transport_is_a_dedicated_port_off_by_default(self):
        base = " ".join(native.arguments(Path("/q"), Path("/g"), "/s"))
        self.assertNotIn("sp16.pcm", base)
        self.assertNotIn("sp16.audio", base)
        audio = native.arguments(Path("/q"), Path("/g"), "/s", None, None, False, "file")
        text = " ".join(audio)
        self.assertIn("name=sp16.pcm", text)
        self.assertIn("path=/s.pcm,server=on", text)
        self.assertIn("sp16.audio=file", text)
        self.assertIn("name=sp16.ui", text)
        with self.assertRaises(ValueError):
            native.arguments(Path("/q"), Path("/g"), "/s", None, None, False, "device")
        with self.assertRaises(ValueError):
            native.build("x", "y", research=False, profile=None, audio="file")
        self.assertIn("audio-sink.swift", native.SWIFT_SOURCES)
        self.assertIn("audio-output.swift", native.SWIFT_SOURCES)

    def test_host_output_is_opt_in_and_guest_transport_is_unchanged(self):
        self.assertEqual(native.AUDIO_MODES, ("file", "coreaudio", "both"))
        for mode in ("coreaudio", "both"):
            text = " ".join(native.arguments(Path("/q"), Path("/g"), "/s", None, None, False, mode))
            self.assertIn("sp16.audio=file", text)   # the guest only knows the transport, not the host sink
            self.assertIn("name=sp16.pcm", text)
        with self.assertRaises(ValueError):
            native.build("x", "y", research=True, audio="file", audio_device="uid")
        with self.assertRaises(ValueError):
            native.build("x", "y", research=True, audio="coreaudio", audio_device="bad\nuid")

    def test_fragmented_messages_preserve_frame_and_following_ack(self):
        payload = struct.pack(">HHI", 800, 480, 21) + b"pixel-data"
        stream = Fragmented(protocol.message(protocol.FRAME, payload)
                            + protocol.message(protocol.ACK, struct.pack(">I", 7)))
        self.assertEqual(protocol.receive(stream), (protocol.FRAME, payload))
        self.assertEqual(protocol.receive(stream), (protocol.ACK, struct.pack(">I", 7)))

    def test_unbounded_length_and_truncation_rejected(self):
        for length in [0, protocol.MAX_BODY + 1, 0xffffffff]:
            with self.assertRaises(ValueError):
                protocol.receive(io.BytesIO(struct.pack(">I", length)))
        with self.assertRaises(EOFError):
            protocol.receive(io.BytesIO(b"\0\0\0\x06\x02"))

    def test_touch_bounds_and_actions(self):
        for action, name in enumerate(["move", "down", "up"]):
            self.assertEqual(protocol.decode_touch(struct.pack(">HHB", 799, 479, action)), (799, 479, name))
        for payload in [b"", struct.pack(">HHB", 800, 0, 0),
                        struct.pack(">HHB", 0, 480, 0), struct.pack(">HHB", 1, 1, 3)]:
            with self.assertRaises(ValueError):
                protocol.decode_touch(payload)

    def test_xwd_pixels_preserved_without_png_or_color_table(self):
        source, pixels = framebuffer()
        self.assertEqual(protocol.frame_pixels(source), pixels)

    def test_wheel_signed_steps_and_bounds(self):
        for steps in [-12, -1, 1, 12]:
            self.assertEqual(protocol.decode_wheel(struct.pack(">HHh", 400, 300, steps)), (400, 300, steps))
        for payload in [b"", struct.pack(">HHh", 800, 0, 1), struct.pack(">HHh", 0, 480, 1),
                        struct.pack(">HHh", 1, 1, 0), struct.pack(">HHh", 1, 1, 13)]:
            with self.assertRaises(ValueError):
                protocol.decode_wheel(payload)

    def test_navigation_keys_are_allowlisted(self):
        for code, name in enumerate(["Up", "Down", "Left", "Right", "Return", "Escape"]):
            self.assertEqual(protocol.decode_key(bytes([code])), name)
        for payload in [b"", b"\x06", b"\xff", b"\0\0"]:
            with self.assertRaises(ValueError):
                protocol.decode_key(payload)

    def test_panel_crc_vectors_and_raw_counter_order(self):
        self.assertEqual(panel.crc16(b"123456789"), 0x906e)
        for count, buttons, crc in [(0, 0, "5a23"), (1, 0, "acb7"),
                                    (-1, 0, "3178"), (0, 2, "41f6")]:
            frame = panel.packet(count, buttons)
            self.assertEqual(len(frame), 128)
            self.assertEqual(frame[0], 1)
            self.assertEqual(frame[1], buttons)
            self.assertEqual(frame[60:62], struct.pack(">H", count & 0xffff))
            self.assertEqual(frame[124:126], bytes.fromhex(crc))
        with self.assertRaises(ValueError):
            panel.packet(0, 0xff)

    def test_panel_turns_keep_cumulative_state_and_release_button(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "panel.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(path)
                receiver.settimeout(.5)
                control = panel.PanelInput(path)
                try:
                    control.turn(2)
                    control.turn(-1)
                    control.press(2)
                    frames = [receiver.recv(128) for _ in range(6)]
                    self.assertEqual(frames, [panel.packet(0), panel.packet(1),
                                             panel.packet(2), panel.packet(1),
                                             panel.packet(1, 2), panel.packet(1)])
                    for rows in [0, 13, -13, .5]:
                        with self.assertRaises(ValueError):
                            control.turn(rows)
                finally:
                    control.close()

    def test_wrong_pixel_layout_and_truncated_frame_rejected(self):
        for changes in [{"i4": 801}, {"i7": 1}, {"i12": 3204}, {"i14": 0xff}, {"i0": 99}]:
            source, _ = framebuffer(**changes)
            with self.assertRaises(ValueError):
                protocol.frame_pixels(source)
        source, _ = framebuffer()
        with self.assertRaises(ValueError):
            protocol.frame_pixels(source[:-1])

    def test_runtime_is_offline_native_hvf_with_private_channel(self):
        args = native.arguments(Path("/native/qemu"), Path("/private/guest"), "/tmp/sp16-unit/ui.sock")
        self.assertEqual(args[args.index("-accel") + 1], "hvf")
        self.assertEqual(args[args.index("-cpu") + 1], "host")
        self.assertEqual(args[args.index("-nic") + 1], "none")
        self.assertEqual(args[args.index("-display") + 1], "none")
        self.assertIn("socket,id=sp16,path=/tmp/sp16-unit/ui.sock,server=on,wait=off", args)
        self.assertFalse(any("docker" in a or "http://" in a or "-virtfs" == a for a in args))


class BundleIdentityTests(unittest.TestCase):
    def plist_for(self, **kwargs):
        """Bundle an app into a temp dir with swift/codesign/icon steps stubbed; return its Info.plist dict."""
        import plistlib
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            with mock.patch.object(native, "run"), mock.patch.object(native, "build_icon"):
                app = native.bundle(output, Path("/q"), Path("/g"), **kwargs)
            return plistlib.loads((app / "Contents/Info.plist").read_bytes())

    @unittest.skip("legacy build files are not part of the public repository")
    def test_default_bundle_ids_unchanged(self):
        self.assertEqual(self.plist_for()["CFBundleIdentifier"], "local.sp16-emulator.legacy")
        full = self.plist_for(profile="fullpanel")
        self.assertEqual(full["CFBundleIdentifier"], "local.sp16-emulator.legacy.fullpanel")
        self.assertEqual(full["CFBundleName"], "SP16 Emulator Full Panel")

    @unittest.skip("legacy build files are not part of the public repository")
    def test_suffix_appends_to_bundle_id_and_name(self):
        suffixed = self.plist_for(profile="fullpanel", bundle_suffix="devtest")
        self.assertEqual(suffixed["CFBundleIdentifier"],
                         "local.sp16-emulator.legacy.fullpanel.devtest")
        self.assertEqual(suffixed["CFBundleName"], "SP16 Emulator Full Panel devtest")

    def test_suffix_validation(self):
        for token in ["a", "devtest", "task-42", "0", "a" * 32]:
            self.assertEqual(native.validate_bundle_suffix(token), token)
        self.assertIsNone(native.validate_bundle_suffix(None))
        for token in ["", "a" * 33, "Upper", "under_score", "dot.ted", "sp ace", "a\n", "émoji", 7]:
            with self.assertRaises(ValueError):
                native.validate_bundle_suffix(token)

    def test_build_rejects_bad_suffix_before_any_work(self):
        with self.assertRaises(ValueError):
            native.build("x", "y", profile="fullpanel", bundle_suffix="Bad_Token")

    def test_argument_parser_accepts_bundle_suffix(self):
        from unittest import mock
        argv = ["native.py", "build", "--output", "o", "--profile", "fullpanel", "--decoupled",
                "--bundle-suffix", "devtest"]
        with mock.patch.object(native, "build") as build, mock.patch("sys.argv", argv):
            native.main()
        self.assertEqual(build.call_args.kwargs["bundle_suffix"], "devtest")

    def test_decoupled_menus_have_a_standard_edit_menu(self):
        window = (TOOLS / "firmware-window.swift").read_text()
        for title, key in (("Undo", "z"), ("Redo", "z"), ("Cut", "x"), ("Copy", "c"), ("Paste", "v"), ("Select All", "a")):
            self.assertRegex(window, rf'addItem\(withTitle: "{title}", action: [^\n]+keyEquivalent: "{key}"\)')
        self.assertEqual(window.count("menu.addItem(editMenuItem())"), 2)  # import screen and library-only app
        main = (TOOLS / "native-window.swift").read_text()
        branch = main.split("if config.firmware != nil {", 1)[1].split("}", 1)[0]
        self.assertIn("editMenuItem()", branch)  # decoupled only; legacy menus unchanged
        self.assertEqual(main.count("editMenuItem()"), 1)

    def test_code_created_windows_are_not_released_on_close(self):
        # A programmatic NSWindow/NSPanel is released by AppKit on close unless told otherwise, while the Swift
        # controller still owns it: every construction must clear isReleasedWhenClosed before the next construction.
        constructions = 0
        for name in native.SWIFT_SOURCES:
            source = (TOOLS / name).read_text()
            for match in re.finditer(r"(\w+) = NS(?:Window|Panel)\(", source):
                constructions += 1
                flag = re.compile(rf"\b{match.group(1)}\.isReleasedWhenClosed = false").search(source, match.end())
                self.assertIsNotNone(flag, f"{name}: {match.group(0)} lacks isReleasedWhenClosed = false")
                self.assertIsNone(re.compile(r"NS(?:Window|Panel)\(").search(source, match.end(), flag.start()),
                                  f"{name}: a second window is constructed before its isReleasedWhenClosed line")
        self.assertGreaterEqual(constructions, 4)  # main window, import, library, unverified-firmware notice
        # Closing the library must take an open import window off screen before the library controller can go.
        window = (TOOLS / "firmware-window.swift").read_text()
        library = window.split("final class FirmwareLibraryWindow", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("importWindow?.window.close()", library.split("func windowWillClose", 1)[1].split("}", 1)[0])

    def test_qemu_source_filter_skips_only_absolute_links(self):
        import tarfile
        link = tarfile.TarInfo("qemu/roms/x/IncludeHack")
        link.type, link.linkname = tarfile.SYMTYPE, "/usr/include/X11"
        self.assertIsNone(native.qemu_source_filter(link, "/tmp/out"))
        regular = tarfile.TarInfo("qemu/configure")
        self.assertEqual(native.qemu_source_filter(regular, "/tmp/out").name, "qemu/configure")
        escape = tarfile.TarInfo("../evil")
        with self.assertRaises(tarfile.FilterError):
            native.qemu_source_filter(escape, "/tmp/out")

    def test_copy_without_legacy_files_is_decoupled_only(self):
        """The public copy (no legacy guest Dockerfile): decoupled fullpanel by default, legacy options refused."""
        import contextlib
        import io
        from unittest import mock
        with mock.patch.object(native, "LEGACY_AVAILABLE", False), mock.patch.object(native, "build") as build, \
                mock.patch("sys.argv", ["native.py", "build", "--output", "o"]):
            native.main()
        self.assertTrue(build.call_args.kwargs["decoupled"])
        self.assertEqual(build.call_args.kwargs["profile"], "fullpanel")
        for extra in (["--rootfs", "r"], ["--app-arg", "testmode"], ["--no-persistence"],
                      ["--profile", "testmode"]):
            stderr = io.StringIO()
            with mock.patch.object(native, "LEGACY_AVAILABLE", False), mock.patch.object(native, "build") as build, \
                    mock.patch("sys.argv", ["native.py", "build", "--output", "o", *extra]), \
                    contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                native.main()
            build.assert_not_called()
            self.assertIn("firmware-free apps only", stderr.getvalue())
        stdout = io.StringIO()
        with mock.patch.object(native, "LEGACY_AVAILABLE", False), mock.patch("sys.argv", ["native.py", "build", "-h"]), \
                contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
            native.main()
        for hidden in ("--rootfs", "--mod", "--app-arg", "--no-persistence"):
            self.assertNotIn(hidden, stdout.getvalue())


class GuestImageTests(unittest.TestCase):
    """Guest packaging must address one per-build image ID, never the shared tag."""
    IID = "sha256:" + "ab" * 32
    CONTAINER = "c" * 64

    def run_build(self, rm_code=0, rm_stderr="", iid=None):
        from unittest import mock
        import contextlib
        calls = []
        def fake_run(command, **kwargs):
            calls.append(list(command))
            if "--iidfile" in command:
                Path(command[command.index("--iidfile") + 1]).write_text((iid or self.IID) + "\n")
        def fake_check_output(command, **kwargs):
            calls.append(list(command))
            mount = command[command.index("--mount") + 1]
            guest = Path(mount.split("src=")[1].split(",dst=")[0])
            (guest / "kernel").write_bytes(b"k")
            (guest / "guest.cpio.gz").write_bytes(b"c")
            return self.CONTAINER
        def fake_subprocess_run(command, **kwargs):
            calls.append(list(command))
            return mock.Mock(returncode=rm_code, stderr=rm_stderr)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            output.mkdir()
            root = Path(folder) / "root"
            root.mkdir()
            with mock.patch.object(native, "run", side_effect=fake_run), \
                 mock.patch.object(native.subprocess, "check_output", side_effect=fake_check_output), \
                 mock.patch.object(native.subprocess, "run", side_effect=fake_subprocess_run), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                guest = native.build_guest(root, output, {"variant": "stock"})
                self.assertTrue((guest / "kernel").is_file())
                leftovers = list(output.glob("iid-*"))
            return calls, err.getvalue(), leftovers

    def test_build_packages_by_image_id_and_removes_it(self):
        calls, _, leftovers = self.run_build()
        build = next(c for c in calls if c[:2] == ["docker", "build"])
        self.assertNotIn("-t", build)
        self.assertIn("--iidfile", build)
        create = next(c for c in calls if c[:2] == ["docker", "create"])
        self.assertEqual(create[-1], self.IID)
        self.assertIn(["docker", "image", "rm", self.IID], calls)
        self.assertEqual(leftovers, [])
        for command in calls:
            self.assertNotIn("sp16-native-guest:local", command)

    def test_failed_image_removal_is_logged_not_fatal(self):
        _, err, _ = self.run_build(rm_code=1, rm_stderr="conflict: image is being used")
        self.assertIn("could not remove guest image", err)
        self.assertIn("image is being used", err)

    def test_malformed_image_identity_rejected_and_never_removed(self):
        # A tag-shaped or garbage identity must not reach `docker image rm`, which would remove a shared tag.
        with self.assertRaises(ValueError):
            self.run_build(iid="sp16-native-guest:local")


class PersistenceArgumentTests(unittest.TestCase):
    def test_default_arguments_unchanged_without_data(self):
        args = native.arguments(Path("/q"), Path("/g"), "/s")
        self.assertFalse(any("sp16emmc" in a or "sp16.ctl" in a or "sp16.data" in a for a in args))
        self.assertNotIn("-drive", args)

    def test_data_arguments_add_block_device_and_control_port(self):
        args = native.arguments(Path("/q"), Path("/g"), "/tmp/sp16-x/ui.sock", data=True)
        self.assertIn(f"file={native.DATA_PLACEHOLDER},format=raw,if=none,id=sp16emmc", args)
        self.assertIn("virtio-blk-device,drive=sp16emmc,serial=sp16emmc", args)
        self.assertIn("socket,id=sp16ctl,path=/tmp/sp16-x/ui.sock.ctl,server=on,wait=off", args)
        self.assertIn("virtserialport,chardev=sp16ctl,name=sp16.ctl", args)
        self.assertIn(" sp16.data=1", args[args.index("-append") + 1])
        self.assertIn("-nic", args)  # still offline

    def test_persistence_only_for_fullpanel(self):
        self.assertTrue(native.resolve_persistence("fullpanel"))
        self.assertFalse(native.resolve_persistence("fullpanel", no_persistence=True))
        for profile in (None, "testmode"):
            self.assertFalse(native.resolve_persistence(profile))
            with self.assertRaises(ValueError):
                native.resolve_persistence(profile, no_persistence=True)

    def bundle_config(self, persistent):
        import json
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            with mock.patch.object(native, "run"), mock.patch.object(native, "build_icon"):
                app = native.bundle(output, Path("/q"), Path("/g"), profile="fullpanel", persistent=persistent)
            return json.loads((app / "Contents/Resources/default-config.json").read_text()), output

    @unittest.skip("legacy build files are not part of the public repository")
    def test_bundle_config_carries_data_dir_only_when_persistent(self):
        config, output = self.bundle_config(True)
        self.assertEqual(config["data"], {"dir": str(output / "data"), "size": 1 << 30})
        self.assertIn("-drive", config["arguments"])
        config, _ = self.bundle_config(False)
        self.assertNotIn("data", config)
        self.assertNotIn("-drive", config["arguments"])

    @unittest.skip("legacy build files are not part of the public repository")
    def test_cli_flags(self):
        import subprocess
        out = subprocess.run([sys.executable, str(TOOLS / "native.py"), "build", "--help"],
                             capture_output=True, text=True).stdout
        self.assertIn("--no-persistence", out)
        out = subprocess.run([sys.executable, str(TOOLS / "native.py"), "launch", "--help"],
                             capture_output=True, text=True).stdout
        self.assertIn("--ephemeral", out)
        failed = subprocess.run([sys.executable, str(TOOLS / "native.py"), "build", "--rootfs", "x", "--output", "y",
                                 "--no-persistence"], capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("fullpanel", failed.stderr)

    @unittest.skip("legacy build files are not part of the public repository")
    def test_guest_init_gates_persistence_on_cmdline_token(self):
        text = (TOOLS / "guest-init.sh").read_text()
        self.assertIn('*" sp16.data=1 "*) sp16_data=1', text)
        self.assertIn("mkfs.vfat", text)
        self.assertIn("fsck.vfat -a", text)
        self.assertIn("cp -an", text)  # never overwrite a newer persisted calibration
        self.assertIn("dosfstools", (TOOLS / "Dockerfile.native-guest").read_text())


@unittest.skipUnless(shutil.which("swiftc"), "swiftc required")
class DataImageSwiftTests(unittest.TestCase):
    """Compile the real data-image.swift into a tiny harness and exercise it."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        main = Path(cls.work.name) / "main.swift"
        main.write_text(HARNESS)
        cls.binary = Path(cls.work.name) / "harness"
        subprocess.run(["swiftc", "-O", str(main), str(TOOLS / "data-image.swift"), "-o", str(cls.binary)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def run_harness(self, *args):
        return subprocess.run([str(self.binary), *args], capture_output=True, text=True, timeout=30)

    def test_image_is_sparse_exact_size_and_never_recreated(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            result = self.run_harness("prepare", str(data), str(1 << 24))
            image = data / "emmc.img"
            self.assertEqual(result.stdout.strip(), str(image))
            self.assertEqual(image.stat().st_size, 1 << 24)
            self.assertLess(image.stat().st_blocks * 512, 1 << 20)
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)
            image.write_bytes(b"FORMATTED" + bytes(100))
            self.run_harness("prepare", str(data), str(1 << 24))
            self.assertTrue(image.read_bytes().startswith(b"FORMATTED"))

    def holder(self, data):
        process = subprocess.Popen([str(self.binary), "lock", data], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "locked")
        return process

    def test_second_session_refused_and_stale_lock_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            holder = self.holder(folder)
            try:
                refused = self.run_harness("lock", folder)
                self.assertEqual(refused.returncode, 3)
                self.assertIn(f"pid {holder.pid}", refused.stdout)
            finally:
                holder.kill()  # kill -9: no cleanup runs, the lock file keeps a dead PID
                holder.wait()
                holder.stdin.close()
                holder.stdout.close()
            again = subprocess.Popen([str(self.binary), "lock", folder], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
            self.assertEqual(again.stdout.readline().strip(), "locked")
            self.assertEqual((Path(folder) / "session.lock").read_text().strip(), str(again.pid))
            again.stdin.close()
            again.wait()
            again.stdout.close()

    def test_clean_stop_waits_for_done_and_times_out(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            path = str(Path(folder) / "c.sock")
            for reply, expected in ((b"done\n", "clean"), (None, "unclean")):
                server = socket.socket(socket.AF_UNIX)
                if os.path.exists(path):
                    os.unlink(path)
                server.bind(path)
                server.listen(1)
                result = {}

                def serve():
                    connection, _ = server.accept()
                    result["got"] = connection.recv(64)
                    if reply:
                        connection.sendall(reply)
                    else:
                        time.sleep(3)
                    connection.close()

                thread = threading.Thread(target=serve)
                thread.start()
                outcome = self.run_harness("stop", path)
                thread.join()
                server.close()
                self.assertEqual(outcome.stdout.strip(), expected)
                self.assertEqual(result["got"], b"shutdown\n")
        self.assertEqual(self.run_harness("stop", "/tmp/sp16-no-such.sock").stdout.strip(), "unclean")


HARNESS = """import Foundation
let a = CommandLine.arguments
switch a[1] {
case "prepare": print(try DataImage.prepare(directory: a[2], size: UInt64(a[3])!))
case "lock":
    do { _ = try DataImage.acquireLock(directory: a[2]); print("locked"); fflush(stdout); _ = readLine() }
    catch { print("refused: \\(error)"); exit(3) }
case "stop": print(DataImage.requestCleanStop(socketPath: a[2], timeout: 2) ? "clean" : "unclean")
default: exit(2)
}
"""


class HostLossGuestTests(unittest.TestCase):
    """Guest side of the orphan watchdog: a host that vanishes must lead to the same clean stop as a quit."""

    def test_bridge_exits_on_hangup_or_eof_after_grace(self):
        text = (TOOLS / "guest-bridge.py").read_text()
        self.assertIn("HOST_GRACE = 8.0", text)
        self.assertIn("select.POLLERR | select.POLLHUP", text)
        self.assertIn("except (EOFError, OSError):", text)
        self.assertIn("os._exit(0)", text)

    def test_guest_init_runs_clean_stop_once_after_bridge_exit(self):
        text = (TOOLS / "guest-init.sh").read_text()
        bridge = text.index("python3 -u /opt/sp16/guest-bridge.py")
        once = text.index("clean-stop.py once")
        self.assertGreater(once, bridge)
        self.assertIn("clean-stop.py watch", text)  # the M3c host request path stays on the same script
        self.assertEqual(subprocess.run(["sh", "-n", str(TOOLS / "guest-init.sh")]).returncode, 0)

    def test_guest_init_never_execs_a_missing_poweroff_and_traps_exit(self):
        text = (TOOLS / "guest-init.sh").read_text()
        self.assertNotIn("/sbin/poweroff", text)
        self.assertIn("trap sp16_poweroff EXIT", text)
        self.assertTrue(text.rstrip().endswith("sp16_poweroff"))
        self.assertIn("/proc/sysrq-trigger", text)

    def test_clean_stop_once_is_a_no_op_when_already_unmounted(self):
        if os.path.ismount("/tmp/sp16-root/mnt/emmc"):
            self.skipTest("a guest data mount is present on this host")
        text = (TOOLS / "guest-init.sh").read_text()
        body = text.split("cat > /tmp/sp16-clean-stop.py <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "clean-stop.py"
            script.write_text(body + "\n")
            result = subprocess.run([sys.executable, "-I", str(script), "once", str(os.getpid())],
                                    capture_output=True, text=True, timeout=30, env={**os.environ, **WATCHER_ENV})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SP16_DATA already_clean=yes", result.stdout)


_RUNTIME = __import__("profiles").legacy()["runtime"]
WATCHER_ENV = {"SP16_DATA_DIR": _RUNTIME["data_dir"], "SP16_EMMC_MOUNT": _RUNTIME["emmc_mount"],
               "SP16_USB_MOUNT": _RUNTIME["usb_mount"]}


class VirtualUsbStickTests(unittest.TestCase):
    """M3f: host-side image preparation and the trusted guest watcher's udev-style messages."""

    @staticmethod
    def watcher_namespace():
        text = (TOOLS / "guest-init.sh").read_text()
        body = text.split("cat > /tmp/sp16-clean-stop.py <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        namespace = {"__name__": "sp16_watcher"}
        os.environ.update(WATCHER_ENV)  # the init passes the firmware profile's mount points to the watcher
        exec(compile(body, "clean-stop.py", "exec"), namespace)
        return namespace

    def test_usb_image_is_created_sparse_private_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = native.prepare_usb_image(str(Path(directory) / "stick.img"))
            info = os.stat(path)
            self.assertEqual(info.st_size, native.USB_IMAGE_SIZE)
            self.assertEqual(info.st_mode & 0o777, 0o600)
            self.assertLess(info.st_blocks * 512, 1 << 20)  # sparse
            self.assertEqual(native.prepare_usb_image(path), path)

    def test_usb_image_refuses_symlinks_commas_and_missing_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real.img"
            real.write_bytes(b"")
            link = Path(directory) / "link.img"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                native.prepare_usb_image(str(link))
            with self.assertRaises(ValueError):
                native.prepare_usb_image(str(Path(directory) / "a,b.img"))
            with self.assertRaises(ValueError):
                native.prepare_usb_image(str(Path(directory) / "missing" / "x.img"))

    def test_guest_mounts_with_udev_options_and_never_runs_autoexec(self):
        text = (TOOLS / "guest-init.sh").read_text()
        self.assertIn("rw,dmask=000,fmask=000,shortname=mixed,utf8,noatime", text)
        self.assertIn("serial", text)
        self.assertNotIn("decrypt_autoexec", text)

    def test_announce_sends_connect_then_mount_and_removal_sends_umount_then_disconnect(self):
        namespace = self.watcher_namespace()
        with tempfile.TemporaryDirectory() as directory:
            nodes = {}
            sockets = {}
            for name in ("usb1", "usbctn1"):
                nodes[name] = str(Path(directory) / name)
                sockets[name] = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sockets[name].bind(nodes[name])
                sockets[name].settimeout(2)
            namespace["USB_NODES"].update(nodes)
            self.assertTrue(namespace["announce"]())
            self.assertEqual(sockets["usbctn1"].recv(64), b"connect")
            self.assertEqual(sockets["usb1"].recv(64), b"mount /media/usb1/sda1")
            namespace["remove_messages"]()
            self.assertEqual(sockets["usb1"].recv(64), b"umount /media/usb1/sda1")
            self.assertEqual(sockets["usbctn1"].recv(64), b"disconnect")
            for item in sockets.values():
                item.close()

    def test_announce_waits_until_the_app_has_opened_both_nodes(self):
        namespace = self.watcher_namespace()
        with tempfile.TemporaryDirectory() as directory:
            namespace["USB_NODES"].update({"usb1": str(Path(directory) / "a"), "usbctn1": str(Path(directory) / "b")})
            self.assertFalse(namespace["announce"]())

    def test_eject_is_a_research_command(self):
        sessionctl = (TOOLS / "sessionctl.py").read_text()
        self.assertIn('op="usb"', sessionctl)
        window = (TOOLS / "native-window.swift").read_text()
        self.assertIn('case "usb":', window)
        self.assertIn("usb-ejected", window)


if __name__ == "__main__":
    unittest.main()
