#!/usr/bin/env python3
"""sp16emu: one command to build, run and manage the SP-16 compatible emulator.

A thin wrapper over the tools in this folder (native.py build|launch|firmware|diagnostics, fwdev.py, hwsuite.py,
sessionctl.py). It adds defaults that remove friction (one shared firmware library, automatic QEMU reuse, one folder per
build under build/) and passes every option of those tools through. It runs on the Python 3.9 that ships with the Xcode
Command Line Tools; the build tools themselves run on the first Python 3.12 or later it finds.

    ./sp16emu help                  every command
    ./sp16emu help build            options and examples of one command
    ./sp16emu --dry-run build       print the exact tool commands instead of running them
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_NAME = "sp16"
DEV_NAME = "sp16-dev"
CONTROL_NAME = "sp16-dev-uncal"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")
SUFFIX_RE = re.compile(r"[a-z0-9-]{1,32}")
MIN_TOOL_PYTHON = (3, 12)
MIN_CLI_PYTHON = (3, 9)
MIN_FREE_BYTES = 3 * 1024 ** 3
PROFILE_BUNDLE_ID = "local.sp16-compatible.emulator.fullpanel"
AUDIO_SOURCES = ("audio-selftest.swift", "audio-output.swift", "audio-sink.swift")
AUDIO_SWIFT_FLAGS = ("-O", "-parse-as-library", "-framework", "AVFoundation", "-framework", "CoreAudio")


class CliError(Exception):
    """A failure that is reported as one sentence with its fix."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class UsageError(CliError):
    def __init__(self, message):
        super().__init__(message, 2)


def detect_root(here):
    """The public layout is <root>/emulator/sp16emu.py; the private one is <root>/tools/<tools folder>/sp16emu.py."""
    return here.parent if here.name == "emulator" else here.parents[1]


# ------------------------------------------------------------------------------------------------ option specs
class Opt:
    """One option, described once: the parser, the help text and the tool command line all come from it."""

    def __init__(self, flags, help, metavar=None, nargs=None, flag=False, emit=True, required=False, choices=None,
                 path=False):
        self.flags = (flags,) if isinstance(flags, str) else tuple(flags)
        self.help = help
        self.metavar = metavar
        self.nargs = nargs
        self.flag = flag
        self.emit = emit
        self.required = required
        self.choices = choices
        self.path = path
        self.dest = self.flags[0].lstrip("-").replace("-", "_")

    @property
    def usage(self):
        flags = " | ".join(self.flags)
        if self.flag:
            return flags
        meta = self.metavar or self.dest.upper()
        return f"{flags} {meta}"


class Pos:
    """A positional argument."""

    def __init__(self, dest, help, choices=None, nargs=None, metavar=None):
        self.dest, self.help, self.choices, self.nargs = dest, help, choices, nargs
        self.metavar = metavar or dest.upper()


class Parser(argparse.ArgumentParser):
    intermixed = False

    def error(self, message):
        command = self.prog.split()[-1]
        raise UsageError(f"{command}: {message}; see: sp16emu help {command}")

    def parse_args(self, args=None, namespace=None):
        if self.intermixed:  # options and positionals in any order (sp16emu usb add a.wav --to kicks b.wav)
            return self.parse_intermixed_args(args, namespace)
        return super().parse_args(args, namespace)


class Command:
    def __init__(self, name, group, summary, description, opts=(), positionals=(), examples=(), notes=(), usage=None,
                 passthrough=None, handler=None, direct=False, intermixed=False):
        self.name, self.group, self.summary, self.description = name, group, summary, description
        self.opts, self.positionals, self.examples, self.notes = list(opts), list(positionals), list(examples), list(notes)
        self.usage = usage
        self.passthrough = passthrough
        self.handler = handler
        self.direct = direct  # the handler runs the command itself and returns an exit code
        self.intermixed = intermixed

    def parser(self):
        parser = Parser(prog=f"sp16emu {self.name}", add_help=False, allow_abbrev=False)
        parser.intermixed = self.intermixed
        for pos in self.positionals:
            kwargs = {"choices": pos.choices, "metavar": pos.metavar}
            if pos.nargs:
                kwargs["nargs"] = pos.nargs
            parser.add_argument(pos.dest, **kwargs)
        for opt in self.opts:
            kwargs = {"dest": opt.dest, "required": opt.required}
            if opt.flag:
                kwargs["action"] = "store_true"
            else:
                kwargs["metavar"] = opt.metavar or opt.dest.upper()
                if opt.nargs:
                    kwargs["nargs"] = opt.nargs
                if opt.choices:
                    kwargs["choices"] = opt.choices
            parser.add_argument(*opt.flags, **kwargs)
        return parser

    def emit(self, args, skip=()):
        """The options that were given, in the order they are declared, as tool command-line tokens."""
        tokens = []
        for opt in self.opts:
            if not opt.emit or opt.dest in skip:
                continue
            value = getattr(args, opt.dest, None)
            if opt.flag:
                if value:
                    tokens.append(opt.flags[0])
            elif value is not None:
                tokens.append(opt.flags[0])
                values = value if isinstance(value, list) else [value]
                tokens.extend(os.path.expanduser(str(v)) if opt.path else str(v) for v in values)
        return tokens


class Step:
    def __init__(self, argv, env=None, label=None):
        self.argv, self.env, self.label = list(argv), dict(env or {}), label


class Plan:
    def __init__(self, steps=(), notes=(), setup=(), after=()):
        self.steps, self.notes, self.setup, self.after = list(steps), list(notes), list(setup), list(after)


# --------------------------------------------------------------------------------------------------- context
class Context:
    """Everything the commands touch: locations, environment, process execution. Tests replace the parts they need."""

    def __init__(self, tools=None, root=None, build=None, env=None, dry_run=False, verbose=False, python=None,
                 out=None, err=None, interactive=None):
        self.tools = Path(tools) if tools else HERE
        self.root = Path(root) if root else detect_root(self.tools)
        env = dict(os.environ if env is None else env)
        self.env = env
        build = build or env.get("SP16EMU_BUILD_DIR")
        self.build = Path(build) if build else self.root / "build"
        self.dry_run = dry_run or env.get("SP16EMU_DRY_RUN") == "1"
        self.verbose = verbose or env.get("SP16EMU_VERBOSE") == "1"
        self._python = python or env.get("SP16EMU_PYTHON")
        self._python_cache = {}
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.preflight = True
        self.home = Path(env.get("HOME") or os.path.expanduser("~"))  # the privacy-protected folders live here

    # output
    def say(self, text):
        for line in str(text).splitlines() or [""]:
            print(f"sp16emu: {line}", file=self.err)

    def tool(self, name):
        return str(self.tools / name)

    def display(self, path):
        """A path as the user thinks of it: relative to the repository when it is inside it."""
        path = Path(path)
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # processes
    def call(self, argv, env=None):
        child = dict(self.env)
        child["PYTHONDONTWRITEBYTECODE"] = "1"
        child.update(env or {})
        try:
            return subprocess.call(argv, env=child)
        except FileNotFoundError:
            raise CliError(f"cannot run {argv[0]}: command not found")
        except KeyboardInterrupt:
            return 130

    def capture(self, argv, timeout=30):
        """(exit code, stdout) of a quiet helper command; (127, '') when it cannot start; (124, '') on timeout."""
        try:
            done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                  timeout=timeout, env=self.env)
            return done.returncode, done.stdout
        except FileNotFoundError:
            return 127, ""
        except subprocess.TimeoutExpired:
            return 124, ""

    def run_tool(self, argv, timeout=300):
        """(exit code, stdout, stderr) of a helper that changes something; (127, '', reason) when it cannot start."""
        try:
            done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                  timeout=timeout, env=self.env)
            return done.returncode, done.stdout, done.stderr
        except FileNotFoundError:
            return 127, "", f"{argv[0]}: command not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{argv[0]}: timed out after {timeout} s"

    def which(self, name):
        return shutil.which(name, path=self.env.get("PATH"))

    # tool interpreter
    def python(self, strict=True):
        """The interpreter that runs the build tools: the first Python 3.12 or later (3.9 for the lenient tools)."""
        minimum = MIN_TOOL_PYTHON if strict else MIN_CLI_PYTHON
        if minimum in self._python_cache:
            return self._python_cache[minimum]
        if self._python:
            found = self.python_version(self._python)
            if found is None or found < minimum:
                raise CliError(f"{self._python} is not Python {minimum[0]}.{minimum[1]} or later; "
                               "pass another with --python or SP16EMU_PYTHON")
            chosen = self._python
        else:
            chosen = None
            names = ["python3", "python3.14", "python3.13", "python3.12"]
            candidates = [sys.executable] + [p for n in names for p in [self.which(n)] if p]
            candidates += [f"{d}/{n}" for d in ("/opt/homebrew/bin", "/usr/local/bin") for n in names]
            seen = set()
            for candidate in candidates:
                if not candidate or candidate in seen or not os.access(candidate, os.X_OK):
                    continue
                seen.add(candidate)
                version = self.python_version(candidate)
                if version is not None and version >= minimum:
                    chosen = candidate
                    break
            if chosen is None and not strict:
                chosen = sys.executable
            if chosen is None:
                raise CliError(f"the build tools need Python {minimum[0]}.{minimum[1]} or later and none was found; "
                               "install it with: brew install python@3.12")
        self._python_cache[minimum] = chosen
        return chosen

    def python_version(self, path):
        if os.path.realpath(path) == os.path.realpath(sys.executable):
            return sys.version_info[:2]
        code, out = self.capture([path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], timeout=15)
        if code:
            return None
        try:
            major, minor = out.strip().split(".")
            return int(major), int(minor)
        except ValueError:
            return None


# ------------------------------------------------------------------------------------------- build discovery
class BuildInfo:
    def __init__(self, name, path, assets):
        self.name, self.path, self.assets = name, Path(path), assets

    @property
    def complete(self):
        return self.assets is not None

    @property
    def decoupled(self):
        return bool(self.assets) and self.assets.get("decoupled") is True

    @property
    def research(self):
        return bool(self.assets) and bool(self.assets.get("research"))

    @property
    def library(self):
        value = (self.assets or {}).get("library")
        return Path(value) if value else None

    @property
    def built(self):
        try:
            return (self.path / "assets.json").stat().st_mtime
        except OSError:
            return 0.0


def read_assets(path):
    try:
        data = json.loads((Path(path) / "assets.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_builds(ctx):
    builds = []
    try:
        entries = sorted(ctx.build.iterdir())
    except OSError:
        return builds
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "library" or entry.is_symlink() or not entry.is_dir():
            continue
        builds.append(BuildInfo(entry.name, entry, read_assets(entry)))
    return builds


def library_imports(path):
    """Number of firmware imports in a library folder (entries with a manifest)."""
    try:
        return sum(1 for p in Path(path).iterdir() if p.is_dir() and not p.is_symlink() and (p / "manifest.json").is_file())
    except OSError:
        return 0


def check_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name == "library":
        raise UsageError(f"build name {name!r} is not valid; use letters, digits, '.', '_' or '-' (at most 48), and not 'library'")
    return name


def running_pids(ctx, path):
    """Process ids whose command line mentions a path inside the folder (a running emulator app and its QEMU)."""
    code, out = ctx.capture(["ps", "-axo", "pid=,command="], timeout=10)
    if code:
        return []
    needle = str(path).rstrip("/") + "/"
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and needle in parts[1] and int(parts[0]) != os.getpid():
            pids.append(int(parts[0]))
    return pids


def build_hint(name, extra=""):
    return f"sp16emu build --name {name}{(' ' + extra) if extra else ''}"


def pick_build(ctx, name=None, need_research=False, prefer=(DEFAULT_NAME,), allow_incomplete=False):
    """The build a command works on: the named one, else the first preferred name that exists, else the only one."""
    builds = list_builds(ctx)
    usable = [b for b in builds if (b.decoupled or (allow_incomplete and not b.complete))]
    extra = "--research --audio file" if need_research else ""
    if name is not None:
        check_name(name)
        found = next((b for b in builds if b.name == name), None)
        if found is None or not (found.complete or allow_incomplete):
            others = [b.name for b in usable]
            tail = f" (existing builds: {', '.join(others)})" if others else ""
            raise CliError(f"{ctx.display(ctx.build / name)} does not exist; create it with: {build_hint(name, extra)}{tail}")
        if found.complete and not found.decoupled:
            raise CliError(f"{ctx.display(found.path)} is not a firmware-free build, which sp16emu does not manage")
        chosen = found
    else:
        pool = [b for b in usable if b.research] if need_research else usable
        chosen = next((b for want in prefer for b in pool if b.name == want), None)
        if chosen is None and len(pool) == 1:
            chosen = pool[0]
        if chosen is None:
            if not pool:
                kind = "research build" if need_research else "build"
                raise CliError(f"there is no {kind} in {ctx.display(ctx.build)}; create one with: "
                               f"{build_hint(prefer[0], extra)}")
            raise CliError(f"several builds exist ({', '.join(b.name for b in pool)}); say which with --build NAME")
    if need_research and not chosen.research:
        raise CliError(f"{ctx.display(chosen.path)} is not a research build; rebuild it with: "
                       f"{build_hint(chosen.name, '--rebuild --research --audio file')}")
    return chosen


def sessions_of(build_path):
    try:
        folders = [p for p in (Path(build_path) / "sessions").iterdir() if p.is_dir() and (p / "runtime.json").is_file()]
    except OSError:
        return []
    return sorted(folders, key=lambda p: (p / "runtime.json").stat().st_mtime, reverse=True)


# ------------------------------------------------------------------------------------------------------ build
def qemu_version(ctx):
    try:
        match = re.search(r'^QEMU_VERSION = "([^"]+)"', (ctx.tools / "native.py").read_text(), re.MULTILINE)
    except OSError:
        return None
    return match.group(1) if match else None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def qemu_donor_ok(ctx, info):
    """A build whose verified QEMU can be copied: a decoupled build whose QEMU still matches its recorded hash."""
    assets = info.assets
    if not info.decoupled or not assets.get("qemu"):
        return False
    wanted = qemu_version(ctx)
    if wanted and assets.get("qemu_version") != wanted:
        return False
    try:
        return sha256_file(assets["qemu"]) == assets["hashes"][assets["qemu"]]
    except (OSError, KeyError, TypeError):
        return False


def find_qemu_donor(ctx, name):
    """The newest other build in build/ with a usable QEMU, or None."""
    donors = sorted((b for b in list_builds(ctx) if b.name != name and b.complete), key=lambda b: b.built, reverse=True)
    for info in donors:
        if qemu_donor_ok(ctx, info):
            return info
    return None


def resolve_library(ctx, name, explicit, previous):
    """(library path, notes): one shared firmware library for all builds. Nothing is moved or deleted."""
    shared = ctx.build / "library"
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit))), []
    if previous and previous.library:
        return previous.library, []  # a rebuild keeps the library the build already uses
    if library_imports(shared):
        return shared, []
    found = []
    seen = set()
    own = ctx.build / name / "library"
    candidates = [(b.library, b) for b in list_builds(ctx) if b.library] + [(own, None)]
    for path, info in candidates:
        key = os.path.realpath(path)
        if key in seen or key == os.path.realpath(shared) or not library_imports(path):
            continue
        seen.add(key)
        found.append((library_imports(path), info.built if info else 0.0, Path(path)))
    if not found:
        return shared, []
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen = found[0][2]
    notes = [f"reusing the existing firmware library {ctx.display(chosen)} ({found[0][0]} import"
             f"{'s' if found[0][0] != 1 else ''}); nothing was moved or deleted, and new builds share it"]
    if len(found) > 1:
        others = ", ".join(ctx.display(p) for _, _, p in found[1:])
        notes.append(f"other firmware libraries were left untouched: {others}; choose one explicitly with --library DIR")
    return chosen, notes


