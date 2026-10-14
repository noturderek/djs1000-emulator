"""Declarative firmware patch recipes (docs/architecture.md, M6). Shared by host and guest; stdlib only.

A recipe names its base profile and, per guest file, the base file's SHA-256, a list of same-length byte patches and
optionally the expected result SHA-256. A patch is either
  {"offset": N, "original_hex": ..., "replacement_hex": ..., "expected_count": k}
    bytes at N must equal the original; if expected_count is given, the original occurs exactly k times in the file;
  {"pattern_hex": ..., "replacement_hex": ..., "expected_count": k}
    every occurrence is replaced and there must be exactly k of them.
Patches never change a file's length, never apply to a file whose hash differs from base_sha256, and never write the
read-only firmware image: the guest writes results into the overlay's upper layer; the host only writes new files.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

SCHEMA = 1
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
HEX_PATTERN = re.compile(r"(?:[0-9a-f]{2}){1,4096}")
MAX_FILES = 64
MAX_PATCHES = 256


def _require(condition, message):
    if not condition:
        raise ValueError(f"invalid patch recipe: {message}")


def guest_path(path):
    """A normalised absolute guest path with no '..' or empty parts."""
    _require(isinstance(path, str) and path.startswith("/") and len(path) < 1024, "file path")
    parts = PurePosixPath(path).parts[1:]
    _require(parts and all(p not in ("", ".", "..") for p in parts) and "\0" not in path, "file path")
    return "/" + "/".join(parts)


def validate(recipe):
    _require(isinstance(recipe, dict) and recipe.get("schema") == SCHEMA, "schema")
    _require(isinstance(recipe.get("id"), str) and ID_PATTERN.fullmatch(recipe["id"]), "id")
    _require(isinstance(recipe.get("base_profile"), str) and recipe["base_profile"], "base_profile")
    files = recipe.get("files")
    _require(isinstance(files, dict) and 0 < len(files) <= MAX_FILES, "files")
    for path, spec in files.items():
        _require(guest_path(path) == path, f"file path {path!r}")
        _require(isinstance(spec, dict) and SHA256_PATTERN.fullmatch(str(spec.get("base_sha256", ""))),
                 f"{path}: base_sha256")
        _require(spec.get("result_sha256") is None or SHA256_PATTERN.fullmatch(str(spec["result_sha256"])),
                 f"{path}: result_sha256")
        patches = spec.get("patches")
        _require(isinstance(patches, list) and 0 < len(patches) <= MAX_PATCHES, f"{path}: patches")
        for patch in patches:
            _require(isinstance(patch, dict), f"{path}: patch")
            replacement = patch.get("replacement_hex")
            _require(isinstance(replacement, str) and HEX_PATTERN.fullmatch(replacement), f"{path}: replacement_hex")
            count = patch.get("expected_count")
            _require(count is None or (isinstance(count, int) and 0 < count <= 4096), f"{path}: expected_count")
            if "offset" in patch:
                original = patch.get("original_hex")
                _require(isinstance(patch["offset"], int) and patch["offset"] >= 0, f"{path}: offset")
                _require(isinstance(original, str) and HEX_PATTERN.fullmatch(original)
                         and len(original) == len(replacement), f"{path}: original_hex")
                _require("pattern_hex" not in patch, f"{path}: offset and pattern are exclusive")
            else:
                pattern = patch.get("pattern_hex")
                _require(isinstance(pattern, str) and HEX_PATTERN.fullmatch(pattern)
                         and len(pattern) == len(replacement), f"{path}: pattern_hex")
                _require(count is not None, f"{path}: a pattern patch needs expected_count")
    return recipe


def load_path(path, expected_id=None):
    recipe = validate(json.loads(Path(path).read_text()))
    if expected_id is not None and recipe["id"] != expected_id:
        raise ValueError("patch recipe id does not match its file name")
    return recipe


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def apply_file(data, spec, path="file"):
    """Return the patched bytes for one file spec; the input is never modified."""
    if sha256(data) != spec["base_sha256"]:
        raise ValueError(f"{path}: base file SHA-256 does not match the recipe")
    result = bytearray(data)
    for patch in spec["patches"]:
        replacement = bytes.fromhex(patch["replacement_hex"])
        count = patch.get("expected_count")
        if "offset" in patch:
            original = bytes.fromhex(patch["original_hex"])
            offset = patch["offset"]
            if offset + len(original) > len(data) or bytes(data[offset:offset + len(original)]) != original:
                raise ValueError(f"{path}: original bytes are absent at the recipe offset")
            if count is not None and data.count(original) != count:
                raise ValueError(f"{path}: original bytes occur {data.count(original)} times, expected {count}")
            result[offset:offset + len(original)] = replacement
        else:
            pattern = bytes.fromhex(patch["pattern_hex"])
            found = data.count(pattern)
            if found != count:
                raise ValueError(f"{path}: pattern occurs {found} times, expected {count}")
            index = data.find(pattern)
            while index >= 0:  # the same non-overlapping occurrences that count() saw, in the original bytes
                result[index:index + len(pattern)] = replacement
                index = data.find(pattern, index + len(pattern))
    result = bytes(result)
    if len(result) != len(data):
        raise ValueError(f"{path}: a recipe must not change the file length")
    if spec.get("result_sha256") and sha256(result) != spec["result_sha256"]:
        raise ValueError(f"{path}: patched file SHA-256 does not match the recipe result")
    return result


def apply(recipe, read, write):
    """Apply every file of a validated recipe: read(path) -> bytes, write(path, bytes). Returns {path: new sha256}.
    All files are patched in memory first, so a failing file leaves nothing written."""
    validate(recipe)
    results = {path: apply_file(read(path), spec, path) for path, spec in recipe["files"].items()}
    for path, data in results.items():
        write(path, data)
    return {path: sha256(data) for path, data in results.items()}


def recipe_sha256(recipe):
    """Stable identity of a recipe's content (sorted-key JSON)."""
    return sha256(json.dumps(validate(recipe), sort_keys=True, separators=(",", ":")).encode())
