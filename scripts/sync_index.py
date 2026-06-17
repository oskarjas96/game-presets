#!/usr/bin/env python3
"""Keep index.json in sync with the presets/ directory.

Discovery:
    Walks presets/ recursively, treating every *.json file as a preset.

Per-preset required fields (read from the preset JSON):
    - display_name
    - version
    - game_id

Index file shape:
    {
        "$schema_version": "1.0.0",
        "presets": [
            {
                "id":           str,  # uuid4 hex, persisted by matching `path`
                "display_name": str,
                "version":      str,
                "game_id":      str,
                "path":         str,  # repo-relative posix path
                "checksum":     str,  # blake3 hex of raw file bytes
            },
            ...
        ],
    }

Usage:
    python scripts/sync_index.py             # write index.json in place
    python scripts/sync_index.py --check     # exit 0 if in sync, else 1
    python scripts/sync_index.py --check --diff   # also print unified diff
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = REPO_ROOT / "presets"
INDEX_PATH = REPO_ROOT / "index.json"

REQUIRED_FIELDS = ("display_name", "version", "game_id")


class SyncError(RuntimeError):
    """Raised when the index cannot be built or is invalid."""


def repo_relative_posix(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def blake3_hex(data: bytes) -> str:
    """Compute blake3 hex digest via the `blake3` package (pip install blake3)."""
    import blake3  # type: ignore

    return blake3.blake3(data).hexdigest()


def discover_presets() -> list[Path]:
    if not PRESETS_DIR.is_dir():
        raise SyncError(f"presets directory not found: {PRESETS_DIR}")
    return sorted(p for p in PRESETS_DIR.rglob("*.json") if p.is_file())


def read_preset(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            raw = f.read()
    except OSError as e:
        raise SyncError(f"failed to read {path}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SyncError(f"invalid JSON in {path}: {e}") from e

    if not isinstance(data, dict):
        raise SyncError(f"{path}: top-level JSON must be an object")

    missing = [k for k in REQUIRED_FIELDS if k not in data]
    if missing:
        raise SyncError(f"{path}: missing required field(s): {', '.join(missing)}")

    for k in REQUIRED_FIELDS:
        if not isinstance(data[k], str) or not data[k].strip():
            raise SyncError(f"{path}: field {k!r} must be a non-empty string")

    return {
        "raw": raw,
        "display_name": data["display_name"].strip(),
        "version": data["version"].strip(),
        "game_id": data["game_id"].strip(),
    }


def load_existing_index() -> dict[str, dict[str, Any]]:
    if not INDEX_PATH.exists():
        return {}
    try:
        with INDEX_PATH.open("r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise SyncError(f"failed to read {INDEX_PATH}: {e}") from e
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SyncError(f"index.json is not valid JSON: {e}") from e
    entries: Any
    if isinstance(data, dict):
        if "presets" not in data:
            raise SyncError("index.json is missing required 'presets' array")
        if not isinstance(data["presets"], list):
            raise SyncError("index.json 'presets' must be an array")
        entries = data["presets"]
    elif isinstance(data, list):
        entries = data
    else:
        raise SyncError("index.json must be a top-level object or array")
    by_path: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SyncError("every index.json entry must be an object")
        path = entry.get("path")
        if not isinstance(path, str):
            raise SyncError(f"index.json entry missing string 'path': {entry!r}")
        by_path[path] = entry
    return by_path


def build_index() -> list[dict[str, Any]]:
    existing = load_existing_index()
    presets = discover_presets()
    used_ids: set[str] = set()
    entries: list[dict[str, Any]] = []

    for preset_path in presets:
        rel = repo_relative_posix(preset_path)
        parsed = read_preset(preset_path)
        checksum = blake3_hex(parsed["raw"])

        prev = existing.get(rel)
        if prev and isinstance(prev.get("id"), str) and prev["id"]:
            entry_id = prev["id"]
        else:
            while True:
                entry_id = uuid.uuid4().hex
                if entry_id not in used_ids:
                    break

        used_ids.add(entry_id)
        entries.append(
            {
                "id": entry_id,
                "display_name": parsed["display_name"],
                "version": parsed["version"],
                "game_id": parsed["game_id"],
                "path": rel,
                "checksum": checksum,
            }
        )

    entries.sort(key=lambda e: e["path"])

    seen_game_ids: dict[str, str] = {}
    for e in entries:
        gid = e["game_id"]
        if gid in seen_game_ids:
            raise SyncError(
                f"duplicate game_id {gid!r} in: {seen_game_ids[gid]}, {e['path']}"
            )
        seen_game_ids[gid] = e["path"]

    if len(used_ids) != len(entries):
        raise SyncError("internal: duplicate id generated")

    return entries


def render(entries: list[dict[str, Any]]) -> str:
    return (
        json.dumps(
            {"$schema_version": SCHEMA_VERSION, "presets": entries},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_index(entries: list[dict[str, Any]]) -> None:
    INDEX_PATH.write_text(render(entries), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Exit non-zero if index.json is out of sync.")
    parser.add_argument("--diff", action="store_true", help="With --check, print a unified diff.")
    args = parser.parse_args(argv)

    try:
        entries = build_index()
    except SyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    new_text = render(entries)
    old_text = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else ""

    if args.check:
        if new_text == old_text:
            return 0
        if args.diff:
            diff = difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile="index.json (current)",
                tofile="index.json (expected)",
            )
            sys.stdout.writelines(diff)
        print("index.json is out of sync; run `python scripts/sync_index.py` to update.", file=sys.stderr)
        return 1

    if new_text == old_text:
        print("index.json already up to date.")
        return 0

    write_index(entries)
    print(
        f"wrote {INDEX_PATH.relative_to(REPO_ROOT).as_posix()} "
        f"({len(entries)} entries, schema_version={SCHEMA_VERSION})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
