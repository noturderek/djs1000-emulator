"""The allowlisted OEM application identity of legacy builds, shared by host and guest.

The values come from the verified 1.60 profile (profiles/sp16-1.60.json) instead of constants in code.
"""
import hashlib

import profiles

_LEGACY = profiles.legacy()

FIRMWARE = _LEGACY["firmware"]["version"]
STOCK_SHA256 = _LEGACY["firmware"]["app_sha256"]


def identity(variant="stock"):
    if variant != "stock":
        raise ValueError("unknown application variant")
    return {"firmware": FIRMWARE, "variant": variant, "source_sha256": STOCK_SHA256, "app_sha256": STOCK_SHA256}


def validate_identity(manifest):
    if not isinstance(manifest, dict) or manifest != identity(manifest.get("variant")):
        raise ValueError("application identity does not match an allowlisted variant")
    return manifest


def verify(data, manifest):
    validate_identity(manifest)
    if hashlib.sha256(data).hexdigest() != manifest["app_sha256"]:
        raise ValueError("application SHA-256 does not match the selected variant")
