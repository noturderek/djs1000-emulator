"""Host side of the decoupled firmware path (docs/architecture.md, M2-M5). Stdlib only: no Docker, no
squashfs-tools and no crypto library on the host.

- inspect_update: trailer marker, version, size and CRC of a user-supplied update (stock LIVESAMPLER or our mod
  SP16MODLAB- marker), checked before anything boots.
- load_key: the user's key from a key file or from the official GPL source archive (the .tar.gz, its split parts or the two
  official .zip downloads), held in memory only. It is sent to the import VM over a private socket and never written
  to a file, a manifest, a log or the console.
- import_update: boots the decoupled base image in import mode; the guest decrypts, assembles and writes a read-only
  squashfs; the host verifies the image hash and stores it in the firmware library with its manifest.
- Library: <library>/<id>/{manifest.json, entry.json, firmware.sqfs, update.upd, data/}. Owner-only, no symlinks, and
  git-ignored when inside the repository.
- check_report: the compatibility report of an import; an unverified one is shown as a notice (owner decision of
  2026-10-13: any correctly keyed update runs, marked unverified when no verified profile matches).
"""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
import zlib

import profiles

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
SECTOR = 512
TRAILER = 20
# The stock trailer marker is product data (profiles/products/<product>.json); SP16MODLAB- is our own mod marker.
STOCK_MARKER = re.compile(re.escape(profiles.product()["update"]["stock_marker"].encode()) + rb"([0-9]\.[0-9]{2})\x00")
MOD_MARKER = re.compile(rb"SP16MODLAB-([0-9]\.[0-9]{2})\x00")
KEY_MAGIC = b"SP16KEY1"
KEY_MEMBER = "usr/local/pdj/aes256.key"
MANIFEST_SCHEMA = 1
IMPORTER_VERSION = 1
PRODUCT = "sp16"
ENTRY_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,95}")
SHA256 = re.compile(r"[0-9a-f]{64}")
OUTPUT_DISK_BYTES = 1 << 30
MAX_KEY_FILE = 4096
GPL_KEY_LIMIT = 4096


class FirmwareError(ValueError):
    """A clean refusal with a user-facing reason (never contains key material)."""


# ----------------------------------------------------------------------------------------------- update inspection

