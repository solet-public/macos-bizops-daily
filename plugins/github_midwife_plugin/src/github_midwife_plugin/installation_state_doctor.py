"""Session, router, peer, knowledge, and journal qualification probes."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

from ananta.core.plugins.profile_manifest import load_manifest_plugin_set

from .installation_doctor import (
    _boolean_probe,
    _call_payload,
    _call_succeeded,
    _merged_call_payload,
    _solet_call,
    nonempty_process_probe,
    truncated_solet_call_output,
)
from .setup_adapter_contract import (
    AdapterRequest,
    JsonObject,
    JsonValue,
    evidence,
    result,
)
from .setup_adapter_runtime import CommandOutcome, Runtime, read_json_object

# The register operation whose approved roots back each ``session_sources``
# decision option, and the ledger ``source_kind`` that option's exit-probe row
# is measured over. The second map mirrors
# ``session_ledger_service.selected_sources._SOURCE_SPECS``; the drift guard
# that keeps the two honest lives in ``flow_precondition_wiring_smoke``.
_SESSION_SOURCE_OPERATION_REFS = {
    "codex_local": "hydration::sessions.register_codex_filesystem",
    "claude_code_local": "hydration::sessions.register_claude_filesystem",
}
_QUALIFIED_SOURCE_KINDS = {
    "codex_local": "codex_local",
    "claude_code_local": "claude_code_local",
}
_KNOWLEDGE_LAUNCH_RESULT_RESERVE_SECONDS = 5
_KNOWLEDGE_READINESS_POLL_INITIAL_SECONDS = 0.5
_KNOWLEDGE_READINESS_POLL_MAX_SECONDS = 5.0
_KNOWLEDGE_RESULT_REQUIRED_FIELDS = frozenset(
    {"content", "knowledge_base", "file_path", "score", "tier", "memory_id"}
)


def partition_session_roots(roots: list[Path]) -> tuple[list[Path], list[Path]]:
    """Split approved roots into ``(absent, unreadable)`` without conflating them.

    ``os.access`` answers ``False`` for ENOENT exactly as it does for EACCES, so
    a root a fresh target has simply never created is indistinguishable from one
    the user denied unless existence is checked separately. Only the second is a
    permission problem, and only the second is repairable by the declared
    remediation: ``open_files_permissions_settings`` opens a macOS privacy pane,
    which cannot create a directory that does not exist.
    """

    absent = [path for path in roots if not path.exists()]
    unreadable = [path for path in roots if path.exists() and not os.access(path, os.R_OK)]
    return absent, unreadable


def _absent_root_evidence(source_kind: str, absent: list[Path]) -> list[JsonObject] | None:
    """Record absent roots under their own kind, never as a permission denial.

    An absent root is not a blocker: the Codex and Claude CLIs create these trees
    on first use, and the ingest layer already treats a not-yet-existing root as
    empty rather than broken (``vendor/codex.py`` early-returns on it, and
    ``normalize_root_uri`` stays lexical for exactly this reason). Emitting the
    evidence keeps the green truthful — the probe reports that no root is
    unreadable, not that every root is present.
    """

    if not absent:
        return None
    return [
        evidence(
            evidence_id=f"{source_kind}_session_roots_absent",
            kind="behavior",
            status="verified",
            summary=(
                f"{source_kind} session roots are not created until the agent CLI "
                "first runs; nothing to read and nothing to repair"
            ),
            observed=[str(path) for path in absent],
            expected=True,
            source="filesystem:approved_roots",
        )
    ]


def session_roots(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    from .setup_operations import _SESSION_ROOTS

    source_id = (
        "hydration::sessions.register_codex_filesystem"
        if "codex" in request.operation_ref
        else "hydration::sessions.register_claude_filesystem"
    )
    roots = [runtime.home / relative for _kind, relative in _SESSION_ROOTS[source_id]]
    source_kind = "codex" if "codex" in request.operation_ref else "claude"
    absent, unreadable = partition_session_roots(roots)
    return _boolean_probe(
        request,
        f"{source_kind}_session_roots_unreadable",
        not unreadable,
        [str(path) for path in roots],
        "filesystem:approved_roots",
        "Approve and expose only readable current-user session roots.",
        extra_evidence=_absent_root_evidence(source_kind, absent),
        error_kind=f"{source_kind}_session_roots_unreadable",
    )


def session_retrieval(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    outcome = _solet_call(
        request,
        runtime,
        "service_interface::session_ledger_service::qualify_selected_sources",
        {
            "target": str(request.target),
            "name": request.name,
            "answers_fingerprint": request.answers_fingerprint,
        },
    )
    if outcome.stdout_truncated or outcome.stderr_truncated:
        return truncated_solet_call_output(
            request,
            "service_interface::session_ledger_service::qualify_selected_sources",
            outcome,
        )
    payload = _merged_call_payload(outcome)
    empty_sources = _sources_with_absent_roots(runtime)
    valid = _selected_sources_qualification_valid(payload, empty_sources)
    return _boolean_probe(
        request,
        "selected_session_sources",
        valid,
        _selected_sources_observation(payload, empty_sources),
        "service_interface::session_ledger_service::qualify_selected_sources",
        "Repair manager-answer identity, source registration, backfill, and retrieval proof.",
    )


def _sources_with_absent_roots(runtime: Runtime) -> frozenset[str]:
    """Decision options whose qualified ledger root does not exist on this host.

    A target whose agent CLI has never run has nothing to ingest, so this stage's
    exit demand for ``backfill_count > 0`` is unsatisfiable there for a reason
    that is not a failure — the same fresh-target shape the entry probe stopped
    treating as a permission denial. Measured, never assumed: only a root proven
    absent excuses an empty backfill, so a root that exists and still yields
    nothing keeps blocking, because that one really can be a broken ingest.
    """

    from .setup_operations import _SESSION_ROOTS

    absent: set[str] = set()
    for source, operation_ref in _SESSION_SOURCE_OPERATION_REFS.items():
        qualified_kind = _QUALIFIED_SOURCE_KINDS[source]
        roots = [
            runtime.home / relative
            for kind, relative in _SESSION_ROOTS[operation_ref]
            if kind == qualified_kind
        ]
        if roots and all(not path.exists() for path in roots):
            absent.add(source)
    return frozenset(absent)


def _selected_sources_qualification_valid(
    payload: JsonObject,
    empty_sources: frozenset[str],
) -> bool:
    if payload.get("target_identity_matched") is not True:
        return False
    if payload.get("answers_fingerprint_matched") is not True:
        return False
    sources = payload.get("sources")
    return isinstance(sources, list) and bool(sources) and all(
        _selected_source_row_valid(source, empty_sources) for source in sources
    )


def _selected_source_row_valid(source: JsonValue, empty_sources: frozenset[str]) -> bool:
    """Require measured registration and retrieval for each consented selection."""

    if not isinstance(source, dict):
        return False
    selected = source.get("selected")
    consented = source.get("consented")
    if not isinstance(selected, bool) or not isinstance(consented, bool):
        return False
    if not (selected and consented):
        return True
    if source.get("registered") is not True:
        return False
    if source.get("source") in empty_sources:
        return True
    backfill_count = source.get("backfill_count")
    return (
        isinstance(backfill_count, int)
        and backfill_count > 0
        and source.get("retrieval_ok") is True
    )


def _selected_sources_observation(
    payload: JsonObject,
    empty_sources: frozenset[str],
) -> list[str]:
    sources = payload.get("sources")
    bounded_sources: list[JsonValue] = []
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            name = source.get("source")
            if not isinstance(name, str):
                continue
            bounded_sources.append(
                {
                    "source": name,
                    "selected": source.get("selected") is True,
                    "consented": source.get("consented") is True,
                    "registered": source.get("registered") is True,
                    "backfill_count": source.get("backfill_count")
                    if isinstance(source.get("backfill_count"), int)
                    else 0,
                    "retrieval_ok": source.get("retrieval_ok") is True,
                    "roots_absent": name in empty_sources,
                }
            )
    observed: JsonObject = {
        "record_source": payload.get("record_source")
        if isinstance(payload.get("record_source"), str)
        else "unknown",
        "target_identity_matched": payload.get("target_identity_matched") is True,
        "answers_fingerprint_matched": payload.get("answers_fingerprint_matched") is True,
        "qualification_reason": payload.get("qualification_reason")
        if isinstance(payload.get("qualification_reason"), str)
        else None,
        "sources": bounded_sources,
    }
    return [
        f"{key}={json.dumps(value, sort_keys=True, separators=(',', ':'))}"
        for key, value in sorted(observed.items())
    ]


def router(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    router_selected = _router_selected(request)
    if router_selected is False:
        return result(request, status="not_applicable")
    if router_selected is None:
        return _boolean_probe(
            request,
            "router_identity",
            False,
            "invalid_profile_manifest",
            "profile/config/manifest.yaml",
            "Repair the generated profile manifest before checking router readiness.",
        )
    outcome = _solet_call(
        request,
        runtime,
        "service_interface::local_self_deployment_service::swap_status",
        {},
    )
    if outcome.stdout_truncated or outcome.stderr_truncated:
        return truncated_solet_call_output(
            request,
            "service_interface::local_self_deployment_service::swap_status",
            outcome,
        )
    transport_error_kind = _router_transport_error_kind(outcome)
    if transport_error_kind is not None:
        return _boolean_probe(
            request,
            "router_identity",
            False,
            transport_error_kind,
            "service_interface::local_self_deployment_service::swap_status",
            "Restore router reachability before checking its active identity.",
            duration_ms=outcome.duration_ms,
            error_kind=transport_error_kind,
        )
    payload = _merged_call_payload(outcome)
    router = payload.get("router_status")
    if _router_mgmt_unreachable(router):
        return _boolean_probe(
            request,
            "router_identity",
            False,
            "router_mgmt_unreachable",
            "service_interface::local_self_deployment_service::swap_status",
            "Restore router management-socket reachability before checking its active identity.",
            duration_ms=outcome.duration_ms,
            error_kind="router_mgmt_unreachable",
        )
    active = router.get("active_instance_id") if isinstance(router, dict) else None
    valid = _router_identity_valid(router)
    return _boolean_probe(
        request,
        "router_identity",
        valid,
        _bounded_router_instance_id(active),
        "service_interface::local_self_deployment_service::swap_status",
        "Repair router activation until the active identity belongs to this target.",
    )


def _router_transport_error_kind(outcome: CommandOutcome) -> str | None:
    if not outcome.ok:
        return "router_unreachable"
    if _merged_call_payload(outcome):
        return None
    if "503" in outcome.stdout or "503" in outcome.stderr:
        return "router_transport_503"
    return "router_invalid_response"


def _router_mgmt_unreachable(router: object) -> bool:
    return isinstance(router, dict) and isinstance(router.get("error"), str)


def _router_selected(request: AdapterRequest) -> bool | None:
    try:
        plugins = load_manifest_plugin_set(request.target / "profile")
    except (OSError, ValueError):
        return None
    if plugins is None:
        return None
    return "macos_self_deployment_plugin" in plugins


def _router_identity_valid(router: object) -> bool:
    """Validate the router's canonical active binding without target-name matching."""

    if not isinstance(router, dict):
        return False
    active_color = router.get("active_color")
    active_instance_id = router.get("active_instance_id")
    colors = router.get("colors")
    if not isinstance(active_color, str) or not isinstance(active_instance_id, str):
        return False
    if not _router_active_values_valid(active_color, active_instance_id):
        return False
    if not isinstance(colors, list):
        return False
    return _router_active_roster_matches(colors, active_color, active_instance_id)


