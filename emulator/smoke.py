#!/usr/bin/env python3
"""End-to-end smoke test: boot a research build, do a few basic things, record a sped-up screencast.

About a minute. It checks only that the emulator runs and responds, not the firmware's features (the hardware suite,
hwsuite.py, does that): boot to a settled home screen with live lamps, a selector detent that changes the LCD, pad
presses that reach the firmware, a few seconds of PLAY, a mounted USB stick, and a clean stop. Throughout, it captures
the whole panel through the session's control socket (no screen-recording permission needed) and turns the frames
into a sped-up screencast.

  smoke.py --build-dir build/sp16-dev [--firmware ID] [--speed 4] [--interval 0.25] [--out DIR] [--no-video]
           [--keep-frames]

The build must be made with --research and must not play sound (silent or --audio file). The assertions use the
standard library only; the screencast needs ffmpeg (MP4 and GIF) or, without it, Pillow (GIF). Output: smoke-result.json,
smoke-report.md and the screencast under <build>/smoke/<run id>/. Exit codes: 0 pass, 1 fail, 3 environment error.
Stops only the session it started, by exact process id; never plays audio, never flashes.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import zlib

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import hwsuite  # noqa: E402  (session launch, stop and the socket client)

EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 3
LCD_SIZE = (800, 480)
DEFAULT_SPEED = 4.0
DEFAULT_INTERVAL = 0.25


# ---------------------------------------------------------------------------------------------------- PNG reading

def png_info(data):
    """(width, height, bit depth, colour type) from a PNG's IHDR."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG file")
    return struct.unpack(">IIBB", data[16:26])


