#!/usr/bin/env python3
"""Collect diagnostics for a bug report: one zip with logs, session files and a system report, redacted.

Run it as `native.py diagnostics --output <build>`, from the app's Help > Collect Diagnostics... (the app runs the
copy bundled in Resources/fwlib), or directly (`python3 diagnostics.py --output <build>`). Nothing is uploaded: the
tool writes the zip and prints its path; the user decides whether to send it.

Contents: report.json (versions, build options, system, firmware library summary), the last N sessions (console, metrics,
runtime, app log, QEMU stderr, last screen; audio only with --include-audio), the build-level app logs, the build logs
(last 500 lines each), and the macOS crash reports of the last 14 days for the app and QEMU.

Never included: firmware images, updates, keys, data images (any file ending in .key, .upd, .sqfs, .img, .iso, .sfs or
.mot is refused by name). Every text file is redacted: the home folder becomes ~, the account name <user>, the host
name <host>, e-mail addresses <email>, and crash reports' machine identifiers are blanked. With --key-file the finished contents are also checked for the key in raw, hex and
base64 form (the key is held in memory and never printed); a hit refuses the whole zip.

Stdlib only and compatible with the system Python 3.9 that the app uses.
"""
import argparse
import base64
import datetime
import json
import os
from pathlib import Path
import platform
import plistlib
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile

TOOLS = Path(__file__).resolve().parent
TOOL_VERSION = 1
DEFAULT_SESSIONS = 3
REPORT_DAYS = 14
TAIL_LINES = 500                    # build logs
TAIL_BYTES = 8 << 20                # one text file never exceeds this in the zip (the end is kept)
MAX_CRASH_REPORTS = 10
MAX_APP_LOGS = 10
EXCLUDED_SUFFIXES = (".key", ".upd", ".sqfs", ".img", ".iso", ".sfs", ".mot")
SESSION_TEXT = ("console.txt", "metrics.json", "runtime.json", "app.log", "app.log.1", "qemu-stderr.txt", "clean-exit")
SESSION_BINARY = ("screen.png",)
TEXT_SUFFIXES = (".txt", ".json", ".jsonl", ".log", ".ips", ".md", ".1")
TEXT_NAMES = ("clean-exit",)
GENERIC_NAMES = {"root", "admin", "user", "guest", "test", "daemon", "nobody", "shell", "host", "localhost", "mac"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Stable machine and session identifiers that macOS writes into crash reports (.ips); their values are blanked.
MACHINE_IDS = re.compile(r'("(?:crashReporterKey|bootSessionUUID|sleepWakeUUID|deviceIdentifierForVendor'
                         r'|deviceIdentifierForAdvertising)"\s*:\s*)"[^"]*"')


class DiagnosticsError(RuntimeError):
    pass


class KeyFound(DiagnosticsError):
    """The key occurs in the contents: the zip is refused (the message never contains key bytes)."""


# ------------------------------------------------------------------------------------------------------- redaction

class Redactor:
    """Replaces personal identifiers in text: e-mail, home folder (~), account name (<user>), full name, host name."""

    def __init__(self, home=None, user=None, host=None, full_name=None):
        home = home if home is not None else str(Path.home())
        homes = {h.rstrip("/") for h in (home, os.path.realpath(home)) if h and h.rstrip("/")}
        # Crash reports are JSON with escaped slashes (\/Users\/name), so the escaped spelling is replaced too.
        self.homes = sorted(homes | {h.replace("/", "\\/") for h in homes}, key=len, reverse=True)
        self.user = user if user is not None else _account_name()
        self.host = host if host is not None else socket.gethostname()
        self.full_name = full_name if full_name is not None else _full_name()
        self.counts = {"email": 0, "home": 0, "user": 0, "host": 0, "name": 0, "machine_id": 0}
        names = [self.host, self.host.split(".")[0]] if self.host else []
        self.host_pattern = self._words([n for n in names if len(n) >= 3 and n.lower() not in GENERIC_NAMES])
        self.user_pattern = self._words([self.user] if self.user and len(self.user) >= 3
                                        and self.user.lower() not in GENERIC_NAMES else [])
        self.name_pattern = self._words([self.full_name] if self.full_name and len(self.full_name) >= 5 else [])

    @staticmethod
    def _words(words):
        if not words:
            return None
        parts = sorted({re.escape(w) for w in words}, key=len, reverse=True)
        return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])", re.IGNORECASE)

    def _sub(self, pattern, replacement, text, key):
        if pattern is None:
            return text
        text, count = pattern.subn(replacement, text)
        self.counts[key] += count
        return text

    def text(self, text):
        text, count = EMAIL.subn("<email>", text)
        self.counts["email"] += count
        text, count = MACHINE_IDS.subn(r'\1"<redacted>"', text)
        self.counts["machine_id"] += count
        for home in self.homes:
            count = text.count(home)
            if count:
                self.counts["home"] += count
                text = text.replace(home, "~")
        text = self._sub(self.name_pattern, "<name>", text, "name")
        text = self._sub(self.user_pattern, "<user>", text, "user")
        text = self._sub(self.host_pattern, "<host>", text, "host")
        return text

    def data(self, data):
        return self.text(data.decode("utf-8", errors="replace")).encode("utf-8")


