#!/usr/bin/env python3
"""Build once, then launch the native macOS SP-16 app without Docker/browser."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from prepare import EXPECTED_APP, REPO, guest_file, private_path
from app_identity import FIRMWARE, identity, patch_utility, validate_identity
import firmware
import profiles

TOOLS = Path(__file__).resolve().parent
QEMU_VERSION = "10.2.1"
QEMU_ARCHIVE_SHA256 = "a3717477d8e2c84d630bfffbc20f6cd3293eb45aa1e6dac6d0cc27689991c9e1"
APP_NAME = "SP16 Emulator.app"
GUEST_FILES = ("Dockerfile.native-guest", "Dockerfile", "hardware_stub.c", "guest-init.sh",
               "guest-bridge.py", "pack-guest.sh", "protocol.py", "panel.py", "panel_map.py", "input.py",
               "enter.py", "app_identity.py", "profiles.py", "recipes.py", "profiles/sp16-1.60.json",
               "profiles/products/sp16.json", "mods/utility-label.json")
BASE_GUEST_FILES = ("Dockerfile.native-base", "hardware_stub.c", "guest-init.sh", "guest-bridge.py", "pack-guest.sh",
                    "protocol.py", "panel.py", "panel_map.py", "input.py", "enter.py", "profiles.py", "recipes.py",
                    "prepare.py", "guest-firmware.py", "profiles/sp16-1.60.json",
                    "profiles/products/sp16.json")
SWIFT_SOURCES = ("native-window.swift", "panel-view.swift", "data-image.swift", "audio-sink.swift",
                "audio-output.swift", "firmware-library.swift", "firmware-window.swift")
ICON_TOOL = "make-icon.swift"
ICON_LOGO = "assets/legacy-logo.svg"
ICON_FILE = "sp16-emulator"
PANEL_RESOURCES = ("panel-layout.json", "assets/legacy-logo-2.svg", "assets/legacy-logo-3.svg",
                   "assets/legacy-logo.svg")
PROFILES = ("fullpanel", "testmode")
# Decoupled builds (docs/architecture.md M7, design decision D4): neutral product name and bundle id, no vendor
# logos, a project-made icon, the firmware library tool and third-party notices. Legacy builds keep theirs.
DECOUPLED_APP_NAME = "SP-16 Compatible Emulator.app"
DECOUPLED_PRODUCT = "SP-16 Compatible Emulator"
DECOUPLED_EXECUTABLE = "SP16Emulator"
DECOUPLED_BUNDLE_ID = "local.sp16-compatible.emulator"
DECOUPLED_RESOURCES = ("panel-layout.json",)
NEUTRAL_ICON = "assets/neutral-pads.svg"
DECOUPLED_ICON_FILE = "sp16-compatible-emulator"
FWLIB_FILES = ("firmware.py", "profiles.py", "recipes.py", "fwpack.py")
# Legacy builds (the whole prepared firmware rootfs inside the guest image) need files that only the private
# repository has. Without them (the public copy) the CLI offers decoupled builds only and hides the legacy options.
LEGACY_AVAILABLE = (TOOLS / "Dockerfile.native-guest").is_file()
LEGACY_REFUSAL = ("this copy builds firmware-free apps only: import firmware with `native.py firmware import` "
                  "instead of --rootfs, --mod, --app-arg, --no-persistence or --profile testmode")

# Optional per-task suffix (e.g. a task id) so a dev build gets its own bundle ID and quitting one app by ID
# never quits another. Without it the bundle IDs are unchanged.
BUNDLE_SUFFIX_PATTERN = re.compile(r"[a-z0-9-]{1,32}")
# The pad calibration file (path, format, accepted range, default) is firmware data from the profile. The legacy
# builds stage the 1.60 profile's (JUCE ValueTree XML /root/data/padcalibration.xml, 48..504, default 242: the
# firmware's own default knee level, at which its pressure mapping is the identity).
#. Decoupled builds pass the choice on the
# kernel command line and the guest stages it in the active profile's format. The synthetic (not factory) value keeps
# the firmware out of its uncalibrated pad-blink state; --uncalibrated reproduces the stock behaviour for research.
_LEGACY_CALIBRATION = profiles.legacy()["calibration"]
PAD_CALIBRATION_RANGE = tuple(_LEGACY_CALIBRATION["range"])
PAD_CALIBRATION_PATH = "/opt/sp16/rootfs" + _LEGACY_CALIBRATION["path"]
DEFAULT_PAD_CALIBRATION = _LEGACY_CALIBRATION["default"]


def resolve_pad_calibration(profile, value=None, uncalibrated=False):
    """Calibration value to stage for a build, or None. Only the full-panel profile carries one."""
    if profile != "fullpanel":
        if value is not None or uncalibrated:
            raise ValueError("--pad-calibration-value and --uncalibrated apply to the fullpanel profile only")
        return None
    if uncalibrated and value is not None:
        raise ValueError("--uncalibrated and --pad-calibration-value are mutually exclusive")
    if uncalibrated:
        return None
    return DEFAULT_PAD_CALIBRATION if value is None else value


def validate_bundle_suffix(token):
    """Return the suffix unchanged when it is 1-32 lowercase letters, digits or hyphens; None passes through."""
    if token is None:
        return None
    if not isinstance(token, str) or not BUNDLE_SUFFIX_PATTERN.fullmatch(token):
        raise ValueError("bundle suffix must be 1-32 lowercase letters, digits or hyphens")
    return token


def pad_calibration_xml(value, calibration=None):
    """The calibration file in a profile's own format (default: the legacy 1.60 profile)."""
    calibration = calibration or _LEGACY_CALIBRATION
    low, high = calibration["range"]
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"pad calibration value must be an integer in {low}..{high}")
    attributes = " ".join(calibration["attribute"].format(n=n, value=value) for n in range(1, calibration["pads"] + 1))
    return calibration["template"].format(attributes=attributes)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command, *, cwd=None, log=None):
    if log:
        with Path(log).open("wb") as output:
            result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f"build step failed ({result.returncode}); inspect {log}")
    else:
        subprocess.run(command, cwd=cwd, check=True)