def inspect_update(path):
    """Trailer, version and CRC of an update file. Raises FirmwareError for anything the importer must refuse."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise FirmwareError(f"cannot read the update: {error.strerror}") from None
    if size <= TRAILER:
        raise FirmwareError("update is too small")
    payload = size - TRAILER
    if payload % SECTOR:
        raise FirmwareError("update is truncated or padded (encrypted payload is not sector-aligned)")
    digest = hashlib.sha256()
    crc = 0
    with path.open("rb") as source:
        remaining = payload
        while remaining:
            block = source.read(min(1 << 20, remaining))
            if not block:
                raise FirmwareError("update is truncated")
            crc = zlib.crc32(block, crc)
            digest.update(block)
            remaining -= len(block)
        trailer = source.read(TRAILER)
        digest.update(trailer)
    kind = version = None
    for name, marker in (("stock", STOCK_MARKER), ("mod", MOD_MARKER)):
        match = marker.fullmatch(trailer[:16])
        if match:
            kind, version = name, match.group(1).decode()
    if kind is None:
        raise FirmwareError("not a sampler update (unknown trailer marker)")
    stored = int.from_bytes(trailer[16:20], "little")
    if stored != crc:
        raise FirmwareError("update CRC does not match (file is damaged or modified)")
    return {"kind": kind, "trailer_version": version, "bytes": size, "payload_bytes": payload,
            "crc32": f"{crc:08x}", "sha256": digest.hexdigest()}


# ------------------------------------------------------------------------------------------------------------ keys

def update_key(raw):
    """The 32-byte update key from the key file contents."""
    lines = raw.splitlines()
    if not lines or not lines[0].split(b"\0", 1)[0]:
        raise FirmwareError("key file is empty")
    return lines[0].split(b"\0", 1)[0][:31].ljust(32, b"\0")


class _Chain(io.RawIOBase):
    """Read several file objects back to back (split archive parts)."""

    def __init__(self, parts):
        self.parts = list(parts)

    def readable(self):
        return True

    def readinto(self, buffer):
        while self.parts:
            data = self.parts[0].read(len(buffer))
            if data:
                buffer[:len(data)] = data
                return len(data)
            self.parts.pop(0).close()
        return 0


def _archive_stream(paths):
    """One byte stream over the GPL archive: a .tar.gz, its split parts, or the official .zip downloads that each
    hold one part. Parts are used in the given order."""
    streams = []
    for path in paths:
        path = Path(path)
        if zipfile.is_zipfile(path):
            archive = zipfile.ZipFile(path)
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) != 1:
                raise FirmwareError(f"{path.name}: expected one archive part inside the zip")
            streams.append(archive.open(members[0]))
        else:
            streams.append(path.open("rb"))
    return io.BufferedReader(_Chain(streams), 1 << 20)


def _find_key(stream, depth=0):
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            name = member.name.lstrip("./")
            if member.isfile() and (name == "initramfs/" + KEY_MEMBER or name.endswith("/initramfs/" + KEY_MEMBER)):
                if member.size > GPL_KEY_LIMIT:
                    raise FirmwareError("key member in the GPL archive is unexpectedly large")
                return archive.extractfile(member).read()
            if member.isfile() and name.endswith("initramfs.tar.gz") and depth == 0:
                found = _find_key(archive.extractfile(member), depth + 1)
                if found is not None:
                    return found
    return None


def load_key(key_file=None, gpl_archive=None):
    """Return the 32-byte key from exactly one source; it stays in memory."""
    if (key_file is None) == (not gpl_archive):
        raise FirmwareError("give either a key file or the GPL archive")
    if key_file is not None:
        path = Path(key_file)
        if not path.is_file() or path.stat().st_size > MAX_KEY_FILE:
            raise FirmwareError("key file is missing or too large")
        return update_key(path.read_bytes())
    paths = [gpl_archive] if isinstance(gpl_archive, (str, Path)) else list(gpl_archive)
    for path in paths:
        if not Path(path).is_file():
            raise FirmwareError("GPL archive part is missing")
    try:
        raw = _find_key(_archive_stream(paths))
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError, zlib.error):
        raise FirmwareError("GPL archive is damaged or incomplete (give every part, in order)") from None
    if raw is None:
        raise FirmwareError("GPL archive does not contain the update key")
    return update_key(raw)


def key_absent(key, data):
    """True when neither the key, its hex form nor its unpadded form occurs in data (used by tests and audits)."""
    core = key.rstrip(b"\0")
    needles = [key, core, key.hex().encode(), key.hex().upper().encode()]
    return not any(needle and needle in data for needle in needles if len(needle) >= 8)


# ----------------------------------------------------------------------------------------------- private locations

def git_worktree(path):
    """The enclosing git work tree of path (or of its nearest existing parent), or None."""
    probe = Path(path)
    while not probe.exists():
        probe = probe.parent
    if shutil.which("git") is None:
        return None
    result = subprocess.run(["git", "-C", str(probe if probe.is_dir() else probe.parent), "rev-parse",
                             "--show-toplevel"], capture_output=True, text=True)
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def private_dir(path, *, create=False):
    """An owner-only directory without symlinks; inside any git work tree (this repository or another) it must be
    git-ignored, so firmware can never be committed by accident."""
    path = Path(os.path.abspath(path))
    if "," in str(path):
        raise FirmwareError("paths given to QEMU must not contain commas")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise FirmwareError("firmware library paths must not contain symlinks")
    worktree = REPO if path.is_relative_to(REPO) else git_worktree(path)
    if worktree is not None:
        ignored = subprocess.run(["git", "check-ignore", "-q", "--", str(path)], cwd=worktree,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ignored.returncode:
            raise FirmwareError("a firmware library inside a git work tree must be git-ignored")
    if create and not path.exists():
        if not path.parent.is_dir():
            raise FirmwareError("firmware library parent directory does not exist")
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise FirmwareError("firmware library must be an owner-only directory owned by you")
    return path


def input_file(path, what):
    path = Path(os.path.abspath(path))
    if "," in str(path):
        raise FirmwareError(f"{what} path must not contain commas")
    if path.is_symlink() or not path.is_file():
        raise FirmwareError(f"{what} must be a regular file")
    return path


def clone(source, target):
    """APFS clone when possible (no extra disk space), else a copy. Never follows a symlink target."""
    result = subprocess.run(["cp", "-c", str(source), str(target)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if result.returncode:
        shutil.copyfile(source, target)
    os.chmod(target, 0o600)


def sha256_file(path, limit=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        remaining = limit
        while remaining is None or remaining > 0:
            block = source.read(1 << 20 if remaining is None else min(1 << 20, remaining))
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------------------- library

class Library:
    def __init__(self, path, create=False):
        self.path = private_dir(path, create=create)

    def entry_dir(self, entry_id):
        if not isinstance(entry_id, str) or not ENTRY_ID.fullmatch(entry_id):
            raise FirmwareError("invalid firmware id")
        return self.path / entry_id

    def ids(self):
        return sorted(p.name for p in self.path.iterdir()
                      if p.is_dir() and not p.is_symlink() and ENTRY_ID.fullmatch(p.name)
                      and (p / "manifest.json").is_file())

    def manifest(self, entry_id):
        folder = self.entry_dir(entry_id)
        if not (folder / "manifest.json").is_file():
            raise FirmwareError(f"no firmware {entry_id} in the library")
        return validate_manifest(json.loads((folder / "manifest.json").read_text()), entry_id)

    def entry(self, entry_id):
        folder = self.entry_dir(entry_id)
        try:
            data = json.loads((folder / "entry.json").read_text())
        except (OSError, ValueError):
            data = {}
        name = data.get("name") if isinstance(data.get("name"), str) else entry_id
        return {"id": entry_id, "name": name[:80], "created": data.get("created", "")}

    def summary(self, entry_id):
        manifest = self.manifest(entry_id)
        status = boot_status(manifest, self)
        return {**self.entry(entry_id), "kind": manifest["kind"], "version": manifest["update"]["trailer_version"],
                "app_sha256": manifest["app_sha256"], "status": status["status"], "profile": status["profile"],
                "reason": status["reason"]}

    def rename(self, entry_id, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise FirmwareError("names are 1-80 printable characters")
        self.manifest(entry_id)
        write_json(self.entry_dir(entry_id) / "entry.json", {**self.entry(entry_id), "name": name.strip()})

    def remove(self, entry_id):
        folder = self.entry_dir(entry_id)
        self.manifest(entry_id)
        if (folder / "data" / "session.lock").exists():
            probe = subprocess.run(["lsof", "-t", str(folder / "data" / "session.lock")], capture_output=True)
            if probe.stdout.strip():
                raise FirmwareError("firmware is in use by a running session")
        shutil.rmtree(folder)

    def select(self, entry_id):
        self.manifest(entry_id)
        write_json(self.path / "selected.json", {"id": entry_id})

    def selected(self):
        try:
            entry_id = json.loads((self.path / "selected.json").read_text()).get("id")
            self.manifest(entry_id)
            return entry_id
        except (OSError, ValueError, AttributeError):
            ids = self.ids()
            return ids[0] if len(ids) == 1 else None


def write_json(path, data):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def validate_manifest(manifest, entry_id=None):
    def need(condition, what):
        if not condition:
            raise FirmwareError(f"firmware manifest is invalid ({what})")
    need(isinstance(manifest, dict) and manifest.get("schema") == MANIFEST_SCHEMA, "schema")
    need(manifest.get("product") == PRODUCT, "product")
    need(entry_id is None or manifest.get("id") == entry_id, "id")
    need(manifest.get("kind") in ("stock", "mod"), "kind")
    need(isinstance(manifest.get("app_sha256"), str) and SHA256.fullmatch(manifest["app_sha256"]), "app_sha256")
    image = manifest.get("image")
    need(isinstance(image, dict) and SHA256.fullmatch(str(image.get("sha256"))) and isinstance(image.get("bytes"), int)
         and image["bytes"] > 0 and image["bytes"] % 4096 == 0 and image.get("file") == "firmware.sqfs", "image")
    update = manifest.get("update")
    need(isinstance(update, dict) and re.fullmatch(r"[0-9]\.[0-9]{2}", str(update.get("trailer_version")))
         and SHA256.fullmatch(str(update.get("sha256"))), "update")
    need(profiles.guest_path(manifest.get("app_path")), "app_path")
    need(isinstance(manifest.get("files"), dict), "files")
    if manifest["kind"] == "mod":
        mod = manifest.get("mod")
        need(isinstance(mod, dict) and isinstance(mod.get("verified"), bool), "mod")
        if mod["verified"]:
            need(SHA256.fullmatch(str(mod.get("base_app_sha256"))) and ENTRY_ID.fullmatch(str(mod.get("base_id"))),
                 "mod")
        else:
            need(isinstance(mod.get("reason"), str) and 0 < len(mod["reason"]) <= 300, "mod")
    return manifest


def mod_verified(manifest):
    return manifest["kind"] == "mod" and manifest["mod"].get("verified") is True


def base_app_sha256(manifest):
    """The application whose profile applies: a verified mod inherits its base's, anything else (stock update, mod
    that did not verify) is matched by its own application hash."""
    return manifest["mod"]["base_app_sha256"] if mod_verified(manifest) else manifest["app_sha256"]


def boot_status(manifest, library=None):
    """D2 policy (revised 2026-10-13): every correctly keyed import boots. It is "verified" when a verified profile
    matches its exact application (a mod that was rebuilt from a verified base inherits the base's profile), and
    "unverified" otherwise; an unverified boot uses the product's reference measurements and is marked as such."""
    if manifest["kind"] == "mod" and not mod_verified(manifest):
        return {"status": "unverified", "profile": None,
                "reason": "modified update that does not verify against its manifest: " + manifest["mod"]["reason"]}
    base = base_app_sha256(manifest)
    profile = profiles.for_app(base)
    if profile is None:
        return {"status": "unverified", "profile": None, "reason": "no compatibility profile for this firmware"}
    if profile["firmware"]["version"] != manifest["update"]["trailer_version"]:
        return {"status": "unverified", "profile": profile["id"],
                "reason": "update version differs from the profile's version"}
    if profile["status"] != "verified":
        return {"status": "unverified", "profile": profile["id"], "reason": "profile is a candidate, not verified"}
    if manifest["kind"] == "mod" and library is not None:
        try:
            if library.manifest(manifest["mod"]["base_id"])["app_sha256"] != base:
                raise FirmwareError("base changed")
        except FirmwareError:
            return {"status": "unverified", "profile": profile["id"], "reason": "mod base firmware is not in the library"}
    return {"status": "verified", "profile": profile["id"], "reason": ""}


def verify_image(library, entry_id):
    """Integrity: the stored image must equal the manifest's size and SHA-256."""
    manifest = library.manifest(entry_id)
    image = library.entry_dir(entry_id) / manifest["image"]["file"]
    if image.is_symlink() or not image.is_file() or image.stat().st_size != manifest["image"]["bytes"]:
        raise FirmwareError("firmware image is missing or its size changed")
    if sha256_file(image) != manifest["image"]["sha256"]:
        raise FirmwareError("firmware image does not match its manifest")
    return manifest


# ------------------------------------------------------------------------------------------------- compatibility

def check_report(manifest, library=None):
    """The compatibility report (M5): what the import found against the shim, the panel and the profiles."""
    status = boot_status(manifest, library)
    # The profile the boot really uses: guest-firmware.py asks profiles.active for the same base application.
    boot_source, booted = profiles.active(base_app_sha256(manifest))
    # Panel revisions are compared with the matched profile, else with the 1.60 panel model the shim implements.
    reference = profiles.for_app(base_app_sha256(manifest))
    if reference is None:
        try:
            reference = profiles.load(profiles.LEGACY_PROFILE)
        except ValueError:
            reference = None
    compat = manifest.get("compat") or {}
    revisions = (manifest.get("update") or {}).get("revisions") or {}
    panel = {name: revisions.get(f"{name}.rev") for name in ("subucom1", "subucom2")}
    expected_panel = reference["firmware"].get("panel_revisions", {}) if reference else {}
    items = [
        {"check": "key decrypts the update", "result": "pass"},
        {"check": "update layout (the product's layers)", "result": "pass"},
        {"check": "SP-16 application present", "result": "pass", "detail": manifest["app_path"]},
        {"check": "compatibility profile",
         "result": "pass" if status["status"] == "verified" else "warn",
         "detail": status["profile"] if status["status"] == "verified" else unverified_detail(status, boot_source, booted)},
        {"check": "device nodes known to the shim",
         "result": "pass" if not compat.get("devices_unknown") else "warn",
         "detail": {"found": compat.get("devices_found"), "unknown": compat.get("devices_unknown")}},
        {"check": "ALSA PCM names known to the shim", "result": "pass" if not compat.get("pcms_unknown") else "warn",
         "detail": {"found": compat.get("pcms_found"), "unknown": compat.get("pcms_unknown")}},
        {"check": "panel/driver ioctl requests", "result": "pass" if not compat.get("ioctls_unknown_candidates") else "warn",
         "detail": {"known": compat.get("ioctls_known_found"), "unknown": compat.get("ioctls_unknown_candidates")}},
        {"check": "panel MCU revisions vs the panel model",
         "result": "pass" if expected_panel and panel == expected_panel else "warn",
         "detail": {"update": panel, (reference or {}).get("id", "no reference profile"): expected_panel}},
        {"check": "pad calibration file format (markers of the matched or reference profile)",
         "result": "pass" if compat.get("calibration_markers") and all(compat["calibration_markers"].values())
         else "warn", "detail": compat.get("calibration_markers")},
    ]
    if manifest["kind"] == "mod":
        items.append({"check": "mod rebuilt from its verified base and recipes",
                      "result": "pass" if manifest["mod"].get("verified") else "warn",
                      "detail": {"base": manifest["mod"].get("base_id"), "recipes": manifest["mod"].get("recipes"),
                                 "reason": manifest["mod"].get("reason")}})
    return {"firmware": manifest["id"], "version": manifest["update"]["trailer_version"], "kind": manifest["kind"],
            "boots": True, "verified": status["status"] == "verified", "status": status["status"],
            "profile": status["profile"] or booted["id"], "boot": {"profile": booted["id"], "source": boot_source},
            "reason": status["reason"], "checks": items}


def unverified_detail(status, boot_source, booted):
    """The compatibility detail of an unverified boot: which profile it runs on and why it is not verified."""
    if boot_source == "reference":
        return f"unverified: runs on the reference measurements of {booted['id']} ({status['reason']})"
    return f"unverified: runs on profile {booted['id']} ({status['reason']})"


def candidate_profile(manifest, from_profile=None):
    """A candidate profile for an imported version (M5): identity from the manifest; runtime layout, panel maps and
    calibration copied from a reference profile as starting assumptions to verify. Never verified on creation."""
    product = profiles.product()
    reference = profiles.load(from_profile or product["reference_profile"])
    version = manifest["update"]["trailer_version"]
    revisions = manifest["update"].get("revisions") or {}
    profile = json.loads(json.dumps(reference))
    profile["id"] = f"sp16-{version}"
    profile["status"] = "candidate"
    profile["verification"] = {"method": "pending", "result": "pending",
                               "evidence": f"measurements copied from {reference['id']}; verify on this version"}
    profile["firmware"] = {"version": version, "upd_sha256": manifest["update"]["sha256"],
                           "app_path": manifest["app_path"], "app_sha256": manifest["app_sha256"],
                           "app_revision": revisions.get("app.rev"),
                           "panel_revisions": {name: revisions.get(f"{name}.rev") for name in ("subucom1", "subucom2")},
                           "loader_path": manifest.get("loader_path") or reference["firmware"].get("loader_path")}
    profile["derived_from"] = reference["id"]
    panel_revision = profile["firmware"]["panel_revisions"]
    if "identity" in reference:
        profile["identity"] = {**reference["identity"], "version": version, "manifest_sha256": None,
                               "image_sha256": manifest["image"]["sha256"] if "image" in manifest else None,
                               "panel_protocol_revision": panel_revision}
    compat = manifest.get("compat") or {}
    if compat.get("devices_found"):
        profile["device_nodes"] = compat["devices_found"]  # the static scan; HW-45 confirms at run time
    if "hardware" in reference:
        profile["hardware"] = {"absent": []}  # to be filled for this version
    return profiles.validate(profile)


def unverified_mod(mod_id, reason):
    """The manifest `mod` section of a modified update that could not be verified against its own manifest. It is
    still imported and boots as unverified (owner decision of 2026-10-13); the reason is recorded."""
    token = mod_id if isinstance(mod_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", mod_id) else "unverified"
    return {"id": token, "verified": False, "reason": str(reason)[:300]}


def refusal_report(update_meta, reason):
    """Report for an update the importer refused before it produced a firmware image."""
    return {"firmware": None, "version": (update_meta or {}).get("trailer_version"), "boots": False,
            "status": "refused", "reason": reason, "checks": [{"check": "import", "result": "fail", "detail": reason}]}


# ----------------------------------------------------------------------------------------- distribution audit (M3/M7)

def cpio_files(path, keep_small=False):
    """Yield (name, sha256, size, md5, contents-if-dpkg-md5sums) for every regular file in a gzip-compressed newc
    initramfs (stdlib only)."""
    import gzip
    with gzip.open(path, "rb") as stream:
        while True:
            header = stream.read(110)
            if len(header) < 110 or header[:6] not in (b"070701", b"070702"):
                raise FirmwareError("initramfs is not a newc cpio archive")
            fields = [int(header[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
            mode, size, name_size = fields[1], fields[6], fields[11]
            name = stream.read(name_size)[:-1].decode(errors="replace")
            stream.read((4 - (110 + name_size) % 4) % 4)
            if name == "TRAILER!!!":
                return
            digest, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
            keep = (name.startswith("var/lib/dpkg/info/") and name.endswith(".md5sums")) or name == "var/lib/dpkg/status"
            small = keep_small and size <= 4 << 20
            kept = []
            remaining = size
            while remaining:
                block = stream.read(min(1 << 20, remaining))
                if not block:
                    raise FirmwareError("initramfs is truncated")
                digest.update(block)
                md5.update(block)
                if keep or small:
                    kept.append(block)
                remaining -= len(block)
            stream.read((4 - size % 4) % 4)
            cpio_files.last_data = b"".join(kept) if small else None
            if stat.S_ISREG(mode) and size:
                yield name, digest.hexdigest(), size, md5.hexdigest(), b"".join(kept) if keep else None


def firmware_hashes(library):
    """Every non-empty file hash, image hash and update hash of every imported firmware."""
    hashes = {}
    for entry_id in library.ids():
        manifest = library.manifest(entry_id)
        for path, item in manifest["files"].items():
            if item.get("type") == "f" and item.get("size"):
                hashes.setdefault(item["sha256"], f"{entry_id}:{path}")
        hashes.setdefault(manifest["image"]["sha256"], f"{entry_id}:firmware.sqfs")
        hashes.setdefault(manifest["update"]["sha256"], f"{entry_id}:update")
    return hashes


def audit_build(bundle_paths, initrds, library, forbidden_files=None, forbidden_strings=None, forbidden_names=()):
    """Distribution scan: no file of the app bundle, kernel or base initramfs may carry a hash that appears in any
    imported firmware manifest (or in `forbidden_files`, e.g. vendor logos), and no file up to 4 MB may contain one of
    `forbidden_strings` (firmware facts not needed for verification). Returns a report; `clean` is False on any hit."""
    known = firmware_hashes(library)
    if not known:
        raise FirmwareError("import at least one firmware before auditing (nothing to compare against)")
    forbidden_files = forbidden_files or {}
    needles = [s.encode() for s in (forbidden_strings or {}) if s]
    matches, scanned, extra = [], 0, []

    def inspect(label, digest, data):
        if Path(label.split(":")[-1]).name in forbidden_names:
            extra.append({"file": label, "forbidden": "legacy vendor logo or icon file name"})
        if digest in forbidden_files:
            extra.append({"file": label, "forbidden": forbidden_files[digest]})
        if data is not None:
            for needle in needles:
                if needle in data:
                    extra.append({"file": label, "forbidden": "contains " + forbidden_strings[needle.decode()]})
    for root in bundle_paths:
        root = Path(root)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        for path in files:
            if path.stat().st_size == 0:
                continue
            scanned += 1
            digest = sha256_file(path)
            inspect(str(path), digest, path.read_bytes() if path.stat().st_size <= 4 << 20 else None)
            if digest in known:
                matches.append({"file": str(path), "firmware_file": known[digest]})
    debian = []
    for initrd in initrds:
        packaged, candidates = {}, []
        for name, digest, size, md5, md5sums in cpio_files(initrd, keep_small=bool(needles)):
            scanned += 1
            inspect(f"{Path(initrd).name}:{name}", digest, cpio_files.last_data)
            if md5sums is not None and name == "var/lib/dpkg/status":
                # Conffiles keep their packaged MD5 in the status database: " /etc/x <md5>".
                for line in md5sums.decode(errors="replace").splitlines():
                    parts = line.split()
                    if line.startswith(" /") and len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{32}", parts[1]):
                        packaged[parts[0].lstrip("/")] = parts[1]
            elif md5sums is not None:
                for line in md5sums.decode(errors="replace").splitlines():
                    checksum, _, member = line.partition("  ")
                    packaged[member.strip().lstrip("/")] = checksum.strip()
            if digest in known:
                candidates.append((name, md5, known[digest]))
        for name, md5, origin in candidates:
            item = {"file": f"{Path(initrd).name}:{name}", "firmware_file": origin}
            # The same upstream open-source file shipped by a Debian package (verified against that package's own
            # md5sums inside the base image) is third-party content, not firmware-derived.
            path = name.lstrip("./")
            aliases = [path] + [path[4:] for prefix in ("usr/lib/", "usr/bin/", "usr/sbin/") if path.startswith(prefix)]
            if any(packaged.get(alias) == md5 for alias in aliases):  # merged /usr: packages may list lib/..., bin/...
                debian.append(item)
            else:
                matches.append(item)
    return {"clean": not matches and not extra, "files_scanned": scanned, "firmware_hashes": len(known),
            "forbidden_hits": extra[:50],
            "firmware_ids": library.ids(), "matches": matches[:50], "match_count": len(matches),
            "debian_packaged_upstream_matches": len(debian),
            "debian_packaged_dirs": sorted({m["file"].split(":", 1)[1].rsplit("/", 2)[0] for m in debian})[:10]}


# ------------------------------------------------------------------------------------------------------- import VM

def import_arguments(qemu, kernel, initrd, update, output, sock, update_bytes):
    return [str(qemu), "-machine", "virt,gic-version=3", "-cpu", "host", "-accel", "hvf", "-smp", "4", "-m", "4096",
            "-nodefaults", "-display", "none", "-monitor", "none", "-serial", "stdio", "-nic", "none", "-no-reboot",
            "-kernel", str(kernel), "-initrd", str(initrd),
            "-append", f"console=ttyAMA0 rdinit=/init panic=-1 quiet sp16.mode=import sp16.upd_bytes={update_bytes}",
            "-device", "virtio-serial-device",
            "-chardev", f"socket,id=sp16imp,path={sock},server=on,wait=off",
            "-device", "virtserialport,chardev=sp16imp,name=sp16.import",
            "-drive", f"file={update},format=raw,if=none,id=sp16upd,readonly=on",
            "-device", "virtio-blk-device,drive=sp16upd,serial=sp16upd",
            "-drive", f"file={output},format=raw,if=none,id=sp16out",
            "-device", "virtio-blk-device,drive=sp16out,serial=sp16out"]


def run_import_vm(boot, update, update_bytes, key, output, console, timeout=900, progress=None):
    """Boot the import VM and exchange key and result over the private socket. Returns the guest's result dict."""
    sock_dir = Path(tempfile.mkdtemp(prefix="sp16-import-", dir="/tmp"))
    sock = sock_dir / "import.sock"
    args = import_arguments(boot["qemu"], boot["kernel"], boot["initrd"], update, output, sock, update_bytes)
    result = None
    try:
        with Path(console).open("wb") as log:
            os.chmod(console, 0o600)
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                deadline = time.monotonic() + 30
                while True:
                    try:
                        client.connect(str(sock))
                        break
                    except OSError:
                        if time.monotonic() > deadline or process.poll() is not None:
                            raise FirmwareError("import VM did not start") from None
                        time.sleep(.05)
                client.sendall(KEY_MAGIC + key)
                client.settimeout(timeout)
                reader = client.makefile("rb")
                deadline = time.monotonic() + timeout
                while result is None and time.monotonic() < deadline:
                    line = reader.readline(64 << 20)
                    if not line:
                        break
                    message = json.loads(line)
                    if "result" in message:
                        result = message
                    elif progress:
                        progress(message)
                client.close()
                process.wait(timeout=60)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
    if result is None:
        raise FirmwareError("import VM ended without a result (see the import console log)")
    return result


def import_update(library, boot, update_path, key, *, name=None, verify_mod=None, progress=None):
    """Import one update into the library. Returns (entry id, report). Raises FirmwareError with a report on refusal.
    `boot` is {"qemu", "kernel", "initrd", "initrd_sha256"} of a decoupled base build."""
    update_path = input_file(update_path, "update")
    meta = inspect_update(update_path)
    if meta["kind"] == "stock" and library.entry_dir(f"sp16-{meta['trailer_version']}-{meta['sha256'][:12]}").exists():
        error = FirmwareError(f"this update is already in the library as sp16-{meta['trailer_version']}-"
                              f"{meta['sha256'][:12]}")
        error.report = refusal_report(meta, str(error))
        raise error
    work = library.path / f".import-{secrets.token_hex(6)}"
    work.mkdir(mode=0o700)
    try:
        output = work / "firmware.sqfs"
        with output.open("xb") as handle:
            handle.truncate(OUTPUT_DISK_BYTES)
        os.chmod(output, 0o600)
        result = run_import_vm(boot, update_path, meta["bytes"], key, output, work / "import-console.txt",
                               progress=progress)
        if result.get("result") != "ok":
            raise FirmwareError(str(result.get("error", "import refused"))[:300])
        guest = result["manifest"]
        if guest.get("key_scan") != "absent":
            raise FirmwareError("the importer did not confirm that the key is absent from the firmware tree")
        if guest["update"]["trailer_version"] != meta["trailer_version"] or guest["update"]["crc32"] != meta["crc32"]:
            raise FirmwareError("guest and host disagree about the update")
        image = guest["image"]
        if not isinstance(image.get("bytes"), int) or not 0 < image["bytes"] <= OUTPUT_DISK_BYTES:
            raise FirmwareError("import produced no firmware image")
        with output.open("r+b") as handle:
            handle.truncate(image["bytes"])
        if sha256_file(output) != image["sha256"]:
            raise FirmwareError("firmware image hash differs between guest and host")
        kind = meta["kind"]
        version = meta["trailer_version"]
        entry_id = f"sp16-{version}-{meta['sha256'][:12]}" if kind == "stock" else None
        manifest = {
            "schema": MANIFEST_SCHEMA, "product": PRODUCT, "kind": kind,
            "update": {**guest["update"], "sha256": meta["sha256"], "file_name": update_path.name[:120]},
            "app_path": guest["app_path"], "app_sha256": guest["app_sha256"],
            "image": {"file": "firmware.sqfs", "bytes": image["bytes"], "sha256": image["sha256"],
                      "format": image.get("format", "squashfs")},
            "files": guest["files"], "compat": guest["compat"],
            "credential_or_special_entries_skipped": guest.get("credential_or_special_entries_skipped"),
            "key_scan": guest.get("key_scan"),
            "importer": {"version": IMPORTER_VERSION, "base_initrd_sha256": boot.get("initrd_sha256"),
                         "tools": guest.get("tools")},
            "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mod": None,
        }
        if kind == "mod":
            manifest["mod"] = (verify_mod(guest, meta, work) if verify_mod is not None else
                               unverified_mod(None, "no mod verification was available"))
            entry_id = f"sp16-{version}-mod-{manifest['mod']['id']}-{meta['sha256'][:12]}"
        manifest["id"] = entry_id
        validate_manifest(manifest, entry_id)
        target = library.entry_dir(entry_id)
        if target.exists():
            raise FirmwareError(f"this update is already in the library as {entry_id}")
        clone(update_path, work / "update.upd")
        write_json(work / "manifest.json", manifest)
        write_json(work / "entry.json", {"name": name or f"SP-16 {version}" + (" mod" if kind == "mod" else ""),
                                         "created": manifest["created"]})
        for path in work.iterdir():
            os.chmod(path, 0o700 if path.is_dir() else 0o600)  # owner-only whatever the caller's umask
        os.replace(work, target)
        return entry_id, check_report(manifest, library)
    except FirmwareError as error:
        error.report = refusal_report(meta, str(error))
        raise
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------------- CLI

def main(argv=None):
    """The app's import screen and library window call this (bundled in Resources/fwlib), so the GUI uses the same
    tested path as `native.py firmware`. One JSON document on stdout; progress lines as JSON on stderr."""
    import argparse
    import sys
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="SP-16 firmware library (decoupled emulator)")
    parser.add_argument("action", choices=("import", "list", "check", "rename", "remove", "select"))
    parser.add_argument("--library", required=True)
    parser.add_argument("--qemu")
    parser.add_argument("--kernel")
    parser.add_argument("--initrd")
    parser.add_argument("--update")
    parser.add_argument("--key-file")
    parser.add_argument("--gpl-archive", nargs="+")
    parser.add_argument("--id")
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    try:
        library = Library(args.library, create=args.action == "import")
        if args.action == "import":
            if not (args.qemu and args.kernel and args.initrd and args.update):
                raise FirmwareError("import needs --qemu, --kernel, --initrd and --update")
            boot = {"qemu": args.qemu, "kernel": args.kernel, "initrd": args.initrd,
                    "initrd_sha256": sha256_file(args.initrd)}
            key = load_key(args.key_file, args.gpl_archive)
            try:
                import fwpack
                entry_id, report = import_update(library, boot, args.update, key, name=args.name,
                                                 verify_mod=fwpack.mod_verifier(library, boot, key),
                                                 progress=lambda m: print(json.dumps(m), file=sys.stderr, flush=True))
            finally:
                key = None
            if library.selected() in (None, entry_id):  # the first import becomes the explicit selection
                library.select(entry_id)
            result = {"status": "firmware_imported", "id": entry_id, "report": report}
        elif args.action == "list":
            result = {"library": str(library.path), "selected": library.selected(),
                      "firmware": [library.summary(i) for i in library.ids()]}
        else:
            if not args.id:
                raise FirmwareError(f"{args.action} needs --id")
            if args.action == "check":
                result = check_report(verify_image(library, args.id), library)
            elif args.action == "rename":
                library.rename(args.id, args.name)
                result = library.summary(args.id)
            elif args.action == "remove":
                library.remove(args.id)
                result = {"status": "firmware_removed", "id": args.id}
            else:
                library.select(args.id)
                result = {"status": "firmware_selected", "id": args.id}
    except FirmwareError as error:
        print(json.dumps({"status": "firmware_refused", "error": str(error),
                          "report": getattr(error, "report", refusal_report(None, str(error)))}))
        return 1
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
