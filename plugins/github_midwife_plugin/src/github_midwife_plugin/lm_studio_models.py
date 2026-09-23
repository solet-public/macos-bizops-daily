"""Reviewed LM Studio artifact identities and passive host observations."""

from __future__ import annotations

import json
import os
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .setup_adapter_contract import JsonValue
from .setup_adapter_runtime import Runtime

BASE_URL = "http://127.0.0.1:1234/v1"
MODEL_REGISTRY = Path("plugins/github_midwife_plugin/knowledge_base/profile_templates/lm_studio_models.yaml")


@dataclass(frozen=True)
class ModelArtifact:
    """One exact download and its independently checked served identifier."""

    role: str
    api_identifier: str
    repository: str
    filename: str
    size_bytes: int
    get_argv: tuple[str, ...]
    load_argv: tuple[str, ...]

    def path(self, home: Path) -> Path:
        return home / ".lmstudio/models" / self.repository / self.filename


def reviewed_models(target: Path) -> dict[str, ModelArtifact]:
    """Reject registry drift without making pre-venv provisioning depend on PyYAML.

    The reviewed registry intentionally has a tiny, fixed surface.  The exact
    artifact values remain in this module, and the source file must retain each
    of their canonical YAML lines.  This is deliberately fail-closed: an edit
    to a reviewed value needs a corresponding code review here rather than
    becoming a new command merely because a general YAML parser accepted it.
    """

    try:
        registry_lines = tuple(
            line.strip()
            for line in (target / MODEL_REGISTRY).read_text(encoding="utf-8").splitlines()
        )
    except OSError as exc:
        raise ValueError("LM Studio model registry is unreadable") from exc
    expected = (
        (
            "embeddings",
            "text-embedding-nomic-embed-text-v1.5-embedding",
            "gaianet/Nomic-embed-text-v1.5-Embedding-GGUF",
            "nomic-embed-text-v1.5.f16.gguf",
            274290560,
            "",
        ),
        (
            "inference",
            "qwen3-14b",
            "lmstudio-community/Qwen3-14B-GGUF",
            "Qwen3-14B-Q4_K_M.gguf",
            9001753376,
            "@Q4_K_M",
        ),
    )
    result: dict[str, ModelArtifact] = {}
    for role, identifier, repository, filename, size, suffix in expected:
        source = f"https://huggingface.co/{repository}{suffix}"
        get_argv = ("get", source, "--gguf", "--yes")
        load_argv = ("load", identifier, "--gpu", "off")
        if role == "inference":
            load_argv += ("--context-length", "8192")
        load_argv += ("--yes",)
        required_lines = (
            f"{role}:",
            f"api_identifier: {identifier}",
            f"download_source: {source}",
            f"download_file: {filename}",
            f"size_bytes: {size}",
            "get_argv: [" + ", ".join(get_argv) + "]",
            "load_argv: ["
            + ", ".join(
                f'"{item}"' if item in {"off", "8192"} else item for item in load_argv
            )
            + "]",
        )
        if any(registry_lines.count(line) != 1 for line in required_lines):
            raise ValueError(f"LM Studio {role} registry differs from the reviewed artifact")
        result[role] = ModelArtifact(role, identifier, repository, filename, size, get_argv, load_argv)
    return result


def cli_path(home: Path) -> Path:
    """Use the vendor location regardless of the caller's PATH."""

    return home / ".lmstudio/bin/lms"


def cli_available(home: Path) -> bool:
    path = cli_path(home)
    return contained_regular_file(path, home) and os.access(path, os.X_OK)


def contained_regular_file(path: Path, home: Path) -> bool:
    """Reject symlink traversal in the vendor-owned path beneath the home."""

    try:
        path.relative_to(home)
        return path.is_file() and not any(part.is_symlink() for part in (path, *path.parents) if part != home and home in part.parents)
    except (OSError, ValueError):
        return False


def artifact_present(model: ModelArtifact, home: Path) -> bool:
    """Require exact provenance, a complete artifact and its GGUF signature."""

    path = model.path(home)
    if not contained_regular_file(path, home):
        return False
    try:
        if path.stat().st_size != model.size_bytes:
            return False
        with path.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                return False
        metadata: object = json.loads((home / ".lmstudio/.internal/model-data.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return _provenance_matches(metadata, model)


def _provenance_matches(metadata: object, model: ModelArtifact) -> bool:
    rows = metadata.get("json") if isinstance(metadata, dict) else None
    if not isinstance(rows, list):
        return False
    key = f"{model.repository}/{model.filename}"
    matching = [row for row in rows if isinstance(row, list) and len(row) == 2 and row[0] == key]
    if len(matching) != 1 or not isinstance(matching[0][1], dict):
        return False
    owner, repository = model.repository.split("/", 1)
    expected = {"type": "huggingface", "owner": owner, "repo": repository, "file": model.filename}
    return matching[0][1].get("source") == expected


def partial_bytes(model: ModelArtifact, home: Path) -> int:
    """Report the vendor's receipted resumable partial without changing it."""

    path = model.path(home).with_name(f"downloading_{model.filename}.part")
    return path.stat().st_size if contained_regular_file(path, home) else 0


def served_models(runtime: Runtime, *, timeout_seconds: int = 2) -> tuple[str, ...] | None:
    """Observe server availability only: v1 includes unloaded models with JIT."""

    try:
        status, payload = runtime.http_json(f"{BASE_URL}/models", timeout_seconds=timeout_seconds)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(rows, list):
        return None
    identifiers: list[str] = []
    for row in rows:
        identifier = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            return None
        identifiers.append(identifier)
    return tuple(identifiers)


def model_loaded(runtime: Runtime, identifier: str) -> bool | None:
    """Validate the target's v0 shape on every passive loaded-state check.

    Unknown is distinct from absent: older or changed servers cannot pass by
    falling back to the v1 discovery list. See ruling uev_b78a36b4.
    """

    try:
        status, payload = runtime.http_json("http://127.0.0.1:1234/api/v0/models", timeout_seconds=2)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(rows, list):
        return None
    states = _loaded_states(rows)
    return None if states is None else states.get(identifier) == "loaded"


def _loaded_states(rows: list[JsonValue]) -> dict[str, str] | None:
    states: dict[str, str] = {}
    for row in rows:
        model_id = row.get("id") if isinstance(row, dict) else None
        state = row.get("state") if isinstance(row, dict) else None
        if not isinstance(model_id, str) or not model_id or model_id in states or state not in ("loaded", "not-loaded"):
            return None
        states[model_id] = str(state)
    return states


def selected_roles(inputs: dict[str, JsonValue]) -> tuple[str, ...]:
    """Select only the already-reviewed implementation decisions."""

    roles: list[str] = []
    for role, decision in (("embeddings", "embeddings_implementation"), ("inference", "inference_implementation")):
        value = inputs.get(decision)
        if not isinstance(value, str):
            raise ValueError(f"LM Studio input {decision} is unresolved")
        if value == "lm_studio":
            roles.append(role)
    return tuple(roles)