def host_check():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("this native HVF build currently requires an Apple Silicon Mac")


def build_qemu(output):
    archive = output / f"qemu-{QEMU_VERSION}.tar.xz"
    if not archive.exists():
        print("Downloading pinned official QEMU source…", flush=True)
        with urllib.request.urlopen(f"https://download.qemu.org/{archive.name}", timeout=60) as source:
            with archive.open("xb") as target:
                shutil.copyfileobj(source, target)
    if digest(archive) != QEMU_ARCHIVE_SHA256:
        raise ValueError("QEMU source archive SHA-256 mismatch")
    source = output / f"qemu-{QEMU_VERSION}"
    if not source.exists():
        with tarfile.open(archive) as package:
            package.extractall(output, filter="data")
    build = output / "qemu-build"
    build.mkdir(mode=0o700, exist_ok=True)
    if not (build / "build.ninja").exists():
        flags = ["--target-list=aarch64-softmmu", "--enable-hvf", "--disable-pvg",
                 "--disable-docs", "--disable-tools", "--disable-guest-agent", "--disable-cocoa",
                 "--disable-sdl", "--disable-gtk", "--disable-opengl", "--disable-vnc",
                 "--disable-slirp", "--disable-libssh", "--disable-gnutls", "--disable-nettle",
                 "--disable-gcrypt", "--disable-linux-user", "--disable-bsd-user", "--disable-werror"]
        run([str(source / "configure"), *flags], cwd=build, log=output / "qemu-configure.log")
    else:
        run([str(build / "pyvenv/bin/meson"), "configure", "-Dpvg=disabled"], cwd=build,
            log=output / "qemu-configure-refresh.log")
    print("Building native QEMU with Hypervisor.framework…", flush=True)
    run(["ninja", "-j", str(min(12, os.cpu_count() or 4)), "qemu-system-aarch64"],
        cwd=build, log=output / "qemu-build.log")
    return build / "qemu-system-aarch64"


def remove_guest_image(image):
    # Untagged per-build image: removing it by ID never touches another build's tag.
    # A failure (for example, the image is still used by another container) is logged, not fatal.
    result = subprocess.run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        print(f"warning: could not remove guest image {image[:19]}: {result.stderr.strip()}",
              file=sys.stderr, flush=True)


def build_guest(root, output, application, patched_app=None, extra_files=(), dockerfile="Dockerfile.native-guest"):
    print("Packaging offline Linux guest (Docker is build tooling only)…", flush=True)
    # No shared tag: --iidfile records this build's image ID, so parallel builds cannot
    # package each other's guest code.
    iid_dir = Path(tempfile.mkdtemp(prefix="iid-", dir=output))
    iidfile = iid_dir / "image.iid"
    image = None
    try:
        run(["docker", "build", "-f", str(TOOLS / dockerfile),
             "--iidfile", str(iidfile), str(TOOLS)], log=output / "guest-build.log")
        built = iidfile.read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", built):
            raise ValueError("docker build returned an invalid image identity")
        image = built
        return package_guest(root, output, application, image, patched_app, extra_files)
    finally:
        if image:
            remove_guest_image(image)
        shutil.rmtree(iid_dir, ignore_errors=True)


def package_guest(root, output, application, image, patched_app, extra_files):
    guest = Path(tempfile.mkdtemp(prefix="guest-", dir=output))
    manifest = guest / "app-identity.json"
    if root is not None:
        manifest.write_text(json.dumps(application, indent=2) + "\n")
    identity = None
    try:
        identity = subprocess.check_output(["docker", "create", "--network", "none",
            "--mount", f"type=bind,src={guest},dst=/output", image], text=True).strip()
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid packaging container identity")
        if root is not None:
            run(["docker", "cp", str(root) + "/.", identity + ":/opt/sp16/rootfs"])
        if patched_app:
            run(["docker", "cp", str(patched_app),
                 identity + ":/opt/sp16/rootfs/root/pdj/EP085_Main_R"])
        for source, destination in extra_files:
            run(["docker", "cp", str(source), identity + ":" + destination])
        if root is not None:
            run(["docker", "cp", str(manifest), identity + ":/opt/sp16/app-identity.json"])
        run(["docker", "start", "-a", identity], log=output / "guest-pack.log")
    finally:
        if identity:
            subprocess.run(["docker", "rm", "-f", identity], stdout=subprocess.DEVNULL, check=False)
    if not (guest / "guest.cpio.gz").is_file() or not (guest / "kernel").is_file():
        raise ValueError("guest packer did not produce both boot assets")
    if root is None:
        manifest.unlink(missing_ok=True)
    return guest


APP_ARGS = ("testmode",)
TRACE_MODES = ("strace", "gdb", "pcmtap")


# Persistent per-profile eMMC image (fullpanel): <output>/data/emmc.img, sparse raw, formatted once by the guest.
DATA_DIRECTORY = "data"
DATA_IMAGE_SIZE = 1 << 30
DATA_PLACEHOLDER = "@DATA@"


AUDIO_MODES = ("file", "coreaudio", "both")
# Decoupled builds (docs/architecture.md, M3): the app fills these from the selected firmware's manifest after
# checking its profile and image hash; values are validated hex hashes, a d.dd version and a profile token.
FIRMWARE_PLACEHOLDERS = ("@FWIMAGE@", "@FWSHA@", "@FWBYTES@", "@FWAPP@", "@FWBASE@", "@FWVER@", "@FWAPPPATH@")
PADCAL_PATTERN = re.compile(r"default|off|[0-9]{1,5}")


