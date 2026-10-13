"""Patch and launch identity boundaries using synthetic bytes, never OEM fixtures."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_identity as app
import native


class ModIdentityTests(unittest.TestCase):
    def test_patch_preserves_all_other_bytes_and_rejects_wrong_input(self):
        original = b"prefix!UTILITY\0suffix"
        expected = b"prefix!MOD LAB\0suffix"
        with patch.multiple(app, LABEL_OFFSET=7,
                            STOCK_SHA256=hashlib.sha256(original).hexdigest(),
                            MOD_SHA256=hashlib.sha256(expected).hexdigest()):
            self.assertEqual(app.patch_utility(original), expected)
            self.assertEqual(len(original), len(expected))
            app.verify(expected, app.identity("utility-label"))
            for bad in (original[:-1], original + b"x", expected):
                with self.assertRaises(ValueError):
                    app.patch_utility(bad)
            with self.assertRaises(ValueError):
                app.verify(expected, app.identity())

    def test_duplicate_label_rejected_even_with_matching_fixture_hash(self):
        data = b"UTILITY\0UTILITY\0"
        with patch.multiple(app, LABEL_OFFSET=0, STOCK_SHA256=hashlib.sha256(data).hexdigest()):
            with self.assertRaises(ValueError):
                app.patch_utility(data)

    def test_manifest_cannot_authorize_arbitrary_binary_or_patch(self):
        for field, value in (("app_sha256", "0" * 64), ("source_sha256", "0" * 64),
                             ("variant", "other"), ("patch", {})):
            manifest = app.identity("utility-label")
            manifest[field] = value
            with self.assertRaises(ValueError):
                app.validate_identity(manifest)
        for invalid in (None, [], {}, {"variant": "stock"}):
            with self.assertRaises(ValueError):
                app.validate_identity(invalid)
        self.assertEqual(app.STOCK_SHA256, native.EXPECTED_APP)

    def test_launch_checks_variant_and_all_required_boot_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            guest = root / "guest"
            guest.mkdir()
            paths = [root / "qemu", guest / "kernel", guest / "guest.cpio.gz"]
            for path in paths:
                path.write_bytes(b"synthetic boot asset")
            assets = {"firmware": "1.60", "qemu": str(paths[0]), "guest": str(guest),
                      "app_sha256": app.MOD_SHA256, "application": app.identity("utility-label"),
                      "hashes": {str(p): native.digest(p) for p in paths}}
            def save():
                (root / "assets.json").write_text(json.dumps(assets))
            save()
            native.validated_assets(root)
            assets["app_sha256"] = app.STOCK_SHA256
            save()
            with self.assertRaises(ValueError):
                native.validated_assets(root)
            # Existing stock builds without variant metadata remain supported.
            del assets["application"]
            save()
            native.validated_assets(root)
            del assets["hashes"][str(paths[1])]
            save()
            with self.assertRaises(ValueError):
                native.validated_assets(root)


if __name__ == "__main__":
    unittest.main()
