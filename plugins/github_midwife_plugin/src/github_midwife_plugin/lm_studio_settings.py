"""Preserve host settings while explicitly disabling prohibited JIT loading."""

from __future__ import annotations

import json
from pathlib import Path

from .lm_studio_models import contained_regular_file
from .setup_adapter_runtime import Runtime, read_json_object


def settings_path(home: Path) -> Path:
    return home / ".lmstudio/.internal/http-server-config.json"


def jit_disabled(home: Path) -> bool:
    path = settings_path(home)
    if not contained_regular_file(path, home):
        return False
    settings = read_json_object(path)
    return settings is not None and settings.get("justInTimeModelLoading") is False


def disable_jit(runtime: Runtime) -> bool:
    """Seed before first start, or patch exactly one key of an existing object.

    Existing unreadable or malformed settings are a failure, never replaced
    with defaults. The caller repeats the readback after starting the server.
    """

    path = settings_path(runtime.home)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if runtime.home in parent.parents):
        return False
    if path.exists():
        settings = read_json_object(path)
        if settings is None:
            return False
    else:
        settings = {}
    if settings.get("justInTimeModelLoading") is not False:
        settings["justInTimeModelLoading"] = False
        runtime.atomic_write(path, json.dumps(settings, indent=2, sort_keys=True) + "\n", mode=0o600)
    return jit_disabled(runtime.home)