def _account_name():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return os.environ.get("USER", "")


def _full_name():
    try:
        return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0]
    except KeyError:
        return ""


def is_excluded(name):
    """Files that must never enter the zip: firmware images, updates, keys, data images."""
    return str(name).lower().endswith(EXCLUDED_SUFFIXES)


def is_text(name):
    name = str(name).lower()
    return name.endswith(TEXT_SUFFIXES) or Path(name).name in TEXT_NAMES


def tail_lines(data, count=TAIL_LINES):
    lines = data.splitlines(keepends=True)
    if len(lines) <= count:
        return data
    note = f"[diagnostics: truncated to the last {count} of {len(lines)} lines]\n".encode()
    return note + b"".join(lines[-count:])


def tail_bytes(data, limit=TAIL_BYTES):
    if len(data) <= limit:
        return data
    cut = data[-limit:]
    cut = cut[cut.find(b"\n") + 1:] if b"\n" in cut else cut
    return f"[diagnostics: truncated to the last {limit} bytes of {len(data)}]\n".encode() + cut


# ------------------------------------------------------------------------------------------------ system facts

def _run(command, timeout=10):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_commit(start):
    """Short HEAD of the git repository containing start (up to five folders up), or None."""
    probe = Path(start).resolve()
    for folder in [probe, *list(probe.parents)[:5]]:
        if (folder / ".git").exists():
            commit = _run(["git", "-C", str(folder), "rev-parse", "--short", "HEAD"])
            return commit or None
    return None


def bundle_info(script=None):
    """Info.plist of the app bundle when this tool runs from Contents/Resources/fwlib."""
    script = Path(script or __file__).resolve()
    plist = script.parent.parent.parent / "Info.plist"
    return read_plist(plist)


def read_plist(plist):
    try:
        with open(plist, "rb") as source:
            info = plistlib.load(source)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return {"bundle_id": info.get("CFBundleIdentifier"), "version": info.get("CFBundleShortVersionString")}


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def scrub_arguments(arguments):
    """QEMU arguments without host folders: an absolute path keeps only its file name."""
    out = []
    for item in arguments or []:
        out.append(re.sub(r"(?<![\w.-])/(?:[^\s,=]*/)+([^\s,=/]*)", r"<path>/\1", item) if isinstance(item, str) else item)
    return out


def build_options(build):
    """default-config.json of the app bundle and assets.json, without paths."""
    assets = read_json(build / "assets.json") or {}
    options = {"assets": {k: assets.get(k) for k in ("decoupled", "qemu_version", "profile", "research", "trace", "audio",
                                                      "audio_device", "pad_calibration", "persistent_data")
                          if k in assets}}
    # A CoreAudio device UID can carry a USB serial number or a Bluetooth address: report only that one was chosen.
    if options["assets"].get("audio_device"):
        options["assets"]["audio_device"] = "<set>"
    config = None
    bundle = assets.get("app_bundle")
    candidates = [Path(bundle)] if bundle else []
    candidates += sorted(build.glob("*.app"))
    for app in candidates:
        config = read_json(app / "Contents/Resources/default-config.json")
        if config:
            options["bundle"] = {**(read_plist(app / "Contents/Info.plist") or {}), "name": app.name}
            options["build_info"] = read_json(app / "Contents/Resources/build-info.json")
            break
    if config:
        options["default_config"] = {
            "variant": config.get("variant"), "profile": config.get("profile"), "research": config.get("research"),
            "audio": (config.get("audio") or "").split(":")[0] or None, "data_size": (config.get("data") or {}).get("size"),
            "decoupled": "firmware" in config, "arguments": scrub_arguments(config.get("arguments"))}
    return options, assets


