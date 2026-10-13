"""Allowlisted OEM/experimental application identities, shared by host and guest.

The values come from the verified 1.60 profile (profiles/sp16-1.60.json) and the utility-label recipe
(mods/utility-label.json) instead of constants in code (docs/architecture.md, M1); the checks are unchanged.
"""
import hashlib

import profiles
import recipes

_LEGACY = profiles.legacy()
_RECIPE = recipes.load("utility-label")
if _RECIPE["base_profile"] != _LEGACY["id"]:
    raise ValueError("utility-label recipe does not target the legacy profile")
_APP_PATH = _LEGACY["firmware"]["app_path"]
_SPEC = _RECIPE["files"][_APP_PATH]
_PATCH = _SPEC["patches"][0]

FIRMWARE = _LEGACY["firmware"]["version"]
STOCK_SHA256 = _LEGACY["firmware"]["app_sha256"]
MOD_SHA256 = _SPEC["result_sha256"]
LABEL_OFFSET = _PATCH["offset"]
ORIGINAL_LABEL = bytes.fromhex(_PATCH["original_hex"])
MOD_LABEL = bytes.fromhex(_PATCH["replacement_hex"])
if _SPEC["base_sha256"] != STOCK_SHA256 or len(_SPEC["patches"]) != 1:
    raise ValueError("utility-label recipe does not match the legacy application")


def identity(variant="stock"):
    if variant not in ("stock", "utility-label"):
        raise ValueError("unknown application variant")
    result = {"firmware": FIRMWARE, "variant": variant, "source_sha256": STOCK_SHA256,
              "app_sha256": STOCK_SHA256 if variant == "stock" else MOD_SHA256}
    if variant == "utility-label":
        result["patch"] = {"file_offset": LABEL_OFFSET, "original_hex": ORIGINAL_LABEL.hex(),
                           "replacement_hex": MOD_LABEL.hex()}
    return result


def validate_identity(manifest):
    if not isinstance(manifest, dict) or manifest != identity(manifest.get("variant")):
        raise ValueError("application identity does not match an allowlisted variant")
    return manifest


def verify(data, manifest):
    validate_identity(manifest)
    if hashlib.sha256(data).hexdigest() != manifest["app_sha256"]:
        raise ValueError("application SHA-256 does not match the selected variant")


def patch_utility(data):
    """Return a new byte string; never modify the supplied OEM input."""
    verify(data, identity())
    end = LABEL_OFFSET + len(ORIGINAL_LABEL)
    if data[LABEL_OFFSET:end] != ORIGINAL_LABEL or data.count(ORIGINAL_LABEL) != 1:
        raise ValueError("expected unique UTILITY label is absent")
    result = data[:LABEL_OFFSET] + MOD_LABEL + data[end:]
    verify(result, identity("utility-label"))
    return result