def arguments(qemu, guest, socket, app_arg=None, trace=None, data=False, audio=None, firmware=False,
              allow_unverified=False, padcal=None):
    if audio is not None and audio not in AUDIO_MODES:
        raise ValueError("unsupported audio mode")
    if trace is not None and trace not in TRACE_MODES:
        raise ValueError("unsupported trace mode")
    if app_arg is not None and app_arg not in APP_ARGS:
        raise ValueError("unsupported application argument")
    persistent = ["-drive", f"file={DATA_PLACEHOLDER},format=raw,if=none,id=sp16emmc",
                  "-device", "virtio-blk-device,drive=sp16emmc,serial=sp16emmc",
                  "-chardev", f"socket,id=sp16ctl,path={socket}.ctl,server=on,wait=off",
                  "-device", "virtserialport,chardev=sp16ctl,name=sp16.ctl"] if data else []
    # Audio transport (M4b1): a dedicated virtio-serial port carrying playback frames to the host file sink.
    pcm = ["-chardev", f"socket,id=sp16pcm,path={socket}.pcm,server=on,wait=off",
           "-device", "virtserialport,chardev=sp16pcm,name=sp16.pcm"] if audio else []
    if allow_unverified and not firmware:
        raise ValueError("--allow-unverified applies to decoupled builds only")
    if padcal is not None and (not firmware or not PADCAL_PATTERN.fullmatch(str(padcal))):
        raise ValueError("padcal is default, off or a number, for decoupled builds only")
    fw = ["-drive", "file=@FWIMAGE@,format=raw,if=none,id=sp16fw,readonly=on",
          "-device", "virtio-blk-device,drive=sp16fw,serial=sp16fw"] if firmware else []
    return ["-machine", "virt,gic-version=3", "-cpu", "host", "-accel", "hvf",
            "-smp", "4", "-m", "4096", "-nodefaults", "-display", "none",
            "-monitor", "none", "-serial", "stdio", "-nic", "none", "-no-reboot",
            "-kernel", str(guest / "kernel"), "-initrd", str(guest / "guest.cpio.gz"),
            "-append", "console=ttyAMA0 rdinit=/init panic=-1 oops=panic random.trust_cpu=on"
                       + (f" sp16.app={app_arg}" if app_arg else "")
                       + (f" sp16.trace={trace}" if trace else "")
                       + (" sp16.data=1" if data else "")
                       + (" sp16.audio=file" if audio else "")
                       + (" sp16.fw=@FWSHA@ sp16.fwbytes=@FWBYTES@ sp16.fwapp=@FWAPP@ sp16.fwbase=@FWBASE@"
                          " sp16.fwver=@FWVER@ sp16.app_path=@FWAPPPATH@" if firmware else "")
                       + (f" sp16.padcal={padcal}" if firmware and padcal is not None else "")
                       + (" sp16.allow_unverified=1" if allow_unverified else ""),
            "-device", "virtio-serial-device",
            "-chardev", f"socket,id=sp16,path={socket},server=on,wait=off",
            "-device", "virtserialport,chardev=sp16,name=sp16.ui", *fw, *persistent, *pcm]


def shipped_profile(profile):
    """A copy of a profile for distribution: only what the D2 check, the guest runtime and the panel need (no update
    hash, no verification notes, no hardware-suite probes)."""
    copy = {key: json.loads(json.dumps(profile[key])) for key in profiles.RUNTIME_FIELDS if key in profile}
    copy["firmware"].pop("upd_sha256", None)
    return profiles.validate(copy)


def third_party_notices(guest):
    """THIRD-PARTY-NOTICES.md for a decoupled bundle: QEMU from pinned source, the Debian guest packages (list and
    copyright files collected by pack-guest.sh) and how to obtain their sources."""
    packages = (guest / "third-party-packages.txt").read_text() if (guest / "third-party-packages.txt").exists() else ""
    kernel = next((line.split()[1] for line in packages.splitlines() if line.startswith("linux-image-")), "unknown")
    return f"""# Third-party notices

{DECOUPLED_PRODUCT} contains no firmware, firmware-derived files or key material. The user imports their own update.

## QEMU {QEMU_VERSION}

`qemu-system-aarch64`, built from the official source archive `https://download.qemu.org/qemu-{QEMU_VERSION}.tar.xz`
(SHA-256 `{QEMU_ARCHIVE_SHA256}`) with Hypervisor.framework support. QEMU is licensed under the GNU General Public
License version 2; see `COPYING` in that archive. The configure options are recorded in `native.py` (`build_qemu`).

## Guest base image (Debian 12 "bookworm", arm64)

The guest kernel (Debian `linux-image-arm64`, kernel package {kernel}) and initramfs are built from Debian packages
plus this project's own scripts and hardware shim. `ThirdParty/packages.txt` lists every package with its version and
source package; `ThirdParty/copyright.txt` holds each package's copyright and licence file. The corresponding sources
are the Debian source packages of those exact versions (`https://snapshot.debian.org/`, `apt-get source`).

## Written offer

For three years from distribution, the distributor will provide the complete corresponding source code of the GPL
components above on request (contact to be filled in by the owner before any distribution).
"""


def build_icon(output, resources, logo=ICON_LOGO, name=ICON_FILE):
    """Render the app icon (make-icon.swift draws every size) and package it as Resources/<ICON_FILE>.icns."""
    work = output / "icon-build"
    iconset = work / f"{name}.iconset"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    tool = work / "make-icon"
    run(["swiftc", "-O", "-framework", "AppKit", str(TOOLS / ICON_TOOL), "-o", str(tool)], log=output / "icon-build.log")
    run([str(tool), str(TOOLS / logo), str(iconset)], log=output / "icon-render.log")
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / f"{name}.icns")], log=output / "icon-pack.log")


def resolve_persistence(profile, no_persistence=False):
    """Only the full-panel profile carries the persistent data image; stock/mod-lab/testmode never do."""
    if profile != "fullpanel":
        if no_persistence:
            raise ValueError("--no-persistence applies to the fullpanel profile only")
        return False
    return not no_persistence