def _router_active_values_valid(active_color: object, active_instance_id: object) -> bool:
    if not isinstance(active_color, str) or active_color not in {"blue", "green"}:
        return False
    if not isinstance(active_instance_id, str):
        return False
    return active_instance_id == _canonical_router_instance_id(active_color, active_instance_id)


def _bounded_router_instance_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _canonical_router_instance_id("blue", value) or _canonical_router_instance_id(
        "green", value
    )


def _canonical_router_instance_id(color: str, value: str) -> str | None:
    return value if re.fullmatch(rf"solet-{color}-[0-9a-f]{{8}}", value) is not None else None


def _router_active_roster_matches(
    colors: list[object], active_color: str, active_instance_id: str
) -> bool:
    active_rows: list[dict[str, object]] = []
    for entry in colors:
        if not _router_roster_row_valid(entry):
            return False
        if entry["status"] == "active":
            active_rows.append(entry)
    return len(active_rows) == 1 and (
        active_rows[0].get("color") == active_color
        and active_rows[0].get("instance_id") == active_instance_id
    )


def _router_roster_row_valid(entry: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(entry, dict):
        return False
    color = entry.get("color")
    instance_id = entry.get("instance_id")
    status = entry.get("status")
    return (
        isinstance(color, str)
        and isinstance(instance_id, str)
        and status in {"active", "inactive"}
        and _router_active_values_valid(color, instance_id)
    )


def peer_identity(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    outcome = _solet_call(request, runtime, "plugin::agent_messaging_plugin::peer_identity", {})
    if outcome.stdout_truncated or outcome.stderr_truncated:
        return truncated_solet_call_output(
            request,
            "plugin::agent_messaging_plugin::peer_identity",
            outcome,
        )
    payload = _call_payload(outcome)
    valid = (
        _call_succeeded(outcome)
        and isinstance(payload.get("bridge_identity"), str)
        and _launcher_identity_valid(request, _selected_coding_agents(request))
        and _named_cli_valid(request, runtime)
    )
    return _boolean_probe(
        request,
        "peer_identity",
        valid,
        valid,
        "plugin::agent_messaging_plugin::peer_identity",
        "Restore a successful local bridge response with a string identity, then re-probe.",
    )


def _launcher_identity_valid(
    request: AdapterRequest,
    selected: tuple[str, ...] | None,
) -> bool:
    if selected is None:
        return False
    launchers = {
        "claude_code": (request.target / "client/bin" / f"claude-{request.name}", (
            f'SOLET_NAME="{request.name}"',
            "AGENT_SESSION_ID=",
        )),
        "codex": (request.target / "client/bin" / f"codex-{request.name}", (
            f'export SOLET_NAME="{request.name}"',
            "export AGENT_SESSION_ID=",
        )),
    }
    return all(
        all(marker in _read_text(launchers[agent][0]) for marker in launchers[agent][1])
        for agent in selected
    )


def _selected_coding_agents(request: AdapterRequest) -> tuple[str, ...] | None:
    value = request.public_inputs.get("selected_coding_agents")
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(item, str) and item in {"codex", "claude_code"}
            for item in value
        )
        or len(value) != len(set(value))
    ):
        return None
    return tuple(item for item in value if isinstance(item, str))


