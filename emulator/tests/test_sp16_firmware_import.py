"""Decoupled firmware import (docs/architecture.md M2-M5) with synthetic updates, keys and archives only."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import firmware
import native
import profiles

spec = importlib.util.spec_from_file_location("guest_firmware", TOOLS / "guest-firmware.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)

SYNTHETIC_KEY_LINE = b"synthetic-test-key-0123456789abcdefXYZ"
LEGACY_APP = profiles.legacy()["firmware"]["app_sha256"]
LEGACY_MARKERS = profiles.legacy()["calibration"]["markers"]


def make_update(path, version=b"1.60", sectors=8, marker=b"LIVESAMPLER", payload=None):
    payload = payload if payload is not None else bytes(range(256)) * (2 * sectors)
    trailer = marker + version + b"\0"
    path.write_bytes(payload + trailer + struct.pack("<I", zlib.crc32(payload)))
    return path


def tar_gz(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def gpl_archive(key_line=SYNTHETIC_KEY_LINE):
    initramfs = tar_gz([("initramfs/init", b"#!/bin/sh\n"), ("initramfs/" + firmware.KEY_MEMBER, key_line + b"\n")])
    return tar_gz([("gpl_source/BUILD/readme", b"x" * 5000), ("gpl_source/initramfs.tar.gz", initramfs)])


def private_tmp():
    folder = Path(tempfile.mkdtemp(prefix="sp16-fw-test-")).resolve()  # /var is a symlink on macOS
    os.chmod(folder, 0o700)
    return folder


def guest_manifest(app_sha=LEGACY_APP, version="1.60", crc="00000000", image=b"hsqs" + bytes(4092)):
    return {"update": {"trailer_version": version, "kind": "stock", "crc32": crc, "bytes": 0,
                       "revisions": {"subucom1.rev": "Rev.5187", "subucom2.rev": "Rev.3671"}},
            "app_path": "/root/pdj/EP085_Main_R", "app_sha256": app_sha, "key_scan": "absent",
            "credential_or_special_entries_skipped": 11,
            "image": {"bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(), "format": "squashfs-gzip"},
            "compat": {"devices_found": list(guest.SHIM_DEVICES), "devices_unknown": [], "pcms_found": [],
                       "pcms_unknown": [], "ioctls_known_found": [], "ioctls_unknown_candidates": [],
                       "calibration_markers": {m: True for m in LEGACY_MARKERS}},
            "files": {"/root/pdj/EP085_Main_R": {"type": "f", "mode": 0o755, "size": 1, "sha256": app_sha}},
            "mod": None, "tools": {"mksquashfs": "test"}}


class UpdateInspectionTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_tmp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def test_stock_and_mod_markers(self):
        meta = firmware.inspect_update(make_update(self.dir / "a.UPD"))
        self.assertEqual((meta["kind"], meta["trailer_version"], meta["payload_bytes"]), ("stock", "1.60", 8 * 512))
        mod = firmware.inspect_update(make_update(self.dir / "b.UPD", marker=b"SP16MODLAB-"))
        self.assertEqual((mod["kind"], mod["trailer_version"]), ("mod", "1.60"))
        self.assertEqual(meta["sha256"], hashlib.sha256((self.dir / "a.UPD").read_bytes()).hexdigest())

    def test_refusals(self):
        path = make_update(self.dir / "c.UPD")
        data = bytearray(path.read_bytes())
        data[100] ^= 1
        (self.dir / "crc.UPD").write_bytes(bytes(data))
        (self.dir / "short.UPD").write_bytes(path.read_bytes()[:-700])
        (self.dir / "tiny.UPD").write_bytes(b"x" * 20)
        make_update(self.dir / "djs.UPD", marker=b"OTHERSAMPLE")
        make_update(self.dir / "ver.UPD", version=b"160 ")
        for name, reason in (("crc.UPD", "CRC"), ("short.UPD", "sector-aligned"), ("tiny.UPD", "too small"),
                             ("djs.UPD", "marker"), ("ver.UPD", "marker"), ("missing.UPD", "cannot read")):
            with self.assertRaisesRegex(firmware.FirmwareError, reason):
                firmware.inspect_update(self.dir / name)

    def test_guest_trailer_parser_matches_host(self):
        self.assertEqual(guest.parse_trailer(b"LIVESAMPLER1.22\0" + b"abcd"), ("stock", "1.22"))
        self.assertEqual(guest.parse_trailer(b"SP16MODLAB-1.60\0" + b"abcd"), ("mod", "1.60"))
        with self.assertRaises(ValueError):
            guest.parse_trailer(b"LIVESAMPLERX.60\0abcd")

    def test_guest_pvd_check(self):
        pvd = bytearray(2048)
        pvd[:7] = b"\x01CD001\x01"
        struct.pack_into("<I", pvd, 80, 10)
        struct.pack_into(">I", pvd, 84, 10)
        struct.pack_into("<H", pvd, 128, 2048)
        struct.pack_into(">H", pvd, 130, 2048)
        guest.verify_pvd(bytes(pvd), 10 * 2048)
        with self.assertRaisesRegex(ValueError, "key does not decrypt"):
            guest.verify_pvd(bytes(2048), 10 * 2048)
        with self.assertRaisesRegex(ValueError, "geometry"):
            guest.verify_pvd(bytes(pvd), 9 * 2048)


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_tmp()
        self.expected = SYNTHETIC_KEY_LINE[:31].ljust(32, b"\0")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def test_key_file_follows_the_key_file_rules(self):
        (self.dir / "k").write_bytes(SYNTHETIC_KEY_LINE + b"\nsecond line\n")
        self.assertEqual(firmware.load_key(self.dir / "k"), self.expected)
        (self.dir / "n").write_bytes(b"short\0ignored\n")
        self.assertEqual(firmware.load_key(self.dir / "n"), b"short".ljust(32, b"\0"))
        (self.dir / "e").write_bytes(b"\n")
        with self.assertRaisesRegex(firmware.FirmwareError, "empty"):
            firmware.load_key(self.dir / "e")
        with self.assertRaises(firmware.FirmwareError):
            firmware.load_key(self.dir / "k", [self.dir / "k"])
        with self.assertRaises(firmware.FirmwareError):
            firmware.load_key()

    def test_gpl_archive_whole_split_and_zipped(self):
        archive = gpl_archive()
        (self.dir / "gpl_source.tar.gz").write_bytes(archive)
        self.assertEqual(firmware.load_key(gpl_archive=self.dir / "gpl_source.tar.gz"), self.expected)
        half = len(archive) // 2
        parts = [self.dir / "p.00", self.dir / "p.01"]
        parts[0].write_bytes(archive[:half])
        parts[1].write_bytes(archive[half:])
        self.assertEqual(firmware.load_key(gpl_archive=parts), self.expected)
        zips = []
        for index, part in enumerate(parts):
            zipped = self.dir / f"gpl-0{index}.zip"
            with zipfile.ZipFile(zipped, "w") as handle:
                handle.write(part, part.name)
            zips.append(zipped)
        self.assertEqual(firmware.load_key(gpl_archive=zips), self.expected)
        with self.assertRaisesRegex(firmware.FirmwareError, "damaged or incomplete"):
            firmware.load_key(gpl_archive=list(reversed(zips)))
        (self.dir / "nokey.tar.gz").write_bytes(tar_gz([("gpl_source/initramfs.tar.gz", tar_gz([("x", b"y")]))]))
        with self.assertRaisesRegex(firmware.FirmwareError, "does not contain the update key"):
            firmware.load_key(gpl_archive=[self.dir / "nokey.tar.gz"])

    def test_key_absent_detects_every_encoding(self):
        key = self.expected
        self.assertTrue(firmware.key_absent(key, b"nothing here"))
        for form in (key, key.rstrip(b"\0"), key.hex().encode(), key.hex().upper().encode()):
            self.assertFalse(firmware.key_absent(key, b"prefix" + form + b"suffix"))

    def test_guest_key_scan(self):
        root = self.dir / "tree"
        (root / "etc").mkdir(parents=True)
        (root / "etc/clean").write_bytes(b"clean")
        (root / "etc/leak").write_bytes(b"x" + self.expected.hex().encode())
        os.symlink("leak", root / "etc/link")
        self.assertEqual(guest.files_containing_key(root, self.expected), ["etc/leak"])


class GuestScanTests(unittest.TestCase):
    def test_shim_inventory_matches_the_c_source(self):
        source = (TOOLS / "hardware_stub.c").read_text()
        devices = set(re.findall(r'!strcmp\(path, "(/(?:dev|proc)/[^"]+)"\)', source))
        self.assertEqual(devices, set(guest.SHIM_DEVICES))
        pcms = set(re.findall(r'"(hw:ak44\d\daudio,\d)"', source))
        self.assertEqual(pcms, set(guest.SHIM_PCMS))
        body = source[source.index("int ioctl(int fd"):source.index("/* Use the stock ALSA null plugin")]
        ioctls = {int(v, 16) for v in re.findall(r"(0x[0-9a-fA-F]{8})UL", body)}
        self.assertEqual(ioctls, set(guest.SHIM_IOCTLS))

    def test_scan_application(self):
        def movw(rd, value):
            return (0xe3000000 | (value >> 12 & 0xf) << 16 | rd << 12 | value & 0xfff).to_bytes(4, "little")

        def movt(rd, value):
            return (0xe3400000 | (value >> 12 & 0xf) << 16 | rd << 12 | value & 0xfff).to_bytes(4, "little")
        code = movw(1, 0x7001) + movt(1, 0x4001) + movw(2, 0x7099) + movt(2, 0x4004) + bytes(8)
        data = code + b"\0/dev/subucom_spi0.1\0/dev/spi_new9\0hw:ak4458audio,0\0hw:xx9audio,1\0padcalibration\0"
        report = guest.scan_application(data, ["padcalibration", "PadCalibrationData"])
        self.assertEqual(report["ioctls_known_found"], ["0x40017001"])
        self.assertEqual(report["ioctls_unknown_candidates"], ["0x40047099"])
        self.assertIn("/dev/spi_new9", report["devices_unknown"])
        self.assertNotIn("/dev/subucom_spi0.1", report["devices_unknown"])
        self.assertEqual(report["pcms_unknown"], ["hw:xx9audio,1"])
        self.assertEqual(report["calibration_markers"], {"padcalibration": True, "PadCalibrationData": False})

    def test_tree_manifest(self):
        root = private_tmp()
        try:
            (root / "a/b").mkdir(parents=True)
            (root / "a/b/f").write_bytes(b"data")
            os.chmod(root / "a/b/f", 0o640)
            os.symlink("/lib/x", root / "a/l")
            listing = guest.tree_manifest(root)
            self.assertEqual(list(listing), ["/a", "/a/b", "/a/b/f", "/a/l"])
            self.assertEqual(listing["/a/b/f"], {"type": "f", "mode": 0o640, "size": 4,
                                                 "sha256": hashlib.sha256(b"data").hexdigest()})
            self.assertEqual(listing["/a/l"], {"type": "l", "target": "/lib/x"})
        finally:
            import shutil
            shutil.rmtree(root)


class UnknownIoctlMetricTests(unittest.TestCase):
    def test_bridge_counts_unknown_ioctls_from_the_shim_log(self):
        # The bridge imports guest-only modules at load time, so exercise its pattern and counter class from source.
        source = (TOOLS / "guest-bridge.py").read_text()
        pattern = re.compile(re.search(r'UNKNOWN_IOCTL = re\.compile\(r"(.+)"\)', source).group(1))
        namespace = {"threading": __import__("threading")}
        exec(source[source.index("class Counters:"):source.index("UNKNOWN_IOCTL = ")], namespace)
        counters = namespace["Counters"]()
        for line in ("[sp16-ui-stub] unsupported ioctl fd=7 request=0x40047099",
                     "[sp16-ui-stub] unsupported ioctl fd=8 request=0x40047099",
                     "[sp16-ui-stub] open /dev/cdc_spi2.0 => fd=7", "unsupported ioctl fd=1 request=0x1"):
            match = pattern.match(line)
            if match:
                counters.unknown_ioctl(match.group(1))
        self.assertEqual(counters.snapshot()["unknown_ioctls"], {"0x40047099": 2})
        self.assertIn("counters.unknown_ioctl(unknown.group(1))", source)
        self.assertIn('"[sp16-ui-stub] unsupported ioctl fd=%d request=0x%lx\\n"',
                      (TOOLS / "hardware_stub.c").read_text())  # the shim still fails them loudly
        self.assertIn("errno=ENOTTY; return -1;", (TOOLS / "hardware_stub.c").read_text())


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_tmp()
        self.library = firmware.Library(self.dir / "lib", create=True)
        self.boot = {"qemu": "/q", "kernel": "/k", "initrd": "/i", "initrd_sha256": "0" * 64}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def fake_vm(self, manifest_factory):
        def run(boot, update, update_bytes, key, output, console, **kwargs):
            image = b"hsqs" + bytes(8188)
            with open(output, "r+b") as handle:
                handle.write(image)
            Path(console).write_bytes(b"SP16_IMPORT_DONE\n")
            meta = firmware.inspect_update(update)
            return {"result": "ok", "manifest": manifest_factory(meta, image)}
        return run

    def import_synthetic(self, factory=None, version=b"1.60"):
        update = make_update(self.dir / f"u{version.decode()}.UPD", version=version)
        key = firmware.update_key(SYNTHETIC_KEY_LINE)
        factory = factory or (lambda meta, image: guest_manifest(version=meta["trailer_version"],
                                                                  crc=meta["crc32"], image=image))
        with mock.patch.object(firmware, "run_import_vm", self.fake_vm(factory)):
            return firmware.import_update(self.library, self.boot, update, key), key

    def test_import_stores_verified_entry_and_never_the_key(self):
        (entry_id, report), key = self.import_synthetic()
        self.assertRegex(entry_id, r"^sp16-1\.60-[0-9a-f]{12}$")
        self.assertTrue(report["boots"])
        self.assertEqual(self.library.ids(), [entry_id])
        self.assertEqual(self.library.selected(), entry_id)
        firmware.verify_image(self.library, entry_id)
        for path in self.library.path.rglob("*"):
            if path.is_file():
                self.assertTrue(firmware.key_absent(key, path.read_bytes()), path.name)
                self.assertEqual(path.stat().st_mode & 0o077, 0, path.name)
        self.assertEqual(firmware.boot_status(self.library.manifest(entry_id))["status"], "verified")
        with self.assertRaisesRegex(firmware.FirmwareError, "already in the library"):
            self.import_synthetic()

    def test_djs_application_imports_as_the_djs1000_product(self):
        def factory(meta, image):
            manifest = guest_manifest(app_sha="f" * 64, version=meta["trailer_version"], crc=meta["crc32"], image=image)
            manifest["app_path"] = "/root/pdj/EP113_Main_R"
            manifest["files"] = {"/root/pdj/EP113_Main_R": manifest["files"].pop("/root/pdj/EP085_Main_R")}
            return manifest
        (entry_id, report), _ = self.import_synthetic(factory, version=b"1.22")
        self.assertRegex(entry_id, r"^djs-1\.22-[0-9a-f]{12}$")
        self.assertEqual(self.library.manifest(entry_id)["product"], "djs1000")
        self.assertEqual(json.loads((self.library.entry_dir(entry_id) / "entry.json").read_text())["name"],
                         "DJS-1000 1.22")
        self.assertEqual(report["profile"], "djs-1.22")
        broken = copy.deepcopy(self.library.manifest(entry_id))
        broken["product"] = "cdj3000"
        with self.assertRaisesRegex(firmware.FirmwareError, "product"):
            firmware.validate_manifest(broken)

    def test_unknown_application_imports_and_boots_marked_unverified(self):
        (entry_id, report), _ = self.import_synthetic(
            lambda meta, image: guest_manifest(app_sha="e" * 64, version=meta["trailer_version"], crc=meta["crc32"],
                                               image=image), version=b"1.50")
        self.assertTrue(report["boots"])  # any correctly keyed update runs (D2 in docs/architecture.md)
        self.assertFalse(report["verified"])
        self.assertEqual(report["status"], "unverified")
        self.assertIn("no compatibility profile", report["reason"])
        self.assertEqual((report["profile"], report["boot"]), ("sp16-1.60", {"profile": "sp16-1.60", "source": "reference"}))
        summary = self.library.summary(entry_id)
        self.assertEqual((summary["status"], summary["profile"]), ("unverified", None))
        # The image is still re-verified before every boot: a changed image is refused, verified or not.
        image = self.library.entry_dir(entry_id) / "firmware.sqfs"
        data = bytearray(image.read_bytes())
        data[10] ^= 1
        image.write_bytes(bytes(data))
        with self.assertRaisesRegex(firmware.FirmwareError, "does not match"):
            firmware.verify_image(self.library, entry_id)

    def test_modified_stock_trailer_update_imports_unverified(self):
        (entry_id, report), _ = self.import_synthetic(
            lambda meta, image: guest_manifest(app_sha="d" * 64, version=meta["trailer_version"], crc=meta["crc32"],
                                               image=image))
        self.assertEqual((report["boots"], report["status"], report["profile"]), (True, "unverified", "sp16-1.60"))
        self.assertEqual(self.library.manifest(entry_id)["kind"], "stock")

    def test_version_mismatch_and_candidate_profiles_are_unverified(self):
        manifest = self.library.manifest(self.import_synthetic()[0][0])
        other = copy.deepcopy(manifest)
        other["update"]["trailer_version"] = "1.50"
        self.assertEqual(firmware.boot_status(other)["status"], "unverified")
        candidate = copy.deepcopy(profiles.legacy())
        candidate["status"] = "candidate"
        with mock.patch.object(profiles, "for_app", return_value=candidate):
            self.assertIn("candidate", firmware.boot_status(manifest)["reason"])

    def test_guest_disagreement_and_missing_key_scan_are_refused(self):
        for bad in ({"key_scan": None}, {"update": {"trailer_version": "1.50", "crc32": "0"}}):
            def factory(meta, image, bad=bad):
                manifest = guest_manifest(version=meta["trailer_version"], crc=meta["crc32"], image=image)
                manifest.update(bad)
                return manifest
            with self.assertRaises(firmware.FirmwareError):
                self.import_synthetic(factory)
            self.assertEqual(list(self.library.path.iterdir()), [])

    def test_image_tamper_is_detected(self):
        entry_id = self.import_synthetic()[0][0]
        image = self.library.entry_dir(entry_id) / "firmware.sqfs"
        data = bytearray(image.read_bytes())
        data[10] ^= 1
        image.write_bytes(bytes(data))
        with self.assertRaisesRegex(firmware.FirmwareError, "does not match"):
            firmware.verify_image(self.library, entry_id)

    def test_mod_update_without_verification_imports_as_unverified(self):
        update = make_update(self.dir / "mod.UPD", marker=b"SP16MODLAB-")
        with mock.patch.object(firmware, "run_import_vm", self.fake_vm(
                lambda meta, image: guest_manifest(version="1.60", crc=meta["crc32"], image=image))):
            entry_id, report = firmware.import_update(self.library, self.boot, update, b"k" * 32)
        self.assertRegex(entry_id, r"^sp16-1\.60-mod-unverified-[0-9a-f]{12}$")
        manifest = self.library.manifest(entry_id)
        self.assertEqual((manifest["kind"], manifest["mod"]["verified"]), ("mod", False))
        self.assertTrue(manifest["mod"]["reason"])
        self.assertEqual((report["boots"], report["status"], report["profile"]), (True, "unverified", "sp16-1.60"))
        self.assertIn("does not verify", report["reason"])
        # an unverified mod is matched by its own application hash, never by a base it did not prove
        self.assertEqual(firmware.base_app_sha256(manifest), manifest["app_sha256"])
        # a mod section that claims verification without its base is an invalid manifest
        broken = copy.deepcopy(manifest)
        broken["mod"]["verified"] = True
        with self.assertRaisesRegex(firmware.FirmwareError, "invalid"):
            firmware.validate_manifest(broken)

    def test_still_refused_without_a_valid_trailer_or_with_the_wrong_key(self):
        bad_crc = make_update(self.dir / "crc.UPD")
        data = bytearray(bad_crc.read_bytes())
        data[3] ^= 1
        bad_crc.write_bytes(bytes(data))
        with self.assertRaisesRegex(firmware.FirmwareError, "CRC"):
            firmware.import_update(self.library, self.boot, bad_crc, b"k" * 32)
        wrong_key = make_update(self.dir / "wrong.UPD")
        with mock.patch.object(firmware, "run_import_vm", lambda *a, **k: {"result": "refused", "error": "no volume"}):
            with self.assertRaisesRegex(firmware.FirmwareError, "no volume"):
                firmware.import_update(self.library, self.boot, wrong_key, b"k" * 32)
        self.assertEqual(list(self.library.path.iterdir()), [])

    def test_rename_select_remove_and_ids(self):
        entry_id = self.import_synthetic()[0][0]
        self.library.rename(entry_id, "My SP-16")
        self.assertEqual(self.library.summary(entry_id)["name"], "My SP-16")
        for bad in ("", "x" * 81, "bad\nname"):
            with self.assertRaises(firmware.FirmwareError):
                self.library.rename(entry_id, bad)
        for bad in ("../x", "A", "", "a/b"):
            with self.assertRaises(firmware.FirmwareError):
                self.library.entry_dir(bad)
        self.library.select(entry_id)
        self.library.remove(entry_id)
        self.assertEqual(self.library.ids(), [])

    def test_private_dir_rules(self):
        open_dir = self.dir / "open"
        open_dir.mkdir(mode=0o755)
        os.chmod(open_dir, 0o755)
        with self.assertRaisesRegex(firmware.FirmwareError, "owner-only"):
            firmware.Library(open_dir)
        os.symlink(self.dir / "lib", self.dir / "link")
        with self.assertRaisesRegex(firmware.FirmwareError, "symlink"):
            firmware.Library(self.dir / "link")
        with self.assertRaisesRegex(firmware.FirmwareError, "commas"):
            firmware.Library(self.dir / "a,b", create=True)
        tracked = Path(__file__).resolve().parent
        with self.assertRaisesRegex(firmware.FirmwareError, "git-ignored"):
            firmware.private_dir(tracked)

    def test_check_report_lists_shim_and_panel_checks(self):
        manifest = self.library.manifest(self.import_synthetic()[0][0])
        report = firmware.check_report(manifest, self.library)
        names = [item["check"] for item in report["checks"]]
        self.assertTrue(any("ioctl" in n for n in names) and any("panel MCU" in n for n in names))
        self.assertTrue(all(item["result"] in ("pass", "warn", "fail") for item in report["checks"]))

    def test_compatibility_check_label_is_plain_and_detail_names_the_profile(self):
        def profile_item(report):
            return next(item for item in report["checks"] if item["check"] == "compatibility profile")
        unverified = firmware.check_report(self.library.manifest(self.import_synthetic(
            lambda meta, image: guest_manifest(app_sha="e" * 64, version=meta["trailer_version"], crc=meta["crc32"],
                                               image=image), version=b"1.50")[0][0]))
        self.assertEqual((profile_item(unverified)["result"], unverified["profile"]), ("warn", "sp16-1.60"))
        self.assertEqual(profile_item(unverified)["detail"], "unverified: runs on the reference measurements of sp16-1.60 "
                         "(no compatibility profile for this firmware)")
        verified = firmware.check_report(self.library.manifest(self.import_synthetic()[0][0]))
        self.assertEqual((verified["status"], verified["profile"], verified["boot"]["source"]),
                         ("verified", "sp16-1.60", "verified"))
        self.assertEqual(profile_item(verified), {"check": "compatibility profile", "result": "pass", "detail": "sp16-1.60"})


class DecoupledBuildTests(unittest.TestCase):
    def test_firmware_arguments(self):
        args = native.arguments(Path("/q"), Path("/g"), "/s", data=True, firmware=True)
        joined = " ".join(args)
        self.assertIn("file=@FWIMAGE@,format=raw,if=none,id=sp16fw,readonly=on", joined)
        self.assertIn("serial=sp16fw", joined)
        for token in ("sp16.fw=@FWSHA@", "sp16.fwbytes=@FWBYTES@", "sp16.fwapp=@FWAPP@", "sp16.fwbase=@FWBASE@",
                      "sp16.fwver=@FWVER@"):
            self.assertIn(token, joined)
        self.assertNotIn("allow_unverified", joined)
        self.assertNotIn("@FW", " ".join(native.arguments(Path("/q"), Path("/g"), "/s", data=True)))
        # --allow-unverified is a no-op flag now: nothing about it reaches the guest command line.
        self.assertNotIn("allow_unverified", " ".join(native.arguments(Path("/q"), Path("/g"), "/s", firmware=True)))

    def test_decoupled_build_option_rules(self):
        with self.assertRaises(ValueError):
            native.build("out", profile="testmode")
        with self.assertRaises(ValueError):
            native.build("out", profile=None)
        with self.assertRaises(ValueError):  # research only
            native.build("out", profile="fullpanel", trace="strace")
        with self.assertRaises(ValueError):
            native.build("out", profile="fullpanel", uncalibrated=True, pad_calibration=242)
        with self.assertRaises(ValueError):
            native.build("out", profile="fullpanel", audio="file", audio_device="uid")
        with self.assertRaises(ValueError):
            native.build("out", profile="fullpanel", audio="coreaudio", audio_device="bad\nuid")

    def test_decoupled_assets_never_name_an_application(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            guest_dir = out / "guest"
            guest_dir.mkdir()
            paths = [out / "qemu", guest_dir / "kernel", guest_dir / "guest.cpio.gz"]
            for path in paths:
                path.write_bytes(b"x")
            assets = {"decoupled": True, "firmware": None, "qemu": str(paths[0]), "guest": str(guest_dir),
                      "hashes": {str(p): native.digest(p) for p in paths}}
            native.validated_base_assets(out, assets)
            for bad in ({"application": {}}, {"firmware": "1.60"}):
                with self.assertRaises(ValueError):
                    native.validated_base_assets(out, {**assets, **bad})
            paths[2].write_bytes(b"changed")
            with self.assertRaises(ValueError):
                native.validated_base_assets(out, assets)

    def test_base_image_carries_no_oem_rootfs(self):
        dockerfile = (TOOLS / "Dockerfile.native-base").read_text()
        self.assertIsNone(re.search(r"^(COPY|ADD)\b.*rootfs", dockerfile, re.M))
        self.assertIn("touch /opt/sp16/sp16-base", dockerfile)
        packer = (TOOLS / "pack-guest.sh").read_text()
        self.assertIn("test ! -e /opt/sp16/rootfs", packer)
        for name in native.BASE_GUEST_FILES:
            self.assertTrue((TOOLS / name).is_file(), name)
        ignore = (TOOLS / ".dockerignore").read_text()
        for name in ("guest-firmware.py", "prepare.py", "Dockerfile.native-base"):
            self.assertIn("!" + name, ignore)


def newc(entries):
    """A gzip newc archive from [(name, data, mode)]."""
    import gzip
    out = io.BytesIO()
    for index, (name, data, mode) in enumerate(entries + [("TRAILER!!!", b"", 0)]):
        encoded = name.encode() + b"\0"
        fields = [index + 1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0]
        header = b"070701" + b"".join(b"%08x" % f for f in fields)
        out.write(header + encoded + b"\0" * ((4 - (110 + len(encoded)) % 4) % 4))
        out.write(data + b"\0" * ((4 - len(data) % 4) % 4))
    return gzip.compress(out.getvalue())


class AuditTests(unittest.TestCase):
    def test_audit_flags_firmware_files_and_proves_debian_upstream_ones(self):
        root = private_tmp()
        try:
            library = firmware.Library(root / "lib", create=True)
            upstream, oem, conf = b"upstream locale data\n", b"vendor binary\n", b"1 realm\n"

            def md5(data):
                return hashlib.md5(data).hexdigest()

            def sha(data):
                return hashlib.sha256(data).hexdigest()
            entry = library.entry_dir("sp16-1.60-aaaaaaaaaaaa")
            entry.mkdir(mode=0o700)
            manifest = {"schema": 1, "product": "sp16", "id": entry.name, "kind": "stock",
                        "app_path": "/root/pdj/EP085_Main_R", "app_sha256": "a" * 64, "mod": None,
                        "image": {"file": "firmware.sqfs", "bytes": 4096, "sha256": "b" * 64},
                        "update": {"trailer_version": "1.60", "sha256": "c" * 64},
                        "files": {f"/f{i}": {"type": "f", "size": len(d), "sha256": sha(d)}
                                  for i, d in enumerate((upstream, oem, conf))}}
            (entry / "manifest.json").write_text(json.dumps(manifest))
            initrd = root / "guest.cpio.gz"
            initrd.write_bytes(newc([
                ("usr/share/X11/locale/x", upstream, 0o100644),
                ("opt/sp16/copied-from-firmware", oem, 0o100644),
                ("etc/iproute2/rt_realms", conf, 0o100644),
                ("var/lib/dpkg/info/libx11-data.md5sums", f"{md5(upstream)}  usr/share/X11/locale/x\n".encode(),
                 0o100644),
                ("var/lib/dpkg/status",
                 f"Package: iproute2\nConffiles:\n /etc/iproute2/rt_realms {md5(conf)}\n".encode(), 0o100644)]))
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "clean").write_bytes(b"our code")
            report = firmware.audit_build([bundle], [initrd], library)
            self.assertFalse(report["clean"])
            self.assertEqual([m["file"] for m in report["matches"]], ["guest.cpio.gz:opt/sp16/copied-from-firmware"])
            self.assertEqual(report["debian_packaged_upstream_matches"], 2)
            (bundle / "leak").write_bytes(upstream)  # the bundle has no package database: always a match
            self.assertEqual(firmware.audit_build([bundle], [], library)["match_count"], 1)
        finally:
            import shutil
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