def bundle(output, qemu, guest, variant="stock", profile=None, research=False, app_arg=None, trace=None,
           bundle_suffix=None, persistent=False, audio=None, audio_device=None, decoupled=False, library=None,
           allow_unverified=False, padcal=None):
    app = output / (DECOUPLED_APP_NAME if decoupled else APP_NAME)
    if decoupled and (output / APP_NAME).exists():
        shutil.rmtree(output / APP_NAME)  # an earlier, pre-M7 bundle of this same decoupled build
    executable = app / "Contents/MacOS" / (DECOUPLED_EXECUTABLE if decoupled else "SP16LegacyEmulator")
    executable.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resources = app / "Contents/Resources"
    if decoupled:
        shutil.rmtree(resources, ignore_errors=True)  # start clean: no stale legacy icon or logo survives a rebuild
    resources.mkdir(mode=0o700, exist_ok=True)
    run(["swiftc", "-O", "-parse-as-library", "-framework", "AppKit", "-framework", "AVFoundation", "-framework", "CoreAudio",
         *(str(TOOLS / name) for name in SWIFT_SOURCES), "-o", str(executable)],
        log=output / "swift-build.log")
    for name in (DECOUPLED_RESOURCES if decoupled else PANEL_RESOURCES):
        shutil.copy2(TOOLS / name, resources / Path(name).name)
    if decoupled:
        # The app checks the selected firmware against these profiles before boot (D2); the firmware library tool
        # (import screen, library window) is the same code as `native.py firmware`.
        fwlib = resources / "fwlib"
        shutil.rmtree(fwlib, ignore_errors=True)
        for folder in (resources / "profiles", fwlib, fwlib / "profiles"):
            shutil.rmtree(folder, ignore_errors=True) if folder == resources / "profiles" else None
            folder.mkdir(mode=0o700, exist_ok=True)
        for source in sorted((TOOLS / "profiles").glob("*.json")):
            text = json.dumps(shipped_profile(profiles.load(source.stem)), indent=2, sort_keys=True) + "\n"
            (resources / "profiles" / source.name).write_text(text)
            (fwlib / "profiles" / source.name).write_text(text)
        for name in FWLIB_FILES:
            shutil.copy2(TOOLS / name, fwlib / name)
        (fwlib / "profiles/products").mkdir(mode=0o700)
        for source in sorted((TOOLS / "profiles/products").glob("*.json")):
            profiles.product(source.stem)
            shutil.copy2(source, fwlib / "profiles/products" / source.name)
        notices = resources / "ThirdParty"
        shutil.rmtree(notices, ignore_errors=True)
        notices.mkdir(mode=0o700)
        (resources / "THIRD-PARTY-NOTICES.md").write_text(third_party_notices(guest))
        for source, name in (("third-party-packages.txt", "packages.txt"), ("third-party-copyright.txt", "copyright.txt")):
            if (guest / source).exists():
                shutil.copy2(guest / source, notices / name)
    build_icon(output, resources, NEUTRAL_ICON if decoupled else ICON_LOGO,
               DECOUPLED_ICON_FILE if decoupled else ICON_FILE)
    info = {"CFBundleIdentifier": "local.sp16-emulator.legacy", "CFBundleName": "SP16 Emulator",
            "CFBundleExecutable": "SP16LegacyEmulator", "CFBundlePackageType": "APPL", "CFBundleIconFile": ICON_FILE,
            "CFBundleShortVersionString": "0.1", "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0"}
    if decoupled:
        info.update(CFBundleIdentifier=DECOUPLED_BUNDLE_ID, CFBundleName=DECOUPLED_PRODUCT,
                    CFBundleExecutable=DECOUPLED_EXECUTABLE, CFBundleIconFile=DECOUPLED_ICON_FILE)
    if variant != "stock":
        info["CFBundleIdentifier"] += "." + variant
        info["CFBundleName"] += " MOD LAB"
    if profile:
        info["CFBundleIdentifier"] += "." + profile
        info["CFBundleName"] += "" if decoupled else {"fullpanel": " Full Panel"}.get(profile, " Factory Test")
    if bundle_suffix:
        info["CFBundleIdentifier"] += "." + bundle_suffix
        info["CFBundleName"] += " " + bundle_suffix
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    sessions = output / "sessions"
    sessions.mkdir(mode=0o700, exist_ok=True)
    defaults = {"qemu": str(qemu), "arguments": arguments(qemu, guest, "@SOCKET@", app_arg, trace, persistent, audio,
                                                       decoupled, allow_unverified, padcal if decoupled else None),
                "session": str(sessions), "socket": "@SOCKET@", "variant": variant,
                "profile": profile or "", "research": research}
    if audio:
        defaults["audio"] = audio + (":" + audio_device if audio_device else "")
    if persistent:
        defaults["data"] = {"dir": str(output / DATA_DIRECTORY), "size": DATA_IMAGE_SIZE}
    if decoupled:
        # Data images are per firmware identity: <library>/<id>/data (the app replaces data.dir at launch).
        defaults["firmware"] = {"library": str(library), "allow_unverified": allow_unverified}
    (resources / "default-config.json").write_text(json.dumps(defaults, indent=2) + "\n")
    run(["codesign", "--force", "--sign", "-", str(app)], log=output / "app-sign.log")
    return app


def validated_assets(output):
    assets = json.loads((output / "assets.json").read_text())
    if assets.get("decoupled") is True:
        return validated_base_assets(output, assets)
    application = validate_identity(assets.get("application", identity()))
    if assets.get("app_sha256") != application["app_sha256"] or assets.get("firmware") != FIRMWARE:
        raise ValueError("native assets do not identify an allowed SP-16 application")
    required = (Path(assets["qemu"]), Path(assets["guest"]) / "kernel",
                Path(assets["guest"]) / "guest.cpio.gz")
    if not all(str(path) in assets["hashes"] for path in required):
        raise ValueError("native boot asset hashes are incomplete")
    for name, expected in assets["hashes"].items():
        path = Path(name)
        if not path.resolve().is_relative_to(output.resolve()) or digest(path) != expected:
            raise ValueError("native boot asset identity changed; rebuild")
    return assets


