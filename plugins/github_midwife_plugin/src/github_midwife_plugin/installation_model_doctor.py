"""Shell and local-model installation qualification probes."""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
from pathlib import Path
from typing import cast

from ananta.core.orchestration.service_bindings import ServiceBindings, ServiceName
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import AnantaError
from ananta.services.embedding_service import EmbeddingService

from .installation_doctor import _boolean_probe, _command_probe, _evidence, blocked
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, public_string, result
from .setup_adapter_runtime import Runtime, read_json_object

__all__ = ("_platform_embedding_dimension",)

_INFERENCE_QUALIFICATION_TIMEOUT_SECONDS = 180


def shell_path(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    script = 'command -v "$1" && command -v "claude-$1" && command -v "codex-$1"'
    outcome = runtime.run(
        ("/bin/zsh", "-lic", script, "solet-path-probe", request.name),
        timeout_seconds=min(request.timeout_seconds, 15),
    )
    return _command_probe(
        request,
        outcome,
        "fresh_shell_path",
        "Re-run executable shell hydration, then open a fresh login shell.",
    )


def shell_python(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    python = request.target / ".venv/bin/python3"
    outcome = runtime.run((str(python), "--version"), timeout_seconds=5)
    supported = outcome.ok and re.search(r"Python 3\.13(?:\.|$)", outcome.stdout + outcome.stderr)
    return _boolean_probe(
        request,
        evidence_id="fresh_shell_python",
        ok=bool(supported),
        observed=bool(supported),
        source=str(python),
        repair="Repair the target venv and absolute hook interpreter bindings.",
    )


def model_discovery(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    decision_id = public_string(request, "decision_id")
    if decision_id not in {"embedding_model", "inference_model"}:
        return blocked(request, "operation_input_missing", "decision_id is required for discovery.")
    base_url = _model_base_url(request)
    if base_url is None:
        return blocked(
            request, "operation_input_missing", "The pinned LM Studio base URL is unavailable."
        )
    try:
        status, payload = runtime.http_json(f"{base_url.rstrip('/')}/models", timeout_seconds=10)
    except urllib.error.URLError:
        return blocked(
            request,
            "model_discovery_failed",
            "The model host became unavailable after provisioning. Restore its service, then resume the reviewed solet create command.",
        )
    except json.JSONDecodeError:
        return blocked(
            request,
            "model_discovery_invalid_response",
            "Repair the selected model service response, then resume.",
        )
    if status != 200:
        return blocked(
            request,
            "model_discovery_failed",
            "The model host became unavailable after provisioning. Restore its service, then resume the reviewed solet create command.",
        )
    models = _model_rows(payload)
    if models is None:
        return blocked(
            request,
            "model_discovery_invalid_response",
            "Repair the selected model service response, then resume.",
        )
    candidates = [
        _candidate(decision_id, model_id, index)
        for index, model_id in enumerate(models)
    ]
    return result(
        request,
        status="verified",
        candidates=candidates,
        evidence_items=[
            _evidence("model_candidates", True, f"{base_url}/models", len(models), 0)
        ],
    )


def ollama_discovery(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    del runtime
    return blocked(
        request,
        "ollama_discovery_unimplemented",
        "Ollama model discovery is not implemented; choose a supported model service.",
    )


def _model_base_url(request: AdapterRequest) -> str | None:
    explicit = public_string(request, "lm_studio_base_url")
    if explicit is not None:
        return explicit
    flow_path = (
        request.target / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
    )
    flow = read_json_object(flow_path)
    if flow is None:
        return None
    inputs = flow.get("inputs")
    if not isinstance(inputs, dict):
        return None
    definition = inputs.get("lm_studio_base_url")
    if not isinstance(definition, dict):
        return None
    default = definition.get("default")
    return default if isinstance(default, str) and default else None


def _materialized_inference_model(request: AdapterRequest) -> str | None:
    """Read the model written by the inference operation's apply phase."""

    config_path = (
        request.target / "profile/config/plugins/default_inference_plugin.json"
    )
    config = read_json_object(config_path)
    if config is None:
        return None
    model = config.get("model")
    return model if isinstance(model, str) and model else None


def _model_rows(payload: JsonValue) -> list[str] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    rows: list[str] = []
    for item in cast(list[JsonValue], payload["data"]):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        rows.append(cast(str, item["id"]))
    return sorted(set(rows))


def _platform_embedding_dimension(target: Path) -> int | None:
    """Resolve the bound provider's schema-shaping dimension declaration."""

    try:
        bindings = ServiceBindings(target / "profile")
        bindings.load()
        plugin_name = bindings.get_plugin_name(ServiceName.EMBEDDING_SERVICE)
        if plugin_name is None:
            return None
        manager = PluginManager()
        manager.discover_plugins(allowed_plugins={plugin_name})
        dimension = cast(
            object,
            EmbeddingService(
                plugin_manager=manager,
                embedding_plugin_name=plugin_name,
            ).get_default_dimensions(),
        )
    except (AnantaError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        return None
    return (
        dimension
        if isinstance(dimension, int) and not isinstance(dimension, bool) and dimension > 0
        else None
    )


def _candidate(
    decision_id: str,
    model_id: str,
    rank: int,
) -> JsonObject:
    metadata: JsonObject
    if decision_id == "embedding_model":
        metadata = {
            "provider": "lm_studio",
            "embedding_capable": None,
            "dimensions": None,
            "local_disk_bytes": None,
        }
    else:
        metadata = {
            "provider": "lm_studio",
            "context_tokens": None,
            "structured_output_support": None,
            "estimated_memory_bytes": None,
        }
    return {
        "decision_id": decision_id,
        "value": model_id[:256],
        "label": model_id[:256],
        "recommendation_rank": rank,
        "metadata": metadata,
    }


def embedding_qualification(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    candidate = public_string(request, "candidate_id")
    base_url = _model_base_url(request)
    if candidate is None or base_url is None:
        return blocked(
            request, "operation_input_missing", "candidate_id and base URL are required."
        )
    status, payload = runtime.http_json(
        f"{base_url.rstrip('/')}/embeddings",
        timeout_seconds=15,
        payload={"model": candidate, "input": ["qualification", "qualification"]},
    )
    repeatable, observed_dimension = _embedding_observation(payload)
    returned_candidate = _response_model_identity(payload)
    expected_dimension = _platform_embedding_dimension(request.target)
    valid = (
        status == 200
        and repeatable
        and returned_candidate == candidate
        and expected_dimension is not None
        and observed_dimension == expected_dimension
    )
    repair = _embedding_repair(
        requested_candidate=candidate,
        returned_candidate=returned_candidate,
        expected_dimension=expected_dimension,
        observed_dimension=observed_dimension,
    )
    return _boolean_probe(
        request,
        evidence_id="embedding_qualification",
        ok=valid,
        observed=valid,
        source=f"{base_url}/embeddings",
        repair=repair,
    )


def _embedding_repair(
    *,
    requested_candidate: str,
    returned_candidate: str | None,
    expected_dimension: int | None,
    observed_dimension: int | None,
) -> str:
    observed = str(observed_dimension) if observed_dimension is not None else "unavailable"
    returned = returned_candidate if returned_candidate is not None else "unavailable"
    if returned_candidate != requested_candidate:
        return (
            "Embedding response model mismatch: "
            f"requested public model ID {requested_candidate!r}; "
            f"returned public model ID {returned!r}. Load the requested embedding "
            "model in the local server and retry setup."
        )
    if expected_dimension is None:
        return (
            "Platform expected dimension is unavailable; "
            f"observed {observed}. Repair the bound embedding provider's "
            "get_default_dimensions() declaration, then retry setup."
        )
    if observed_dimension != expected_dimension:
        return (
            f"Embedding dimension mismatch: expected {expected_dimension}; "
            f"observed {observed}. Load or configure a model matching the bound "
            "provider's get_default_dimensions() declaration, then retry setup."
        )
    return (
        f"Expected two finite, repeatable {expected_dimension}-dimensional vectors; "
        f"observed {observed}. Repair the model response, then retry setup."
    )


def _response_model_identity(payload: JsonValue) -> str | None:
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None


def _embedding_observation(payload: JsonValue) -> tuple[bool, int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return False, None
    data = cast(list[JsonValue], payload["data"])
    vectors = [_embedding_vector(item) for item in data]
    observed_dimension = len(vectors[0]) if vectors and vectors[0] is not None else None
    valid = len(vectors) == 2 and None not in vectors and vectors[0] == vectors[1]
    return valid, observed_dimension


def _embedding_vector(item: JsonValue) -> list[float] | None:
    if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
        return None
    raw = cast(list[JsonValue], item["embedding"])
    if not raw or not all(_finite_number(value) for value in raw):
        return None
    return [float(cast(int | float, value)) for value in raw]


def _finite_number(value: JsonValue) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def inference_qualification(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    candidate = public_string(request, "candidate_id") or _materialized_inference_model(
        request
    )
    base_url = _model_base_url(request)
    if candidate is None or base_url is None:
        return blocked(
            request, "operation_input_missing", "candidate_id is required."
        )
    payload: JsonObject = {
        "model": candidate,
        "messages": [
            {
                "role": "user",
                "content": "What model is this?",
            }
        ],
        "temperature": 0,
        "max_tokens": 8,
    }
    source = f"{base_url}/chat/completions"
    started_at = time.monotonic()
    try:
        status, body = runtime.http_json(
            f"{base_url.rstrip('/')}/chat/completions",
            timeout_seconds=_INFERENCE_QUALIFICATION_TIMEOUT_SECONDS,
            payload=payload,
        )
    except TimeoutError:
        duration_ms = round((time.monotonic() - started_at) * 1000)
        return _boolean_probe(
            request,
            evidence_id="inference_qualification",
            ok=False,
            observed=False,
            source=source,
            repair=_inference_repair(candidate, None, timed_out=True),
            duration_ms=duration_ms,
            extra_evidence=_inference_timing_evidence(duration_ms, None),
            error_kind="inference_qualification_timed_out",
        )
    duration_ms = round((time.monotonic() - started_at) * 1000)
    returned_candidate = _response_model_identity(body)
    valid = status == 200 and returned_candidate == candidate
    return _boolean_probe(
        request,
        evidence_id="inference_qualification",
        ok=valid,
        observed=valid,
        source=source,
        repair=_inference_repair(
            candidate,
            returned_candidate,
            timed_out=status == 408,
        ),
        duration_ms=duration_ms,
        extra_evidence=_inference_timing_evidence(duration_ms, _reasoning_tokens(body)),
        error_kind=(
            "inference_model_identity_mismatch"
            if status == 200 and returned_candidate != candidate
            else None
        ),
    )


def _inference_repair(
    requested_candidate: str,
    returned_candidate: str | None,
    *,
    timed_out: bool,
) -> str:
    """Describe an inference qualification failure without accepting substitution."""

    if timed_out:
        return (
            "Inference qualification timed out before the response could be validated. Keep the "
            "requested inference model loaded and retry; a slow reasoning model may need the "
            "full qualification budget."
        )
    if returned_candidate != requested_candidate:
        returned = returned_candidate if returned_candidate is not None else "unavailable"
        return (
            "Inference response model mismatch: "
            f"requested public model ID {requested_candidate!r}; "
            f"returned public model ID {returned!r}. Load the requested inference "
            "model in the local server and retry setup."
        )
    return "Inference qualification request did not return HTTP 200; retry the local model service."


def _inference_timing_evidence(
    duration_ms: int, reasoning_tokens: int | None
) -> list[JsonObject]:
    evidence_items = [
        _evidence(
            "inference_qualification_elapsed_seconds",
            True,
            "monotonic:inference_qualification",
            duration_ms / 1000,
            _INFERENCE_QUALIFICATION_TIMEOUT_SECONDS,
        )
    ]
    if reasoning_tokens is not None:
        evidence_items.append(
            _evidence(
                "inference_qualification_reasoning_tokens",
                True,
                "response:usage.reasoning_tokens",
                reasoning_tokens,
                "reported by model response",
            )
        )
    return evidence_items


def _reasoning_tokens(payload: JsonValue) -> int | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    tokens = usage.get("reasoning_tokens")
    return tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else None
