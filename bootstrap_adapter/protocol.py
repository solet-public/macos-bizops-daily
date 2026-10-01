"""Frozen request/result protocol for the stdlib bootstrap adapter."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .homebrew import CommandOutcome
from .models import (
    PROBE_TIMEOUT_SECONDS,
    AdapterError,
    AdapterRequestError,
    AdapterRuntime,
)

Request = dict[str, Any]
Result = dict[str, Any]
Executor = Callable[[object], Result]

# The text rules the Manager's ``adapter_validation.public_string`` holds every envelope text to, enforced by ``public_text`` below for ``evidence``,
# ``planned_action`` and ``result``.  A twin of ``github_midwife_plugin.setup_adapter_contract.public_text`` (this package never imports the plugin);
# ``bootstrap_public_text_smoke`` drives both through the real validator and proves they agree (iss_67472e3f).
_SECRET_SHAPED = (
    re.compile(r"(?i)(password|secret|token|authorization|oauth[_ -]?code|private[_ -]?key)\s*[:=]\s*(?:bearer\s+)*\S+"),
    re.compile(r"(?i)bearer(?:\s+bearer)*\s+[A-Za-z0-9._~+/-]+"),
)
_FORMULA_MARKER = "/Cellar/solet/"
_REDACTED = "[REDACTED]"
_KEG_PATH = "[keg path]"
_EMPTY = "[empty]"
_WITHHELD = "[withheld]"
_STABLE_PASSES = 8
_WIDE_TEXT_LIMIT = 2048
_TAIL_SHARE = 3  # of every 5 kept characters: the remedy is written last
#: This adapter never cut a repair; it now holds one to the Manager's own maximum.
REPAIR_LIMIT = 2048


def neutralized(text: str) -> str:
    """``text`` with every secret-shaped run and formula-keg path replaced, stable under a second pass (what the Manager re-checks)."""
    for _ in range(_STABLE_PASSES):
        cleaned = text
        for pattern in _SECRET_SHAPED:
            cleaned = pattern.sub(_REDACTED, cleaned)
        cleaned = cleaned.replace(_FORMULA_MARKER, _KEG_PATH)
        if cleaned == text:
            return text
        text = cleaned
    return _WITHHELD


def _fitted(text: str, limit: int) -> str:
    """``text`` unchanged when it fits ``limit`` characters, else its head and its tail around a marker that says how long it was."""
    if len(text) <= limit:
        return text
    marker = f" [... {len(text)} characters, middle cut ...] "
    kept = limit - len(marker)
    if kept < 2:
        return text[:limit]
    tail = kept * _TAIL_SHARE // 5
    return text[: kept - tail] + marker + text[len(text) - tail :]


def public_text(text: str, limit: int) -> str:
    """``text`` as the Manager's ``public_string`` accepts it for a field of at most ``limit`` characters; a text that already does is returned as it is."""
    current = text or _EMPTY
    for _ in range(_STABLE_PASSES):
        cleaned = neutralized(current)
        cleaned = _fitted(cleaned, limit if cleaned.isascii() else min(limit, _WIDE_TEXT_LIMIT))
        if cleaned == current:
            return current
        current = cleaned
    return _WITHHELD


def _public_value(value: bool | int | float | str | list[str] | None, limit: int) -> bool | int | float | str | list[str] | None:
    """An evidence ``observed``/``expected`` value with every string made public; a list keeps its order and drops the entries that became equal."""
    if isinstance(value, str):
        return public_text(value, limit)
    if isinstance(value, list):
        return list(dict.fromkeys(public_text(item, limit) for item in value))
    return value