def system_report(build, assets, now):
    qemu = assets.get("qemu")
    qemu_version = _run([qemu, "--version"]) if qemu and Path(qemu).is_file() else None
    docker = shutil.which("docker") or next((p for p in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                                                         "/Applications/Docker.app") if Path(p).exists()), None)
    try:
        free = shutil.disk_usage(build).free
    except OSError:
        free = None
    return {
        "macos": {"product": _run(["/usr/bin/sw_vers", "-productName"]), "version": _run(["/usr/bin/sw_vers", "-productVersion"]),
                  "build": _run(["/usr/bin/sw_vers", "-buildVersion"])},
        "hardware_model": _run(["/usr/sbin/sysctl", "-n", "hw.model"]),
        "cpu": _run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor() or platform.machine(),
        "cpu_cores": os.cpu_count(),
        "memory_bytes": int(_run(["/usr/sbin/sysctl", "-n", "hw.memsize"]) or 0) or None,
        "architecture": platform.machine(),
        "free_disk_bytes": free,
        "python": sys.version.split()[0],
        "docker_present": bool(docker),
        "qemu_version": qemu_version.splitlines()[0] if qemu_version else None,
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
    }


# ------------------------------------------------------------------------------------------------ firmware library

def library_summary(library):
    """Per entry: identity, status and reason, hashes and compatibility findings. No firmware files."""
    summary = {"selected": (read_json(library / "selected.json") or {}).get("id"), "entries": []}
    try:
        import firmware
    except Exception:  # noqa: BLE001  (the bundled tool may lack its imports; the raw manifest is still reported)
        firmware = None
    try:
        names = sorted(p.name for p in library.iterdir() if p.is_dir() and (p / "manifest.json").is_file())
    except OSError:
        return summary
    for name in names:
        manifest = read_json(library / name / "manifest.json") or {}
        entry = read_json(library / name / "entry.json") or {}
        update = manifest.get("update") if isinstance(manifest.get("update"), dict) else {}
        image = manifest.get("image") if isinstance(manifest.get("image"), dict) else {}
        item = {"id": name, "name": entry.get("name"), "version": update.get("trailer_version"),
                "kind": manifest.get("kind"), "update_sha256": update.get("sha256"), "app_sha256": manifest.get("app_sha256"),
                "image_sha256": image.get("sha256"), "image_bytes": image.get("bytes"), "compat": manifest.get("compat"),
                "mod": manifest.get("mod"), "status": "unknown", "verified": None, "reason": None, "profile": None}
        if firmware is not None:
            try:
                firmware.validate_manifest(manifest, name)
                status = firmware.boot_status(manifest, None)
                item.update(status=status["status"], verified=status["status"] == "verified", reason=status["reason"],
                            profile=status["profile"])
                report = firmware.check_report(manifest, None)
                item["boot_profile"] = report.get("boot")
                item["checks"] = [{"check": c.get("check"), "result": c.get("result")} for c in report.get("checks", [])]
            except Exception as error:  # noqa: BLE001
                item["error"] = str(error)[:300]
        summary["entries"].append(item)
    return summary


# ------------------------------------------------------------------------------------------------ file selection

def session_dirs(build, count):
    """The newest sessions (by the time runtime.json was written), newest first."""
    found = []
    for folder in (build / "sessions").glob("native-*"):
        if not folder.is_dir() or folder.is_symlink():
            continue
        marker = folder / "runtime.json"
        stamp = marker.stat().st_mtime if marker.exists() else folder.stat().st_mtime
        found.append((stamp, folder))
    return [folder for _, folder in sorted(found, reverse=True)[:max(0, count)]]


def crash_reports(now, root=None):
    root = Path(root) if root else Path.home() / "Library/Logs/DiagnosticReports"
    found = []
    for pattern in ("SP16Emulator-*.ips", "qemu-system-aarch64-*.ips"):
        for path in root.glob(pattern):
            try:
                if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime <= REPORT_DAYS * 86400:
                    found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    return [path for _, path in sorted(found, reverse=True)[:MAX_CRASH_REPORTS]]


def iso(stamp):
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------------------- the zip

