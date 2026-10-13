"""Developer workflow (docs/architecture.md M6): overlay planning/apply in the guest, fwdev preflight and packaging,
with synthetic trees only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import firmware
import fwdev
import native
import profiles
import recipes

spec = importlib.util.spec_from_file_location("guest_firmware_dev", TOOLS / "guest-firmware.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)

APP = "/root/pdj/EP085_Main_R"
BASE = b"\x7fELF" + b"\0" * 60 + b"xxUTILITY\0yy"
PATCHED = BASE.replace(b"UTILITY\0", b"MOD LAB\0")
ARM_ELF = b"\x7fELF\x01\x01\x01" + b"\0" * 11 + (40).to_bytes(2, "little") + b"\0" * 40


def recipe(base=BASE, offset=BASE.index(b"UTILITY"), result=PATCHED, profile=profiles.LEGACY_PROFILE):
    spec = {"base_sha256": hashlib.sha256(base).hexdigest(),
            "patches": [{"offset": offset, "original_hex": b"UTILITY\0".hex(),
                         "replacement_hex": b"MOD LAB\0".hex(), "expected_count": 1}]}
    if result is not None:
        spec["result_sha256"] = hashlib.sha256(result).hexdigest()
    return {"schema": 1, "id": "label", "base_profile": profile, "files": {APP: spec}}


class GuestOverlayTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.lower, self.root, self.share = self.dir / "lower", self.dir / "root", self.dir / "share"
        for tree in (self.lower, self.root):
            (tree / "root/pdj").mkdir(parents=True)
            (tree / "root/pdj/EP085_Main_R").write_bytes(BASE)
            (tree / "etc").mkdir()
            (tree / "etc/config").write_bytes(b"stock config\n")
        for sub in ("recipes", "files/etc", "preload"):
            (self.share / sub).mkdir(parents=True)
        self.state = self.dir / "state.json"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def apply(self):
        return guest.dev_apply({}, share=self.share, root=self.root, lower=self.lower, state=self.state,
                               profile_id=profiles.LEGACY_PROFILE)

    def test_recipe_files_and_preload_apply_and_restore(self):
        (self.share / "recipes/label.json").write_text(json.dumps(recipe()))
        (self.share / "files/etc/config").write_bytes(b"dev config\n")
        (self.share / "files/etc/new-file").write_bytes(b"added\n")
        (self.share / "preload/libmod.so").write_bytes(ARM_ELF)
        self.apply()
        self.assertEqual((self.root / "root/pdj/EP085_Main_R").read_bytes(), PATCHED)
        self.assertEqual((self.root / "etc/config").read_bytes(), b"dev config\n")
        self.assertTrue((self.root / "etc/new-file").exists())
        self.assertEqual((self.root / "opt/sp16/dev/libmod.so").read_bytes(), ARM_ELF)
        self.assertEqual((self.lower / "root/pdj/EP085_Main_R").read_bytes(), BASE)  # the image is never written
        for path in self.share.rglob("*"):
            if path.is_file():
                path.unlink()
        self.apply()  # an emptied overlay restores the firmware's own content and removes added files
        self.assertEqual((self.root / "root/pdj/EP085_Main_R").read_bytes(), BASE)
        self.assertEqual((self.root / "etc/config").read_bytes(), b"stock config\n")
        self.assertFalse((self.root / "etc/new-file").exists())

    def test_failures_leave_the_firmware_unchanged(self):
        (self.share / "files/etc/config").write_bytes(b"dev config\n")
        bad = recipe()
        bad["files"][APP]["patches"][0]["original_hex"] = b"UTILITX\0".hex()
        (self.share / "recipes/label.json").write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "original bytes are absent"):
            self.apply()
        self.assertEqual((self.root / "etc/config").read_bytes(), b"stock config\n")
        for broken in (recipe(profile="sp16-9.99"), recipe(base=b"other")):
            (self.share / "recipes/label.json").write_text(json.dumps(broken))
            with self.assertRaises(ValueError):
                self.apply()
        (self.share / "recipes/label.json").unlink()
        (self.share / "preload/libbad.so").write_bytes(b"\x7fELF\x02" + b"\0" * 40)
        with self.assertRaisesRegex(ValueError, "32-bit ARM"):
            self.apply()

    def test_recipe_can_patch_an_overlay_file(self):
        (self.share / "files/root/pdj").mkdir(parents=True)
        (self.share / "files/root/pdj/EP085_Main_R").write_bytes(BASE)
        (self.share / "recipes/label.json").write_text(json.dumps(recipe()))
        plan, applied, preload = guest.dev_plan(self.share, self.lower, profiles.LEGACY_PROFILE)
        self.assertEqual(plan[APP][0], PATCHED)
        self.assertEqual(applied, ["label"])


class FwdevHostTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        os.chmod(self.dir, 0o700)
        self.library = firmware.Library(self.dir / "lib", create=True)
        entry = self.library.entry_dir("sp16-1.60-aaaaaaaaaaaa")
        entry.mkdir(mode=0o700)
        self.manifest = {"schema": 1, "product": "sp16", "id": entry.name, "kind": "stock",
                         "app_path": APP, "app_sha256": profiles.legacy()["firmware"]["app_sha256"], "mod": None,
                         "image": {"file": "firmware.sqfs", "bytes": 4096, "sha256": "b" * 64},
                         "update": {"trailer_version": "1.60", "sha256": "c" * 64},
                         "files": {APP: {"type": "f", "size": len(BASE), "sha256": hashlib.sha256(BASE).hexdigest()},
                                   "/etc/config": {"type": "f", "size": 13,
                                                   "sha256": hashlib.sha256(b"stock config\n").hexdigest()}}}
        (entry / "manifest.json").write_text(json.dumps(self.manifest))
        self.mod = self.dir / "mod"
        fwdev.new(self.mod, "label-mod")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_new_scaffold(self):
        self.assertEqual(json.loads((self.mod / "mod.json").read_text())["base_profile"], profiles.LEGACY_PROFILE)
        self.assertTrue((self.mod / "src/label-mod.c").read_text().startswith("/* Example preload"))
        with self.assertRaises(fwdev.ModError):
            fwdev.new(self.mod, "again")
        with self.assertRaises(fwdev.ModError):
            fwdev.new(self.dir / "x", "Bad_Id")
        fwdev.new(self.dir / "from-recipe", "ul", "utility-label")
        self.assertTrue((self.dir / "from-recipe/recipes/utility-label.json").is_file())

    def test_check_catches_wrong_base_missing_path_oem_copies_and_bad_preloads(self):
        (self.mod / "recipes/label.json").write_text(json.dumps(recipe()))
        self.assertTrue(fwdev.check(self.mod, self.library, "sp16-1.60-aaaaaaaaaaaa")["ok"])
        (self.mod / "recipes/label.json").write_text(json.dumps(recipe(base=b"other")))
        problems = fwdev.check(self.mod, self.library, "sp16-1.60-aaaaaaaaaaaa")["problems"]
        self.assertTrue(any("wrong base" in p for p in problems))
        missing = recipe()
        missing["files"]["/root/pdj/missing"] = missing["files"].pop(APP)
        (self.mod / "recipes/label.json").write_text(json.dumps(missing))
        self.assertTrue(any("not a file" in p for p in fwdev.check(self.mod, self.library,
                                                                     "sp16-1.60-aaaaaaaaaaaa")["problems"]))
        (self.mod / "recipes/label.json").unlink()
        (self.mod / "files/etc").mkdir(parents=True)
        (self.mod / "files/etc/config").write_bytes(b"stock config\n")
        (self.mod / "preload/x.so").write_bytes(b"not elf")
        problems = fwdev.check(self.mod, self.library, "sp16-1.60-aaaaaaaaaaaa")["problems"]
        self.assertTrue(any("unmodified firmware file" in p for p in problems))
        self.assertTrue(any("ARM ELF" in p for p in problems))

    def test_package_is_deterministic_and_carries_no_oem_file(self):
        (self.mod / "recipes/label.json").write_text(json.dumps(recipe()))
        (self.mod / "preload/label-mod.so").write_bytes(ARM_ELF)
        first = fwdev.package(self.mod, self.library, "sp16-1.60-aaaaaaaaaaaa", self.dir / "a.tar.gz")
        second = fwdev.package(self.mod, self.library, "sp16-1.60-aaaaaaaaaaaa", self.dir / "b.tar.gz")
        self.assertEqual(first["sha256"], second["sha256"])
        with tarfile.open(self.dir / "a.tar.gz") as archive:
            names = archive.getnames()
            package = json.loads(archive.extractfile("label-mod/package.json").read())
        self.assertIn("label-mod/recipes/label.json", names)
        self.assertEqual(package["base_app_sha256"], self.manifest["app_sha256"])
        oem = {item["sha256"] for item in self.manifest["files"].values()}
        self.assertFalse(oem & set(package["files"].values()))
        (self.mod / "files/etc").mkdir(parents=True)
        (self.mod / "files/etc/config").write_bytes(b"stock config\n")
        with self.assertRaisesRegex(fwdev.ModError, "preflight"):
            fwdev.package(self.mod, self.library, "sp16-1.60-aaaaaaaaaaaa", self.dir / "c.tar.gz")

    def test_sdk_flags_match_the_shim(self):
        source = (TOOLS / "fwdev.py").read_text()
        for flag in ('"-march=armv7-a"', '"-mfpu=vfpv3"', '"-mfloat-abi=softfp"', '"-nostdlib"', '"-l:libdl.so.2"'):
            self.assertIn(flag, source)
        self.assertNotIn("libc.so.6", source)  # bind to the firmware's own glibc at run time

    def test_dev_overlay_requires_owned_plain_folder(self):
        self.assertEqual(native.dev_overlay_path(self.mod), str(self.mod))
        with self.assertRaises(ValueError):
            native.dev_overlay_path(self.dir / "missing")
        with self.assertRaises(ValueError):
            native.dev_overlay_path(self.dir / "a,b")
        os.symlink(self.mod, self.dir / "link")
        with self.assertRaises(ValueError):
            native.dev_overlay_path(self.dir / "link")

    def test_supervisor_and_restart_are_wired(self):
        init = (TOOLS / "guest-init.sh").read_text()
        self.assertIn("app-restart", init)
        self.assertIn("/run/sp16-app.pid", init)
        self.assertIn("rm -f /tmp/sp16-root/dev/sp16-panel-input", init)
        self.assertIn("sp16_devlayer", init)
        swift = (TOOLS / "native-window.swift").read_text()
        for op in ('case "app-restart":', 'case "firmware":', "virtio-9p-device,fsdev=sp16dev", "readonly=on"):
            self.assertIn(op, swift)
        bridge = (TOOLS / "guest-bridge.py").read_text()
        self.assertIn('"[sp16-mod]"', bridge)


if __name__ == "__main__":
    unittest.main()