CREATE_FLOW_ID = "macos.repository_setup"
EXISTING_INSTALL_FLOW_ID = "existing-install"
EXISTING_INSTALL_REF_PREFIX = "existing::"
#: Closed two-member flow set with the operation-ref cross-check (existing-
#: install design section 3.3, held identically on all three validators).
#: The value says whether the flow's callables carry the ``existing::``
#: vocabulary.  Never an open string.
FLOW_OPERATION_PAIRING: dict[str, bool] = {
    CREATE_FLOW_ID: False,
    EXISTING_INSTALL_FLOW_ID: True,
}
REQUEST_KEYS = {
    "protocol_version",
    "kind",
    "request_id",
    "operation_id",
    "operation_ref",
    "phase",
    "probe_purpose",
    "attempt",
    "name",
    "target",
    "flow_id",
    "flow_source_revision",
    "answers_fingerprint",
    "approval_fingerprint",
    "dry_run",
    "timeout_seconds",
    "public_inputs",
}
PROBE_PURPOSES = {
    "preview",
    "pre_apply",
    "post_apply",
    "completion",
    "decision_discovery",
    "decision_qualification",
    "stage_entry",
    "stage_exit",
}
EMPTY_ACTION_PURPOSES = {
    "post_apply",
    "completion",
    "decision_discovery",
    "decision_qualification",
    "stage_entry",
    "stage_exit",
}

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_CALLABLE_REF = re.compile(r"^[a-z][a-z0-9_]*::[a-z][a-z0-9_.]*$")
_INPUT_KEY = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SECRET_FIELD = re.compile(
    r"password|secret|token|credential|private_key|oauth_code",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _closed_mapping(raw: object) -> Request:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise AdapterRequestError("adapter stdin must contain one JSON object")
    if set(raw) != REQUEST_KEYS:
        raise AdapterRequestError("request does not match the closed v1 field set")
    return raw


def _validate_protocol_identity(request: Request) -> None:
    if request["protocol_version"] != 1 or request["kind"] != "operation_request":
        raise AdapterRequestError("request protocol identity is invalid")
    try:
        uuid.UUID(str(request["request_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AdapterRequestError("request_id must be a UUID") from exc
    operation_id = request["operation_id"]
    operation_ref = request["operation_ref"]
    if not isinstance(operation_id, str) or _IDENTIFIER.fullmatch(operation_id) is None:
        raise AdapterRequestError("operation_id does not match the closed grammar")
    if not isinstance(operation_ref, str) or _CALLABLE_REF.fullmatch(operation_ref) is None:
        raise AdapterRequestError("operation_ref does not match the closed grammar")


def _validate_name_and_target(request: Request) -> None:
    name = request["name"]
    target = request["target"]
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise AdapterRequestError("name does not match the closed envelope grammar")
    if not isinstance(target, str) or len(target) < 2 or not Path(target).is_absolute():
        raise AdapterRequestError("target must be an absolute path")


def _validate_flow_identity(request: Request) -> None:
    revision = request["flow_source_revision"]
    answers = request["answers_fingerprint"]
    flow_id = request["flow_id"]
    if not isinstance(flow_id, str) or flow_id not in FLOW_OPERATION_PAIRING:
        raise AdapterRequestError("flow_id is invalid")
    if str(request["operation_ref"]).startswith(EXISTING_INSTALL_REF_PREFIX) != FLOW_OPERATION_PAIRING[flow_id]:
        raise AdapterRequestError("operation_ref vocabulary does not match flow_id")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise AdapterRequestError("flow_source_revision is invalid")
    if not isinstance(answers, str) or _FINGERPRINT.fullmatch(answers) is None:
        raise AdapterRequestError("answers_fingerprint is invalid")


def _validate_bounds(request: Request) -> None:
    attempt = request["attempt"]
    timeout = request["timeout_seconds"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise AdapterRequestError("attempt must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise AdapterRequestError("timeout_seconds is outside the closed bound")


def _validate_public_value(value: object, label: str) -> None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if len(value) != len(set(value)):
            raise AdapterRequestError(f"{label} contains duplicate array values")
        return
    raise AdapterRequestError(f"{label} is not a closed public value")


def _validate_public_inputs(request: Request) -> None:
    inputs = request["public_inputs"]
    if not isinstance(inputs, dict) or not all(isinstance(key, str) for key in inputs):
        raise AdapterRequestError("public_inputs must be one object")
    for key, value in inputs.items():
        if _INPUT_KEY.fullmatch(key) is None or _SECRET_FIELD.search(key):
            raise AdapterRequestError("public_inputs contains a forbidden field")
        _validate_public_value(value, f"public input {key!r}")


def _validate_phase(request: Request) -> None:
    phase = request["phase"]
    purpose = request["probe_purpose"]
    approval = request["approval_fingerprint"]
    dry_run = request["dry_run"]
    if phase == "probe":
        if purpose not in PROBE_PURPOSES or dry_run is not True or approval is not None:
            raise AdapterRequestError(
                "probe request requires a declared purpose, dry_run true, and null approval",
            )
        return
    if phase != "apply":
        raise AdapterRequestError("phase must be probe or apply")
    if purpose is not None or dry_run is not False:
        raise AdapterRequestError("apply request requires null purpose and dry_run false")
    if not isinstance(approval, str) or _FINGERPRINT.fullmatch(approval) is None:
        raise AdapterRequestError("apply request requires a well-formed approval fingerprint")


_LM_SERVED_REFS = frozenset(f"setup::lm_studio.{suffix}" for suffix in (
    "load_embedding", "load_inference", "embedding_model_served", "inference_model_served",
))

_LM_STUDIO_PUBLIC_INPUTS = frozenset({"embeddings_implementation", "inference_implementation", "lm_studio_base_url"})


def _validate_lm_deadline_input(inputs: dict[str, object]) -> None:
    if "lm_studio_parent_deadline_ns" not in inputs:
        return
    deadline = inputs["lm_studio_parent_deadline_ns"]
    if type(deadline) is not int or deadline <= 0:
        raise AdapterRequestError("LM Studio parent deadline must be a positive integer")


def validate_route_inputs(request: Request, *, lm_studio_operation_ids: frozenset[str], existing_ref: str) -> None:
    """Refuse public inputs outside each route's closed registry.

    The existing-install dependency route (``existing_ref``) accepts exactly
    ``declared_closure``; the create-flow routes keep their per-operation sets.
    """
    inputs = request["public_inputs"]
    if request["operation_ref"] == existing_ref:
        allowed: set[str] = {"declared_closure"}
    elif request["operation_id"] in lm_studio_operation_ids:
        allowed = set(_LM_STUDIO_PUBLIC_INPUTS)
        if request["operation_ref"] in _LM_SERVED_REFS:
            allowed.add("lm_studio_parent_deadline_ns")
            _validate_lm_deadline_input(inputs)
    else:
        allowed = {"solet_name"} if request["operation_id"] == "configure_postgresql" else set()
    if set(inputs) - allowed:
        raise AdapterRequestError("operation public_inputs are outside the closed registry")
    if "solet_name" in inputs and inputs["solet_name"] != request["name"]:
        raise AdapterRequestError("solet_name public input differs from request identity")


def validate_request(raw: object) -> Request:
    request = _closed_mapping(raw)
    _validate_protocol_identity(request)
    _validate_name_and_target(request)
    _validate_flow_identity(request)
    _validate_bounds(request)
    _validate_public_inputs(request)
    _validate_phase(request)
    return request


def captured_at(runtime: AdapterRuntime) -> str:
    value = runtime.now()
    if value.tzinfo is None:
        raise AdapterError("adapter clock returned a naive datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def evidence(
    runtime: AdapterRuntime,
    *,
    evidence_id: str,
    kind: str,
    status: str,
    summary: str,
    observed: bool | int | float | str | list[str] | None,
    expected: bool | int | float | str | list[str] | None,
    source: str,
) -> dict[str, Any]:
    public_observed = _public_value(observed, 4096)
    canonical = json.dumps(public_observed, sort_keys=True, separators=(",", ":"))
    return {
        "id": evidence_id,
        "kind": kind,
        "status": public_text(status, 512),
        "summary": public_text(summary, 512),
        "observed": public_observed,
        "expected": _public_value(expected, 4096),
        "source": public_text(source, 512),
        "digest": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        "captured_at": captured_at(runtime),
        "sensitivity": "public",
    }


def result(
    request: Mapping[str, Any],
    *,
    status: str,
    error_kind: str | None = None,
    retry_safe: bool = True,
    planned_actions: Sequence[dict[str, Any]] = (),
    evidence_items: Sequence[dict[str, Any]] = (),
    reason: dict[str, Any] | None = None,
    repair: str | None = None,
    duration_ms: int = 0,
) -> Result:
    purpose = request.get("probe_purpose")
    actions = list(planned_actions)
    if request.get("phase") == "apply" or purpose in EMPTY_ACTION_PURPOSES:
        actions = []
    error_status = status in {"awaiting_user", "blocked", "failed"}
    if error_status != (error_kind is not None):
        raise AdapterError("checkpoint status and error_kind disagree")
    return {
        "protocol_version": 1,
        "kind": "operation_result",
        "request_id": request["request_id"],
        "operation_id": request["operation_id"],
        "phase": request["phase"],
        "probe_purpose": purpose,
        "checkpoint_status": status,
        "error_kind": error_kind,
        "retry_safe": retry_safe,
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": max(0, duration_ms),
        "stdout": "",
        "stderr": "",
        "planned_actions": actions,
        "discovered_candidates": [],
        "evidence": list(evidence_items),
        "reason": reason,
        "repair": None if repair is None else public_text(repair, REPAIR_LIMIT),
    }


def command_failure_result(
    request: Mapping[str, Any],
    *,
    error_kind: str,
    evidence_items: Sequence[dict[str, Any]],
    repair: str,
    outcome: CommandOutcome,
) -> Result:
    """Render a required-command receipt without falling back to default fields."""

    reason = (
        None
        if outcome.returncode == 0 and not outcome.timed_out
        else {
            "outcome_class": "timeout" if outcome.timed_out else "nonzero_exit",
            "exit_code": outcome.returncode,
            "duration_ms": outcome.duration_ms,
            "timed_out": outcome.timed_out,
            "stdout_bytes": len(outcome.stdout.encode()),
            "stderr_bytes": len(outcome.stderr.encode()),
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    )
    failed = result(
        request,
        status="failed",
        error_kind=error_kind,
        retry_safe=True,
        evidence_items=evidence_items,
        repair=repair,
        duration_ms=outcome.duration_ms,
        reason=reason,
    )
    failed.update(
        exit_code=outcome.returncode,
        timed_out=outcome.timed_out,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
    )
    return failed


def protocol_error_result(raw: object, message: str) -> Result:
    if not isinstance(raw, dict):
        raise AdapterRequestError(message)
    required_echo = {"request_id", "operation_id", "phase", "probe_purpose"}
    if not required_echo.issubset(raw) or raw.get("phase") not in {"probe", "apply"}:
        raise AdapterRequestError(message)
    request = dict(raw)
    if request["phase"] == "apply":
        request["probe_purpose"] = None
    return result(
        request,
        status="blocked",
        error_kind="adapter_protocol_error",
        retry_safe=False,
        repair="Correct the closed adapter request and retry.",
    )


def planned_action(
    action_id: str,
    title: str,
    mutation_kind: str,
    target: str,
    evidence_ref: str,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "title": public_text(title, 256),
        "mutation_kind": mutation_kind,
        "target": public_text(target, 512),
        "requires_confirmation": True,
        "condition_or_evidence_ref": public_text(evidence_ref, 256),
    }


def run_public(
    runtime: AdapterRuntime,
    command: list[str],
    *,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return runtime.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
            input=input,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


_HOMEBREW_CANDIDATES = (
    "/opt/homebrew/bin/brew",
    "/usr/local/bin/brew",
)
_HOMEBREW_BIN_DIRECTORIES = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
)
# Claude Code's native installer puts ``claude`` here, which a launchd job's or solet session's PATH lacks (iss_26fdde33).
_NATIVE_BIN_RELATIVE = Path(".local") / "bin"


def _brew_candidate_works(runtime: AdapterRuntime, candidate: str) -> bool:
    if not Path(candidate).is_absolute():
        return False
    completed = run_public(runtime, [candidate, "--version"])
    return completed is not None and completed.returncode == 0


def resolve_brew_executable(runtime: AdapterRuntime) -> str | None:
    """Resolve Homebrew through PATH first, then validated standard locations."""
    candidates = (runtime.which("brew"), *_HOMEBREW_CANDIDATES)
    for candidate in dict.fromkeys(item for item in candidates if item is not None):
        if _brew_candidate_works(runtime, candidate):
            return candidate
    return None


def resolve_executable(runtime: AdapterRuntime, executable_name: str) -> str | None:
    """Resolve an executable through PATH, the standard Homebrew bin directories, then ``$HOME/.local/bin``."""

    directories = (*_HOMEBREW_BIN_DIRECTORIES, str(Path.home() / _NATIVE_BIN_RELATIVE))
    candidates = (
        runtime.which(executable_name),
        *(runtime.which(f"{directory}/{executable_name}") for directory in directories),
    )
    for candidate in candidates:
        if candidate is not None and Path(candidate).is_absolute():
            return candidate
    return None


def operation_adapter_main(executor: Executor) -> int:
    """Read one UTF-8 request and emit exactly one compact JSON result."""

    try:
        raw_bytes = sys.stdin.buffer.read()
        raw_text = raw_bytes.decode("utf-8")
        decoder = json.JSONDecoder()
        raw, end = decoder.raw_decode(raw_text)
        if raw_text[end:].strip():
            raise AdapterRequestError("adapter stdin contains more than one JSON value")
        with contextlib.redirect_stdout(io.StringIO()):
            adapter_result = executor(raw)
    except (UnicodeError, json.JSONDecodeError, AdapterRequestError):
        print("adapter request was not one valid closed UTF-8 JSON object", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(adapter_result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0
