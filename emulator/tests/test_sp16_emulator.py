"""Archive isolation tests; fixtures contain no OEM firmware or real credentials."""

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "sp16_prepare", Path(__file__).resolve().parents[1] / "prepare.py"
)
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class ArchiveIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "guest"
        self.root.mkdir()

    def archive(self, entries):
        path = self.base / "layer.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, kind, payload in entries:
                member = tarfile.TarInfo(name)
                if kind == "file":
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                else:
                    member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    member.linkname = payload
                    archive.addfile(member)
        return path

    def test_traversal_and_absolute_members_cannot_write_outside_guest(self):
        for name in ("../outside", str(self.base / "outside")):
            with self.subTest(name=name):
                archive = self.archive([(name, "file", b"fixture")])
                with self.assertRaises(ValueError):
                    PREPARE.unpack_layer(archive, self.root)
                self.assertFalse((self.base / "outside").exists())

    def test_write_through_guest_symlink_is_rejected(self):
        outside = self.base / "host"
        outside.mkdir()
        archive = self.archive([
            ("redirect", "symlink", str(outside)),
            ("redirect/outside", "file", b"fixture"),
        ])
        with self.assertRaises(ValueError):
            PREPARE.unpack_layer(archive, self.root)
        self.assertFalse((outside / "outside").exists())

    def test_loader_links_resolve_inside_guest_and_layers_replace_files(self):
        archive = self.archive([
            ("lib/ld-2.13.so", "file", b"first"),
            ("lib/ld-linux.so.3", "symlink", "ld-2.13.so"),
            ("absolute-loader", "symlink", "/lib/ld-linux.so.3"),
        ])
        PREPARE.unpack_layer(archive, self.root)
        PREPARE.unpack_layer(self.archive([("lib/ld-2.13.so", "file", b"second")]), self.root)
        self.assertEqual(PREPARE.guest_file(self.root, "/absolute-loader").read_bytes(), b"second")

    def test_sensitive_names_and_device_nodes_are_omitted(self):
        archive = self.archive([
            ("etc/shadow", "file", b"fixture"),
            ("etc/passwd", "file", b"fixture"),
            ("root/data/fixture.key", "file", b"fixture"),
            ("root/.ssh/fixture", "file", b"fixture"),
            ("dev/device", "file", b"fixture"),
            ("etc/ordinary.conf", "file", b"keep"),
        ])
        self.assertEqual(PREPARE.unpack_layer(archive, self.root), 5)
        self.assertEqual((self.root / "etc/ordinary.conf").read_bytes(), b"keep")
        self.assertFalse((self.root / "root/data/fixture.key").exists())
        self.assertFalse((self.root / "dev/device").exists())

    def test_hardlink_requires_explicit_handling(self):
        archive = self.archive([("alias", "hardlink", "../outside")])
        with self.assertRaises(ValueError):
            PREPARE.unpack_layer(archive, self.root)

    def test_guest_link_loops_and_parent_escape_are_rejected(self):
        (self.root / "loop").symlink_to("loop")
        (self.root / "escape").symlink_to("../outside")
        for name in ("/loop", "/escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                PREPARE.guest_file(self.root, name)


if __name__ == "__main__":
    unittest.main()