def validated_base_assets(output, assets):
    """A decoupled build: QEMU, base kernel and base initramfs hashes; no OEM application is part of the build."""
    if "application" in assets or assets.get("firmware") is not None:
        raise ValueError("decoupled build assets must not name a firmware application")
    required = (Path(assets["qemu"]), Path(assets["guest"]) / "kernel", Path(assets["guest"]) / "guest.cpio.gz")
    if not all(str(path) in assets["hashes"] for path in required):
        raise ValueError("native boot asset hashes are incomplete")
    for name, expected in assets["hashes"].items():
        path = Path(name)
        if not path.resolve().is_relative_to(output.resolve()) or digest(path) != expected:
            raise ValueError("native boot asset identity changed; rebuild")
    return assets


def base_boot(output):
    """QEMU and base guest paths of a validated decoupled build, for the import VM."""
    assets = validated_assets(private_path(output))
    if assets.get("decoupled") is not True:
        raise ValueError("firmware commands need a decoupled build (native.py build --decoupled)")
    guest = Path(assets["guest"])
    return assets, {"qemu": assets["qemu"], "kernel": str(guest / "kernel"), "initrd": str(guest / "guest.cpio.gz"),
                    "initrd_sha256": assets["hashes"][str(guest / "guest.cpio.gz")]}


def build(root, output, *, mod=None, reuse_qemu_from=None, profile=None, research=False, app_arg=None,
          pad_calibration=None, uncalibrated=False, trace=None, bundle_suffix=None, no_persistence=False, audio=None,
          audio_device=None, decoupled=False, library=None, allow_unverified=False):
    bundle_suffix = validate_bundle_suffix(bundle_suffix)
    if decoupled:
        return build_decoupled(output, reuse_qemu_from=reuse_qemu_from, profile=profile, research=research,
                               pad_calibration=pad_calibration, uncalibrated=uncalibrated, trace=trace,
                               bundle_suffix=bundle_suffix, audio=audio, audio_device=audio_device, library=library,
                               allow_unverified=allow_unverified, root=root, mod=mod, app_arg=app_arg,
                               no_persistence=no_persistence)
    if allow_unverified or library:
        raise ValueError("--allow-unverified and --library apply to decoupled builds only")
    if not (TOOLS / "Dockerfile.native-guest").is_file():
        raise ValueError(LEGACY_REFUSAL)
    if root is None:
        raise ValueError("--rootfs is required (or build --decoupled)")
    persistent = resolve_persistence(profile, no_persistence)
    if trace and not research:
        raise ValueError("--trace is research-only; add --research")
    if trace and mod:
        raise ValueError("--trace is not allowed on --mod builds")
    if audio and not (research or profile == "fullpanel"):
        raise ValueError("--audio is for research or fullpanel builds only")
    if audio_device is not None and audio not in ("coreaudio", "both"):
        raise ValueError("--audio-device needs --audio coreaudio or both")
    if audio_device is not None and (not audio_device or len(audio_device) > 512 or any(ord(c) < 32 for c in audio_device)):
        raise ValueError("--audio-device must be a device UID (or offline:<wav>[:rate[:ppm[:block]]])")
    if audio and mod:
        raise ValueError("--audio is not allowed on --mod builds")
    pad_calibration = resolve_pad_calibration(profile, pad_calibration, uncalibrated)
    host_check()
    root = private_path(root)
    if digest(guest_file(root, "/root/pdj/EP085_Main_R")) != EXPECTED_APP:
        raise ValueError("build requires the validated unmodified SP-16 1.60 application")
    # Experimental builds must have a fresh destination, preserving stock and prior experiments.
    output = private_path(output, fresh=bool(mod) or not Path(output).exists())
    if profile and (output / "assets.json").exists():
        # An experimental profile may be rebuilt in place; never overwrite another build.
        if json.loads((output / "assets.json").read_text()).get("profile") != profile:
            raise ValueError("existing output belongs to a different build profile")
    output.mkdir(mode=0o700, exist_ok=True)
    if "," in str(output):
        raise ValueError("build path must not contain commas")
    extra_files = []
    if pad_calibration is not None:
        calibration = output / "pad-calibration.xml"
        calibration.write_text(pad_calibration_xml(pad_calibration))
        calibration.chmod(0o600)
        extra_files.append((calibration, PAD_CALIBRATION_PATH))
    application = identity(mod or "stock")
    patched_app = None
    if mod:
        patched_app = output / "EP085_Main_R"
        patched_app.write_bytes(patch_utility(guest_file(root, "/root/pdj/EP085_Main_R").read_bytes()))
        patched_app.chmod(0o700)
        (output / "patch-manifest.json").write_text(json.dumps(application, indent=2) + "\n")
    if reuse_qemu_from:
        previous = validated_assets(private_path(reuse_qemu_from))
        if previous.get("qemu_version") != QEMU_VERSION:
            raise ValueError("reused QEMU must match the pinned version")
        qemu = output / "qemu-system-aarch64"
        if Path(previous["qemu"]).resolve() != qemu.resolve():
            shutil.copy2(previous["qemu"], qemu)
        if digest(qemu) != previous["hashes"][previous["qemu"]]:
            raise ValueError("reused QEMU copy differs")
    else:
        qemu = build_qemu(output)
    guest = build_guest(root, output, application, patched_app, extra_files)
    app = bundle(output, qemu, guest, application["variant"], profile, research, app_arg, trace, bundle_suffix,
                 persistent, audio, audio_device)
    assets = {"firmware": FIRMWARE, "app_sha256": application["app_sha256"],
              "application": application, "qemu_version": QEMU_VERSION,
              "app_bundle": str(app), "qemu": str(qemu), "guest": str(guest),
              "profile": profile or "", "research": research, "app_arg": app_arg or "", "trace": trace or "", "audio": audio or "", "audio_device": audio_device or "",
              "pad_calibration": pad_calibration, "persistent_data": persistent,
              "hashes": {str(p): digest(p) for p in (qemu, guest / "kernel", guest / "guest.cpio.gz")},
              "guest_source_hashes": {name: digest(TOOLS / name) for name in GUEST_FILES}}
    (output / "assets.json").write_text(json.dumps(assets, indent=2) + "\n")
    if profile:
        for stale in output.glob("guest-*"):
            if stale.is_dir() and stale.resolve() != guest.resolve():
                shutil.rmtree(stale)
    print(json.dumps({"status": "native_app_built", "app": str(app), "runtime_requires_docker": False}))