def ensure_build_root(ctx):
    path = ctx.build
    if path.is_symlink():
        raise CliError(f"{ctx.display(path)} is a symlink; the build tool refuses it, so replace it with a real folder")
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
        os.chmod(str(path), 0o700)
    elif not path.is_dir():
        raise CliError(f"{ctx.display(path)} exists but is not a folder; move it away")
    elif path.stat().st_mode & 0o077:
        raise CliError(f"{ctx.display(path)} is readable by others and the build tool refuses that; run: "
                       f"chmod 700 '{path}'")


def preflight_build(ctx, name, need_qemu_toolchain):
    """Fail in seconds with the fix, not minutes into a build."""
    problems = []
    if platform.system() != "Darwin" or not is_apple_silicon(ctx):
        problems.append("the emulator needs a Mac with Apple silicon (Hypervisor.framework); this machine is not one")
    if not ctx.which("swiftc"):
        problems.append("swiftc is missing; install the Xcode Command Line Tools with: xcode-select --install")
    if not ctx.which("docker"):
        problems.append("Docker is not installed; install Docker Desktop (it builds the Linux guest image)")
    else:
        code, _ = ctx.capture(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=25)
        if code:
            problems.append("Docker is not running; start Docker Desktop and retry (it builds the Linux guest image)")
    if need_qemu_toolchain:
        missing = [t for t in ("ninja",) if not ctx.which(t)]
        pkg = ctx.which("pkg-config") or ctx.which("pkgconf")
        if not pkg:
            missing.append("pkgconf")
        else:
            code, _ = ctx.capture([pkg, "--exists", "glib-2.0", "pixman-1"])
            if code:
                missing.append("glib/pixman")
        if missing:
            problems.append("QEMU needs build tools that are missing (" + ", ".join(missing) + "); install them with: "
                            "brew install ninja pkgconf glib pixman")
    problems += git_problems(ctx, name)
    if problems:
        raise CliError("cannot build yet:\n  - " + "\n  - ".join(problems) + "\nRun `sp16emu doctor` for the full check.")


def is_apple_silicon(ctx):
    code, out = ctx.capture(["sysctl", "-n", "hw.optional.arm64"], timeout=5)
    return out.strip() == "1" if code == 0 else platform.machine() == "arm64"


def git_problems(ctx, name):
    """The build tool only writes inside a git work tree where the build folder is git-ignored (so nothing it holds
    can be committed by accident)."""
    code, top = ctx.capture(["git", "-C", str(ctx.root), "rev-parse", "--show-toplevel"], timeout=10)
    if code:
        return [f"{ctx.root} is not a git work tree; clone the repository with git, or run `git init -b main` there"]
    ignored, _ = ctx.capture(["git", "-C", str(ctx.root), "check-ignore", "-q", "--", str(ctx.build / name)], timeout=10)
    if ignored:
        return [f"{ctx.display(ctx.build)} is not git-ignored here; add a line `/build/` to .gitignore"]
    return []


def default_suffix(name):
    return name if name != DEFAULT_NAME and SUFFIX_RE.fullmatch(name) else None


BUILD_OPTS = [
    Opt("--name", "Build folder name: the build goes to build/NAME (default: sp16).", "NAME", emit=False),
    Opt("--audio", "Audio: file records each stream to a WAV in the session folder (silent); coreaudio plays "
        "through the Mac's output (AUDIBLE); both. Default: silent, nothing recorded.", "MODE", choices=("file", "coreaudio", "both")),
    Opt("--audio-device", "With coreaudio or both: the output device by CoreAudio UID (list them with "
        "`sp16emu audio devices`; default: the system output). offline:<wav>[:rate[:ppm[:block]]] renders the "
        "output to a file on a software clock instead (verification; opens no device).", "UID"),
    Opt("--research", "Add the owner-only research socket (events, raw frames, snapshots), used by `sp16emu ctl`, "
        "`sp16emu mod` and `sp16emu hwsuite`.", flag=True),
    Opt("--trace", "Research only: strace (syscall trace in console.txt), gdb (stub on port 1234, blocks the app "
        "until attached) or pcmtap (records 4 s of each playback stream into console.txt; no host audio).", "MODE",
        choices=("strace", "gdb", "pcmtap")),
    Opt("--pad-calibration-value", "Stage a synthetic pad calibration N for all pads (default: the profile's own "
        "value, not factory data).", "N"),
    Opt("--uncalibrated", "Stage no pad calibration so the firmware reports uncalibrated pads (they blink); the "
        "hardware suite's control build.", flag=True),
    Opt("--bundle-suffix", "Append .TOKEN to the app's bundle id and title (lowercase letters, digits, hyphens; at "
        "most 32). Default: the build name unless it is 'sp16', so each build has its own id.", "TOKEN"),
    Opt("--allow-unverified", "Accepted and ignored (every correctly keyed firmware boots, marked unverified when no "
        "verified profile matches).", flag=True),
    Opt("--library", "Firmware library folder. Default: the shared build/library (an existing library from an "
        "earlier build is reused in place; a rebuild keeps the library the build already uses).", "DIR", path=True),
    Opt("--reuse-qemu-from", "Copy the verified QEMU from this existing build folder. Default: automatic, from the "
        "newest other build in build/ that has one.", "DIR", path=True),
    Opt("--no-reuse-qemu", "Compile QEMU from source even if another build has one.", flag=True, emit=False),
    Opt("--rebuild", "Rebuild an existing build/NAME in place: the app, guest image and settings are replaced; the "
        "firmware library, data images and QEMU are kept. Pass the options again (they are not remembered).",
        flag=True, emit=False),
    Opt("--no-preflight", "Skip the Docker, tool and git checks that run before a build.", flag=True, emit=False),
]


def plan_build(ctx, args, tail):
    name = check_name(args.name or DEFAULT_NAME)
    out = ctx.build / name
    previous = BuildInfo(name, out, read_assets(out))
    notes = []
    if previous.complete and not args.rebuild:
        raise CliError(f"{ctx.display(out)} already exists; run it with `sp16emu run --name {name}`, rebuild it in "
                       f"place (keeps firmware and data) with `sp16emu build --name {name} --rebuild`, or pick "
                       "another --name")
    if previous.complete and running_pids(ctx, out):
        raise CliError(f"{ctx.display(out)} is running; quit it first (stop only its own process ids: "
                       f"ps -axo pid,command | grep -F '{out}/')")
    if args.reuse_qemu_from and args.no_reuse_qemu:
        raise UsageError("build: --reuse-qemu-from and --no-reuse-qemu exclude each other")
    library, library_notes = resolve_library(ctx, name, args.library, previous if previous.complete else None)
    notes += library_notes
    donor_path = None
    if args.reuse_qemu_from:
        donor_path = args.reuse_qemu_from
    elif not args.no_reuse_qemu:
        own = previous if previous.complete and qemu_donor_ok(ctx, previous) else None
        donor = own or find_qemu_donor(ctx, name)
        if donor:
            donor_path = str(donor.path)
            notes.append(f"reusing the verified QEMU of {ctx.display(donor.path)}"
                         + (" (rebuild in place)" if donor is own else ""))
    if donor_path is None and not args.no_reuse_qemu:
        notes.append("no build to take QEMU from: QEMU is downloaded and compiled (a few minutes, first build only)")
    args.library = str(library)
    args.reuse_qemu_from = donor_path
    if args.bundle_suffix is None:
        args.bundle_suffix = default_suffix(name)
    python = ctx.python()
    argv = [python, ctx.tool("native.py"), "build", "--decoupled", "--profile", "fullpanel", "--output", str(out)]
    argv += BUILD_COMMAND.emit(args) + list(tail)
    if previous.complete and args.rebuild:
        notes.append(f"rebuilding {ctx.display(out)} in place; the firmware library and data images are kept")
    setup = []
    if not ctx.dry_run:
        setup.append(lambda: ensure_build_root(ctx))
        if ctx.preflight and not args.no_preflight:
            setup.append(lambda: preflight_build(ctx, name, donor_path is None))
    run_hint = "sp16emu run" + ("" if name == DEFAULT_NAME else f" --name {name}")
    if library_imports(library):
        after = [f"built {ctx.display(out)}. Next: {run_hint}"]
    else:
        after = [f"built {ctx.display(out)}. Next: import your firmware with `sp16emu firmware import --build {name} "
                 f"--update FILE.UPD --gpl-archive PART1 PART2` (or --key-file KEY), then `{run_hint}`"]
    return Plan([Step(argv)], notes, setup, after)


# -------------------------------------------------------------------------------------------------------- run
RUN_OPTS = [
    Opt(("--name", "--build"), "The build to launch (default: build/sp16, else the only build).", "NAME", emit=False),
    Opt("--firmware", "Firmware id from the library (see `sp16emu firmware list`); default: the selected one.", "ID"),
    Opt("--usb-image", "Attach this raw image as a virtual USB stick (created sparse if missing and formatted FAT32 "
        "on first attach). Cold attach: chosen at launch. Default: build/stick.img when it exists (see `sp16emu usb`).",
        "PATH", path=True),
    Opt("--no-usb", "Do not attach build/stick.img (it is attached automatically when it exists and no --usb-image "
        "is given).", flag=True, emit=False),
    Opt("--ephemeral", "Use a disposable data image for this session (nothing persists; no lock on the firmware's "
        "data, so a second app can run the same firmware).", flag=True),
    Opt("--dev-overlay", "Research builds: share this mod folder into the guest (see `sp16emu mod`).", "DIR", path=True),
]


def plan_run(ctx, args, tail):
    info = pick_build(ctx, args.name)
    notes = []
    library = info.library
    if library is not None and not library_imports(library):
        notes.append("the firmware library is empty: the app opens its import screen, or import first with "
                     "`sp16emu firmware import --update FILE.UPD --key-file KEY`")
    if args.usb_image and args.no_usb:
        raise UsageError("run: --usb-image and --no-usb contradict each other; give one")
    stick, stick_notes = stick_for_run(ctx, args)
    if stick:
        args.usb_image = stick
    notes += stick_notes
    argv = [ctx.python(), ctx.tool("native.py"), "launch", "--output", str(info.path)] + RUN_COMMAND.emit(args) + list(tail)
    return Plan([Step(argv)], notes)


