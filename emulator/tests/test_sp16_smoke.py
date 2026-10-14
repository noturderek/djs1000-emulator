"""The end-to-end smoke test's logic with a fake session: PNG reading, screencast timing, checks and verdicts."""
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hwsuite  # noqa: E402
import smoke  # noqa: E402


def paeth(left, up, corner):
    p = left + up - corner
    pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
    return left if pa <= pb and pa <= pc else up if pb <= pc else corner


def make_png(width, height, pixel, filters=(0,), channels=4):
    """A PNG whose row y uses filter filters[y % len(filters)]; pixel(x, y) gives a tuple of `channels` bytes."""
    colour = {3: 2, 4: 6}[channels]
    raw, previous = bytearray(), bytes(width * channels)
    for y in range(height):
        row = bytes(v for x in range(width) for v in pixel(x, y))
        kind = filters[y % len(filters)]
        out = bytearray([kind])
        for i, value in enumerate(row):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            predictor = (0, left, up, (left + up) // 2, paeth(left, up, corner))[kind]
            out.append((value - predictor) & 255)
        raw += out
        previous = row

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def pattern(x, y):
    return ((x * 37 + y * 11) & 255, (x * y) & 255, (x + 3 * y) & 255, 255)


BLACK = make_png(800, 480, lambda x, y: (0, 0, 0, 255))
HOME = make_png(800, 480, lambda x, y: (20, 20, 30, 255) if (x // 40 + y // 40) % 2 else (200, 120, 40, 255))
BROWSE = make_png(800, 480, lambda x, y: (200, 120, 40, 255) if (x // 40 + y // 40) % 2 else (20, 20, 30, 255))


class PngTests(unittest.TestCase):
    def test_every_filter_round_trips(self):
        data = make_png(9, 7, pattern, filters=(0, 1, 2, 3, 4))
        self.assertEqual(smoke.png_info(data), (9, 7, 8, 6))
        rows = smoke.png_rows(data)
        self.assertEqual(rows[3], bytes(v for x in range(9) for v in pattern(x, 3)))
        self.assertEqual(len(rows), 7)

    @unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "Pillow is not installed")
    def test_decodes_what_pillow_writes(self):
        from PIL import Image
        image = Image.new("RGB", (64, 32))
        image.putdata([pattern(x, y)[:3] for y in range(32) for x in range(64)])
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.assertEqual(b"".join(smoke.png_rows(buffer.getvalue())), image.tobytes())

    def test_uniform_with_and_without_pillow(self):
        small_black = make_png(16, 8, lambda x, y: (0, 0, 0, 255), filters=(1, 2))
        small_home = make_png(16, 8, pattern, filters=(4,))
        with mock.patch.dict(sys.modules, {"PIL": None}):  # forces the pure-Python decoder
            self.assertTrue(smoke.png_uniform(small_black))
            self.assertFalse(smoke.png_uniform(small_home))
        self.assertTrue(smoke.png_uniform(small_black))
        self.assertFalse(smoke.png_uniform(small_home))

    def test_rejects_what_it_cannot_read(self):
        with self.assertRaises(ValueError):
            smoke.png_info(b"GIF89a....")
        sixteen_bit = bytearray(make_png(2, 2, pattern))
        sixteen_bit[24] = 16
        with self.assertRaises(ValueError):
            smoke.png_rows(bytes(sixteen_bit))


class ScreencastTests(unittest.TestCase):
    def test_concat_script_plays_faster_than_real_time(self):
        frames = [(10.0, Path("a.png")), (10.5, Path("b.png")), (12.5, Path("c.png"))]
        script = smoke.concat_script(frames, speed=4, tail=1.0).splitlines()
        self.assertEqual(script[0], "ffconcat version 1.0")
        self.assertEqual(script[1:7], ["file 'a.png'", "duration 0.1250", "file 'b.png'", "duration 0.5000",
                                       "file 'c.png'", "duration 1.0000"])
        self.assertEqual(script[-1], "file 'c.png'")  # repeated so the last duration counts
        with self.assertRaises(ValueError):
            smoke.concat_script([], speed=4)

    def test_no_frames_no_screencast(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(smoke.encode([], folder, 4), ({}, ["no frames were recorded"]))


class FakeSession:
    """Plays a boot: a few black frames, then the home screen; a selector detent shows another screen."""

    def __init__(self, session_dir, black_frames=3, draws=True):
        self.dir = Path(session_dir)
        self.black, self.draws, self.screen = black_frames, draws, HOME
        self.guest = {"events": 0, "failed": 0, "tx_frames": 120}
        self.closed = False

    def call(self, **command):
        if command["op"] == "panelshot":
            (self.dir / command["name"]).write_bytes(HOME)
        return {"ok": True}

    def snapshot(self, tag="snap"):
        path = self.dir / f"hw-{tag}.png"
        if self.black > 0 or not self.draws:
            self.black -= 1
            path.write_bytes(BLACK)
        else:
            path.write_bytes(self.screen)
        return path

    def event(self, name, action, value=0):
        self.guest["events"] += 1
        if name == "selector":
            self.screen = BROWSE if value > 0 else HOME

    def metrics(self):
        return {"lamps_live": True, "guest": dict(self.guest)}

    def console(self):
        return (self.dir / "console.txt").read_text()

    def close(self):
        self.closed = True


class RunTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        root = Path(self.work.name).resolve()  # run() resolves the build (macOS: /var is /private/var)
        self.build = root / "sp16-dev"
        self.build.mkdir()
        (self.build / "assets.json").write_text(json.dumps({"decoupled": True, "research": True, "audio": "file"}))
        self.session_dir = self.build / "sessions" / "native-1"
        self.session_dir.mkdir(parents=True)
        (self.session_dir / "console.txt").write_text(
            "SP16_NATIVE_READY firmware=1.60 display=800x480 app_sha256_verified=yes variant=stock\n"
            "SP16_USB formatted=yes\nSP16_USB mounted=yes\nSP16_HOST_CLEAN_STOP clean=yes\n")
        self.runtime = self.session_dir / "runtime.json"
        self.runtime.write_text("{}")
        self.stopped = []
        sleep = mock.patch.object(smoke.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        self.work.cleanup()

    def launch(self, build, firmware_id=None, usb_image=None, ephemeral=False):
        self.assertTrue(ephemeral)
        self.assertEqual(Path(usb_image).parent.parent, self.build / "smoke")
        return 4242, self.runtime, 2.5

    def stop(self, build, pid):
        self.stopped.append(pid)
        return 0.4

    def run_smoke(self, session, **kwargs):
        # Quick passes of time: each lookup advances the monotonic clock, so holds and timeouts finish.
        clock = iter(i * 0.05 for i in range(1, 100000))
        with mock.patch.object(smoke.time, "monotonic", side_effect=lambda: next(clock)):
            return smoke.run(self.build, launcher=self.launch, stopper=self.stop,
                             session_factory=lambda runtime: session, **kwargs)

    def test_a_healthy_session_passes_every_check(self):
        session = FakeSession(self.session_dir)
        result, out = self.run_smoke(session, video=False)
        self.assertEqual(result["verdict"], "PASS", result["checks"])
        self.assertEqual([c["id"] for c in result["checks"]],
                         ["boot", "home", "lamps", "selector", "pads", "transport", "usb", "stop"])
        self.assertEqual(self.stopped, [4242])
        self.assertTrue(session.closed)
        self.assertEqual(json.loads((out / "smoke-result.json").read_text())["verdict"], "PASS")
        self.assertIn("| A selector detent changes the LCD | PASS |", (out / "smoke-report.md").read_text())
        self.assertEqual(list(self.session_dir.glob("hw-*.png")), [])  # snapshots are removed again

    def test_records_frames_out_of_the_session_folder(self):
        session = FakeSession(self.session_dir)
        with mock.patch.object(smoke, "encode", return_value=({}, ["no encoder in tests"])) as encode:
            result, out = self.run_smoke(session, video=True, keep_frames=True)
        frames = encode.call_args.args[0]
        self.assertGreater(len(frames), 10)
        self.assertTrue(all(path.parent == out / "frames" and path.is_file() for _, path in frames))
        self.assertEqual(result["notes"], ["no encoder in tests"])
        self.assertEqual(list(self.session_dir.glob("smoke-frame-*.png")), [])

    def test_a_screen_that_never_draws_fails_and_still_stops(self):
        result, _ = self.run_smoke(FakeSession(self.session_dir, draws=False), video=False)
        self.assertEqual(result["verdict"], "FAIL")
        home = next(c for c in result["checks"] if c["id"] == "home")
        self.assertEqual(home["status"], "FAIL")
        self.assertIn("blank", home["detail"])
        self.assertNotIn("selector", [c["id"] for c in result["checks"]])  # later steps need the home screen
        self.assertEqual(self.stopped, [4242])

    def test_refuses_builds_it_must_not_run(self):
        for assets, message in (({"research": False, "audio": "file"}, "--research"),
                                ({"research": True, "audio": "coreaudio"}, "plays sound")):
            (self.build / "assets.json").write_text(json.dumps(assets))
            with self.assertRaisesRegex(hwsuite.EnvError, message):
                smoke.run(self.build, launcher=self.launch, stopper=self.stop)

    def test_a_failed_launch_stops_what_it_left_running(self):
        def failing(*args, **kwargs):
            raise hwsuite.EnvError("no SP16_NATIVE_READY within 120 s")
        with mock.patch.object(hwsuite, "build_processes", return_value=([777], [])):
            with self.assertRaises(hwsuite.EnvError):
                smoke.run(self.build, launcher=failing, stopper=self.stop)
        self.assertEqual(self.stopped, [777])


if __name__ == "__main__":
    unittest.main()