def build_decoupled(output, *, reuse_qemu_from=None, profile=None, research=False, pad_calibration=None,
                    uncalibrated=False, trace=None, bundle_suffix=None, audio=None, audio_device=None, library=None,
                    allow_unverified=False, root=None, mod=None, app_arg=None, no_persistence=False):
    """Decoupled build (docs/architecture.md, M3): the app bundle, QEMU and a guest base image with no OEM files. Firmware comes
    from the per-build library (native.py firmware import) and is attached at launch as a read-only disk."""
    if root is not None or mod or app_arg:
        raise ValueError("a decoupled build takes no --rootfs, --mod or --app-arg (firmware is imported separately)")
    if profile != "fullpanel":
        raise ValueError("--decoupled currently supports --profile fullpanel")
    if no_persistence:
        raise ValueError("decoupled builds keep a per-firmware data image")
    if trace and not research:
        raise ValueError("--trace is research-only; add --research")
    if allow_unverified and not research:
        raise ValueError("--allow-unverified is a developer option; add --research")
    if audio_device is not None and audio not in ("coreaudio", "both"):
        raise ValueError("--audio-device needs --audio coreaudio or both")
    if uncalibrated and pad_calibration is not None:
        raise ValueError("--uncalibrated and --pad-calibration-value are mutually exclusive")
    # The range and format are the active firmware profile's; the guest validates the value at boot.
    padcal = "off" if uncalibrated else ("default" if pad_calibration is None else str(int(pad_calibration)))
    host_check()
    output = private_path(output, fresh=not Path(output).exists())
    if (output / "assets.json").exists():
        previous = json.loads((output / "assets.json").read_text())
        if previous.get("decoupled") is not True or previous.get("profile") != profile:
            raise ValueError("existing output belongs to a different build")
    output.mkdir(mode=0o700, exist_ok=True)
    if "," in str(output):
        raise ValueError("build path must not contain commas")
    library = Path(library) if library else output / "library"
    firmware.Library(library, create=True)
    extra_files = []
    if reuse_qemu_from:
        previous = validated_assets(private_path(reuse_qemu_from))
        if previous.get("qemu_version") != QEMU_VERSION:
            raise ValueError("reused QEMU must match the pinned version")
        qemu = output / "qemu-system-aarch64"
        if Path(previous["qemu"]).resolve() != qemu.resolve():
            shutil.copy2(previous["qemu"], qemu)
        if digest(qemu) != previous["hashes"][previous["qemu"]]:
            raise ValueError("reused QEMU copy differs")
    else:
        qemu = build_qemu(output)
    guest = build_guest(None, output, None, None, extra_files, dockerfile="Dockerfile.native-base")
    app = bundle(output, qemu, guest, "stock", profile, research, None, trace, bundle_suffix, True, audio,
                 audio_device, decoupled=True, library=library, allow_unverified=allow_unverified, padcal=padcal)
    assets = {"decoupled": True, "firmware": None, "qemu_version": QEMU_VERSION,
              "app_bundle": str(app), "qemu": str(qemu), "guest": str(guest), "library": str(library),
              "profile": profile, "research": research, "trace": trace or "", "audio": audio or "",
              "audio_device": audio_device or "", "pad_calibration": padcal, "persistent_data": True,
              "allow_unverified": allow_unverified,
              "hashes": {str(p): digest(p) for p in (qemu, guest / "kernel", guest / "guest.cpio.gz")},
              "guest_source_hashes": {name: digest(TOOLS / name) for name in BASE_GUEST_FILES}}
    (output / "assets.json").write_text(json.dumps(assets, indent=2) + "\n")
    for stale in output.glob("guest-*"):
        if stale.is_dir() and stale.resolve() != guest.resolve():
            shutil.rmtree(stale)
    print(json.dumps({"status": "native_app_built", "decoupled": True, "app": str(app), "library": str(library),
                      "runtime_requires_docker": False}))


USB_IMAGE_SIZE = 1 << 30