# ---------------------------------------------------------------------------------------------------- firmware
FIRMWARE_ACTIONS = ("import", "list", "show", "check", "select", "rename", "remove", "profile", "audit")
FIRMWARE_OPTS = [
    Opt("--build", "The build whose library and guest do the work (default: build/sp16, else the only build).", "NAME",
        emit=False),
    Opt("--update", "import: the update file (.UPD).", "FILE", path=True),
    Opt("--key-file", "import: the key file (read in memory, never written or printed).", "FILE", path=True),
    Opt("--gpl-archive", "import: the GPL source archive parts, in order (the key is read from them in memory).",
        "PART", nargs="+", path=True),
    Opt("--id", "show, check, select, rename, remove, profile: the firmware id (see `list`).", "ID"),
    Opt("--name", "import, rename: a display name.", "TEXT"),
    Opt("--from-profile", "profile: the profile whose measurements to start from (default: the product reference).", "ID"),
]


def plan_firmware(ctx, args, tail):
    action = args.action
    info = pick_build(ctx, args.build)
    if action == "import":
        if not args.update:
            raise UsageError("firmware import: --update FILE.UPD is required")
        if not (args.key_file or args.gpl_archive):
            raise UsageError("firmware import: give --key-file KEY or --gpl-archive PART1 PART2")
        for label, path in [("--update", args.update), ("--key-file", args.key_file)] + \
                [("--gpl-archive", p) for p in (args.gpl_archive or [])]:
            if path and not Path(path).expanduser().exists():
                raise CliError(f"{label}: {path} does not exist")
    elif action in ("show", "check", "select", "rename", "remove", "profile") and not args.id:
        raise UsageError(f"firmware {action}: --id ID is required (list ids with: sp16emu firmware list)")
    if action == "rename" and not args.name:
        raise UsageError("firmware rename: --name TEXT is required")
    argv = [ctx.python(), ctx.tool("native.py"), "firmware", action, "--output", str(info.path)]
    argv += FIRMWARE_COMMAND.emit(args) + list(tail)
    return Plan([Step(argv)])


# ----------------------------------------------------------------------------------------------- diagnostics
DIAGNOSTICS_OPTS = [
    Opt("--build", "The build folder to collect from (default: build/sp16, else the only build).", "NAME", emit=False),
    Opt("--sessions", "How many recent sessions to include (default 3).", "N"),
    Opt("--include-audio", "Also include the sessions' WAV captures (can be large).", flag=True),
    Opt("--out", "The zip to write (default ~/Desktop/sp16-diagnostics-<time>.zip, else the build folder).", "ZIP", path=True),
    Opt("--key-file", "Also prove that this key file's key is absent from the zip (never printed).", "FILE", path=True),
]


def plan_diagnostics(ctx, args, tail):
    info = pick_build(ctx, args.build, allow_incomplete=True)
    argv = [ctx.python(strict=False), ctx.tool("native.py"), "diagnostics", "--output", str(info.path)]
    argv += DIAGNOSTICS_COMMAND.emit(args) + list(tail)
    return Plan([Step(argv)])


# ---------------------------------------------------------------------------------------------------- mod
MOD_ACTIONS = ("new", "check", "run", "test", "sdk", "package")
MOD_OPTS = [
    Opt("--mod", "The mod folder (all actions).", "DIR", required=True, path=True),
    Opt("--build", "check, run, test, package: the build (run and test need a research build; default: "
        "build/sp16-dev, else the only research build).", "NAME", emit=False),
    Opt("--id", "new: the mod id (lowercase letters, digits, hyphens).", "ID"),
    Opt("--from-recipe", "new: start from a built-in recipe such as utility-label.", "NAME"),
    Opt("--firmware", "check, run, test, package: the firmware id (default: the library's selected one).", "ID"),
    Opt("--baseline", "test: a stock LCD snapshot PNG to compare against.", "PNG", path=True),
    Opt("--expect-change", "test: the LCD region that must differ from the baseline.", "X0 Y0 X1 Y1", nargs=4),
    Opt("--touch", "test: tap the LCD here before the comparison snapshot.", "X Y", nargs=2),
    Opt("--out", "package: where to write the archive or update.", "PATH", path=True),
    Opt("--upd", "package: a repacked, re-encrypted update for this emulator only (needs --key-file or --gpl-archive).",
        flag=True),
    Opt("--key-file", "package --upd: the key file (never printed).", "FILE", path=True),
    Opt("--gpl-archive", "package --upd: the GPL source archive parts, in order.", "PART", nargs="+", path=True),
]


def plan_mod(ctx, args, tail):
    action = args.action
    argv = [ctx.python(), ctx.tool("fwdev.py"), action]
    if action in ("check", "run", "test", "package"):
        info = pick_build(ctx, args.build, need_research=action in ("run", "test"), prefer=(DEV_NAME, DEFAULT_NAME))
        argv += ["--output", str(info.path)]
    elif args.build:
        raise UsageError(f"mod {action}: --build does not apply to this action")
    argv += MOD_COMMAND.emit(args) + list(tail)
    return Plan([Step(argv)])


# ---------------------------------------------------------------------------------------------------- ctl
CTL_COMMANDS = ("event", "press", "turn", "touch", "raw", "clear", "snapshot", "metrics", "txlog", "period",
                "panelshot", "resize", "adc", "usb", "app-restart", "firmware")
CTL_OPTS = [
    Opt("--build", "The research build whose newest running session to drive (default: build/sp16-dev, else the "
        "only research build).", "NAME", emit=False),
    Opt("--runtime", "A session's runtime.json instead of the newest session of a build.", "FILE", emit=False, path=True),
]


def find_runtime(ctx, build_name):
    info = pick_build(ctx, build_name, need_research=True, prefer=(DEV_NAME, DEFAULT_NAME))
    for folder in sessions_of(info.path):
        try:
            runtime = json.loads((folder / "runtime.json").read_text())
        except (OSError, ValueError):
            continue
        socket_path = runtime.get("control_socket")
        if socket_path and os.path.exists(socket_path):
            return folder / "runtime.json"
    raise CliError(f"no running research session in {ctx.display(info.path)}; start one with: "
                   f"sp16emu run --name {info.name}")


def plan_ctl(ctx, args, tail):
    runtime = os.path.expanduser(args.runtime) if args.runtime else str(find_runtime(ctx, args.build))
    argv = [ctx.python(strict=False), ctx.tool("sessionctl.py"), "--runtime", runtime, args.ctl_command] + list(args.ctl_args) + list(tail)
    return Plan([Step(argv)])


# ------------------------------------------------------------------------------------------------ hwsuite
HWSUITE_OPTS = [
    Opt("--build", "The research build to test (built with --research --audio file; default: build/sp16-dev).",
        "NAME", emit=False),
    Opt("--control", "The control build made with --uncalibrated that shares the library (default: "
        "build/sp16-dev-uncal when it exists).", "NAME", emit=False),
    Opt("--profile", "The firmware's compatibility profile: a version such as 1.60 or a profile file (default: the "
        "version of the firmware under test).", "VERSION|FILE", emit=False),
    Opt("--firmware", "The firmware id in the library (default: the session's).", "ID"),
    Opt("--runtime", "The runtime.json of a running research session to use as the main session.", "FILE", path=True),
    Opt("--tier", "smoke, core or full (default full).", "TIER", choices=("smoke", "core", "full")),
    Opt("--smoke", "Same as --tier smoke.", flag=True),
    Opt("--only", "Comma-separated check ids, such as HW-24.", "IDS"),
    Opt("--fixtures", "Fixture folder (default <out>/fixtures).", "DIR", path=True),
    Opt("--out", "Output folder (default <build>/hwsuite/<run id>).", "DIR", path=True),
    Opt("--mark-verified", "After a full run in which every check passes, record the profile as verified.", flag=True),
    Opt("--from-result", "With --mark-verified: mark from a stored hwsuite-result.json (or its folder) without running.",
        "PATH", path=True),
    Opt("--make-fixtures", "Only generate the fixtures into DIR and exit.", "DIR", path=True),
    Opt("--json-only", "Print JSON only.", flag=True),
]


def profile_for(ctx, library, firmware_id, given):
    """A profile file path from a version, a path, or the version of the firmware that will be tested."""
    folder = ctx.tools / "profiles"
    if given:
        if Path(given).suffix == ".json":
            return str(Path(given))
        path = folder / f"sp16-{given}.json"
        if not path.is_file():
            raise CliError(f"there is no profile for version {given}; available: "
                           + (", ".join(sorted(p.stem[5:] for p in folder.glob("sp16-*.json"))) or "none"))
        return str(path)
    version = None
    if library is not None:
        try:
            chosen = firmware_id or json.loads((library / "selected.json").read_text()).get("id")
            version = json.loads((library / chosen / "manifest.json").read_text())["update"]["trailer_version"]
        except (OSError, ValueError, KeyError, TypeError):
            version = None
    if version and (folder / f"sp16-{version}.json").is_file():
        return str(folder / f"sp16-{version}.json")
    raise CliError("cannot tell which profile to use; pass --profile VERSION (for example --profile 1.60)")


def plan_hwsuite(ctx, args, tail):
    notes = []
    argv = [ctx.python(), ctx.tool("hwsuite.py")]
    if args.make_fixtures or args.from_result:
        if args.from_result and not (args.mark_verified and args.profile):
            raise UsageError("hwsuite: --from-result needs --mark-verified and --profile")
        if args.profile:
            argv += ["--profile", profile_for(ctx, None, None, args.profile)]
        return Plan([Step(argv + HWSUITE_COMMAND.emit(args) + list(tail))])
    info = pick_build(ctx, args.build, need_research=True, prefer=(DEV_NAME,))
    if (info.assets or {}).get("audio") != "file":
        raise CliError(f"{ctx.display(info.path)} must be built with --audio file for the hardware suite; rebuild it "
                       f"with: {build_hint(info.name, '--rebuild --research --audio file')}")
    argv += ["--profile", profile_for(ctx, info.library, args.firmware, args.profile), "--build-dir", str(info.path)]
    control = args.control
    if control is None:
        candidate = ctx.build / (info.name + "-uncal")
        control = candidate.name if (candidate / "assets.json").is_file() else None
        if control is None:
            notes.append(f"no control build ({info.name}-uncal): the control check is not run; create it with: "
                         f"{build_hint(info.name + '-uncal', '--research --audio file --uncalibrated --library ' + str(info.library))}")
    if control:
        argv += ["--control-build", str(pick_build(ctx, control, need_research=True).path)]
    argv += HWSUITE_COMMAND.emit(args) + list(tail)
    return Plan([Step(argv)], notes)


# ------------------------------------------------------------------------------------------------- test
TEST_OPTS = []


def plan_test(ctx, args, tail):
    public = (ctx.tools / "tests").is_dir()
    folder = ctx.tools / "tests" if public else ctx.root / "tests"
    pattern = "test_sp16_*.py" if public else "test_sp16_*.py"
    argv = [ctx.python(), "-m", "unittest", "discover", "-s", str(folder), "-p", pattern] + list(tail)
    return Plan([Step(argv, {"PYTHONDONTWRITEBYTECODE": "1"})])


# ------------------------------------------------------------------------------------------------- audio
AUDIO_ACTIONS = ("devices", "selftest")


def audio_stamp(ctx):
    digest = hashlib.sha256()
    for name in AUDIO_SOURCES:
        digest.update((ctx.tools / name).read_bytes())
    digest.update(" ".join(AUDIO_SWIFT_FLAGS).encode())
    return digest.hexdigest()


def audio_tool_paths(ctx):
    cache = ctx.build / ".cache"
    return cache, cache / "audio-selftest", cache / "audio-selftest.stamp"


def audio_compile_argv(ctx):
    _, binary, _ = audio_tool_paths(ctx)
    return ["swiftc", *AUDIO_SWIFT_FLAGS, *(ctx.tool(n) for n in AUDIO_SOURCES), "-o", str(binary)]


def ensure_audio_tool(ctx):
    """Compile the audio self-test tool (the app's own device listing) on first use and whenever its sources change."""
    cache, binary, stamp = audio_tool_paths(ctx)
    wanted = audio_stamp(ctx)
    try:
        if binary.is_file() and stamp.read_text().strip() == wanted:
            return binary
    except OSError:
        pass
    if not ctx.which("swiftc"):
        raise CliError("swiftc is missing; install the Xcode Command Line Tools with: xcode-select --install")
    ensure_build_root(ctx)
    cache.mkdir(mode=0o700, exist_ok=True)
    ctx.say("compiling the audio tool (first use, a few seconds)")
    log = cache / "audio-selftest.log"
    with open(str(log), "wb") as stream:
        code = subprocess.call(audio_compile_argv(ctx), stdout=stream, stderr=subprocess.STDOUT, env=ctx.env)
    if code or not binary.is_file():
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-8:])
        raise CliError(f"compiling the audio tool failed (see {ctx.display(log)}):\n{tail}")
    stamp.write_text(wanted + "\n")
    return binary


def parse_devices(text):
    devices = []
    for line in text.splitlines():
        if "\t" in line:
            uid, name = line.split("\t", 1)
            devices.append((uid.strip(), name.strip()))
    return devices


