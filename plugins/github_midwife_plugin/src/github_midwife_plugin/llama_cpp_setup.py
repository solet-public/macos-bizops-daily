"""Host-shared llama.cpp services for Macs below the Apple-native host profiles (iss_3a2a74ea).

The setup flow's host gate withholds Core AI embeddings and Apple Foundation
Models from a Mac below macOS 27 on arm64 (rul_c11cf191, rul_5cc2910c), and the
macos-bizops fresh create takes Homebrew llama.cpp there instead: one loopback
``llama-server`` login job per selected role, each serving the pinned GGUF the
reviewed registry ``llama_cpp_models.yaml`` names for that role.

The jobs follow the LM Studio login-job pattern (rul_5d2b3a95): host-shared
infrastructure every solet renders byte-identically and none removes.  Each
job's helper fetches its model from the pinned revision and accepts it only at
the declared size and SHA-256 before serving it, so a slow download finishes in
the background and a missing model is a warning, never a blocked stage
(rul_18bd93a3).  Setup points the bound plugins at the loopback servers:
``openai_embeddings_plugin`` through its address-book entry (the only place it
reads its endpoint), ``default_inference_plugin`` through its config file.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from .lm_studio_login_agent import launchd_job_state
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, evidence, planned_action, result
from .setup_adapter_runtime import Runtime, read_json_object, resolve_executable

__all__ = [
    "REGISTRY_PATH",
    "LlamaCppRegistryError",
    "LlamaModel",
    "LlamaRegistry",
    "LlamaRole",
    "configure_embeddings",
    "configure_inference",
    "embedding_config_valid",
    "inference_config_valid",
    "install_services",
    "load_registry",
    "models_present",
    "render_service",
    "selected_roles",
    "services_current",
]

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "knowledge_base" / "profile_templates" / "llama_cpp_models.yaml"
HOST_ROOT = Path("Library/Application Support/Solet/llama.cpp")
_LAUNCH_AGENTS = Path("Library/LaunchAgents")
_LABEL_PREFIX = "local.solet.llama-server."
_SERVICE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_ADDRESS_BOOK_SEED = Path("profile/config/plugins/default_address_book_plugin/entries.json")
_INFERENCE_CONFIG = Path("profile/config/plugins/default_inference_plugin.json")
_EMBEDDINGS_ENTRY = "openai_embeddings"
#: role -> (implementation decision, plugin bound to that role's service); the registry must agree.
_ROLE_CONTRACT: dict[str, tuple[str, str]] = {
    "summaries": ("inference_implementation", "default_inference_plugin"),
    "embeddings": ("embeddings_implementation", "openai_embeddings_plugin"),
}
_OPTION = "llama_cpp"
_HEALTH_POLL_SECONDS = 2.0
_HEALTH_WAIT_CEILING_SECONDS = 600
_HASH_CHUNK = 8 * 1024 * 1024


class LlamaCppRegistryError(ValueError):
    """The reviewed llama.cpp model registry is malformed."""


@dataclass(frozen=True, slots=True)
class LlamaModel:
    """One reviewed GGUF artifact at a pinned Hugging Face revision."""

    model_id: str
    alias: str
    repository: str
    revision: str
    filename: str
    size_bytes: int
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repository}/resolve/{self.revision}/{self.filename}"

    def path(self, home: Path) -> Path:
        return home / HOST_ROOT / "models" / self.repository / self.revision / self.filename


@dataclass(frozen=True, slots=True)
class LlamaRole:
    """One served role: its decision, consumer plugin, model, and server arguments."""

    name: str
    decision: str
    consumed_by: str
    model: LlamaModel
    port: int
    server_args: tuple[str, ...]
    max_input_tokens: int | None
    plugin_config: dict[str, JsonValue]

    @property
    def label(self) -> str:
        return f"{_LABEL_PREFIX}{self.name}"

    def base_url(self, host: str) -> str:
        return f"http://{host}:{self.port}/v1"


@dataclass(frozen=True, slots=True)
class LlamaRegistry:
    formula: str
    executable: str
    host: str
    roles: dict[str, LlamaRole]


def load_registry(path: Path = REGISTRY_PATH) -> LlamaRegistry:
    """Parse the closed registry; roles name catalog models, so a swap is one line."""

    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    document = _mapping(raw, "registry", {"schema_version", "formula", "executable", "host", "roles", "models"})
    if document["schema_version"] != 1:
        raise LlamaCppRegistryError("registry schema_version must be 1")
    models = {
        model_id: _model(model_id, row)
        for model_id, row in _mapping(document["models"], "models", None).items()
    }
    roles = {
        name: _role(name, row, models)
        for name, row in _mapping(document["roles"], "roles", None).items()
    }
    if set(roles) != set(_ROLE_CONTRACT):
        raise LlamaCppRegistryError(f"registry roles must be exactly {sorted(_ROLE_CONTRACT)}")
    if len({role.port for role in roles.values()}) != len(roles):
        raise LlamaCppRegistryError("registry roles must use distinct ports")
    return LlamaRegistry(
        formula=_text(document["formula"], "formula"),
        executable=_text(document["executable"], "executable"),
        host=_loopback(document["host"]),
        roles=roles,
    )


def _mapping(value: object, label: str, keys: set[str] | None) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in cast(dict[object, object], value)):
        raise LlamaCppRegistryError(f"{label} must be a mapping with string keys")
    mapping = cast(dict[str, object], value)
    if keys is not None and set(mapping) != keys:
        raise LlamaCppRegistryError(f"{label} must declare exactly {sorted(keys)}")
    return mapping


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LlamaCppRegistryError(f"{label} must be a non-empty string")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise LlamaCppRegistryError(f"{label} must be a positive integer")
    return value


def _loopback(value: object) -> str:
    if value != "127.0.0.1":
        raise LlamaCppRegistryError("llama.cpp services bind only 127.0.0.1")
    return "127.0.0.1"


def _hex(value: object, length: int, label: str) -> str:
    text = _text(value, label)
    if len(text) != length or any(char not in "0123456789abcdef" for char in text):
        raise LlamaCppRegistryError(f"{label} must be {length} lowercase hex characters")
    return text


def _model(model_id: str, raw: object) -> LlamaModel:
    row = _mapping(raw, f"models.{model_id}", {"alias", "repository", "revision", "filename", "size_bytes", "sha256"})
    repository, filename = _text(row["repository"], "repository"), _text(row["filename"], "filename")
    if repository.count("/") != 1 or ".." in repository or "/" in filename or not filename.endswith(".gguf"):
        raise LlamaCppRegistryError(f"models.{model_id} must name owner/repo and one .gguf file")
    return LlamaModel(
        model_id=model_id,
        alias=_text(row["alias"], "alias"),
        repository=repository,
        revision=_hex(row["revision"], 40, f"models.{model_id}.revision"),
        filename=filename,
        size_bytes=_positive(row["size_bytes"], "size_bytes"),
        sha256=_hex(row["sha256"], 64, f"models.{model_id}.sha256"),
    )


_ROLE_REQUIRED = frozenset({"decision", "consumed_by", "model", "port", "server_args"})
_ROLE_OPTIONAL = frozenset({"max_input_tokens", "plugin_config"})


def _role_row(name: str, raw: object) -> dict[str, object]:
    if name not in _ROLE_CONTRACT:
        raise LlamaCppRegistryError(f"unknown llama.cpp role {name!r}")
    row = _mapping(raw, f"roles.{name}", None)
    if not _ROLE_REQUIRED <= frozenset(row) <= _ROLE_REQUIRED | _ROLE_OPTIONAL:
        raise LlamaCppRegistryError(f"roles.{name} must declare {sorted(_ROLE_REQUIRED)} and only optional {sorted(_ROLE_OPTIONAL)}")
    if (row["decision"], row["consumed_by"]) != _ROLE_CONTRACT[name]:
        raise LlamaCppRegistryError(f"roles.{name} must be {_ROLE_CONTRACT[name]}")
    return row


def _arguments(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in cast(list[object], value)):
        raise LlamaCppRegistryError(f"{label} must be a list of strings")
    return tuple(cast(list[str], value))


def _scalars(value: object, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str | int | float | bool)
        for key, item in cast(dict[object, object], value).items()
    ):
        raise LlamaCppRegistryError(f"{label} must map keys to scalars")
    return cast(dict[str, JsonValue], value)


def _role(name: str, raw: object, models: Mapping[str, LlamaModel]) -> LlamaRole:
    row = _role_row(name, raw)
    model = models.get(_text(row["model"], f"roles.{name}.model"))
    if model is None:
        raise LlamaCppRegistryError(f"roles.{name}.model names no catalog model")
    budget = row.get("max_input_tokens")
    return LlamaRole(
        name=name,
        decision=_ROLE_CONTRACT[name][0],
        consumed_by=_ROLE_CONTRACT[name][1],
        model=model,
        port=_positive(row["port"], f"roles.{name}.port"),
        server_args=_arguments(row["server_args"], f"roles.{name}.server_args"),
        max_input_tokens=None if budget is None else _positive(budget, f"roles.{name}.max_input_tokens"),
        plugin_config=_scalars(row.get("plugin_config", {}), f"roles.{name}.plugin_config"),
    )


def selected_roles(registry: LlamaRegistry, public_inputs: Mapping[str, JsonValue]) -> tuple[LlamaRole, ...] | None:
    """Roles whose decision selected llama.cpp; ``None`` when the projected decisions are unusable."""

    values = [public_inputs.get(decision) for decision, _plugin in _ROLE_CONTRACT.values()]
    if not all(isinstance(value, str) and value for value in values):
        return None
    roles = tuple(role for role in registry.roles.values() if public_inputs.get(role.decision) == _OPTION)
    return roles or None


def helper_path(home: Path, role: LlamaRole) -> Path:
    return home / HOST_ROOT / f"{role.name}.sh"


def plist_path(home: Path, role: LlamaRole) -> Path:
    return home / _LAUNCH_AGENTS / f"{role.label}.plist"


def render_service(home: Path, registry: LlamaRegistry, role: LlamaRole, server: str) -> tuple[str, str]:
    """The login job and its helper, identical for every solet on this host.

    The helper keeps a verified model in place, otherwise resumes the pinned
    download and accepts it only at the declared size and SHA-256, then execs
    the server.  Any failure exits non-zero and launchd retries after the
    throttle interval, resuming the partial file.
    """

    model = role.model
    artifact = model.path(home)
    quoted = shlex.quote(str(artifact))
    size, digest = str(model.size_bytes), model.sha256
    serve = (
        server, "--model", str(artifact), "--alias", model.alias, "--host", registry.host,
        "--port", str(role.port), "--no-webui", *role.server_args,
    )
    lines = (
        "#!/bin/sh",
        "# Host-shared llama.cpp login service rendered by solet setup (iss_3a2a74ea).",
        "set -eu",
        f"model={quoted}",
        'partial="$model.partial"',
        'stamp="$model.verified"',
        f'if [ "$(/bin/cat "$stamp" 2>/dev/null || true)" != {digest} ] || [ "$(/usr/bin/stat -f %z "$model" 2>/dev/null || echo 0)" != {size} ]; then',
        f"  /bin/mkdir -p {shlex.quote(str(artifact.parent))}",
        '  /bin/rm -f "$stamp"',
        '  if [ -f "$model" ]; then /bin/mv -f "$model" "$partial"; fi',
        f'  if [ "$(/usr/bin/stat -f %z "$partial" 2>/dev/null || echo 0)" != {size} ]; then',
        "    " + shlex.join((
            "/usr/bin/curl", "--fail", "--location", "--silent", "--show-error", "--retry", "3",
            "--speed-limit", "1024", "--speed-time", "120", "--continue-at", "-", "--output",
        )) + f' "$partial" {shlex.quote(model.url)}',
        "  fi",
        f'  [ "$(/usr/bin/stat -f %z "$partial")" = {size} ] || {{ /bin/rm -f "$partial"; exit 1; }}',
        f'  [ "$(/usr/bin/shasum -a 256 "$partial" | /usr/bin/cut -d " " -f 1)" = {digest} ] || {{ /bin/rm -f "$partial"; exit 1; }}',
        '  /bin/mv -f "$partial" "$model"',
        f'  printf "%s\\n" {digest} > "$stamp"',
        "fi",
        "exec " + shlex.join(serve),
        "",
    )
    logs = home / HOST_ROOT / "logs"
    plist = {
        "Label": role.label,
        "ProgramArguments": ["/bin/sh", str(helper_path(home, role))],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "EnvironmentVariables": {"HOME": str(home), "PATH": _SERVICE_PATH},
        "StandardOutPath": str(logs / f"{role.name}.stdout.log"),
        "StandardErrorPath": str(logs / f"{role.name}.stderr.log"),
    }
    return plistlib.dumps(plist, sort_keys=True).decode(), "\n".join(lines)


def _definition_current(home: Path, registry: LlamaRegistry, role: LlamaRole, server: str) -> bool:
    expected = render_service(home, registry, role, server)
    for path, content, mode in zip((plist_path(home, role), helper_path(home, role)), expected, (0o644, 0o700), strict=True):
        try:
            if path.is_symlink() or path.read_text(encoding="utf-8") != content or path.stat().st_mode & 0o777 != mode:
                return False
        except (OSError, UnicodeError):
            return False
    return True


def _write_definition(runtime: Runtime, registry: LlamaRegistry, role: LlamaRole, server: str) -> None:
    (runtime.home / HOST_ROOT / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    rendered = render_service(runtime.home, registry, role, server)
    for path, content, mode in zip((plist_path(runtime.home, role), helper_path(runtime.home, role)), rendered, (0o644, 0o700), strict=True):
        runtime.atomic_write(path, content, mode=mode)


def _serving(runtime: Runtime, registry: LlamaRegistry, role: LlamaRole) -> bool:
    try:
        status, _body = runtime.http_json(f"http://{registry.host}:{role.port}/health", timeout_seconds=2)
    except (OSError, ValueError):
        return False
    return status == 200


def model_verified(home: Path, model: LlamaModel) -> tuple[bool, int]:
    """Readback: the served file at its pinned size and SHA-256, plus the bytes present so far."""

    artifact = model.path(home)
    for candidate in (artifact, artifact.with_name(artifact.name + ".partial")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        size = candidate.stat().st_size
        if candidate != artifact or size != model.size_bytes:
            return False, size
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK):
                digest.update(chunk)
        return digest.hexdigest() == model.sha256, size
    return False, 0


# --------------------------------------------------------------------------- services


def install_services(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Write and load each selected role's login job; wait, bounded, for embeddings."""

    registry = load_registry()
    roles = selected_roles(registry, request.public_inputs)
    if roles is None:
        return _blocked(request, "llama_cpp_inputs_invalid", "Resolve the embeddings and summaries choices, then re-preview.")
    server = resolve_executable(runtime, registry.executable)
    if server is None:
        return _server_missing(request, runtime, roles, registry)
    states = {role.name: launchd_job_state(runtime, role.label) for role in roles}
    stop = _state_stop(request, states)
    if stop is not None:
        return stop
    stale = tuple(role for role in roles if states[role.name] != "loaded" or not _definition_current(runtime.home, registry, role, server))
    if not stale:
        return _services_verified(request, runtime, registry, roles)
    if request.phase == "probe":
        return _planned(request, runtime, stale, registry)
    return _apply_services(request, runtime, registry, (roles, stale), server, states)


