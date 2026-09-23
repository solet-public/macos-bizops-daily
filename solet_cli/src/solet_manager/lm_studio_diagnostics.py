"""Passive LM Studio inspection without importing or executing target code."""

from __future__ import annotations

import json
import os
import plistlib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .existing_solet_diagnostics import DiagnosticCheck, DiagnosticStatus
from .models import JsonValue

type ModelReader = Callable[[], dict[str, str] | None]
_LABEL = "local.solet.lm-studio"
_ARTIFACTS = {
    "embeddings": ("text-embedding-nomic-embed-text-v1.5-embedding", "gaianet/Nomic-embed-text-v1.5-Embedding-GGUF/nomic-embed-text-v1.5.f16.gguf", 274290560),
    "inference": ("qwen3-14b", "lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf", 9001753376),
}


def _read_object(path: Path) -> dict[str, JsonValue] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            return None
        raw: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return cast(dict[str, JsonValue], raw) if isinstance(raw, dict) else None


def selected_lm_studio_roles(target: Path) -> tuple[str, ...]:
    bindings = _read_object(target / "profile/config/service_bindings.json") or {}
    roles: list[str] = []
    for role, service, plugin in (("embeddings", "embedding_service", "openai_embeddings_plugin"), ("inference", "inference_service", "default_inference_plugin")):
        if bindings.get(service) != plugin:
            continue
        config = _read_object(target / "profile/config/plugins" / f"{plugin}.json")
        if config is not None and config.get("base_url") in {"http://localhost:1234/v1", "http://127.0.0.1:1234/v1"}:
            roles.append(role)
    return tuple(roles)


def _models() -> dict[str, str] | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:1234/api/v0/models", timeout=2) as response:
            body = response.read(1_048_577)
        if len(body) > 1_048_576:
            return None
        payload = cast(JsonValue, json.loads(body))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    return _model_states(rows)


def _model_states(rows: JsonValue) -> dict[str, str] | None:
    if not isinstance(rows, list):
        return None
    states: dict[str, str] = {}
    for row in rows:
        identifier = row.get("id") if isinstance(row, dict) else None
        state = row.get("state") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in states or state not in ("loaded", "not-loaded"):
            return None
        states[identifier] = str(state)
    return states


def _check(name: str, observed: bool | None, source: Path | str) -> DiagnosticCheck:
    status = DiagnosticStatus.UNKNOWN if observed is None else DiagnosticStatus.VERIFIED if observed else DiagnosticStatus.FAILED
    return DiagnosticCheck(
        check_id=f"inspect::lm_studio_{name}", status=status,
        summary=f"LM Studio {name.replace('_', ' ')}: {status.value}",
        reason_code=None if observed else f"lm_studio_{name}_unknown" if observed is None else f"lm_studio_{name}_unavailable",
        repair_code=None if observed else "resume_incomplete_setup_or_restore_named_host_condition",
        observed=observed, expected=True, source=str(source),
    )


def _artifact_valid(path: Path, size: int) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            return False
        with path.open("rb") as stream:
            return stream.read(4) == b"GGUF"
    except OSError:
        return False


def _login_valid(home: Path) -> bool | None:
    path = home / "Library/LaunchAgents" / f"{_LABEL}.plist"
    helper = home / "Library/Application Support/Solet/LM Studio/start.sh"
    if not path.exists() or not helper.exists():
        return False
    try:
        if path.is_symlink() or helper.is_symlink() or path.stat().st_size > 65536:
            return None
        plist = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return plist.get("Label") == _LABEL and plist.get("ProgramArguments") == ["/bin/sh", str(helper)] and plist.get("RunAtLoad") is True


def inspect_lm_studio(target: Path, *, home: Path | None = None, model_reader: ModelReader | None = None) -> tuple[DiagnosticCheck, ...]:
    """Read named host surfaces only when the target selects local LM Studio."""

    roles = selected_lm_studio_roles(target)
    if not roles:
        return ()
    home = Path.home() if home is None else home
    cli = home / ".lmstudio/bin/lms"
    config = home / ".lmstudio/.internal/http-server-config.json"
    settings = _read_object(config)
    models = (model_reader or _models)()
    checks = [
        _check("cli", cli.is_file() and not cli.is_symlink() and os.access(cli, os.X_OK), cli),
        _check("jit_disabled", None if settings is None else settings.get("justInTimeModelLoading") is False, config),
        _check("server", None if models is None else True, "http://127.0.0.1:1234/api/v0/models"),
        _check("login_agent_definition", _login_valid(home), home / "Library/LaunchAgents" / f"{_LABEL}.plist"),
    ]
    for role in roles:
        identifier, relative, size = _ARTIFACTS[role]
        artifact = home / ".lmstudio/models" / relative
        checks.extend((
            _check(f"{role}_artifact", _artifact_valid(artifact, size), artifact),
            _check(f"{role}_loaded", None if models is None else models.get(identifier) == "loaded", "http://127.0.0.1:1234/api/v0/models"),
        ))
    return tuple(checks)