def run_audio(ctx, args, tail):
    if args.action == "devices":
        if ctx.dry_run:
            ctx.out.write("+ " + shlex.join(audio_compile_argv(ctx)) + "\n+ " + shlex.join([str(audio_tool_paths(ctx)[1]), "devices"]) + "\n")
            return 0
        binary = ensure_audio_tool(ctx)
        code, out = ctx.capture([str(binary), "devices"], timeout=30)
        devices = parse_devices(out)
        if code or not devices:
            raise CliError("no CoreAudio output devices were found (the tool exited with code %d)" % code)
        width = max(len(uid) for uid, _ in devices)
        ctx.out.write(f"{'UID'.ljust(width)}  NAME\n")
        for uid, name in devices:
            ctx.out.write(f"{uid.ljust(width)}  {name}\n")
        ctx.out.write("\nPlay a build through one: sp16emu build --audio coreaudio --audio-device UID "
                      "(without --audio-device it uses the system output)\n")
        return 0
    if ctx.dry_run:
        ctx.out.write("+ " + shlex.join(audio_compile_argv(ctx)) + "\n+ " + shlex.join([str(audio_tool_paths(ctx)[1])] + list(tail)) + "\n")
        return 0
    binary = ensure_audio_tool(ctx)
    return ctx.call([str(binary)] + list(tail))


# ----------------------------------------------------------------------------------------------- builds
def describe_build(ctx, info):
    assets = info.assets or {}
    audio = assets.get("audio") or "silent"
    if assets.get("audio_device"):
        audio += ":" + assets["audio_device"]
    suffix = "-"
    app = assets.get("app_bundle")
    if app:
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as stream:
                ident = plistlib.load(stream).get("CFBundleIdentifier", "")
            suffix = ident[len(PROFILE_BUNDLE_ID) + 1:] or "-" if ident.startswith(PROFILE_BUNDLE_ID) else "-"
        except (OSError, ValueError):
            pass
    pads = assets.get("pad_calibration") or "-"
    pids = running_pids(ctx, info.path)
    state = "incomplete" if not info.complete else ("running" if pids else "built")
    return {
        "name": info.name, "path": str(info.path), "state": state, "audio": audio,
        "research": bool(assets.get("research")), "trace": assets.get("trace") or "",
        "pads": "uncalibrated" if pads == "off" else pads, "bundle_suffix": suffix,
        "library": str(info.library) if info.library else "", "firmware": library_imports(info.library) if info.library else 0,
        "qemu": assets.get("qemu_version") or "", "decoupled": info.decoupled,
        "built": time.strftime("%Y-%m-%d %H:%M", time.localtime(info.built)) if info.built else "",
        "pids": pids,
    }


def run_builds(ctx, args, tail):
    rows = [describe_build(ctx, b) for b in list_builds(ctx)]
    if args.json:
        ctx.out.write(json.dumps(rows, indent=2) + "\n")
        return 0
    if not rows:
        ctx.out.write(f"No builds in {ctx.display(ctx.build)}. Create one with: sp16emu build\n")
        return 0
    header = ("NAME", "STATE", "AUDIO", "RESEARCH", "PADS", "ID SUFFIX", "FIRMWARE", "LIBRARY", "BUILT")
    table = [header]
    for row in rows:
        research = ("yes" + (f"+{row['trace']}" if row["trace"] else "")) if row["research"] else "no"
        state = row["state"] + (f" ({','.join(str(p) for p in row['pids'])})" if row["pids"] else "")
        library = ctx.display(row["library"]) if row["library"] else "-"
        table.append((row["name"], state, row["audio"], research, row["pads"], row["bundle_suffix"],
                      str(row["firmware"]), library, row["built"]))
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    for r in table:
        ctx.out.write("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() + "\n")
    shared = ctx.build / "library"
    if shared.is_dir():
        ctx.out.write(f"\nShared firmware library {ctx.display(shared)}: {library_imports(shared)} import(s)\n")
    return 0


# ----------------------------------------------------------------------------------------------- doctor
def doctor_checks(ctx):
    """[(status, title, detail, fix)] with status ok, warn or fail."""
    checks = []

    def add(status, title, detail="", fix=""):
        checks.append((status, title, detail, fix))

    mac = platform.system() == "Darwin"
    if mac and is_apple_silicon(ctx):
        add("ok", "Apple silicon Mac", platform.machine())
    elif mac:
        add("fail", "Apple silicon Mac", "an Intel Mac or a terminal running under Rosetta",
            "use a Mac with Apple silicon (the emulator uses Hypervisor.framework); open a native terminal")
    else:
        add("fail", "Apple silicon Mac", platform.system(), "the emulator runs on macOS only")
    version = platform.mac_ver()[0]
    try:
        major = int(version.split(".")[0]) if version else 0
    except ValueError:
        major = 0
    if major >= 13:
        add("ok", "macOS 13 or later", version)
    else:
        add("fail", "macOS 13 or later", version or "unknown", "update macOS")
    code, path = ctx.capture(["xcode-select", "-p"], timeout=10)
    if code == 0:
        add("ok", "Xcode Command Line Tools", path.strip())
    else:
        add("fail", "Xcode Command Line Tools", "not installed", "xcode-select --install")
    for tool in ("swiftc", "codesign", "iconutil"):
        found = ctx.which(tool)
        if found:
            add("ok", tool, found)
        else:
            add("fail", tool, "not found", "xcode-select --install")
    if os.access("/usr/bin/python3", os.X_OK):
        add("ok", "/usr/bin/python3 (the app imports firmware with it)", "")
    else:
        add("fail", "/usr/bin/python3 (the app imports firmware with it)", "missing", "xcode-select --install")
    try:
        tool_python = ctx.python()
        add("ok", "Python 3.12 or later for the build tools", tool_python)
    except CliError as error:
        add("fail", "Python 3.12 or later for the build tools", "not found", "brew install python@3.12")
        del error
    donor = find_qemu_donor(ctx, "")
    need_qemu = donor is None
    for tool, formula in (("ninja", "ninja"), ("pkg-config", "pkgconf")):
        found = ctx.which(tool) or (ctx.which("pkgconf") if tool == "pkg-config" else None)
        status = "warn" if not found and need_qemu else "fail" if not found else "ok"
        if found:
            add("ok", f"{tool} (QEMU build)", found)
        else:
            add("fail" if need_qemu else "warn", f"{tool} (QEMU build)", "not found" + ("" if need_qemu else
                " (only needed to compile QEMU; an existing build has one)"), f"brew install {formula}")
        del status
    pkg = ctx.which("pkg-config") or ctx.which("pkgconf")
    if pkg:
        code, _ = ctx.capture([pkg, "--exists", "glib-2.0", "pixman-1"])
        if code == 0:
            add("ok", "glib and pixman (QEMU build)", "")
        else:
            add("fail" if need_qemu else "warn", "glib and pixman (QEMU build)", "not found", "brew install glib pixman")
    if ctx.which("docker"):
        code, out = ctx.capture(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=25)
        if code == 0:
            add("ok", "Docker running (builds only; running needs no Docker)", "version " + out.strip())
        else:
            add("fail", "Docker running (builds only; running needs no Docker)", "installed but not running",
                "start Docker Desktop")
    else:
        add("fail", "Docker (builds only; running needs no Docker)", "not installed", "install Docker Desktop")
    free = shutil.disk_usage(str(ctx.root if ctx.root.exists() else Path("/"))).free
    gigabytes = free / 1024 ** 3
    if free >= MIN_FREE_BYTES:
        add("ok", "free disk space", f"{gigabytes:.1f} GB")
    else:
        add("fail", "free disk space", f"{gigabytes:.1f} GB", "free at least 3 GB")
    code, top = ctx.capture(["git", "-C", str(ctx.root), "rev-parse", "--show-toplevel"], timeout=10)
    if code:
        add("fail", "git work tree", "not a git repository",
            "clone the repository with git (or run `git init -b main` in this folder): the build tool only writes "
            "inside git-ignored folders")
    else:
        ignored, _ = ctx.capture(["git", "-C", str(ctx.root), "check-ignore", "-q", "--", str(ctx.build / "x")], timeout=10)
        if ignored:
            add("fail", "build folder is git-ignored", ctx.display(ctx.build), "add a line `/build/` to .gitignore")
        else:
            add("ok", "git work tree with a git-ignored build folder", ctx.display(ctx.build))
    path_text = str(ctx.root)
    if "," in path_text:
        add("fail", "folder path", "contains a comma", "move the repository to a path without commas")
    elif any(p.is_symlink() for p in (ctx.root, *ctx.root.parents)):
        add("fail", "folder path", "passes through a symlink (on macOS /tmp is one)",
            "use the real path, for example a folder in your home directory")
    else:
        add("ok", "folder path", "no commas or symlinks")
    protected = protected_folder(ctx.root, ctx.home)
    if protected:
        add("warn", "repository location (macOS privacy)", f"inside {ctx.display(protected)}: the USB stick mounts in "
            f"{ctx.display(ctx.home / USB_CACHE_MOUNTS)} instead, because macOS refuses mounts inside that folder",
            "move the clone outside Downloads, Desktop, Documents and iCloud Drive (for example ~/src/sp16-emulator), "
            "or give your terminal app Full Disk Access")
    else:
        add("ok", "repository location (macOS privacy)", "outside the folders macOS protects")
    if ctx.build.is_dir():
        if ctx.build.stat().st_mode & 0o077:
            add("fail", "build folder permissions", "readable by others", f"chmod 700 '{ctx.build}'")
        else:
            add("ok", "build folder permissions", "owner only")
    optional = []
    py = None
    try:
        py = ctx.python()
    except CliError:
        pass
    if py:
        code, _ = ctx.capture([py, "-c", "import PIL, numpy"], timeout=30)
        if code:
            optional.append("pillow numpy (python3 -m pip install pillow numpy)")
    if not ctx.which("tesseract"):
        optional.append("tesseract (brew install tesseract)")
    if optional:
        add("warn", "hardware suite extras (only for `sp16emu hwsuite`)", "missing: " + ", ".join(optional))
    else:
        add("ok", "hardware suite extras", "pillow, numpy, tesseract")
    return checks


def run_doctor(ctx, args, tail):
    checks = doctor_checks(ctx)
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    for status, title, detail, fix in checks:
        line = f"[{marks[status]}] {title}" + (f": {detail}" if detail else "")
        ctx.out.write(line + "\n")
        if fix and status != "ok":
            ctx.out.write(f"       fix: {fix}\n")
    failed = sum(1 for c in checks if c[0] == "fail")
    warned = sum(1 for c in checks if c[0] == "warn")
    if failed:
        ctx.out.write(f"\n{failed} problem(s) to fix before building (see the fix lines above).\n")
        return 1
    ctx.out.write("\nReady." + (f" ({warned} optional item(s) missing.)" if warned else "") + " Next: sp16emu build\n")
    return 0


# ------------------------------------------------------------------------------------------------- clean
CLEAN_KEEP = ("library", "data")
CLEAN_OPTS = [
    Opt("--name", "Clean this build only.", "NAME"),
    Opt("--all", "Clean every build in build/.", flag=True),
    Opt("--everything", "Delete everything in build/, including the shared firmware library, imported firmware and "
        "data images. Needs --yes (or a typed confirmation).", flag=True),
    Opt("--yes", "Do not ask for confirmation.", flag=True),
]


def inside_build(ctx, path):
    """True when the path's parent folder is build/ or inside it (the last component is never followed)."""
    root = os.path.realpath(str(ctx.build))
    parent = os.path.realpath(os.path.dirname(str(path)))
    return parent == root or parent.startswith(root + os.sep)


def remove_path(ctx, path):
    """Delete a file, symlink or folder inside build/ (never following symlinks, never leaving build/)."""
    path = Path(path)
    if not inside_build(ctx, path):
        raise CliError(f"refusing to delete {path}: it is outside {ctx.display(ctx.build)}")
    if ctx.dry_run:
        ctx.out.write(f"would remove {ctx.display(path)}\n")
    elif path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(str(path))


def run_clean(ctx, args, tail):
    if sum(1 for flag in (args.name, args.all, args.everything) if flag) != 1:
        raise UsageError("clean: choose exactly one of --name NAME, --all or --everything")
    if not ctx.build.is_dir():
        ctx.out.write(f"Nothing to clean: {ctx.display(ctx.build)} does not exist.\n")
        return 0
    if ctx.build.is_symlink():
        raise CliError(f"{ctx.display(ctx.build)} is a symlink; refusing to clean through it")
    if args.everything:
        held = [m for root in usb_mount_roots(ctx) if root.is_dir() for m in sorted(root.iterdir()) if os.path.ismount(str(m))]
        if held:
            raise CliError(f"the USB stick is mounted on this Mac at {held[0]}; release it first with: sp16emu usb unmount")
    for pid_owner in ([ctx.build] if args.everything else [b.path for b in list_builds(ctx) if args.all or b.name == args.name]):
        pids = running_pids(ctx, pid_owner)
        if pids:
            raise CliError(f"{ctx.display(pid_owner)} is running (process ids {', '.join(map(str, pids))}); quit it "
                           "first and stop only those ids")
    if args.everything:
        shared = ctx.build / "library"
        message = (f"This deletes everything in {ctx.display(ctx.build)}: all builds, the firmware library "
                   f"({library_imports(shared)} import(s) in {ctx.display(shared)}), every data image and the USB stick image.")
        if not args.yes and not ctx.dry_run:
            if not ctx.interactive:
                raise CliError(message + " Add --yes to confirm.")
            ctx.out.write(message + "\nType 'delete everything' to continue: ")
            ctx.out.flush()
            if sys.stdin.readline().strip() != "delete everything":
                raise CliError("not confirmed; nothing was deleted")
        targets = [p for p in sorted(ctx.build.iterdir())]
    else:
        names = [b for b in list_builds(ctx) if args.all or b.name == args.name]
        if args.name and not names:
            raise CliError(f"{ctx.display(ctx.build / args.name)} does not exist; see: sp16emu builds")
        targets = []
        for info in names:
            targets += [p for p in sorted(info.path.iterdir()) if p.name not in CLEAN_KEEP]
        if args.all and (ctx.build / ".cache").exists():
            targets.append(ctx.build / ".cache")
    for target in targets:
        remove_path(ctx, target)
    if not ctx.dry_run:
        for info in list_builds(ctx):
            try:
                info.path.rmdir()  # a build folder that is now empty
            except OSError:
                pass
        kept = []
        if not args.everything:
            shared = ctx.build / "library"
            if shared.is_dir():
                kept.append(f"{ctx.display(shared)} ({library_imports(shared)} import(s))")
            kept += [ctx.display(b.path / k) for b in list_builds(ctx) for k in CLEAN_KEEP if (b.path / k).exists()]
        ctx.out.write(f"Removed {len(targets)} item(s) from {ctx.display(ctx.build)}."
                      + (f" Kept the firmware library and data: {', '.join(kept)}." if kept else "") + "\n")
    return 0


# ------------------------------------------------------------------------------------------------------- usb
# The virtual USB stick is one raw FAT32 image (no partition table), exactly what the guest formats on the first
# attach. These commands prepare it on the Mac without booting: they attach the image with hdiutil, copy, and detach.
STICK_NAME = "stick.img"
STICK_LABEL = "SP16USB"              # the volume label the guest gives a stick it formats
STICK_FOLDER = "PIONEER DJ SAMPLER"  # the firmware's own folder on a stick; samples live in its Samples folder
STICK_SAMPLES = "Samples"
STICK_DEFAULT_SIZE = 1 << 30
STICK_MIN_SIZE = 64 << 20
STICK_MAX_SIZE = 32 << 30
USB_ACTIONS = ("create", "add", "ls", "mount", "unmount")
USB_MOUNTS = ".usb-mnt"              # mount points live in build/.usb-mnt (hidden, so it is not taken for a build)
# macOS privacy protection lets the disk-image service read an image in these folders, but the mount of a volume
# whose mount point is inside one is refused (mount(2): Operation not permitted, which hdiutil reports as "no mountable
# file systems"). Such a build folder mounts in the per-user cache instead.
PROTECTED_FOLDERS = ("Downloads", "Desktop", "Documents", "Library/Mobile Documents/com~apple~CloudDocs")
USB_CACHE_MOUNTS = "Library/Caches/sp16emu/usb-mnt"
AUDIO_EXTENSIONS = (".wav", ".aif", ".aiff")
JUNK_FILES = (".DS_Store", ".apdisk")
JUNK_PREFIX = "._"
JUNK_FOLDERS = (".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems", ".DocumentRevisions-V100")
FAT_BAD_CHARS = set('"*/:<>?\\|')
USB_OPTS = [
    Opt("--image", "The stick image (default: build/stick.img, or stick.img in SP16EMU_BUILD_DIR).", "PATH", emit=False),
    Opt("--size", "create: image size, 64M to 32G (default 1G). The file is sparse: it takes no disk space until used.",
        "SIZE", emit=False),
    Opt("--force", "create: replace an existing image (its content is lost).", flag=True, emit=False),
    Opt("--to", "add: a folder under Samples to copy into (created if missing), for example kicks.", "SUBDIR", emit=False),
    Opt("--tree", "add: copy the contents of this folder tree (any number of levels) keeping its structure, without the "
        "folder's own name; with --to SUBDIR it lands under Samples/SUBDIR. Folders passed as FILES keep their name.",
        "DIR", emit=False),
    Opt("--any", "add: also copy files that are not WAV or AIFF audio (they are skipped with a warning otherwise).",
        flag=True, emit=False),
]


def default_stick(ctx):
    return ctx.build / STICK_NAME


def resolve_image(ctx, given):
    """The stick image path: --image, else build/stick.img. Absolute, no symlink, no comma (QEMU option syntax)."""
    path = Path(os.path.expanduser(given)) if given else default_stick(ctx)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.normpath(str(path)))
    if "," in str(path):
        raise CliError(f"the image path {path} contains a comma, which the emulator cannot pass to QEMU; rename it")
    if path.is_symlink():
        raise CliError(f"{ctx.display(path)} is a symlink; point --image at the real file")
    return path