def _server_missing(request: AdapterRequest, runtime: Runtime, roles: tuple[LlamaRole, ...], registry: LlamaRegistry) -> JsonObject:
    """Preview still plans the services (llama.cpp installs earlier in this run); apply blocks loud."""
    if request.phase == "probe":
        return _planned(request, runtime, roles, registry)
    return _blocked(request, "llama_cpp_server_missing", "Install llama.cpp through setup's system step, then retry.")


def _apply_services(
    request: AdapterRequest,
    runtime: Runtime,
    registry: LlamaRegistry,
    selection: tuple[tuple[LlamaRole, ...], tuple[LlamaRole, ...]],
    server: str,
    states: Mapping[str, str],
) -> JsonObject:
    roles, stale = selection
    for role in stale:
        failure = _load_role(request, runtime, registry, role, server, reload=states[role.name] == "loaded")
        if failure is not None:
            return failure
    embeddings = registry.roles["embeddings"]
    if embeddings in roles:
        _wait_serving(runtime, registry, embeddings, min(_HEALTH_WAIT_CEILING_SECONDS, max(0, request.timeout_seconds - 60)))
    return result(request, status="applied", retry_safe=True)


def _load_role(
    request: AdapterRequest,
    runtime: Runtime,
    registry: LlamaRegistry,
    role: LlamaRole,
    server: str,
    *,
    reload: bool,
) -> JsonObject | None:
    _write_definition(runtime, registry, role, server)
    domain = f"gui/{os.getuid()}"
    if reload and not runtime.run(("/bin/launchctl", "bootout", f"{domain}/{role.label}"), timeout_seconds=30).ok:
        return _blocked(request, "llama_cpp_service_reload_failed", f"launchd kept the previous {role.label} job; retry once it has stopped.")
    if not runtime.run(("/bin/launchctl", "enable", f"{domain}/{role.label}"), timeout_seconds=10).ok:
        return _blocked(request, "llama_cpp_service_enable_failed", f"Allow {role.label} in System Settings > General > Login Items, then retry.")
    if not runtime.run(("/bin/launchctl", "bootstrap", domain, str(plist_path(runtime.home, role))), timeout_seconds=10).ok:
        return _blocked(request, "llama_cpp_service_load_failed", f"launchd refused {role.label}; allow it under Login Items, then retry.")
    if launchd_job_state(runtime, role.label) != "loaded":
        return _blocked(request, "llama_cpp_service_load_failed", f"launchd did not report {role.label} loaded; retry.")
    return None


