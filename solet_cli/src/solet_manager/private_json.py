"""Private regular-file validation and JSON object loading."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .errors import StateError
from .models import JsonValue


def load_json_object(
    path: Path,
    *,
    missing_ok: bool = False,
) -> dict[str, JsonValue] | None:
    """Load a private regular JSON object, refusing corrupt or unsafe files."""

    info = _private_file_info(path, missing_ok=missing_ok)
    if info is None:
        return None
    _validate_private_file(path, info.st_mode, info.st_uid)
    parsed = _read_json(path)
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise StateError(f"state file must contain one JSON object: {path}")
    return parsed


def _private_file_info(path: Path, *, missing_ok: bool) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise StateError(f"required state file is missing: {path}") from None


def _validate_private_file(path: Path, mode: int, owner: int) -> None:
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise StateError(f"state path is not a regular file: {path}")
    if owner != os.getuid():
        raise StateError(f"state file is not owned by the current user: {path}")
    if stat.S_IMODE(mode) != 0o600:
        raise StateError(f"state file mode must be exactly 0600: {path}")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(
            f"state file is unreadable or corrupt at {path}: {exc}"
        ) from exc
