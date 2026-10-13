"""Launch identity boundaries using synthetic bytes, never OEM fixtures."""
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


class AppIdentityTests(unittest.TestCase):
    def test_only_the_stock_application_is_allowlisted(self):
        data = b"synthetic application"
        with patch.object(app, "STOCK_SHA256", hashlib.sha256(data).hexdigest()):
            app.verify(data, app.identity())
            with self.assertRaises(ValueError):
                app.verify(data + b"x", app.identity())
        for variant in ("utility-label", "other", None):
            with self.assertRaises(ValueError):
                app.identity(variant)

    def test_manifest_cannot_authorize_arbitrary_binary(self):
        for field, value in (("app_sha256", "0" * 64), ("source_sha256", "0" * 64),
                             ("variant", "other"), ("patch", {})):
            manifest = app.identity()
            manifest[field] = value
            with self.assertRaises(ValueError):
                app.validate_identity(manifest)
        for invalid in (None, [], {}, {"variant": "stock"}):
            with self.assertRaises(ValueError):
                app.validate_identity(invalid)
        self.assertEqual(app.STOCK_SHA256, native.EXPECTED_APP)

    def test_launch_checks_identity_and_all_required_boot_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            guest = root / "guest"
            guest.mkdir()
            paths = [root / "qemu", guest / "kernel", guest / "guest.cpio.gz"]
            for path in paths:
                path.write_bytes(b"synthetic boot asset")
            assets = {"firmware": "1.60", "qemu": str(paths[0]), "guest": str(guest),
                      "app_sha256": app.STOCK_SHA256, "application": app.identity(),
                      "hashes": {str(p): native.digest(p) for p in paths}}
            def save():
                (root / "assets.json").write_text(json.dumps(assets))
            save()
            native.validated_assets(root)
            assets["app_sha256"] = "0" * 64
            save()
            with self.assertRaises(ValueError):
                native.validated_assets(root)
            # Existing stock builds without application metadata remain supported.
            assets["app_sha256"] = app.STOCK_SHA256
            del assets["application"]
            save()
            native.validated_assets(root)
            del assets["hashes"][str(paths[1])]
            save()
            with self.assertRaises(ValueError):
                native.validated_assets(root)


if __name__ == "__main__":
    unittest.main()