def _wait_serving(runtime: Runtime, registry: LlamaRegistry, role: LlamaRole, budget_seconds: int) -> bool:
    deadline = time.monotonic() + budget_seconds
    while True:
        if _serving(runtime, registry, role):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_HEALTH_POLL_SECONDS)


def services_current(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Verified when every selected job is current and loaded; a server still fetching its model is a warning."""

    registry = load_registry()
    roles = selected_roles(registry, request.public_inputs)
    if roles is None:
        return _blocked(request, "llama_cpp_inputs_invalid", "Resolve the embeddings and summaries choices, then re-preview.")
    server = resolve_executable(runtime, registry.executable)
    if server is None:
        return _pending(request, "llama_cpp_services", "Install llama.cpp, then install its login services.")
    states = {role.name: launchd_job_state(runtime, role.label) for role in roles}
    stop = _state_stop(request, states)
    if stop is not None:
        return stop
    if all(states[role.name] == "loaded" and _definition_current(runtime.home, registry, role, server) for role in roles):
        return _services_verified(request, runtime, registry, roles)
    return _pending(request, "llama_cpp_services", "Approve installing the llama.cpp login services.")


def _services_verified(request: AdapterRequest, runtime: Runtime, registry: LlamaRegistry, roles: tuple[LlamaRole, ...]) -> JsonObject:
    items: list[JsonObject] = []
    waiting: list[str] = []
    for role in roles:
        serving = _serving(runtime, registry, role)
        if not serving:
            waiting.append(role.name)
        items.append(evidence(
            evidence_id=f"llama_cpp_{role.name}_service",
            kind="readiness",
            status="passed" if serving else "warning",
            summary=(
                f"{role.label} is loaded and serving {role.model.alias}" if serving
                else f"{role.label} is loaded; it is still fetching or loading {role.model.alias}"
            ),
            observed="serving" if serving else "starting",
            expected="serving; starting is a warning while the model downloads",
            source=f"launchctl print {role.label}; http://{registry.host}:{role.port}/health",
        ))
    repair = None if not waiting else (
        f"The {', '.join(waiting)} service is still fetching or loading its model; it finishes on its own. "
        f"Its log is in ~/{HOST_ROOT}/logs."
    )
    return result(request, status="verified", evidence_items=items, repair=repair)


def _state_stop(request: AdapterRequest, states: Mapping[str, str]) -> JsonObject | None:
    if "gui_session_absent" in states.values():
        return result(
            request,
            status="blocked",
            error_kind="llama_cpp_service_gui_session_required",
            repair=(
                "Log into the macOS graphical desktop as the account running setup, "
                f"then run solet create {request.name} --dry-run --json and review the new fingerprint before resuming."
            ),
        )
    if "unknown" in states.values():
        return _blocked(request, "llama_cpp_service_state_unknown", "launchctl could not report the llama.cpp login jobs; retry.")
    return None


def _planned(request: AdapterRequest, runtime: Runtime, roles: tuple[LlamaRole, ...], registry: LlamaRegistry) -> JsonObject:
    actions = [
        planned_action(
            action_id=f"llama_cpp.install_{role.name}_service",
            title=f"Run llama.cpp {role.name} ({role.model.alias}) as a host-shared login service",
            mutation_kind="host_provisioning",
            target=(
                f"{plist_path(runtime.home, role)} -> {role.base_url(registry.host)}; "
                f"model {role.model.url} ({role.model.size_bytes} bytes, sha256 {role.model.sha256})"
            ),
            evidence_ref=f"llama_cpp_{role.name}_service",
        )
        for role in roles
    ]
    return result(request, status="pending", actions=actions, repair="Approve the llama.cpp login services shown.")


# --------------------------------------------------------------------------- models


def models_present(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Always verified: each selected model is present at its pinned digest, or a warning."""

    registry = load_registry()
    roles = selected_roles(registry, request.public_inputs)
    if roles is None:
        return _blocked(request, "llama_cpp_inputs_invalid", "Resolve the embeddings and summaries choices, then re-preview.")
    items: list[JsonObject] = []
    missing: list[str] = []
    for role in roles:
        verified, present = model_verified(runtime.home, role.model)
        if not verified:
            missing.append(role.model.filename)
        items.append(evidence(
            evidence_id=f"llama_cpp_{role.name}_model",
            kind="readiness",
            status="passed" if verified else "warning",
            summary=(
                f"{role.model.filename} is present at its pinned SHA-256" if verified
                else f"{role.model.filename} is not verified yet ({present} of {role.model.size_bytes} bytes)"
            ),
            observed=[f"bytes={present}", f"verified={str(verified).lower()}"],
            expected=[f"bytes={role.model.size_bytes}", f"sha256={role.model.sha256}"],
            source=str(role.model.path(runtime.home)),
        ))
    repair = None if not missing else (
        f"The llama.cpp services are still fetching {', '.join(missing)}; the download resumes on its own "
        "and the solet uses the model once it is verified (a missing model is a warning, rul_18bd93a3)."
    )
    return result(request, status="verified", evidence_items=items, repair=repair)


# --------------------------------------------------------------------------- plugin configuration


def embeddings_entry(registry: LlamaRegistry) -> JsonObject:
    """The ``openai_embeddings`` address-book entry openai_embeddings_plugin reads its endpoint from."""

    role = registry.roles["embeddings"]
    fields: list[JsonValue] = [
        {"field_type": "base_url", "description": "llama.cpp OpenAI-compatible base URL", "value": role.base_url(registry.host)},
        {"field_type": "model", "description": "Embeddings model name", "value": role.model.alias},
    ]
    if role.max_input_tokens is not None:
        fields.append({
            "field_type": "max_input_tokens",
            "description": "Largest input the server embeds, counted with its /tokenize",
            "value": str(role.max_input_tokens),
        })
    return {
        "name": _EMBEDDINGS_ENTRY,
        "address_type": "api",
        "description": "OpenAI-compatible embeddings from the solet's llama.cpp server",
        "tags": [],
        "entries": fields,
    }


def inference_settings(registry: LlamaRegistry) -> JsonObject:
    role = registry.roles["summaries"]
    return {"base_url": role.base_url(registry.host), "model": role.model.alias, **role.plugin_config}


def _seed_entries(path: Path) -> list[JsonValue] | None:
    if not path.exists():
        return []
    document = read_json_object(path)
    entries = None if document is None else document.get("entries")
    return cast(list[JsonValue], entries) if isinstance(entries, list) else None


def _entry_named(entries: list[JsonValue], name: str) -> JsonValue | None:
    matching = [entry for entry in entries if isinstance(entry, dict) and entry.get("name") == name]
    return matching[0] if len(matching) == 1 else None


def embeddings_entry_error(target: Path) -> str | None:
    """Return why the llama.cpp embeddings entry is not written yet, or None.

    ``openai_embeddings_plugin`` reads its server only from this address-book
    entry, so a solet started before ``configure_llama_cpp_embeddings`` writes
    it cannot load the plugin (iss_aec1ef16).
    """
    path = target / _ADDRESS_BOOK_SEED
    entries = None if path.is_symlink() else _seed_entries(path)
    if entries is not None and _entry_named(entries, _EMBEDDINGS_ENTRY) == embeddings_entry(load_registry()):
        return None
    return f"the llama.cpp {_EMBEDDINGS_ENTRY} address-book entry is not written yet in {path}"


def configure_embeddings(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    return _embeddings_config(request, runtime, apply=request.phase == "apply")


def embedding_config_valid(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    return _embeddings_config(request, runtime, apply=False)


def _embeddings_config(request: AdapterRequest, runtime: Runtime, *, apply: bool) -> JsonObject:
    path = request.target / _ADDRESS_BOOK_SEED
    desired = embeddings_entry(load_registry())
    entries = None if path.is_symlink() else _seed_entries(path)
    if entries is None:
        return _blocked(request, "llama_cpp_config_conflict", f"{path} is not a readable address-book seed; repair it, then retry.")
    if _entry_named(entries, _EMBEDDINGS_ENTRY) == desired:
        return _verified(request, "llama_cpp_embeddings_config", "openai_embeddings points at the llama.cpp server", str(path))
    if not apply:
        return _config_pending(request, path, "llama_cpp.write_embeddings_entry", "Point openai_embeddings_plugin at the llama.cpp server")
    updated = [entry for entry in entries if not (isinstance(entry, dict) and entry.get("name") == _EMBEDDINGS_ENTRY)]
    updated.append(desired)
    runtime.atomic_write(path, json.dumps({"entries": updated}, indent=2) + "\n", mode=0o600)
    if _entry_named(_seed_entries(path) or [], _EMBEDDINGS_ENTRY) != desired:
        return _blocked(request, "llama_cpp_config_write_failed", "Address-book seed readback differs; inspect it before retrying.")
    return result(request, status="applied", retry_safe=True)


def configure_inference(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    return _inference_config(request, runtime, apply=request.phase == "apply")


def inference_config_valid(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    return _inference_config(request, runtime, apply=False)


def _inference_config(request: AdapterRequest, runtime: Runtime, *, apply: bool) -> JsonObject:
    path = request.target / _INFERENCE_CONFIG
    desired = inference_settings(load_registry())
    current = None if path.is_symlink() else read_json_object(path) if path.exists() else {}
    if current is None:
        return _blocked(request, "llama_cpp_config_conflict", f"{path} is not a readable plugin config; repair it, then retry.")
    if all(current.get(key) == value for key, value in desired.items()):
        return _verified(request, "llama_cpp_inference_config", "default_inference_plugin points at the llama.cpp server", str(path))
    if not apply:
        return _config_pending(request, path, "llama_cpp.write_inference_config", "Point default_inference_plugin at the llama.cpp server")
    runtime.atomic_write(path, json.dumps({**current, **desired}, indent=2, sort_keys=True) + "\n", mode=0o600)
    readback = read_json_object(path) or {}
    if not all(readback.get(key) == value for key, value in desired.items()):
        return _blocked(request, "llama_cpp_config_write_failed", "Plugin config readback differs; inspect it before retrying.")
    return result(request, status="applied", retry_safe=True)


def _config_pending(request: AdapterRequest, path: Path, action_id: str, title: str) -> JsonObject:
    return result(
        request,
        status="pending",
        actions=[planned_action(action_id=action_id, title=title, mutation_kind="config_write", target=str(path), evidence_ref=f"{action_id}_missing")],
        repair="Approve the reviewed llama.cpp plugin configuration.",
    )


# --------------------------------------------------------------------------- results and registry


def _verified(request: AdapterRequest, key: str, summary: str, observed: str) -> JsonObject:
    return result(
        request,
        status="verified",
        evidence_items=[evidence(evidence_id=key, kind="readiness", status="passed", summary=summary, observed=observed, expected=observed, source=request.operation_ref)],
    )


def _pending(request: AdapterRequest, key: str, repair: str) -> JsonObject:
    return result(
        request,
        status="pending",
        evidence_items=[evidence(evidence_id=key, kind="host", status="pending", summary=f"{key}: pending", observed=False, expected=True, source=request.operation_ref)],
        repair=repair,
    )


def _blocked(request: AdapterRequest, code: str, repair: str) -> JsonObject:
    return result(request, status="blocked", error_kind=code, retry_safe=False, exit_code=None, repair=repair)