def _named_cli_valid(request: AdapterRequest, runtime: Runtime) -> bool:
    launcher = runtime.home / ".local/bin" / request.name
    try:
        return launcher.resolve(strict=True) == (request.target / ".venv/bin/solet-bridge").resolve(
            strict=True
        )
    except OSError:
        return False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def knowledge(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    if request.probe_purpose == "stage_exit":
        return _wait_for_knowledge_retrieval(request, runtime)
    return _knowledge_probe(request, runtime)


def _wait_for_knowledge_retrieval(
    request: AdapterRequest,
    runtime: Runtime,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> JsonObject:
    """Poll the measured retrieval result until indexing is observable or exhausted.

    Bridge health proves only that the target is reachable.  The completion
    boundary additionally requires a corpus result, so an empty *successful*
    retrieval is retried with bounded backoff inside the already-approved probe
    timeout. Transport and protocol failures remain immediate failures.
    """

    started = monotonic()
    deadline = started + request.timeout_seconds - _KNOWLEDGE_LAUNCH_RESULT_RESERVE_SECONDS
    delay = _KNOWLEDGE_READINESS_POLL_INITIAL_SECONDS
    attempts = 0
    while True:
        remaining = deadline - monotonic()
        call_timeout = min(60, math.floor(remaining))
        if call_timeout < 1:
            response = _knowledge_timeout_failure(request, attempts, remaining)
            terminal = "budget_exhausted"
            break
        outcome = _knowledge_call(request, runtime, timeout_seconds=call_timeout)
        attempts += 1
        remaining = deadline - monotonic()
        if outcome.stdout_truncated or outcome.stderr_truncated:
            response = truncated_solet_call_output(
                request, "service_interface::knowledge_service::search", outcome
            )
            terminal = "output_truncated"
            break
        state = _knowledge_state(outcome)
        if state not in {"empty", "nonempty"}:
            response = _knowledge_probe_result(request, outcome, attempts)
            terminal = str(response.get("error_kind"))
            break
        if remaining <= 0:
            response = _knowledge_timeout_failure(request, attempts, remaining)
            terminal = "late_result"
            break
        if state == "nonempty":
            response = _knowledge_probe_result(request, outcome, attempts)
            terminal = "nonempty"
            break
        sleep(min(delay, remaining))
        delay = min(delay * 2, _KNOWLEDGE_READINESS_POLL_MAX_SECONDS)
    _knowledge_timing(response, request, attempts, monotonic() - started, terminal)
    return response


def _knowledge_timing(
    response: JsonObject,
    request: AdapterRequest,
    attempts: int,
    elapsed: float,
    terminal: str,
) -> None:
    items = response.get("evidence")
    if isinstance(items, list):
        items.append(evidence(
            evidence_id="knowledge_readiness_timing",
            kind="behavior",
            status=str(response["checkpoint_status"]),
            summary="Bounded retrieval readiness timing; does not prove all KBs hydrated.",
            observed=[
                f"attempts={attempts}",
                f"elapsed_seconds={elapsed:.6f}",
                f"parent_seconds={request.timeout_seconds}",
                f"launch_result_reserve_seconds={_KNOWLEDGE_LAUNCH_RESULT_RESERVE_SECONDS}",
                f"terminal={terminal}",
            ],
            expected="canonical nonempty retrieval before inner deadline",
            source="monotonic:knowledge_stage_exit",
        ))


def _knowledge_probe(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    outcome = _knowledge_call(request, runtime)
    if outcome.stdout_truncated or outcome.stderr_truncated:
        return truncated_solet_call_output(
            request,
            "service_interface::knowledge_service::search",
            outcome,
        )
    return _knowledge_probe_result(request, outcome, 1)


def _knowledge_call(
    request: AdapterRequest,
    runtime: Runtime,
    *,
    timeout_seconds: int | None = None,
) -> CommandOutcome:
    outcome = _solet_call(
        request,
        runtime,
        "service_interface::knowledge_service::search",
        {"query": "session start orientation", "top_k": 1},
        timeout_seconds=timeout_seconds,
    )
    return outcome


def _knowledge_state(outcome: CommandOutcome) -> str:
    if outcome.executable_missing or outcome.launch_error:
        return "failed"
    if not _call_succeeded(outcome) or not _knowledge_envelope_valid(outcome):
        return "failed"
    payload = _call_payload(outcome)
    data_shape = (payload.get("count"), payload.get("results"))
    if not _valid_knowledge_payload(data_shape):
        return "malformed"
    count, results = data_shape
    return "nonempty" if count > 0 or bool(results) else "empty"



def _knowledge_envelope_valid(outcome: CommandOutcome) -> bool:
    """Reject contradictory transport/service status even with a success payload."""
    raw = json.loads(outcome.stdout)
    if not isinstance(raw, dict):
        return False
    outer = raw.get("result")
    if not isinstance(outer, dict):
        return False
    return (
        raw.get("status") in (None, "completed")
        and raw.get("error_message") is None
        and raw.get("error") is None
        and raw.get("success", True) is True
        and outer.get("status") in (None, "success", "completed")
        and outer.get("action_status") in (None, "completed")
    )

def _valid_knowledge_payload(
    data_shape: tuple[object, object],
) -> TypeGuard[tuple[int, list[object]]]:
    count, results = data_shape
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return False
    if not isinstance(results, list) or count != len(results):
        return False
    return all(_valid_knowledge_row(row) for row in results)


def _valid_knowledge_row(row: object) -> bool:
    if not isinstance(row, dict) or not _KNOWLEDGE_RESULT_REQUIRED_FIELDS <= row.keys():
        return False
    if not all(isinstance(row[key], str) for key in _KNOWLEDGE_RESULT_REQUIRED_FIELDS - {"score"}):
        return False
    score = row["score"]
    if isinstance(score, bool):
        return False
    return isinstance(score, int) or (isinstance(score, float) and math.isfinite(score))


def _knowledge_probe_result(
    request: AdapterRequest,
    outcome: CommandOutcome,
    attempts: int,
) -> JsonObject:
    if outcome.timed_out:
        return _knowledge_timeout_failure(request, attempts)
    failure_kind = _knowledge_command_failure(outcome)
    if failure_kind is not None:
        return _boolean_probe(
            request, "knowledge_retrieval", False, [f"attempts={attempts}"],
            "service_interface::knowledge_service::search",
            "Repair the knowledge bridge or service before retrying.",
            error_kind=failure_kind,
        )
    state = _knowledge_state(outcome)
    if state in {"failed", "malformed"}:
        return _knowledge_protocol_failure(request, attempts)
    base = nonempty_process_probe(request, outcome, "knowledge_retrieval")
    evidence_items = base.get("evidence")
    if isinstance(evidence_items, list) and evidence_items and isinstance(evidence_items[0], dict):
        evidence_items[0]["summary"] = (
            f"knowledge retrieval verified after {attempts} measured indexing readiness poll(s)"
            if state == "nonempty"
            else f"knowledge retrieval remained empty through {attempts} measured indexing readiness poll(s)"
        )
    return base



def _knowledge_command_failure(outcome: CommandOutcome) -> str | None:
    if outcome.executable_missing:
        return "knowledge_retrieval_executable_missing"
    if outcome.launch_error or not outcome.ok:
        return "knowledge_retrieval_transport_failed"
    try:
        raw: object = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(raw, dict):
        outer = raw.get("result")
        if isinstance(outer, dict) and outer.get("success") is False:
            return "knowledge_retrieval_service_failed"
    return None

def _knowledge_protocol_failure(request: AdapterRequest, attempts: int) -> JsonObject:
    return _boolean_probe(
        request,
        "knowledge_retrieval",
        False,
        [f"attempts={attempts}", "data_shape=missing_valid_count_results"],
        "service_interface::knowledge_service::search",
        "Repair the knowledge-service result envelope before retrying setup.",
        error_kind="knowledge_retrieval_protocol_invalid",
    )


def _knowledge_timeout_failure(
    request: AdapterRequest,
    attempts: int,
    remaining_seconds: float | None = None,
) -> JsonObject:
    observed = [f"attempts={attempts}"]
    if remaining_seconds is not None:
        observed.append(f"remaining_seconds={remaining_seconds:.6f}")
    return _boolean_probe(
        request,
        "knowledge_retrieval",
        False,
        observed,
        "service_interface::knowledge_service::search",
        "Increase the knowledge readiness budget or retry after indexing is available.",
        error_kind="knowledge_retrieval_timeout",
    )


def journal_resume(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    del runtime
    path = request.target / ".solet/install-state.json"
    state = read_json_object(path)
    valid = state is not None and all(
        (
            state.get("name") == request.name,
            state.get("target") == str(request.target),
            state.get("flow_id") == "macos.repository_setup",
            state.get("flow_source_revision") == request.flow_source_revision,
            state.get("answers_fingerprint") == request.answers_fingerprint,
        )
    )
    return _boolean_probe(
        request,
        "journal_resume",
        valid,
        valid,
        str(path),
        "Resume create once so the target completion projection is verified.",
    )
