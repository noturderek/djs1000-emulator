#!/usr/bin/env python3
"""Assemble a private SP-16 1.60 userspace without executing OEM programs."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

import profiles

REPO = Path(__file__).resolve().parents[1]
# The legacy builds' pin, read from the verified 1.60 profile (docs/architecture.md, M1).
LEGACY_PROFILE = profiles.legacy()
EXPECTED_APP = LEGACY_PROFILE["firmware"]["app_sha256"]
ROOTFS_LAYERS = tuple(f"rootfs{number}.tar.gz" for number in range(1, 5))
SQUASHFS_LAYERS = (("lib", "usr/lib"), ("bin", "usr/bin"), ("share", "usr/share"))
SOURCE_IMAGES = (*ROOTFS_LAYERS, "pdj.tar.gz", "lib.sfs", "bin.sfs", "share.sfs")


def private_path(value, *, fresh=False):
    path = Path(os.path.abspath(value))
    if not path.is_relative_to(REPO) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("workspace/output must be inside the repository without symlink parents")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(path)], cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if ignored.returncode:
        raise ValueError("workspace/output must be Git-ignored")
    if fresh and (path.exists() or not path.parent.is_dir()):
        raise ValueError("output must be absent with an existing private parent")
    permission_path = path.parent if fresh else path
    if permission_path.stat().st_mode & 0o077:
        raise ValueError("workspace/output parent must have owner-only permissions")
    return path


def member_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("archive member escapes its root")
    return tuple(p for p in path.parts if p != ".")


def secret_name(parts):
    return any(p.endswith(".key") or p in {"shadow", "shadow-", "passwd", ".ssh"} for p in parts)


def directory(root, parts):
    """Never follow an archive-created symlink while writing on the host."""
    path = root
    for part in parts:
        path /= part
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("archive directory conflicts with an existing non-directory")
        path.mkdir(exist_ok=True, mode=0o700)
    return path


def destination(root, parts):
    parent = directory(root, parts[:-1])
    path = parent / parts[-1]
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise ValueError("archive file conflicts with an existing directory")
    return path


def unpack_layer(archive, root, prefix=()):
    skipped = 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            parts = prefix + member_path(member.name)
            if not parts:
                continue
            if secret_name(parts) or parts[0] in {"dev", "proc", "sys"}:
                skipped += 1
                continue
            if member.isdir():
                directory(root, parts)
            elif member.issym():
                # Link strings belong to the eventual guest, never the host.
                if "\x00" in member.linkname:
                    raise ValueError("invalid guest symlink")
                os.symlink(member.linkname, destination(root, parts))
            elif member.isfile():
                with source.extractfile(member) as stream, destination(root, parts).open("xb") as target:
                    shutil.copyfileobj(stream, target)
                (root.joinpath(*parts)).chmod(member.mode & 0o755)
            else:
                # Special nodes and hardlinks are not silently materialized.
                if member.islnk():
                    raise ValueError("hardlinked archive member needs explicit handling")
                skipped += 1
    return skipped


def guest_file(root, guest_path):
    """Resolve absolute/relative guest symlinks without escaping to host files."""
    pending = list(PurePosixPath(guest_path).parts)
    current = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in {"/", "."}:
            continue
        if part == "..":
            if not current:
                raise ValueError("guest link escapes root")
            current.pop()
            continue
        path = root.joinpath(*current, part)
        if path.is_symlink():
            links += 1
            if links > 40:
                raise ValueError("guest symlink loop")
            target = PurePosixPath(os.readlink(path))
            if target.is_absolute():
                current = []
            pending = list(target.parts) + pending
        else:
            current.append(part)
    return root.joinpath(*current)


LEGACY_LAYOUT = {
    "tar_layers": [*({"file": name, "root": "/"} for name in ROOTFS_LAYERS), {"file": "pdj.tar.gz", "root": "/root"}],
    "squashfs_layers": [{"file": f"{name}.sfs", "root": "/" + mount} for name, mount in SQUASHFS_LAYERS],
    "mount_points": ["/dev", "/proc", "/sys", "/tmp", "/root/data", "/mnt/emmc"],
}


def assemble(images, root, extractor, layout=None):
    """Unpack an update's layers into the fresh directory `root`, drop credentials and keys, and create the mount
    points. `layout` is the product descriptor's update section (the decoupled importer); the legacy prepare uses the
    1.60 layout it has always used. Returns the number of skipped credential or special entries."""
    images = Path(images)
    layout = layout or LEGACY_LAYOUT
    skipped = 0
    for layer in layout["tar_layers"]:
        skipped += unpack_layer(images / layer["file"], root, tuple(PurePosixPath(layer["root"]).parts[1:]))
    for layer in layout.get("squashfs_layers", []):
        mount = PurePosixPath(layer["root"]).relative_to("/")
        directory(root, tuple(mount.parts))
        result = subprocess.run(
            [extractor, "-no-progress", "-f", "-d", str(root / mount), str(images / layer["file"])],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise ValueError(f"SquashFS extraction failed for {layer['file'].rsplit('.', 1)[0]}")
    # Remove credential/key files if a filesystem image also supplied them.
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + files:
            if secret_name((name,)):
                path = Path(parent) / name
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                if name in dirs:
                    dirs.remove(name)
                skipped += 1
    for name in layout.get("mount_points", []):
        directory(root, tuple(PurePosixPath(name).parts[1:]))
    return skipped


def prepare(workspace, output):
    work = private_path(workspace)
    root = private_path(output, fresh=True)
    images = work / "update-payload/images"
    extractor = shutil.which("unsquashfs")
    if not extractor:
        raise ValueError("unsquashfs is required")
    root.mkdir(mode=0o700)
    try:
        skipped = assemble(images, root, extractor)
        app = guest_file(root, "/root/pdj/EP085_Main_R")
        digest = hashlib.sha256(app.read_bytes()).hexdigest()
        if digest != EXPECTED_APP:
            raise ValueError("sampler executable does not match validated OEM 1.60")
        loader = guest_file(root, "/lib/ld-linux.so.3")
        if not loader.is_file() or loader.read_bytes()[:4] != b"\x7fELF":
            raise ValueError("stock ARM ELF loader is missing")
        manifest = {
            "status": "USERSPACE_ASSEMBLED_NOT_BOOTED", "firmware": LEGACY_PROFILE["firmware"]["version"],
            "app_revision": LEGACY_PROFILE["firmware"]["app_revision"], "app_sha256": digest,
            "loader_guest_path": "/lib/ld-linux.so.3",
            "credential_or_special_entries_skipped": skipped,
            "source_images": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in images.iterdir() if p.name in SOURCE_IMAGES},
        }
        (root / "sp16-emulator-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(root)
        raise
    print(json.dumps({"status": manifest["status"], "output": str(root), "firmware": manifest["firmware"]}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    prepare(args.workspace, args.output)


if __name__ == "__main__":
    main()