def parse_size(text):
    match = re.fullmatch(r"(\d+)\s*([KMGkmg]?)[iI]?[bB]?", str(text).strip())
    if not match:
        raise UsageError(f"usb: {text!r} is not a size; use for example 512M or 2G")
    size = int(match.group(1)) << {"": 0, "K": 10, "M": 20, "G": 30}[match.group(2).upper()]
    if not STICK_MIN_SIZE <= size <= STICK_MAX_SIZE:
        raise UsageError("usb: the size must be between 64M and 32G")
    return size


def human(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def process_table(ctx):
    """[(pid, ppid, command)] of every process."""
    code, out = ctx.capture(["ps", "-axo", "pid=,ppid=,command="], timeout=10)
    if code:
        raise CliError("cannot list the running processes (ps failed), so I cannot tell whether an emulator uses the stick")
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def image_holders(ctx, image):
    """[(pid, command)] of the processes (a running emulator app or its QEMU) whose command line names the image.
    This process and its parents (make, the shell) are never counted."""
    rows = process_table(ctx)
    parents = {pid: ppid for pid, ppid, _ in rows}
    mine = set()
    pid = os.getpid()
    while pid and pid not in mine:
        mine.add(pid)
        pid = parents.get(pid, 0)
    needles = {str(image), os.path.realpath(str(image))}
    patterns = [re.compile(r"(?<![\w.\-/])" + re.escape(n) + r"(?![\w.\-])") for n in needles]
    return [(pid, command) for pid, _, command in rows if pid not in mine and any(p.search(command) for p in patterns)]


def hdiutil_images(ctx):
    code, out = ctx.capture(["hdiutil", "info", "-plist"], timeout=30)
    if code:
        raise CliError("cannot list the attached disk images (hdiutil info failed)")
    try:
        images = plistlib.loads(out.encode("utf-8")).get("images", [])
    except (ValueError, AttributeError, plistlib.InvalidFileException):
        raise CliError("cannot read the list of attached disk images (hdiutil info gave no property list)")
    return [i for i in images if isinstance(i, dict)]


def attachments(ctx, image):
    """[(device, [mount points])] for every time this image is attached to the Mac (usually zero or one)."""
    target = os.path.realpath(str(image))
    found = []
    for item in hdiutil_images(ctx):
        path = item.get("image-path")
        if not path or os.path.realpath(path) != target:
            continue
        entries = [e for e in item.get("system-entities", []) if isinstance(e, dict) and e.get("dev-entry")]
        if entries:
            device = min((e["dev-entry"] for e in entries), key=len)
            found.append((device, [e["mount-point"] for e in entries if e.get("mount-point")]))
    return found


def check_idle(ctx, image, action):
    """Refuse when a running emulator has the image or the Mac already has it attached."""
    holders = image_holders(ctx, image)
    if holders:
        pids = ", ".join(str(pid) for pid, _ in holders)
        raise CliError(f"usb {action}: {ctx.display(image)} is in use by a running emulator (process id {pids}); quit the "
                       "emulator first (stop only those ids), then repeat this command")
    for device, mounts in attachments(ctx, image):
        where = f" at {mounts[0]}" if mounts else ""
        raise CliError(f"usb {action}: {ctx.display(image)} is already attached to this Mac as {device}{where}; "
                       "release it with: sp16emu usb unmount")


def require_macos():
    if platform.system() != "Darwin":
        raise CliError("the usb commands need macOS (they use hdiutil and newfs_msdos)")


def image_is_blank(image):
    """True for an image whose first 64 KiB is zero: no file system yet (the guest formats such a stick itself)."""
    with open(str(image), "rb") as stream:
        return not any(stream.read(65536))


def hdiutil_attach(ctx, image, options):
    code, out, err = ctx.run_tool(["hdiutil", "attach", "-imagekey", "diskimage-class=CRawDiskImage"] + options + [str(image)])
    if code:
        lines = [l for l in err.splitlines() if l.strip() and "deprecated" not in l]
        reason = lines[-1].strip() if lines else f"hdiutil exited with {code}"
        point = options[options.index("-mountpoint") + 1] if "-mountpoint" in options else None
        folder = protected_folder(point, ctx.home) if point else None
        if folder:
            raise CliError(f"cannot mount {ctx.display(image)} inside {folder}: macOS privacy protection refuses mounts in "
                           f"that folder ({reason}); move the repository out of it (for example ~/src/sp16-emulator) or "
                           "give your terminal app Full Disk Access, then repeat")
        raise CliError(f"cannot attach {ctx.display(image)}: {reason}. If it holds no FAT32 file system, recreate "
                       "it with: sp16emu usb create --force")
    for line in out.splitlines():
        token = line.split()[:1]
        if token and re.fullmatch(r"/dev/disk\d+", token[0]):
            return token[0]
    raise CliError(f"cannot attach {ctx.display(image)}: hdiutil printed no device")


def hdiutil_detach(ctx, device):
    reason = ""
    for _ in range(5):
        code, _, err = ctx.run_tool(["hdiutil", "detach", device])
        if code == 0:
            return
        reason = next((l.strip() for l in reversed(err.splitlines()) if l.strip() and "deprecated" not in l), "")
        time.sleep(1)
    code, _, err = ctx.run_tool(["hdiutil", "detach", "-force", device])
    if code:
        raise CliError(f"cannot detach {device} ({reason or 'busy'}); close anything using it, then run: sp16emu usb unmount")
    ctx.say(f"{device} was busy and had to be detached with force; check the stick with: sp16emu usb ls")


def protected_folder(path, home):
    """The privacy-protected folder (Downloads, Desktop, Documents or iCloud Drive under home) that holds path, or None."""
    real = os.path.realpath(str(path)).casefold()
    base = os.path.realpath(str(home))
    for name in PROTECTED_FOLDERS:
        folder = os.path.join(base, name).casefold()
        if real == folder or real.startswith(folder + os.sep):
            return os.path.join(str(home), name)
    return None


def usb_mount_root(ctx):
    """Where this build's mount points go (not created): build/.usb-mnt, or the per-user cache when the build
    folder is inside a privacy-protected folder, where macOS refuses the mount."""
    if protected_folder(ctx.build, ctx.home):
        return ctx.home / USB_CACHE_MOUNTS
    return ctx.build / USB_MOUNTS


def usb_mount_roots(ctx):
    """Every folder a usb mount point can be in (unmount and clean look in all of them)."""
    return [ctx.build / USB_MOUNTS, ctx.home / USB_CACHE_MOUNTS]


def mount_root(ctx):
    root = usb_mount_root(ctx)
    if root.is_symlink():
        raise CliError(f"{ctx.display(root)} is a symlink; replace it with a real folder")
    if root == ctx.build / USB_MOUNTS:
        ensure_build_root(ctx)
    if not root.is_dir():
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def stable_mountpoint(ctx, image):
    tag = hashlib.sha1(os.path.realpath(str(image)).encode()).hexdigest()[:6]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(image).stem)[:24] or "stick"
    return mount_root(ctx) / f"{stem}-{tag}"


def remove_junk(path):
    """Delete what macOS adds to a FAT volume (AppleDouble ._ files, .DS_Store, Spotlight and Trash folders)."""
    removed = 0
    for folder, dirs, files in os.walk(str(path)):
        for name in list(dirs):
            if name in JUNK_FOLDERS:
                shutil.rmtree(os.path.join(folder, name), ignore_errors=True)
                dirs.remove(name)
                removed += 1
        for name in files:
            if name in JUNK_FILES or name.startswith(JUNK_PREFIX):
                try:
                    os.unlink(os.path.join(folder, name))
                    removed += 1
                except OSError:
                    pass
    return removed


@contextlib.contextmanager
def mounted_stick(ctx, image, writable=True, keep=False, mountpoint=None):
    """Attach the image, yield its mount point (inside build/), and always detach, after removing what macOS added.
    With keep=True a successful mount stays mounted (usb mount); a failure still detaches."""
    require_macos()
    point = Path(mountpoint) if mountpoint else Path(tempfile.mkdtemp(prefix="stick-", dir=str(mount_root(ctx))))
    point.mkdir(mode=0o700, exist_ok=True)
    device = None
    ok = False
    try:
        device = hdiutil_attach(ctx, image, ["-nobrowse", "-noverify", "-mountpoint", str(point)]
                                + ([] if writable else ["-readonly"]))
        yield point
        ok = True
    finally:
        left_mounted = keep and ok
        try:
            if device and not left_mounted:
                if writable:
                    remove_junk(point)
                hdiutil_detach(ctx, device)
        except CliError as error:
            if ok:
                raise
            ctx.say(str(error))
        finally:
            if not left_mounted:
                try:
                    point.rmdir()
                except OSError:
                    pass


