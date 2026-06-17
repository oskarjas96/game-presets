#!/usr/bin/env python3
"""Validate the schema of index.json.

This is a separate, focused validator for the on-disk index.json file.
It does not regenerate the index, it only inspects whatever is committed
and exits non-zero if the shape is wrong.

Required shape (top-level object):
    {
        "$schema_version": "1.0.0",
        "presets": [
            {
                "id":           str,  # uuid4 hex, 32 chars
                "display_name": str,  # non-empty
                "version":      str,  # non-empty (semver string)
                "game_id":      str,  # non-empty, unique across the file
                "path":         str,  # repo-relative posix path to a real file
                "checksum":     str,  # blake3 hex of the referenced file's raw bytes
            },
            ...
        ],
    }

Additional invariants:
    - All preset `path` values resolve to files that actually exist on disk.
    - `checksum` for each entry must match blake3 of the referenced file.
    - `game_id` must be unique across all entries.
    - `id` must be a 32-char lowercase hex string and unique across all entries.

Usage:
    python scripts/validate_index.py            # validate index.json, exit 0/1
    python scripts/validate_index.py --strict   # also fail on unknown extra top-level keys
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "index.json"

EXPECTED_SCHEMA_VERSION = "1.0.0"
ALLOWED_TOP_LEVEL_KEYS = {"$schema_version", "presets"}

ID_RE = re.compile(r"^[0-9a-f]{32}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([-+].*)?$")


class ValidationError(Exception):
    """Raised when index.json fails validation."""


def fail(msg: str) -> NoReturn:
    raise ValidationError(msg)


def load_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        fail(f"index.json not found at {INDEX_PATH}")
    try:
        text = INDEX_PATH.read_text(encoding="utf-8")
    except OSError as e:
        fail(f"failed to read {INDEX_PATH}: {e}")
    if not text.strip():
        fail("index.json is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        fail(f"index.json is not valid JSON: {e}")
    if not isinstance(data, dict):
        fail("index.json top-level must be a JSON object")
    return data


def validate_top_level(data: dict[str, Any], strict: bool) -> list[dict[str, Any]]:
    if "$schema_version" not in data:
        fail("index.json is missing required field '$schema_version'")
    sv = data["$schema_version"]
    if not isinstance(sv, str) or not sv.strip():
        fail("index.json '$schema_version' must be a non-empty string")
    if sv != EXPECTED_SCHEMA_VERSION:
        fail(
            f"index.json '$schema_version' is {sv!r}, expected {EXPECTED_SCHEMA_VERSION!r}"
        )

    if "presets" not in data:
        fail("index.json is missing required field 'presets'")
    presets = data["presets"]
    if not isinstance(presets, list):
        fail("index.json 'presets' must be an array")

    if strict:
        extra = sorted(set(data.keys()) - ALLOWED_TOP_LEVEL_KEYS)
        if extra:
            fail(f"index.json has unexpected top-level field(s): {', '.join(extra)}")

    return presets


REQUIRED_ENTRY_FIELDS = (
    "id",
    "display_name",
    "version",
    "game_id",
    "path",
    "checksum",
)


def validate_entry(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        fail(f"presets[{index}] must be a JSON object")
    missing = [k for k in REQUIRED_ENTRY_FIELDS if k not in entry]
    if missing:
        fail(f"presets[{index}] is missing required field(s): {', '.join(missing)}")
    for k in REQUIRED_ENTRY_FIELDS:
        v = entry[k]
        if not isinstance(v, str) or not v.strip():
            fail(f"presets[{index}].{k} must be a non-empty string")
    eid = entry["id"]
    if not ID_RE.match(eid):
        fail(
            f"presets[{index}].id {eid!r} is not a 32-char lowercase hex uuid4 string"
        )
    if not SEMVER_RE.match(entry["version"]):
        fail(
            f"presets[{index}].version {entry['version']!r} is not a valid semver string"
        )
    return entry


def blake3_hex(data: bytes) -> str:
    try:
        import blake3  # type: ignore
    except ImportError:
        fail(
            "the 'blake3' Python package is required to validate checksums "
            "(install with: pip install blake3)"
        )
    return blake3.blake3(data).hexdigest()


def validate_checksum(entry: dict[str, Any], index: int) -> None:
    rel_path = entry["path"]
    full_path = (REPO_ROOT / rel_path).resolve()
    if REPO_ROOT.resolve() not in full_path.parents and full_path != REPO_ROOT.resolve():
        fail(f"presets[{index}].path {rel_path!r} escapes the repository root")
    if not full_path.is_file():
        fail(f"presets[{index}].path {rel_path!r} does not point to an existing file")
    if not full_path.is_file():
        fail(f"presets[{index}].path {rel_path!r} does not point to an existing file")
    try:
        raw = full_path.read_bytes()
    except OSError as e:
        fail(f"presets[{index}].path {rel_path!r} could not be read: {e}")
    actual = blake3_hex(raw)
    expected = entry["checksum"]
    if actual != expected:
        fail(
            f"presets[{index}].path {rel_path!r} checksum mismatch: "
            f"expected {expected}, got {actual}"
        )


def validate_uniqueness(entries: list[dict[str, Any]]) -> None:
    seen_ids: dict[str, int] = {}
    seen_game_ids: dict[str, int] = {}
    seen_paths: dict[str, int] = {}
    for i, e in enumerate(entries):
        for field, seen in (
            ("id", seen_ids),
            ("game_id", seen_game_ids),
            ("path", seen_paths),
        ):
            value = e[field]
            if value in seen:
                fail(
                    f"duplicate {field} {value!r} at presets[{i}] "
                    f"(also at presets[{seen[value]}])"
                )
            seen[value] = i


def validate(strict: bool) -> None:
    data = load_index()
    presets = validate_top_level(data, strict=strict)
    entries = [validate_entry(p, i) for i, p in enumerate(presets)]
    for i, e in enumerate(entries):
        validate_checksum(e, i)
    validate_uniqueness(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the schema of index.json (no regeneration)."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on unknown extra top-level keys.",
    )
    args = parser.parse_args(argv)
    try:
        validate(strict=args.strict)
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("index.json is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