def png_rows(data):
    """The unfiltered scanlines of a non-interlaced 8-bit PNG, as a list of bytes (enough to compare pixels)."""
    width, height, depth, colour = png_info(data)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if depth != 8 or channels is None or data[28] != 0:
        raise ValueError(f"unsupported PNG (depth {depth}, colour type {colour}, interlaced {data[28]})")
    idat, offset = bytearray(), 8
    while offset < len(data):
        length, kind = struct.unpack(">I4s", data[offset:offset + 8])
        if kind == b"IDAT":
            idat += data[offset + 8:offset + 8 + length]
        offset += 12 + length
    raw, stride, step = zlib.decompress(bytes(idat)), width * channels, channels
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, row = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride if kind else 0):
            left = row[i - step] if i >= step else 0
            up = previous[i]
            if kind == 1:
                row[i] = (row[i] + left) & 255
            elif kind == 2:
                row[i] = (row[i] + up) & 255
            elif kind == 3:
                row[i] = (row[i] + (left + up) // 2) & 255
            elif kind == 4:
                corner = previous[i - step] if i >= step else 0
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                row[i] = (row[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
            elif kind != 0:
                raise ValueError(f"bad PNG filter {kind}")
        rows.append(bytes(row))
        previous = row
    return rows


def png_uniform(data):
    """True when every pixel of the PNG has the same value (a blank or black screen). Pillow is used when present
    (fast); the pure-Python decoder is the fallback."""
    try:
        from io import BytesIO
        from PIL import Image
    except ImportError:
        pass
    else:
        return all(low == high for low, high in Image.open(BytesIO(data)).getextrema())
    rows = png_rows(data)
    width, _, _, colour = png_info(data)
    pixel = len(rows[0]) // width
    first = rows[0][:pixel]
    return all(row == first * width for row in rows)


# ------------------------------------------------------------------------------------------------------ recording

class Recorder:
    """Whole-panel frames over the control socket, each with the time it was taken."""

    def __init__(self, session, frames_dir, interval=DEFAULT_INTERVAL, enabled=True):
        self.session, self.dir, self.interval, self.enabled = session, Path(frames_dir), interval, enabled
        self.frames, self.last = [], None
        if enabled:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def shot(self, force=False):
        now = time.monotonic()
        if not self.enabled or (not force and self.last is not None and now - self.last < self.interval):
            return
        self.last = now
        name = f"smoke-frame-{len(self.frames):05d}.png"
        self.session.call(op="panelshot", name=name)
        source = self.session.dir / name
        if source.is_file():
            target = self.dir / name
            shutil.move(str(source), str(target))
            self.frames.append((now, target))

    def hold(self, seconds):
        """Keep recording for `seconds` (the session stays idle except for the captures)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.shot()
            time.sleep(min(self.interval / 2, max(0.0, end - time.monotonic())))


def concat_script(frames, speed, tail=1.0):
    """An ffmpeg concat script that plays the frames `speed` times faster than they were taken."""
    if not frames:
        raise ValueError("no frames")
    lines = ["ffconcat version 1.0"]
    for index, (taken, path) in enumerate(frames):
        following = frames[index + 1][0] if index + 1 < len(frames) else taken + tail * speed
        lines += [f"file '{path.name}'", f"duration {max(following - taken, 0.001) / speed:.4f}"]
    lines.append(f"file '{frames[-1][1].name}'")  # the concat demuxer ignores the last duration without this
    return "\n".join(lines) + "\n"


def encode(frames, out_dir, speed, width=960):
    """screencast.mp4 and screencast.gif with ffmpeg, else screencast.gif with Pillow. Returns {kind: path} and notes."""
    out_dir, made, notes = Path(out_dir), {}, []
    if not frames:
        return made, ["no frames were recorded"]
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        script = frames[0][1].parent / "frames.ffconcat"
        script.write_text(concat_script(frames, speed))
        scale = f"scale={width}:-2:flags=lanczos"
        jobs = {"mp4": ["-vf", f"fps=15,{scale}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
                "gif": ["-vf", f"fps=10,{scale},split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer",
                        "-loop", "0"]}
        for kind, options in jobs.items():
            target = out_dir / f"screencast.{kind}"
            result = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(script),
                                     *options, str(target)], capture_output=True, text=True)
            if result.returncode == 0 and target.is_file():
                made[kind] = target
            else:
                notes.append(f"ffmpeg {kind} failed: {result.stderr.strip()[-300:]}")
        return made, notes
    try:
        from PIL import Image
    except ImportError:
        return made, ["no screencast: install ffmpeg (brew install ffmpeg) or Pillow; the frames are kept"]
    images, durations = [], []
    for index, (taken, path) in enumerate(frames):
        following = frames[index + 1][0] if index + 1 < len(frames) else taken + speed
        image = Image.open(path).convert("RGB")
        images.append(image.resize((width, round(image.height * width / image.width))))
        durations.append(max(20, round((following - taken) / speed * 1000)))
    target = out_dir / "screencast.gif"
    images[0].save(target, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
    made["gif"] = target
    notes.append("ffmpeg not found: GIF only (brew install ffmpeg adds an MP4)")
    return made, notes


# ---------------------------------------------------------------------------------------------------------- checks

class Smoke:
    def __init__(self, session, recorder, console):
        self.session, self.recorder, self.console = session, recorder, console
        self.checks = []

    def record(self, check_id, title, passed, detail):
        self.checks.append({"id": check_id, "title": title, "status": "PASS" if passed else "FAIL", "detail": detail})
        return passed

    def lcd(self):
        """The current LCD snapshot as PNG bytes (the snapshot is removed again)."""
        self.recorder.shot()
        path = self.session.snapshot("smoke")
        try:
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    def wait_settled(self, timeout=30.0, stable=0.6):
        """(seconds, PNG bytes) once two snapshots `stable` seconds apart are identical and not blank, else
        (None, last PNG). The guest is ready before the firmware's app has drawn, so a black screen is not settled."""
        t0, previous, data = time.monotonic(), None, None
        while time.monotonic() - t0 < timeout:
            data = self.lcd()
            if data == previous and not png_uniform(data):
                return time.monotonic() - t0, data
            previous = data
            self.recorder.hold(stable)
        return None, data

    def press(self, control, seconds=0.15):
        self.session.event(control, "press", 100)  # 100: the pressure hwsuite uses for a firm press
        self.recorder.hold(seconds)
        self.session.event(control, "release")
        self.recorder.shot()

    def boot(self, ready_s):
        ready = hwsuite.console_lines(self.console(), "SP16_NATIVE_READY")
        verified = bool(ready) and "app_sha256_verified=yes" in ready[0]
        return self.record("boot", "Firmware boots (ready marker, application verified)", verified,
                           f"ready after {ready_s:.1f} s: " + (ready[0][:140] if ready else "no SP16_NATIVE_READY"))

    def home(self, timeout=90.0):
        settled, data = self.wait_settled(timeout)
        size = png_info(data)[:2] if data else None
        blank = data is None or png_uniform(data)
        return self.record("home", "LCD settles on a non-blank 800x480 screen",
                           settled is not None and size == LCD_SIZE and not blank,
                           f"settled after {settled if settled is None else round(settled, 1)} s, size {size}, "
                           f"{'blank' if blank else 'not blank'}")

    def lamps(self):
        metrics = self.session.metrics()
        live = metrics.get("lamps_live") is True
        tx = (metrics.get("guest") or {}).get("tx_frames", 0)
        return self.record("lamps", "Firmware drives the panel lamps", live and tx > 0,
                           f"lamps_live {metrics.get('lamps_live')}, guest TX frames {tx}")

    def selector(self):
        before = self.lcd()
        self.session.event("selector", "delta", 1)
        t0, changed = time.monotonic(), None
        while time.monotonic() - t0 < 2.5:
            self.recorder.hold(0.1)
            if self.lcd() != before:
                changed = time.monotonic() - t0
                break
        self.session.event("selector", "delta", -1)
        self.wait_settled(timeout=10)
        return self.record("selector", "A selector detent changes the LCD", changed is not None,
                           f"changed after {changed:.2f} s" if changed is not None else "no change within 2.5 s")

    def pads(self, count=4):
        before = (self.session.metrics().get("guest") or {})
        for pad in range(1, count + 1):
            self.press(f"pad{pad}")
        self.recorder.hold(0.5)
        after = (self.session.metrics().get("guest") or {})
        events = after.get("events", 0) - before.get("events", 0)
        failed = after.get("failed", 0) - before.get("failed", 0)
        return self.record("pads", "Pad presses reach the firmware", events >= 2 * count and failed == 0,
                           f"{events} guest events for {2 * count} edges, {failed} failed")

    def transport(self, seconds=3.0):
        """PLAY for a few seconds, then STOP: visible in the screencast; the check is that both are accounted."""
        before = (self.session.metrics().get("guest") or {}).get("events", 0)
        self.press("play")
        self.recorder.hold(seconds)
        self.press("stop")
        self.recorder.hold(1.0)
        events = (self.session.metrics().get("guest") or {}).get("events", 0) - before
        return self.record("transport", "PLAY and STOP reach the firmware", events >= 4, f"{events} guest events")

    def usb(self):
        lines = hwsuite.console_lines(self.console(), "SP16_USB")
        mounted = any("mounted=yes" in line for line in lines)
        return self.record("usb", "The virtual USB stick is mounted", mounted,
                           "; ".join(line.strip()[:60] for line in lines[-3:]) or "no SP16_USB lines")


def report_markdown(result):
    lines = [f"# Smoke test {result['run_id']}", "",
             f"{result['verdict']}: {sum(c['status'] == 'PASS' for c in result['checks'])} of "
             f"{len(result['checks'])} checks passed in {result['seconds']:.0f} s "
             f"(build `{result['build']}`, firmware `{result.get('firmware') or 'selected'}`).", "",
             "| Check | Status | Detail |", "|---|---|---|"]
    lines += [f"| {c['title']} | {c['status']} | {c['detail']} |" for c in result["checks"]]
    if result.get("screencast"):
        lines += ["", "Screencast (" + f"{result['speed']:g}x speed): " +
                  ", ".join(f"`{Path(p).name}`" for p in result["screencast"].values())]
    lines += [f"- {note}" for note in result.get("notes", [])]
    return "\n".join(lines) + "\n"


def verdict(checks):
    return "PASS" if checks and all(c["status"] == "PASS" for c in checks) else "FAIL"


# ------------------------------------------------------------------------------------------------------------- run

def run(build, firmware=None, speed=DEFAULT_SPEED, interval=DEFAULT_INTERVAL, out=None, video=True, keep_frames=False,
        launcher=hwsuite.launch, stopper=hwsuite.stop, session_factory=hwsuite.Session):
    build = Path(build).resolve()
    assets = json.loads((build / "assets.json").read_text())
    if not assets.get("research"):
        raise hwsuite.EnvError(f"{build} was not built with --research (the smoke test needs the control socket)")
    if assets.get("audio") in ("coreaudio", "both"):
        raise hwsuite.EnvError(f"{build} plays sound; the smoke test only runs silent or --audio file builds")
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(out) if out else build / "smoke" / run_id
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    stick = out / "stick.img"
    t0 = time.monotonic()
    try:
        pid, runtime, ready_s = launcher(build, firmware_id=firmware, usb_image=stick, ephemeral=True)
    except hwsuite.EnvError:
        # A launch that timed out may have left its app running; the launcher refuses a build that already had a
        # session, so any app of this build now is the one it started.
        for leftover in hwsuite.build_processes(build)[0]:
            stopper(build, leftover)
        raise
    session = recorder = None
    checks, notes, screencast = [], [], {}
    try:
        session = session_factory(runtime)
        recorder = Recorder(session, out / "frames", interval, enabled=video)
        smoke = Smoke(session, recorder, session.console)
        recorder.shot(force=True)
        smoke.boot(ready_s)
        if smoke.home():
            smoke.lamps()
            smoke.selector()
            smoke.pads()
            smoke.transport()
        smoke.usb()
        recorder.hold(1.0)
        checks = smoke.checks
    finally:
        if session is not None:
            session.close()
        stopped = stopper(build, pid)
    console = (Path(runtime).parent / "console.txt").read_text(errors="replace")
    clean = any("SP16_HOST_CLEAN_STOP clean=yes" in line for line in console.splitlines())
    checks.append({"id": "stop", "title": "The app quits cleanly and its QEMU exits",
                   "status": "PASS" if stopped is not None and clean else "FAIL",
                   "detail": f"stopped in {stopped:.1f} s, clean stop {'yes' if clean else 'no'}" if stopped is not None
                   else "the app or its QEMU did not exit within 25 s"})
    if video and recorder is not None:
        screencast, notes = encode(recorder.frames, out, speed)
        if screencast and not keep_frames:
            shutil.rmtree(out / "frames", ignore_errors=True)
    stick.unlink(missing_ok=True)
    result = {"schema": 1, "run_id": run_id, "build": build.name, "firmware": firmware, "verdict": verdict(checks),
              "seconds": time.monotonic() - t0, "speed": speed, "checks": checks,
              "frames": len(recorder.frames) if recorder else 0,
              "screencast": {kind: str(path) for kind, path in screencast.items()}, "notes": notes,
              "session": str(Path(runtime).parent)}
    (out / "smoke-result.json").write_text(json.dumps(result, indent=2) + "\n")
    (out / "smoke-report.md").write_text(report_markdown(result))
    return result, out


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", required=True, help="a research build (silent or --audio file)")
    parser.add_argument("--firmware", help="firmware id in the build's library (default: the selected one)")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="screencast speed-up (default %(default)g)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between captured frames (default %(default)g)")
    parser.add_argument("--out", help="output folder (default <build>/smoke/<run id>)")
    parser.add_argument("--no-video", action="store_true", help="run the checks without recording")
    parser.add_argument("--keep-frames", action="store_true", help="keep the captured PNG frames")
    args = parser.parse_args(argv)
    if args.speed <= 0 or args.interval <= 0:
        parser.error("--speed and --interval must be positive")
    try:
        result, out = run(args.build_dir, args.firmware, args.speed, args.interval, args.out, not args.no_video,
                          args.keep_frames)
    except (hwsuite.EnvError, hwsuite.SessionLost, OSError, ValueError) as error:
        print(f"smoke: {error}", file=sys.stderr)
        return EXIT_ENV
    print(report_markdown(result), end="")
    print(f"Results: {out}")
    return EXIT_OK if result["verdict"] == "PASS" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