def format_image(ctx, image):
    """Write an empty FAT32 file system (volume SP16USB, no partition table) onto the whole image: the layout the
    guest's own first-attach formatting produces."""
    require_macos()
    device = hdiutil_attach(ctx, image, ["-nomount"])
    try:
        code, _, err = ctx.run_tool(["newfs_msdos", "-F", "32", "-v", STICK_LABEL, device.replace("/dev/", "/dev/r", 1)])
        if code:
            raise CliError(f"newfs_msdos failed on {ctx.display(image)}: " + (err.strip().splitlines() or ["no message"])[-1])
    finally:
        hdiutil_detach(ctx, device)


def make_tree(point, subdir=""):
    target = Path(point) / STICK_FOLDER / STICK_SAMPLES
    if subdir:
        target = target / subdir
    target.mkdir(parents=True, exist_ok=True)
    return target


def create_image(ctx, image, size):
    """Create a new sparse raw image (0600, never over an existing file), format it and make the Samples folder."""
    if not image.parent.is_dir():
        if image.parent == ctx.build:
            ensure_build_root(ctx)
        else:
            raise CliError(f"the folder {ctx.display(image.parent)} does not exist")
    fd = os.open(str(image), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.truncate(size)
    try:
        format_image(ctx, image)
        with mounted_stick(ctx, image) as point:
            make_tree(point)
    except BaseException:
        try:
            image.unlink()
        except OSError:
            pass
        raise


def stick_state(ctx, image):
    """'missing', 'blank' (no file system yet) or 'formatted'; anything but a regular file you own is refused."""
    try:
        info = image.lstat()
    except FileNotFoundError:
        return "missing"
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise CliError(f"{ctx.display(image)} must be a regular file owned by you")
    return "blank" if image_is_blank(image) else "formatted"


def usb_dry_run(ctx, args, image, files):
    lines = {
        "create": [f"create {ctx.display(image)} ({args.size or '1G'}, sparse, mode 0600)",
                   "hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount IMAGE",
                   f"newfs_msdos -F 32 -v {STICK_LABEL} /dev/rdiskN", "hdiutil detach /dev/diskN",
                   f"hdiutil attach ... -mountpoint {ctx.display(usb_mount_root(ctx))}/stick-XXXX IMAGE",
                   f"mkdir -p '{STICK_FOLDER}/{STICK_SAMPLES}'", "hdiutil detach /dev/diskN"],
        "add": ["create and format the image first when it is missing or blank",
                "hdiutil attach ... -mountpoint MOUNTPOINT IMAGE"]
               + [f"cp -X {shlex.quote(f)} '{STICK_FOLDER}/{STICK_SAMPLES}/{args.to or ''}'" for f in files
                  + ([args.tree + "/..."] if args.tree else [])]
               + ["delete the ._* and .DS_Store files macOS added", "hdiutil detach /dev/diskN"],
        "ls": ["hdiutil attach -readonly ... IMAGE", "list the files", "hdiutil detach /dev/diskN"],
        "mount": ["hdiutil attach ... -mountpoint MOUNTPOINT IMAGE (stays mounted)"],
        "unmount": ["delete the ._* and .DS_Store files macOS added", "hdiutil detach /dev/diskN"],
    }[args.action]
    ctx.out.write(f"sp16emu usb {args.action} on {ctx.display(image)} would:\n" + "".join(f"  {l}\n" for l in lines))
    return 0


# audio file checks ------------------------------------------------------------------------------------------
def extended_to_rate(raw):
    """An 80-bit IEEE extended number (the AIFF sample rate) as an int."""
    exponent = ((raw[0] & 0x7F) << 8) | raw[1]
    mantissa = struct.unpack(">Q", bytes(raw[2:10]))[0]
    if exponent == 0 and mantissa == 0:
        return 0
    return int(round(mantissa * 2.0 ** (exponent - 16383 - 63)))


def audio_format(path):
    """(kind, sample rate, bits, channels, compressed) read from a WAV or AIFF header; ValueError when unreadable."""
    with open(str(path), "rb") as stream:
        head = stream.read(12)
        if len(head) < 12:
            raise ValueError("too short to be an audio file")
        outer, form = head[:4], head[8:12]
        if outer == b"RIFF" and form == b"WAVE":
            while True:
                chunk = stream.read(8)
                if len(chunk) < 8:
                    raise ValueError("no fmt chunk")
                ident, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                if ident == b"fmt ":
                    body = stream.read(min(size, 40))
                    if len(body) < 16:
                        raise ValueError("fmt chunk is too short")
                    tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                    if tag == 0xFFFE and len(body) >= 26:
                        tag = struct.unpack("<H", body[24:26])[0]  # WAVE_FORMAT_EXTENSIBLE: the sub-format's code
                    return "WAV", rate, bits, channels, tag != 1
                stream.seek(size + (size & 1), 1)
        if outer == b"FORM" and form in (b"AIFF", b"AIFC"):
            while True:
                chunk = stream.read(8)
                if len(chunk) < 8:
                    raise ValueError("no COMM chunk")
                ident, size = chunk[:4], struct.unpack(">I", chunk[4:])[0]
                if ident == b"COMM":
                    body = stream.read(min(size, 30))
                    if len(body) < 18:
                        raise ValueError("COMM chunk is too short")
                    channels, _, bits = struct.unpack(">hIh", body[:8])
                    compressed = form == b"AIFC" and body[18:22] not in (b"NONE", b"twos", b"")
                    return "AIFF", extended_to_rate(body[8:18]), bits, channels, compressed
                stream.seek(size + (size & 1), 1)
    raise ValueError("not a WAV or AIFF file")


SUPPORTED_AUDIO = "PCM 16- or 24-bit 44.1 kHz"


def audio_class(path):
    """None for a sample in the supported format (integer PCM, 16 or 24 bit, 44.1 kHz, mono or stereo), else a short
    label of what differs, the same for every file with the same difference ("WAV 48 kHz", "AIFF not plain PCM")."""
    try:
        kind, rate, bits, _, compressed = audio_format(path)
    except (OSError, ValueError, struct.error):
        return "with an audio header that cannot be read"
    problems = []
    if compressed:
        problems.append("not plain PCM")
    elif bits not in (16, 24):
        problems.append(f"{bits}-bit")
    if rate != 44100:
        problems.append(f"{rate / 1000:g} kHz")
    return f"{kind} {', '.join(problems)}" if problems else None


def fat_name_problem(name):
    if not name or name in (".", ".."):
        return "an empty name"
    if any(c in FAT_BAD_CHARS or ord(c) < 32 for c in name):
        return "a character that FAT does not allow"
    if name.endswith((" ", ".")):
        return "a name ending in a space or dot"
    return None


def walk_tree(root, strip):
    """[(file, path relative to strip)] of every file under root, in a stable order, without hidden folders."""
    found = []
    for folder, dirs, files in os.walk(str(root)):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__MACOSX")
        for name in sorted(files):
            full = Path(folder) / name
            found.append((full, full.relative_to(strip)))
    return found


class Chatter:
    """Prints the first few messages of a kind and counts the rest (a factory pack has thousands of files)."""
    LIMIT = 8

    def __init__(self, ctx):
        self.ctx, self.counts = ctx, {}

    def say(self, kind, message):
        count = self.counts[kind] = self.counts.get(kind, 0) + 1
        if count <= self.LIMIT:
            self.ctx.say(message)

    def finish(self):
        for kind, count in self.counts.items():
            if count > self.LIMIT:
                self.ctx.say(f"... and {count - self.LIMIT} more {kind} (not listed)")


def collect_samples(ctx, paths, allow_any, trees=()):
    """([(source, relative destination)], skipped count): the files to copy, a warning for each problem. A folder in
    `paths` is walked and keeps its own name; a folder in `trees` is walked and keeps only the structure inside it."""
    missing = [p for p in list(paths) + list(trees) if not os.path.lexists(os.path.expanduser(p))]
    if missing:
        raise CliError("no such file: " + ", ".join(missing))
    sources = []
    for given in trees:
        path = Path(os.path.expanduser(given))
        if not path.is_dir():
            raise CliError(f"--tree needs a folder, and {given} is not one")
        sources += walk_tree(path, path)
    for given in paths:
        path = Path(os.path.expanduser(given))
        if path.is_dir():
            sources += walk_tree(path, path.parent)
        else:
            sources.append((path, Path(path.name)))
    items, skipped, seen = [], 0, {}
    chatter = Chatter(ctx)
    classes = {}
    for source, relative in sources:
        name = source.name
        if name.startswith(".") or not source.is_file():
            chatter.say("hidden or irregular files", f"skipping {name}: hidden or not a regular file")
            skipped += 1
            continue
        problem = fat_name_problem(name) or next((fat_name_problem(p) for p in relative.parts if fat_name_problem(p)), None)
        if problem:
            chatter.say("files with names FAT does not allow", f"skipping {relative}: {problem}; rename it")
            skipped += 1
            continue
        is_audio = source.suffix.lower() in AUDIO_EXTENSIONS
        if not is_audio and not allow_any:
            chatter.say("files that are not WAV or AIFF audio",
                        f"skipping {name}: not WAV or AIFF audio (convert it to PCM 44.1 kHz WAV, or add --any "
                        "to copy it as it is)")
            skipped += 1
            continue
        key = str(relative).lower()
        if key in seen:
            chatter.say("name collisions", f"skipping {source}: {relative} would replace {seen[key]}, which is also "
                                           "being added")
            skipped += 1
            continue
        if is_audio:
            label = audio_class(source)
            if label:
                classes.setdefault(label, []).append(name)
        seen[key] = source
        items.append((source, relative))
    chatter.finish()
    for label, names in classes.items():
        ctx.say(f"warning: {len(names)} file(s) are {label}, not {SUPPORTED_AUDIO}; copied as they are (convert them "
                f"first if the sampler will not load them), for example {', '.join(names[:3])}")
    return items, skipped


def suggest_size(needed):
    """The smallest of 2G, 4G, 8G, 16G, 32G that holds the files with 5% file system overhead."""
    for gib in (2, 4, 8, 16, 32):
        if needed * 1.05 <= gib << 30:
            return f"{gib}G"
    return "32G"


def check_subdir(text):
    if text is None:
        return ""
    parts = [p for p in text.replace("\\", "/").split("/") if p]
    if not parts or text.startswith("/") or any(p in (".", "..") or fat_name_problem(p) for p in parts):
        raise UsageError("usb: --to must be a plain folder name under Samples, for example kicks or drums/kicks")
    return "/".join(parts)


# commands ---------------------------------------------------------------------------------------------------
def usb_prepare(ctx, image, needed=0):
    """For usb add: make sure the image exists and holds a file system (create or format it). A new image is the
    default 1G, or larger when the files need more."""
    state = stick_state(ctx, image)
    if state == "missing":
        check_idle(ctx, image, "add")
        size = max(STICK_DEFAULT_SIZE, parse_size(suggest_size(needed)))
        ctx.say(f"creating {ctx.display(image)} ({human(size)}, FAT32, sparse)")
        create_image(ctx, image, size)
    elif state == "blank":
        check_idle(ctx, image, "add")
        ctx.say(f"{ctx.display(image)} is blank; formatting it as FAT32 first")
        format_image(ctx, image)


def usb_create(ctx, image, args):
    size = parse_size(args.size) if args.size else STICK_DEFAULT_SIZE
    if stick_state(ctx, image) != "missing":
        if not args.force:
            raise CliError(f"{ctx.display(image)} already exists; --force replaces it (its content is lost)")
        check_idle(ctx, image, "create")
        image.unlink()
    create_image(ctx, image, size)
    ctx.out.write(f"Created {ctx.display(image)}: {human(size)} FAT32 (volume {STICK_LABEL}) with {STICK_FOLDER}/"
                  f"{STICK_SAMPLES}. Add samples with: sp16emu usb add FILES...; sp16emu run then attaches it.\n")
    return 0


def usb_add(ctx, image, args, files):
    if not files and not args.tree:
        raise UsageError("usb add: give the sample files to copy, for example: sp16emu usb add ~/samples/*.wav "
                         "(or a whole folder with --tree DIR)")
    subdir = check_subdir(args.to)
    items, skipped = collect_samples(ctx, files, args.any, [args.tree] if args.tree else [])
    if not items:
        raise CliError("nothing to add: no file passed the checks above")
    total = sum(source.stat().st_size for source, _ in items)
    usb_prepare(ctx, image, total)
    check_idle(ctx, image, "add")
    with mounted_stick(ctx, image) as point:
        target = make_tree(point, subdir)
        usage = shutil.disk_usage(str(point))
        free = usage.free
        if total > free:
            raise CliError(f"the files need {human(total)} but the stick has {human(free)} free; nothing was copied. "
                           f"Make a larger stick with: sp16emu usb create --size {suggest_size(usage.used + total)} --force (this "
                           "replaces the stick), or add --image PATH to use a new one")
        for number, (source, relative) in enumerate(items, 1):
            destination = target / relative
            replaced = destination.exists()
            destination.parent.mkdir(parents=True, exist_ok=True)
            code, _, err = ctx.run_tool(["cp", "-X", str(source), str(destination)])
            if code:
                raise CliError(f"cannot copy {source}: {(err.strip().splitlines() or ['cp failed'])[-1]}")
            if replaced:
                ctx.say(f"replaced {relative}")
            if len(items) > 200 and number % 250 == 0:
                ctx.say(f"copied {number} of {len(items)} files")
    where = f"{STICK_FOLDER}/{STICK_SAMPLES}" + (f"/{subdir}" if subdir else "")
    ctx.out.write(f"Added {len(items)} file(s) ({human(total)}) to {where} on {ctx.display(image)}"
                  + (f"; skipped {skipped}" if skipped else "") + ". Next: sp16emu run\n")
    return 0


def usb_ls(ctx, image):
    if stick_state(ctx, image) == "missing":
        raise CliError(f"{ctx.display(image)} does not exist; create it with: sp16emu usb create")
    check_idle(ctx, image, "ls")
    lines, count, used = [], 0, 0
    with mounted_stick(ctx, image, writable=False) as point:
        free = shutil.disk_usage(str(point)).free
        for folder, dirs, files in os.walk(str(point)):
            dirs[:] = sorted(d for d in dirs if d not in JUNK_FOLDERS)
            base = os.path.relpath(folder, str(point))
            lines += [os.path.normpath(os.path.join(base, name)) + "/" for name in dirs]
            for name in sorted(files):
                if name in JUNK_FILES or name.startswith(JUNK_PREFIX):
                    continue
                size = os.path.getsize(os.path.join(folder, name))
                count += 1
                used += size
                lines.append(f"{os.path.normpath(os.path.join(base, name))}  {human(size)}")
    ctx.out.write(f"{ctx.display(image)}: {count} file(s), {human(used)} used, {human(free)} free\n")
    ctx.out.write("".join(line + "\n" for line in sorted(lines)))
    return 0


def usb_mount(ctx, image):
    if stick_state(ctx, image) == "missing":
        raise CliError(f"{ctx.display(image)} does not exist; create it with: sp16emu usb create")
    here = stable_mountpoint(ctx, image)
    attached = attachments(ctx, image)
    if attached and not image_holders(ctx, image) and any(
            os.path.realpath(m) == os.path.realpath(str(here)) for _, mounts in attached for m in mounts):
        ctx.out.write(f"{here}\n")
        ctx.say("already mounted; unmount it before starting the emulator: sp16emu usb unmount")
        return 0
    check_idle(ctx, image, "mount")
    with mounted_stick(ctx, image, keep=True, mountpoint=here):
        pass
    ctx.out.write(f"{here}\n")
    ctx.say("mounted; do not start the emulator until you unmount it: sp16emu usb unmount")
    return 0


def usb_unmount(ctx, image):
    found = attachments(ctx, image)
    if not found:
        ctx.out.write(f"{ctx.display(image)} is not mounted.\n")
        return 0
    roots = [os.path.realpath(str(r)) + os.sep for r in usb_mount_roots(ctx)]
    for device, mounts in found:
        ours = [m for m in mounts if any(os.path.realpath(m).startswith(r) for r in roots)]
        for mount in ours:
            remove_junk(mount)
        hdiutil_detach(ctx, device)
        for mount in ours:
            try:
                os.rmdir(mount)
            except OSError:
                pass
    ctx.out.write(f"Unmounted {ctx.display(image)}.\n")
    return 0


def run_usb(ctx, args, tail):
    image = resolve_image(ctx, args.image)
    files = list(args.files) + list(tail)
    for option, action in (("size", "create"), ("force", "create"), ("to", "add"), ("any", "add"), ("tree", "add")):
        if getattr(args, option) and args.action != action:
            raise UsageError(f"usb {args.action}: --{option} is only for usb {action}")
    if files and args.action != "add":
        raise UsageError(f"usb {args.action}: takes no files; only usb add does")
    if args.size:
        parse_size(args.size)
    if ctx.dry_run:
        return usb_dry_run(ctx, args, image, files)
    require_macos()
    if args.action == "create":
        return usb_create(ctx, image, args)
    if args.action == "add":
        return usb_add(ctx, image, args, files)
    if args.action == "ls":
        return usb_ls(ctx, image)
    if args.action == "mount":
        return usb_mount(ctx, image)
    return usb_unmount(ctx, image)


def stick_for_run(ctx, args):
    """(path, notes): the default stick to attach to `sp16emu run`, or None with the reason in the notes."""
    if args.usb_image or args.no_usb:
        return None, []
    stick = default_stick(ctx)
    try:
        regular = stat.S_ISREG(stick.lstat().st_mode)
    except OSError:
        return None, []
    if not regular or "," in str(stick):
        return None, []
    try:
        holders = image_holders(ctx, stick)
        if holders:
            pids = ", ".join(str(pid) for pid, _ in holders)
            return None, [f"{ctx.display(stick)} is used by a running emulator (process id {pids}); launching without a "
                          "USB stick"]
        for device, mounts in attachments(ctx, stick):
            return None, [f"{ctx.display(stick)} is mounted on this Mac ({mounts[0] if mounts else device}); launching "
                          "without a USB stick. Release it with: sp16emu usb unmount"]
    except CliError as error:
        return None, [f"{error}; launching without a USB stick"]
    return str(stick), [f"attaching the USB stick {ctx.display(stick)} (sp16emu run --no-usb skips it)"]


# ---------------------------------------------------------------------------------------------------- help
def help_ctl_lines():
    return [
        "event NAME ACTION [VALUE]    one control event; ACTION is press, release, delta or value",
        "press NAME [SECONDS [VALUE]] press and release a button or pad",
        "turn NAME DETENTS            turn an encoder (negative turns the other way)",
        "touch X Y move|down|up       a touch on the LCD",
        "raw OFFSET HEX               set raw bytes in the panel frame",
        "clear                        clear raw overrides",
        "snapshot NAME                save the LCD as a PNG in the session folder",
        "panelshot NAME               save the panel view as a PNG",
        "resize WIDTH HEIGHT          resize the panel window",
        "metrics | adc | txlog [off] | period MS    counters, analog words, panel traffic, frame period",
        "usb eject|attach             eject or reattach the virtual USB stick",
        "app-restart | firmware       restart only the firmware's app; library and running firmware",
    ]


def make_commands():
    commands = []

    def add(*a, **k):
        command = Command(*a, **k)
        commands.append(command)
        return command

    add("help", "Start here", "Show this overview, or the options and examples of one command",
        "With no argument, lists every command. `sp16emu help COMMAND` (or `sp16emu COMMAND --help`) shows its "
        "options and examples.", positionals=[Pos("topic", "a command name", nargs="?", metavar="COMMAND")],
        examples=["sp16emu help build", "sp16emu run --help"], usage="sp16emu help [COMMAND]", direct=True)
    add("doctor", "Start here", "Check that this Mac can build and run the emulator, and print the fix for each problem",
        "Checks the Mac (Apple silicon, macOS 13+), Xcode Command Line Tools, Python 3.12+, QEMU build tools, Docker, "
        "free disk space and the git set-up. Exits non-zero when something blocks a build.",
        examples=["sp16emu doctor"], usage="sp16emu doctor", direct=True)
    global BUILD_COMMAND, RUN_COMMAND, FIRMWARE_COMMAND, DIAGNOSTICS_COMMAND, MOD_COMMAND, HWSUITE_COMMAND
    BUILD_COMMAND = add(
        "build", "Build and run", "Build the emulator app (QEMU, Linux guest image, macOS app) into build/NAME",
        "Builds a firmware-free app into build/NAME (default sp16). All builds share one firmware library "
        "(build/library) and reuse QEMU from any existing build, so a later build skips compiling QEMU and the "
        "firmware you imported is already there. The first build downloads and compiles QEMU and needs Docker.",
        opts=BUILD_OPTS, usage="sp16emu build [--name NAME] [options] [-- NATIVE_BUILD_ARGS...]",
        examples=["sp16emu build                                   # silent app in build/sp16",
                  "sp16emu build --audio file                      # record each audio stream to WAV files, silent",
                  "sp16emu audio devices && sp16emu build --name loud --audio coreaudio --audio-device UID",
                  "sp16emu build --name sp16-dev --research --audio file --bundle-suffix dev",
                  "sp16emu build --rebuild --audio file            # rebuild build/sp16 in place, keeps firmware",
                  "sp16emu --dry-run build                         # show the exact tool command and stop"],
        notes=["Everything after `--` goes unchanged to `native.py build` (see `sp16emu build -- --help`); every "
               "option the tool has is listed above.",
               "Builds live in build/ (owner-only, git-ignored). The shared firmware library is never deleted by "
               "--rebuild or `sp16emu clean`.",
               "Firmware is not part of a build: import it once with `sp16emu firmware import`."],
        handler=plan_build)
    RUN_COMMAND = add(
        "run", "Build and run", "Launch a built emulator app",
        "Opens build/NAME with `open -n`; close the window to stop the emulator. The first boot of each firmware "
        "creates a 1 GB data image for it in the library; projects and settings persist there.",
        opts=RUN_OPTS, usage="sp16emu run [--name NAME] [--firmware ID] [--usb-image PATH | --no-usb] [--ephemeral] [--dev-overlay DIR]",
        examples=["sp16emu run", "sp16emu run --name sp16-dev --firmware ID",
                  "sp16emu run --usb-image ~/sticks/other.img", "sp16emu run --no-usb", "sp16emu run --ephemeral"],
        notes=["If the build does not exist, the error names the `sp16emu build` command that creates it.",
               "When build/stick.img exists (see `sp16emu usb`) it is attached as the virtual USB stick unless you give "
               "--usb-image or --no-usb, or another running emulator or this Mac already holds it (then a note says so "
               "and the emulator starts without it).",
               "Two apps cannot use the same firmware's data image at once; give the second one --ephemeral."],
        handler=plan_run)
    add("builds", "Build and run", "List the builds in build/ with the options each was built with",
        "Shows each build's state (built, running with its process ids, incomplete), audio mode, research socket, "
        "pad calibration, bundle id suffix, firmware count, library and build time.",
        opts=[Opt("--json", "Print JSON instead of a table.", flag=True)], usage="sp16emu builds [--json]",
        examples=["sp16emu builds"], direct=True)
    add("usb", "Build and run", "Prepare the virtual USB stick on the Mac: create, add samples, ls, mount, unmount",
        "The stick is one raw FAT32 image, build/stick.img by default; `sp16emu run` attaches it when it exists. "
        "`create` makes and formats it without booting (volume SP16USB, no partition table, the layout the emulated "
        "machine produces itself) and adds the PIONEER DJ SAMPLER/Samples folder. `add FILES...` creates the image if "
        "missing, copies the files into Samples (or a folder under it with --to; a folder argument is copied with its name and "
        "its structure, `--tree DIR` copies only what is inside DIR) and removes the ._ and .DS_Store "
        "files macOS adds. WAV and AIFF files that are not PCM 16- or 24-bit 44.1 kHz are copied with one warning per kind, never "
        "converted; other files are skipped unless --any. `ls` lists the stick, `mount` mounts it (prints the mount "
        "point, under build/.usb-mnt, or in ~/Library/Caches/sp16emu/usb-mnt when the build folder is in Downloads, Desktop, "
        "Documents or iCloud Drive) and `unmount` releases it. Every command that mounts or writes refuses while a "
        "running emulator has the image or this Mac has it attached elsewhere. Needs macOS (hdiutil, newfs_msdos).",
        positionals=[Pos("action", "what to do", choices=USB_ACTIONS), Pos("files", "usb add: the sample files or folders", nargs="*", metavar="FILES")],
        opts=USB_OPTS, usage="sp16emu usb create | add FILES... | ls | mount | unmount [--image PATH] [options]",
        examples=["sp16emu usb add ~/samples/*.wav            # creates build/stick.img the first time",
                  "sp16emu usb add kick.wav snare.wav --to drums", "sp16emu usb create --size 8G --force",
                  "sp16emu usb add --tree ~/SamplePack --to Pack     # a whole folder tree, structure kept",
                  "sp16emu usb ls", "sp16emu usb mount && sp16emu usb unmount",
                  "sp16emu usb add loop.wav --image ~/sticks/gig.img"],
        notes=["Never mount the stick while the emulator runs: the emulated machine and the Mac would both write to "
               "one FAT file system and corrupt it. Quit the emulator, then run `sp16emu usb ...`.",
               "Supported sample format: PCM 16- or 24-bit 44.1 kHz WAV, mono or stereo (what the factory sample pack uses). "
               "Other sample rates, float or compressed audio are copied with one warning per kind.",
               "Sizes: the default stick is 1G; --size goes up to 32G (sparse, so it costs no disk space until used). "
               "The files are checked against the free space before anything is copied. Each file must be under 4 GB "
               "(FAT32); a stick can hold much more in total.",
               "Nothing outside the image and build/ is touched, except a mount point in ~/Library/Caches/sp16emu/usb-mnt when "
               "the build folder is in a protected folder; mount points are removed again."],
        direct=True, intermixed=True)
    add("audio", "Build and run", "List CoreAudio output devices for --audio-device (devices); run the audio self-test",
        "`audio devices` compiles the audio tool on first use (into build/.cache, a few seconds) and prints each "
        "output device with the UID that `sp16emu build --audio-device` takes. `audio selftest` runs the audio "
        "output self-test (virtual-time `test` by default; `live`, `graph`); none of it plays sound.",
        positionals=[Pos("action", "devices | selftest", choices=AUDIO_ACTIONS)],
        usage="sp16emu audio devices | sp16emu audio selftest [-- test|live|graph|devices]",
        examples=["sp16emu audio devices", "sp16emu audio selftest"], direct=True)
    FIRMWARE_COMMAND = add(
        "firmware", "Firmware", "Import, list, show, check, select, rename, remove, audit firmware; make a candidate profile",
        "Works on the shared firmware library through a build (import needs a build's guest to unpack the update). "
        "You download the update and the GPL source archive yourself from the official pages (see the README); "
        "the key is read in memory and never written, printed or logged.",
        positionals=[Pos("action", "what to do", choices=FIRMWARE_ACTIONS)], opts=FIRMWARE_OPTS,
        usage="sp16emu firmware ACTION [--build NAME] [options]",
        examples=["sp16emu firmware import --update ~/Downloads/SP16.UPD --gpl-archive ~/Downloads/gpl-1.zip ~/Downloads/gpl-2.zip",
                  "sp16emu firmware import --update FILE.UPD --key-file KEY", "sp16emu firmware list",
                  "sp16emu firmware check --id ID", "sp16emu firmware select --id ID",
                  "sp16emu firmware profile --id ID > emulator/profiles/sp16-VERSION.json"],
        notes=["import: --update plus --key-file or --gpl-archive. list needs no id; show, check, select, rename, "
               "remove and profile need --id; rename needs --name.",
               "audit scans the build for firmware files, key material and vendor assets (exit 1 on a hit)."],
        handler=plan_firmware)
    DIAGNOSTICS_COMMAND = add(
        "diagnostics", "Maintenance", "Collect logs and system facts into one redacted zip for a bug report",
        "Writes sp16-diagnostics-<time>.zip (Desktop, else the build folder). Nothing is uploaded. Firmware, updates, "
        "key files and data images are never included; paths, account and host names and e-mail addresses are redacted.",
        opts=DIAGNOSTICS_OPTS, usage="sp16emu diagnostics [--build NAME] [options]",
        examples=["sp16emu diagnostics", "sp16emu diagnostics --sessions 5 --include-audio --out ~/Desktop/bug.zip"],
        handler=plan_diagnostics)
    MOD_COMMAND = add(
        "mod", "Develop and test", "Develop firmware mods: new, check, run, test, sdk, package",
        "The mod loop (`fwdev.py`): scaffold a mod folder, check it against the firmware's manifest, run it as a "
        "read-only overlay (or restart only the firmware's app), test it against a stock baseline, build preload "
        "libraries, and package an archive or a repacked update for this emulator only.",
        positionals=[Pos("action", "what to do", choices=MOD_ACTIONS)], opts=MOD_OPTS,
        usage="sp16emu mod ACTION --mod DIR [--build NAME] [options]",
        examples=["sp16emu mod new --mod private/my-mod --id my-mod --from-recipe utility-label",
                  "sp16emu mod check --mod private/my-mod", "sp16emu mod run --mod private/my-mod",
                  "sp16emu mod test --mod private/my-mod --baseline stock.png --expect-change 0 0 100 50",
                  "sp16emu mod sdk --mod private/my-mod",
                  "sp16emu mod package --mod private/my-mod --upd --key-file KEY --out private/my-mod.UPD"],
        notes=["run and test need a research build: sp16emu build --name sp16-dev --research --audio file.",
               "Repacked updates carry a mod-lab marker and are for this emulator only: never flash or share them."],
        handler=plan_mod)
    add("ctl", "Develop and test", "Drive a running research session over its control socket (buttons, encoders, LCD)",
        "Runs sessionctl.py against the newest running session of a research build. The commands:\n  "
        + "\n  ".join(help_ctl_lines()),
        positionals=[Pos("ctl_command", "a session command", choices=CTL_COMMANDS, metavar="COMMAND"),
                     Pos("ctl_args", "its arguments", nargs=argparse.REMAINDER, metavar="ARGS")],
        opts=CTL_OPTS, usage="sp16emu ctl [--build NAME | --runtime FILE] COMMAND [ARGS...]",
        examples=["sp16emu ctl press play", "sp16emu ctl turn knob1 -3", "sp16emu ctl snapshot home.png",
                  "sp16emu ctl metrics"],
        notes=["The session must run from a build made with --research.",
               "NAME is a control: pad1..pad16, step1..step16, bar1..bar4, knob1..knob6, selector, strip, or a button "
               "such as play, rec, stop, shift, home, back (the full list is CONTROL_IDS in panel_map.py)."],
        handler=plan_ctl)
    HWSUITE_COMMAND = add(
        "hwsuite", "Develop and test", "Run the hardware conformance suite against a research build",
        "Checks that the emulated hardware behaves correctly for a firmware (panel protocol, lamps, encoders, pads, "
        "strip, audio clock, USB, storage, power). Needs a build made with --research --audio file, an optional "
        "--uncalibrated control build sharing the library, and pillow, numpy and tesseract. Quit other emulators "
        "first: the suite starts and stops its own sessions.",
        opts=HWSUITE_OPTS, usage="sp16emu hwsuite [--build NAME] [--control NAME] [--profile VERSION] [options]",
        examples=["sp16emu hwsuite --smoke", "sp16emu hwsuite --profile 1.60 --tier core",
                  "sp16emu hwsuite --tier full --mark-verified"],
        notes=["Exit codes: 0 pass, 1 fail, 2 blocked (missing profile data), 3 environment, 4 session lost.",
               "Only a full-tier run in which every check passes can mark a profile verified."],
        handler=plan_hwsuite)
    add("test", "Develop and test", "Run the firmware-free unit tests",
        "Runs the project's unit tests with the build tools' Python. They need no firmware and no running emulator.",
        usage="sp16emu test [-- UNITTEST_ARGS...]", examples=["sp16emu test", "sp16emu test -- -k panel -v"],
        handler=plan_test)
    add("clean", "Maintenance", "Delete build outputs; keeps the firmware library and data images",
        "`--name NAME` or `--all` remove each build's app, QEMU, guest image and logs but keep any library and data "
        "folder and the shared build/library. `--everything --yes` empties build/ completely, firmware included. "
        "Nothing outside build/ is ever deleted, and a running build is refused.",
        opts=CLEAN_OPTS, usage="sp16emu clean (--name NAME | --all | --everything [--yes])",
        examples=["sp16emu clean --all", "sp16emu clean --name sp16-dev", "sp16emu --dry-run clean --everything"],
        direct=True)
    return commands


BUILD_COMMAND = RUN_COMMAND = FIRMWARE_COMMAND = DIAGNOSTICS_COMMAND = MOD_COMMAND = HWSUITE_COMMAND = None
COMMANDS = make_commands()
BY_NAME = {c.name: c for c in COMMANDS}
GLOBAL_HELP = """Global options (before the command):
  -n, --dry-run   print the exact tool commands instead of running them
  -v, --verbose   print each tool command before running it
  --python PATH   the Python 3.12+ that runs the build tools (default: the first one found; env SP16EMU_PYTHON)
Environment: SP16EMU_BUILD_DIR (build folder, default <repository>/build), SP16EMU_PYTHON, SP16EMU_DRY_RUN=1."""


def overview(ctx):
    lines = ["sp16emu: build, run and manage the SP-16 compatible emulator", "",
             "Usage: sp16emu [--dry-run] [--verbose] [--python PATH] COMMAND [options]", ""]
    order = ["Start here", "Build and run", "Firmware", "Develop and test", "Maintenance"]
    for group in order:
        lines.append(group + ":")
        for command in COMMANDS:
            if command.group == group:
                lines.append(f"  {command.name.ljust(12)}{command.summary}")
        lines.append("")
    lines += ["First time: sp16emu doctor; sp16emu build; sp16emu firmware import ...; sp16emu run",
              "Details: sp16emu help COMMAND   (also: sp16emu COMMAND --help)", "", GLOBAL_HELP]
    return "\n".join(lines) + "\n"


def command_help(command):
    lines = [f"sp16emu {command.name}: {command.summary}", "", "Usage:", f"  {command.usage or 'sp16emu ' + command.name}", ""]
    for part in command.description.split("\n"):
        lines += textwrap.wrap(part, 100, subsequent_indent="    " if part.startswith("  ") else "") or [""]
    lines.append("")
    if command.positionals:
        lines.append("Arguments:")
        for pos in command.positionals:
            if pos.dest in ("ctl_args",):
                continue
            choices = f" ({' | '.join(pos.choices)})" if pos.choices and len(pos.choices) < 12 else ""
            lines.append(f"  {pos.metavar.ljust(12)}{pos.help}{choices}")
        lines.append("")
    if command.opts:
        lines.append("Options:")
        for opt in command.opts:
            wrapped = textwrap.wrap(opt.help, 74)
            usage = opt.usage + ("  [required]" if opt.required else "")
            if len(usage) > 24:
                lines.append(f"  {usage}")
                lines += [" " * 28 + w for w in wrapped]
            else:
                lines.append(f"  {usage.ljust(26)}{wrapped[0] if wrapped else ''}")
                lines += [" " * 28 + w for w in wrapped[1:]]
        lines.append("")
    if command.name not in ("help", "doctor", "builds", "audio", "clean", "usb"):
        lines.append("  -- ARGS...                Everything after -- is passed unchanged to the underlying tool")
        lines.append("                            (its own --help works there).")
        lines.append("")
    if command.notes:
        lines.append("Notes:")
        for note in command.notes:
            lines += textwrap.wrap(note, 98, initial_indent="  - ", subsequent_indent="    ")
        lines.append("")
    if command.examples:
        lines.append("Examples:")
        lines += [f"  {example}" for example in command.examples]
        lines.append("")
    return "\n".join(lines)


def run_help(ctx, args, tail):
    if args.topic:
        command = BY_NAME.get(args.topic)
        if command is None:
            raise UsageError(unknown_command(args.topic))
        ctx.out.write(command_help(command))
    else:
        ctx.out.write(overview(ctx))
    return 0


def unknown_command(name):
    close = difflib.get_close_matches(name, list(BY_NAME), n=1)
    return f"unknown command '{name}'" + (f" (did you mean '{close[0]}'?)" if close else "") + "; run `sp16emu help` for the list"


DIRECT = {"help": run_help, "doctor": run_doctor, "builds": run_builds, "audio": run_audio, "clean": run_clean,
          "usb": run_usb}


# ---------------------------------------------------------------------------------------------------- main
def execute(ctx, plan):
    for note in plan.notes:
        ctx.say(note)
    if not ctx.dry_run:
        for setup in plan.setup:
            setup()
    for step in plan.steps:
        text = shlex.join(step.argv)
        if ctx.dry_run:
            ctx.out.write("+ " + text + "\n")
            continue
        if ctx.verbose:
            print("+ " + text, file=ctx.err)
        code = ctx.call(step.argv, step.env)
        if code:
            return code
    if not ctx.dry_run:
        for note in plan.after:
            ctx.say(note)
    return 0


def split_tail(argv):
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return list(argv), []


def parse_global(argv):
    options = {"dry_run": False, "verbose": False, "python": None}
    rest = list(argv)
    while rest and rest[0].startswith("-") and rest[0] not in ("-h", "--help"):
        flag = rest.pop(0)
        if flag in ("-n", "--dry-run"):
            options["dry_run"] = True
        elif flag in ("-v", "--verbose"):
            options["verbose"] = True
        elif flag == "--python" and rest:
            options["python"] = rest.pop(0)
        elif flag.startswith("--python="):
            options["python"] = flag.split("=", 1)[1]
        else:
            raise UsageError(f"unknown option {flag}; global options go before the command (see: sp16emu help)")
    return options, rest


def main(argv=None, ctx=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        options, rest = parse_global(argv)
        if ctx is None:
            ctx = Context(dry_run=options["dry_run"], verbose=options["verbose"], python=options["python"])
        else:
            ctx.dry_run = ctx.dry_run or options["dry_run"]
        if not rest or rest[0] in ("-h", "--help"):
            ctx.out.write(overview(ctx))
            return 0
        name, rest = rest[0], rest[1:]
        command = BY_NAME.get(name)
        if command is None:
            raise UsageError(unknown_command(name))
        head, tail = split_tail(rest)
        if "-h" in head or "--help" in head:
            ctx.out.write(command_help(command))
            return 0
        args = command.parser().parse_args(head)
        if command.direct:
            return DIRECT[command.name](ctx, args, tail)
        return execute(ctx, command.handler(ctx, args, tail))
    except CliError as error:
        print(f"sp16emu: {error}", file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # `sp16emu usb ls | head`
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # no second error when Python flushes
        except (OSError, ValueError, AttributeError):
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