def prepare_usb_image(path):
    """Resolve the virtual USB stick image for `launch --usb-image`: an absolute regular file owned by the user.
    A missing file is created sparse (1 GiB, 0600); it stays blank and the guest's trusted init formats it as FAT32
    on first attach (the OEM never formats anything)."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if "," in str(path):
        raise ValueError("USB image path must not contain a comma")
    if path.is_symlink():
        raise ValueError("USB image must not be a symlink")
    if not path.exists():
        if not path.parent.is_dir():
            raise ValueError(f"USB image directory {path.parent} does not exist")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.truncate(USB_IMAGE_SIZE)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("USB image must be a regular file owned by you")
    return str(path)


def dev_overlay_path(folder):
    """A developer overlay folder for --dev-overlay: an existing directory you own, no symlinks, no commas."""
    path = Path(os.path.abspath(folder))
    if "," in str(path) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("--dev-overlay must not contain commas or symlinks")
    info = path.lstat() if path.exists() else None
    if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("--dev-overlay must be an existing folder owned by you")
    return str(path)


def launch(output, ephemeral=False, usb_image=None, firmware_id=None, dev_overlay=None):
    host_check()
    output = private_path(output)
    assets = validated_assets(output)
    if dev_overlay is not None and not (assets.get("decoupled") is True and assets.get("research")):
        raise ValueError("--dev-overlay needs a decoupled research build")
    if firmware_id is not None:
        if assets.get("decoupled") is not True:
            raise ValueError("--firmware applies to decoupled builds only")
        library = firmware.Library(assets["library"])
        manifest = firmware.verify_image(library, firmware_id)
        status = firmware.boot_status(manifest, library)
        if status["status"] != "verified" and not assets.get("allow_unverified"):
            report = firmware.check_report(manifest, library)
            raise ValueError("firmware refused (no verified compatibility profile): " + status["reason"]
                             + "\n" + json.dumps(report, indent=2))
    # LaunchServices supervises the app. Closing its window stops its own QEMU.
    # --ephemeral: disposable per-session data image and no profile lock (tests); persistent builds default to the profile image.
    app_args = (["--ephemeral"] if ephemeral else []) + (["--usb-image", prepare_usb_image(usb_image)] if usb_image else []) \
        + (["--firmware", firmware_id] if firmware_id else []) \
        + (["--dev-overlay", dev_overlay_path(dev_overlay)] if dev_overlay else [])
    run(["open", "-n", assets["app_bundle"], *(["--args", *app_args] if app_args else [])])
    print(json.dumps({"status": "native_app_opened", "app": assets["app_bundle"],
                      "diagnostics": str(output / "sessions"), "runtime_requires_docker": False}))


def firmware_command(args):
    """native.py firmware ...: prints one JSON document; a refused import prints its compatibility report."""
    assets, boot = base_boot(args.output)
    library = firmware.Library(assets["library"])
    if args.action == "import":
        if not args.update:
            raise ValueError("firmware import needs --update")
        key = firmware.load_key(args.key_file, args.gpl_archive)
        try:
            import fwpack
            entry_id, report = firmware.import_update(library, boot, args.update, key, name=args.name,
                                                      verify_mod=fwpack.mod_verifier(library, boot, key),
                                                      progress=lambda m: print(json.dumps(m), file=sys.stderr))
        except firmware.FirmwareError as error:
            del key
            print(json.dumps({"status": "firmware_refused",
                              "report": getattr(error, "report", firmware.refusal_report(None, str(error)))}, indent=2))
            return 1
        del key
        if library.selected() is None:
            library.select(entry_id)
        print(json.dumps({"status": "firmware_imported", "id": entry_id, "report": report}, indent=2))
        return 0
    if args.action == "audit":
        # Distribution scan (M3 gate, M7): bundle, QEMU, kernel and every file inside the base initramfs.
        # Vendor logo files exist only in the private repository; an exported copy without them still checks names.
        logos = {digest(TOOLS / name): f"vendor logo {name}" for name in (*PANEL_RESOURCES[1:], ICON_LOGO)
                 if (TOOLS / name).is_file()}
        upd_pins = {p["firmware"]["upd_sha256"]: f"update SHA-256 pin of {p['id']} (not needed for verification)"
                    for p in profiles.all_profiles() if p["firmware"].get("upd_sha256")}
        names = {Path(name).name for name in (*PANEL_RESOURCES[1:], ICON_LOGO)} | {f"{ICON_FILE}.icns"}
        report = firmware.audit_build([assets["app_bundle"], assets["qemu"], boot["kernel"]], [boot["initrd"]], library,
                                      forbidden_files=logos, forbidden_strings=upd_pins, forbidden_names=names)
        print(json.dumps(report, indent=2))
        return 0 if report["clean"] else 1
    if args.action == "list":
        print(json.dumps({"library": str(library.path), "selected": library.selected(),
                          "firmware": [library.summary(i) for i in library.ids()]}, indent=2))
        return 0
    if not args.id:
        raise ValueError(f"firmware {args.action} needs --id")
    if args.action == "show":
        manifest = library.manifest(args.id)
        shown = {k: v for k, v in manifest.items() if k != "files"}
        shown["files"] = len(manifest["files"])
        print(json.dumps({**library.summary(args.id), "manifest": shown}, indent=2))
    elif args.action == "check":
        print(json.dumps(firmware.check_report(firmware.verify_image(library, args.id), library), indent=2))
    elif args.action == "rename":
        library.rename(args.id, args.name)
        print(json.dumps(library.summary(args.id), indent=2))
    elif args.action == "remove":
        library.remove(args.id)
        print(json.dumps({"status": "firmware_removed", "id": args.id}))
    elif args.action == "select":
        library.select(args.id)
        print(json.dumps({"status": "firmware_selected", "id": args.id}))
    elif args.action == "profile":
        # A candidate profile for an imported version: identity from its manifest, measurements copied from a
        # reference profile as the starting assumption. Save it as profiles/<id>.json, verify, then mark it verified.
        print(json.dumps(firmware.candidate_profile(library.manifest(args.id), args.from_profile), indent=2))
    return 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="compile QEMU/window and package guest once")
    legacy_help = (lambda text: text) if LEGACY_AVAILABLE else (lambda text: argparse.SUPPRESS)
    builder.add_argument("--rootfs", help=legacy_help("prepared OEM rootfs (legacy builds); omit with --decoupled"))
    builder.add_argument("--output", required=True)
    builder.add_argument("--mod", choices=["utility-label"], help=legacy_help("apply the verified UTILITY → MOD LAB patch"))
    builder.add_argument("--reuse-qemu-from", help="copy hash-verified QEMU from an existing private build")
    builder.add_argument("--profile", choices=PROFILES, default=None if LEGACY_AVAILABLE else "fullpanel",
                         help="separate experimental app profile/bundle ID" if LEGACY_AVAILABLE
                         else "app profile (fullpanel, the default, is the only one in this copy)")
    builder.add_argument("--app-arg", choices=APP_ARGS,
                         help=legacy_help("launch the OEM application with a fixed research argument"))
    builder.add_argument("--pad-calibration-value", type=int, metavar="N",
                         help="fullpanel only: stage a synthetic padcalibration.xml with N (48..504) for all pads "
                              f"(default {DEFAULT_PAD_CALIBRATION}, not factory data)")
    builder.add_argument("--uncalibrated", action="store_true",
                         help="fullpanel only: stage no calibration file, so the OEM reports uncalibrated pads (blinking)")
    builder.add_argument("--no-persistence", action="store_true",
                         help=legacy_help("fullpanel only: build without the persistent <output>/data/emmc.img block device"))
    builder.add_argument("--research", action="store_true",
                         help="enable the owner-only session control socket (events, raw research frames, snapshots)")
    builder.add_argument("--trace", choices=TRACE_MODES,
                         help="research only: strace streams qemu-arm-static -strace to console.txt as SP16_STRACE lines; "
                              "gdb opens the qemu-user stub (-g 1234, guest loopback, blocks the app until attached); "
                              "pcmtap records 4 s of each playback PCM from the first non-zero frame and prints it to "
                              "console.txt as SP16_PCM_DUMP gzip+base64 lines (no host audio output)")
    builder.add_argument("--audio", choices=AUDIO_MODES,
                         help="research or fullpanel only: carry the OEM playback PCM over a dedicated virtio-serial port "
                              "(sp16.pcm). file: one WAV per stream plus counters in the session directory (no sound reaches a "
                              "host device); coreaudio: play ak4458 ch12/ch14 as stereo on the host output (AUDIBLE); both. "
                              "Off (silent) by default")
    builder.add_argument("--audio-device", metavar="UID",
                         help="with --audio coreaudio|both: output device by stable CoreAudio UID (list them with "
                              "`audio-selftest devices`, see docs/architecture.md); default is the system default output. "
                              "offline:<wav>[:rate[:ppm[:block]]] renders the output graph to a file on a software clock "
                              "instead (verification; never opens a device)")
    builder.add_argument("--decoupled", action="store_true", default=not LEGACY_AVAILABLE,
                         help="build the app, QEMU and a guest base image with no OEM files; "
                              "firmware is imported into the build's library (native.py firmware import) and attached "
                              "read-only at launch" + ("" if LEGACY_AVAILABLE else " (always on in this copy)"))
    builder.add_argument("--library", metavar="DIR",
                         help="with --decoupled: firmware library directory (default <output>/library)")
    builder.add_argument("--allow-unverified", action="store_true",
                         help="with --decoupled --research: developer opt-in that boots a firmware whose profile is a "
                              "candidate or missing, to verify it and create its profile (D2); never for distribution")
    builder.add_argument("--bundle-suffix", metavar="TOKEN",
                         help="append .TOKEN to the bundle ID (lowercase letters, digits, hyphens; max 32), for dev builds "
                              "that must be quit separately; default bundle IDs are unchanged")
    launcher = commands.add_parser("launch", help="open the built native app; no Docker needed")
    launcher.add_argument("--output", required=True)
    launcher.add_argument("--ephemeral", action="store_true",
                          help="use a disposable data image for this session (no persistence, no profile lock)")
    launcher.add_argument("--usb-image", metavar="PATH",
                          help="fullpanel only: attach this raw image as a virtual USB stick (created sparse if missing; "
                               "formatted FAT32 by the guest init when blank). Cold attach: chosen at launch")
    launcher.add_argument("--firmware", metavar="ID", help="decoupled builds: firmware id from the build's library")
    launcher.add_argument("--dev-overlay", metavar="DIR",
                          help="decoupled research builds: share this mod folder read-only over 9p; its files/, "
                               "recipes/ and preload/ are applied in the guest before each app start (fwdev.py)")
    fw = commands.add_parser("firmware", help="decoupled builds: import, list, check and manage user-supplied firmware")
    fw.add_argument("action", choices=("import", "list", "show", "check", "rename", "remove", "select", "audit",
                                       "profile"))
    fw.add_argument("--output", required=True, help="decoupled build directory")
    fw.add_argument("--update", help="import: the SP-16 update (.UPD)")
    fw.add_argument("--key-file", help="import: the update key file")
    fw.add_argument("--gpl-archive", nargs="+", metavar="PART",
                    help="import: the official GPL source archive (the .tar.gz, its split parts, or the two official "
                         "zip downloads, in order); the key is read from it in memory")
    fw.add_argument("--id", help="firmware id (show, check, rename, remove, select)")
    fw.add_argument("--name", help="import/rename: display name")
    fw.add_argument("--from-profile", help="profile: measurements to start from (default: the product's reference)")
    args = parser.parse_args()
    if args.command == "build" and not LEGACY_AVAILABLE and (
            args.rootfs or args.mod or args.app_arg or args.no_persistence or args.profile != "fullpanel"):
        parser.exit(1, f"native emulator failed: {LEGACY_REFUSAL}\n")
    try:
        if args.command == "build":
            build(args.rootfs, args.output, mod=args.mod, reuse_qemu_from=args.reuse_qemu_from,
                  profile=args.profile, research=args.research, app_arg=args.app_arg,
                  pad_calibration=args.pad_calibration_value, uncalibrated=args.uncalibrated, trace=args.trace,
                  bundle_suffix=args.bundle_suffix, no_persistence=args.no_persistence, audio=args.audio,
                  audio_device=args.audio_device, decoupled=args.decoupled, library=args.library,
                  allow_unverified=args.allow_unverified)
        elif args.command == "firmware":
            return firmware_command(args)
        else:
            launch(args.output, args.ephemeral, args.usb_image, args.firmware, args.dev_overlay)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"native emulator failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
