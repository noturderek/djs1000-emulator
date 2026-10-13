"""Repacked updates for the emulator only (docs/architecture.md, M6 and design decision D3).

package_update: a verified stock base from the library + a mod's recipes -> the guest rebuilds the affected layer
deterministically, embeds sp16-mod.json (base identity, full recipes, result hashes), builds the ISO with fixed dates,
re-encrypts it with the user's key; the host appends the trailer SP16MODLAB-<base version>, NUL and the CRC-32. The
marker can never be mistaken for a stock LIVESAMPLER update. The result stays in a private path; this project never
distributes or flashes it.

verify_mod: on import of such an update, rebuild it from the verified base and the recipes it declares and compare the
whole file. An identical rebuild is verified and inherits the base's profile; any other correctly keyed update is still
imported, as unverified with the reason recorded (D2 in docs/architecture.md, which replaced the earlier refusal).
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import zlib

import firmware
import profiles
import recipes

MOD_MARKER = b"SP16MODLAB-"
OUTPUT_BYTES = 1 << 30


def package_arguments(boot, base_update, output, sock, update_bytes):
    args = firmware.import_arguments(boot["qemu"], boot["kernel"], boot["initrd"], base_update, output, sock,
                                     update_bytes)
    index = args.index("-append") + 1
    args[index] = args[index].replace("sp16.mode=import", "sp16.mode=package")
    return args


def run_package_vm(boot, base_update, update_bytes, key, request, output, console, timeout=900):
    sock_dir = Path(tempfile.mkdtemp(prefix="sp16-pack-", dir="/tmp"))
    sock = sock_dir / "import.sock"
    result = None
    try:
        with Path(console).open("wb") as log:
            os.chmod(console, 0o600)
            process = subprocess.Popen(package_arguments(boot, base_update, output, sock, update_bytes),
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                deadline = time.monotonic() + 30
                while True:
                    try:
                        client.connect(str(sock))
                        break
                    except OSError:
                        if time.monotonic() > deadline or process.poll() is not None:
                            raise firmware.FirmwareError("package VM did not start") from None
                        time.sleep(.05)
                client.sendall(firmware.KEY_MAGIC + key + (json.dumps(request) + "\n").encode())
                client.settimeout(timeout)
                reader = client.makefile("rb")
                while result is None:
                    line = reader.readline(64 << 20)
                    if not line:
                        break
                    message = json.loads(line)
                    if "result" in message:
                        result = message
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
        raise firmware.FirmwareError("package VM ended without a result")
    if result.get("result") != "ok":
        raise firmware.FirmwareError(str(result.get("error", "packaging refused"))[:300])
    return result


def build_update(library, boot, base_id, mod_id, recipe_list, key, target, work):
    """Build the repacked update file `target` (fresh) from a verified stock base. Returns its metadata."""
    base = library.manifest(base_id)
    status = firmware.boot_status(base, library)
    if base["kind"] != "stock" or status["status"] != "verified":
        raise firmware.FirmwareError("the base must be a stock firmware with a verified profile")
    for recipe in recipe_list:
        recipes.validate(recipe)
        if recipe["base_profile"] != status["profile"]:
            raise firmware.FirmwareError(f"recipe {recipe['id']} targets {recipe['base_profile']}, "
                                         f"base has {status['profile']}")
    base_update = library.entry_dir(base_id) / "update.upd"
    meta = firmware.inspect_update(base_update)
    if meta["sha256"] != base["update"]["sha256"]:
        raise firmware.FirmwareError("the stored base update does not match its manifest")
    output = Path(work) / "payload.bin"
    with output.open("xb") as handle:
        handle.truncate(OUTPUT_BYTES)
    request = {"id": mod_id, "recipes": recipe_list,
               "base": {"upd_sha256": base["update"]["sha256"], "app_sha256": base["app_sha256"]}}
    result = run_package_vm(boot, base_update, meta["bytes"], key, request, output, Path(work) / "package-console.txt")
    size = result["payload_bytes"]
    if not 0 < size <= OUTPUT_BYTES or size % 512:
        raise firmware.FirmwareError("package produced an invalid payload size")
    with output.open("r+b") as handle:
        handle.truncate(size)
    crc = 0
    with output.open("rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            crc = zlib.crc32(block, crc)
    version = meta["trailer_version"].encode()
    with output.open("ab") as handle:
        handle.write(MOD_MARKER + version + b"\0" + crc.to_bytes(4, "little"))
    os.replace(output, target)
    os.chmod(target, 0o600)
    return {"path": str(target), "sha256": firmware.sha256_file(target), "bytes": Path(target).stat().st_size,
            "trailer": (MOD_MARKER + version).decode(), "crc32": f"{crc:08x}", "base_id": base_id,
            "mod_manifest": result["mod_manifest"]}


def package_update(library, boot, base_id, mod_folder, key, out):
    """fwdev package --upd: recipes of a mod folder -> repacked update at `out` (a fresh file in a private folder)."""
    import fwdev
    folder, meta = fwdev.load_mod(mod_folder)
    report = fwdev.check(folder, library, base_id)
    if not report["ok"]:
        raise firmware.FirmwareError("preflight failed: " + "; ".join(report["problems"]))
    if report["files"] or report["preload"]:
        raise firmware.FirmwareError("repacked updates carry recipes only (files/ and preload/ stay developer overlays)")
    recipe_list = [recipe for _, recipe in fwdev.mod_recipes(folder)]
    if not recipe_list:
        raise firmware.FirmwareError("the mod has no recipes")
    out = Path(os.path.abspath(out))
    firmware.private_dir(out.parent)
    if out.exists():
        raise firmware.FirmwareError("output exists; give a fresh path")
    work = Path(tempfile.mkdtemp(prefix=".sp16-pack-", dir=out.parent))
    try:
        result = build_update(library, boot, base_id, meta["id"], recipe_list, key, out, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"status": "mod_update_built", **{k: v for k, v in result.items() if k != "mod_manifest"},
            "recipes": [r["id"] for r in recipe_list], "note": "emulator only; never distribute or flash"}


class Unverifiable(firmware.FirmwareError):
    """A modified update whose own manifest cannot be confirmed. Since D2 in docs/architecture.md such an update
    is still imported (as unverified, reason recorded); infrastructure failures stay plain FirmwareErrors."""


def mod_verifier(library, boot, key):
    """The verify_mod callback for firmware.import_update: rebuild the supplied mod update from its declared base
    and recipes and compare byte for byte. A match is verified and inherits the base profile; anything the manifest
    cannot prove is returned as an unverified mod section with the reason."""
    def check(guest, meta, work):
        mod = guest.get("mod")
        if not isinstance(mod, dict) or mod.get("schema") != 1 or mod.get("kind") != "sp16-mod-update":
            raise Unverifiable("modified update without a valid sp16-mod.json")
        if not isinstance(mod.get("id"), str) or not recipes.ID_PATTERN.fullmatch(mod["id"]):
            raise Unverifiable("modified update has an invalid mod id")
        base_sha = (mod.get("base") or {}).get("upd_sha256")
        base_id = next((i for i in library.ids() if library.manifest(i)["kind"] == "stock"
                        and library.manifest(i)["update"]["sha256"] == base_sha), None)
        if base_id is None:
            raise Unverifiable("the mod's base update is not in the library")
        base = library.manifest(base_id)
        if mod["base"].get("version") != meta["trailer_version"] or mod["base"].get("app_sha256") != base["app_sha256"]:
            raise Unverifiable("the mod's declared base does not match the library base")
        try:
            recipe_list = [recipes.validate(r) for r in mod.get("recipes") or []]
        except ValueError as error:
            raise Unverifiable(str(error)) from None
        if not recipe_list:
            raise Unverifiable("the mod declares no recipes")
        expected = (mod.get("results") or {}).get(base["app_path"])
        if expected and expected != guest["app_sha256"]:
            raise Unverifiable("the mod's application differs from its declared recipe result")
        status = firmware.boot_status(base, library)
        if status["status"] != "verified":
            raise Unverifiable("the mod's base firmware has no verified profile, so it cannot be rebuilt: "
                               + status["reason"])
        if any(r["base_profile"] != status["profile"] for r in recipe_list):
            raise Unverifiable("a declared recipe targets a different profile than the base")
        rebuilt = Path(work) / "rebuilt.upd"
        rebuild_work = Path(work) / "rebuild"
        rebuild_work.mkdir(mode=0o700)
        try:
            result = build_update(library, boot, base_id, mod["id"], recipe_list, key, rebuilt, rebuild_work)
        finally:
            shutil.rmtree(rebuild_work, ignore_errors=True)
        if result["sha256"] != meta["sha256"]:
            rebuilt.unlink(missing_ok=True)
            raise Unverifiable("the modified update is not the rebuild of its base and declared recipes (content differs)")
        rebuilt.unlink()
        return {"id": mod["id"], "base_id": base_id, "base_app_sha256": base["app_sha256"],
                "base_upd_sha256": base_sha, "verified": True, "rebuild_sha256": result["sha256"],
                "recipes": {r["id"]: recipes.recipe_sha256(r) for r in recipe_list},
                "profile": status["profile"]}

    def verify(guest, meta, work):
        try:
            return check(guest, meta, work)
        except Unverifiable as error:
            declared = guest.get("mod")
            return firmware.unverified_mod(declared.get("id") if isinstance(declared, dict) else None, str(error))
    return verify