class Bundle:
    """Writes redacted, checked entries into a zip and records what went in and what was refused."""

    def __init__(self, archive, redactor, key_forms=()):
        self.zip = archive
        self.redactor = redactor
        self.key_forms = [f for f in key_forms if len(f) >= 8]
        self.entries = []
        self.refused = []

    def add_bytes(self, arcname, data, stamp=None):
        if is_excluded(arcname):
            self.refused.append(arcname)
            return False
        for form in self.key_forms:
            if form in data:
                raise KeyFound(f"the key occurs in {arcname}; no diagnostics were written")
        info = zipfile.ZipInfo(arcname, time.localtime(stamp or time.time())[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        self.zip.writestr(info, data)
        self.entries.append({"path": arcname, "bytes": len(data)})
        return True

    def add_file(self, arcname, path, tail=None):
        path = Path(path)
        if is_excluded(path.name) or is_excluded(arcname):
            self.refused.append(arcname)
            return False
        if path.is_symlink() or not path.is_file():
            return False
        try:
            data = path.read_bytes()
            stamp = path.stat().st_mtime
        except OSError:
            return False
        if is_text(arcname):
            data = tail_bytes(tail_lines(data, tail) if tail else data)
            data = self.redactor.data(data)
        return self.add_bytes(arcname, data, stamp)


def key_forms(key_file):
    """Byte strings that would reveal the key (in memory only): raw, unpadded, hex and base64 forms."""
    import firmware
    key = firmware.load_key(key_file)
    core = key.rstrip(b"\0")
    forms = {key, core, key.hex().encode(), key.hex().upper().encode(), core.hex().encode(),
             base64.b64encode(key), base64.b64encode(core), Path(key_file).read_bytes().strip()}
    return [f for f in forms if len(f) >= 8]


def default_zip(build, stamp):
    name = f"sp16-diagnostics-{stamp}.zip"
    desktop = Path.home() / "Desktop"
    if desktop.is_dir() and os.access(desktop, os.W_OK):
        return desktop / name
    return build / f"diagnostics-{stamp}.zip"


README = """SP16 Emulator diagnostics
=========================

Created {created} by diagnostics.py (tool version {version}). Nothing was uploaded: attach this zip to a bug report
yourself if you want to share it, after looking through it.

Contents
  report.json          emulator version and build options, macOS and hardware facts, the firmware library summary
                       (names, versions, kinds, verification status and reasons, hashes, compatibility findings)
  sessions/<id>/       the last {sessions} session(s): console.txt (guest console), metrics.json, runtime.json, app.log
                       (the app's own log), qemu-stderr.txt (QEMU's error output), screen.png (the last LCD frame){audio}
  logs/                app logs written outside a session (import screen, library actions) and the build logs
                       (last {tail} lines each)
  crash-reports/       macOS crash reports of the last {days} days for the app and QEMU, if any

Never included: firmware images, update files, keys, the data image with your projects, or any file ending in .key,
.upd, .sqfs, .img, .iso, .sfs or .mot.

Redacted in every text file: your home folder ({home_note}), your account name (<user>), your Mac's name (<host>),
your full name (<name>) and e-mail addresses (<email>); crash reports' machine identifiers are blanked and an
audio device UID is reported only as <set>. Redactions made: {counts}. Paths outside your home folder (for
example an external drive name) are kept. {key_note}
"""


def collect(output, sessions=DEFAULT_SESSIONS, include_audio=False, out=None, key_file=None, library=None,
            crash_root=None, redactor=None, now=None):
    """Write the diagnostics zip for a build; returns {"zip", "files", "bytes", "refused", "redactions"}."""
    build = Path(os.path.abspath(os.path.expanduser(str(output))))
    if not build.is_dir() or not ((build / "assets.json").is_file() or (build / "sessions").is_dir()):
        raise DiagnosticsError(f"{build} is not an emulator build folder (no assets.json or sessions)")
    now = now if now is not None else time.time()
    redactor = redactor or Redactor()
    forms = key_forms(key_file) if key_file else []
    stamp = datetime.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    options, assets = build_options(build)
    library_path = Path(library) if library else Path(assets["library"]) if assets.get("library") else build / "library"
    chosen = session_dirs(build, sessions)
    destination = Path(out) if out else default_zip(build, stamp)
    fallbacks = [destination] if out else [destination, build / f"diagnostics-{stamp}.zip"]
    last_error = None
    for target in fallbacks:
        try:
            return _write(target, build, options, assets, library_path, chosen, include_audio, key_file, forms, crash_root,
                          redactor, now, sessions)
        except KeyFound:
            raise
        except OSError as error:  # Desktop access can be refused by macOS: fall back to the build folder
            last_error = error
    raise DiagnosticsError(f"cannot write the diagnostics zip: {last_error}")


def _write(target, build, options, assets, library_path, chosen, include_audio, key_file, forms, crash_root, redactor, now,
          sessions):
    target = Path(target)
    if target.suffix.lower() != ".zip":
        raise DiagnosticsError("the output file must end in .zip")
    if is_excluded(target.name):
        raise DiagnosticsError("refusing to write that file name")
    partial = target.with_name("." + target.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
            bundle = Bundle(archive, redactor, forms)
            session_notes = []
            for folder in chosen:
                runtime = read_json(folder / "runtime.json") or {}
                try:
                    ready = b"SP16_NATIVE_READY" in (folder / "console.txt").read_bytes()
                except OSError:
                    ready = False
                session_notes.append({
                    "id": folder.name, "started": iso((folder / "runtime.json").stat().st_mtime)
                    if (folder / "runtime.json").exists() else None,
                    "clean_exit": (folder / "clean-exit").exists(), "firmware": runtime.get("firmware"),
                    "ready_seen": ready,
                    "files": []})
                for name in SESSION_TEXT + SESSION_BINARY:
                    if bundle.add_file(f"sessions/{folder.name}/{name}", folder / name):
                        session_notes[-1]["files"].append(name)
                if include_audio:
                    for wav in sorted(folder.glob("*.wav")):
                        if bundle.add_file(f"sessions/{folder.name}/{wav.name}", wav):
                            session_notes[-1]["files"].append(wav.name)
            logs = sorted((build / "logs").glob("app-*.log*"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_APP_LOGS]
            for path in logs:
                bundle.add_file(f"logs/{path.name}", path)
            for path in sorted(p for p in build.glob("*.log") if p.is_file()):
                bundle.add_file(f"logs/build/{path.name}", path, tail=TAIL_LINES)
            for path in crash_reports(now, crash_root):
                bundle.add_file(f"crash-reports/{path.name}", path)
            report = {
                "tool_version": TOOL_VERSION, "created": iso(now),
                "arguments": {"sessions": sessions, "include_audio": include_audio, "key_checked": bool(key_file)},
                "emulator": {"tool_git_commit": git_commit(TOOLS), "tool_bundle": bundle_info(),
                             "app_build_info": options.get("build_info"), "app_bundle": options.get("bundle")},
                "build_options": {k: v for k, v in options.items() if k not in ("build_info", "bundle")},
                "system": system_report(build, assets, now),
                "firmware_library": library_summary(library_path),
                "sessions": session_notes,
                "included": bundle.entries, "refused_by_name": bundle.refused,
            }
            report_data = redactor.data(json.dumps(report, indent=2, sort_keys=True, default=str).encode())
            readme = README.format(
                created=iso(now), version=TOOL_VERSION, sessions=len(chosen), tail=TAIL_LINES, days=REPORT_DAYS,
                audio=" and its WAV files" if include_audio else "", home_note="shown as ~",
                counts=", ".join(f"{k} {v}" for k, v in redactor.counts.items() if v) or "none",
                key_note="The contents were also checked for the update key (not found)." if key_file else "")
            bundle.add_bytes("report.json", report_data)
            bundle.add_bytes("README.txt", readme.encode())
        os.replace(partial, target)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise
    return {"zip": str(target), "files": len(bundle.entries), "bytes": target.stat().st_size,
            "refused": bundle.refused, "redactions": dict(redactor.counts)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", required=True, help="the emulator build folder")
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS, help="how many recent sessions (default 3)")
    parser.add_argument("--include-audio", action="store_true", help="add the sessions' WAV captures (can be large)")
    parser.add_argument("--out", help="zip to write (default ~/Desktop/sp16-diagnostics-<time>.zip)")
    parser.add_argument("--key-file", help="also prove the update key is absent from the contents (never printed)")
    parser.add_argument("--library", help="firmware library folder (default: the build's)")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = collect(args.output, args.sessions, args.include_audio, args.out, args.key_file, args.library)
    except KeyFound as error:
        print(json.dumps({"status": "diagnostics_refused", "error": str(error)}))
        return 1
    except (DiagnosticsError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2
    print(json.dumps({"status": "diagnostics_written", **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
