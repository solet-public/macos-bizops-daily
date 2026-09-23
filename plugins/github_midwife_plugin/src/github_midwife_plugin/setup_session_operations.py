"""Session-ledger source registration operations."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from ananta.llm.session_ledger.root_uri import canonicalize_root_uri_for_storage

from .installation_state_doctor import partition_session_roots, session_retrieval
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, planned_action, result
from .setup_adapter_runtime import CommandOutcome, Runtime, read_json_object
from .setup_operations import (
    _SESSION_ROOTS,
    _blocked,
    _failed_outcome,
    _kickstart,
    _solet_success_payload,
    solet_call,
    solet_call_succeeded,
)

# Measured on a real guest (2026-09-19): a `launchctl kickstart -k` restart
# returns immediately, but the actual solet child needs ~29s to cold-start
# Python, establish its Postgres pool, and register with the blue-green
# router before `solet-bridge health` reports healthy. 60s keeps a >2x margin.
_KICKSTART_HEALTH_BUDGET_SECONDS = 60
_KICKSTART_HEALTH_POLL_INTERVAL_SECONDS = 1.0


def _wait_for_kickstart_health(
    request: AdapterRequest,
    runtime: Runtime,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll the restarted target's bridge health until it answers or the budget lapses."""

    deadline = monotonic() + _KICKSTART_HEALTH_BUDGET_SECONDS
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        outcome = runtime.run(
            (str(request.target / ".venv/bin/solet-bridge"), "health"),
            timeout_seconds=max(1, min(10, int(remaining) + 1)),
            cwd=request.target,
            extra_env={"SOLET_NAME": request.name},
        )
        if outcome.ok:
            try:
                healthy = json.loads(outcome.stdout).get("status") == "healthy"
            except (json.JSONDecodeError, AttributeError):
                healthy = False
            if healthy:
                return True
        sleep(_KICKSTART_HEALTH_POLL_INTERVAL_SECONDS)


def session_source(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    source_rows = _SESSION_ROOTS[request.operation_ref]
    absolute_rows = [(kind, runtime.home / relative) for kind, relative in source_rows]
    config_path = request.target / "profile/config/plugins/session_ledger_service.json"
    if request.phase == "probe":
        return _session_source_preview(request, runtime, absolute_rows, config_path)
    return _apply_session_source(request, runtime, absolute_rows, config_path)


def _session_source_preview(
    request: AdapterRequest,
    runtime: Runtime,
    absolute_rows: list[tuple[str, Path]],
    config_path: Path,
) -> JsonObject:
    # Absent roots fall through deliberately: the agent CLIs create these trees on
    # first use, and registering a not-yet-existing root is a supported ingest
    # case. Only a root that exists and cannot be read is a permission problem.
    _absent, unreadable = partition_session_roots([path for _kind, path in absolute_rows])
    if unreadable:
        return _blocked(
            request, "session_roots_unreadable", "Approved session roots are not readable."
        )
    roots = sorted({str(path) for _kind, path in absolute_rows})
    configured = read_json_object(config_path)
    if configured is not None and configured.get("ledger_allowed_roots") == roots:
        qualification = session_retrieval(request, runtime)
        if qualification["checkpoint_status"] == "verified":
            return qualification
    actions = [
        planned_action(
            action_id="sessions.write_allowed_roots",
            title="Write only the approved session-ledger roots",
            mutation_kind="config_write",
            target=str(config_path),
            evidence_ref="session_source_not_verified",
        ),
        planned_action(
            action_id="sessions.restart_target",
            title="Restart the target through its LaunchAgent owner",
            mutation_kind="service_restart",
            target=f"launchagent:local.solet.{request.name}",
            evidence_ref="session_config_requires_restart",
        ),
        planned_action(
            action_id="sessions.register_and_backfill",
            title="Register approved sources and run one bounded backfill",
            mutation_kind="session_ingestion",
            target=",".join(roots),
            evidence_ref="session_source_not_verified",
        ),
    ]
    return result(
        request, status="pending", actions=actions, repair="Approve only the displayed roots."
    )


def _apply_session_source(
    request: AdapterRequest,
    runtime: Runtime,
    absolute_rows: list[tuple[str, Path]],
    config_path: Path,
) -> JsonObject:
    current = read_json_object(config_path) or {}
    roots = sorted({str(path) for _kind, path in absolute_rows})
    updated = dict(current)
    updated["ledger_allowed_roots"] = cast(JsonValue, roots)
    runtime.atomic_write(
        config_path, json.dumps(updated, indent=2, sort_keys=True) + "\n", mode=0o600
    )
    started = _kickstart(request, runtime)
    if not started.ok:
        return _failed_outcome(request, started, "session_source_restart_failed")
    if not _wait_for_kickstart_health(request, runtime):
        return result(
            request,
            status="failed",
            error_kind="session_source_restart_not_ready",
            retry_safe=True,
            repair=(
                "The restarted target did not report healthy within "
                f"{_KICKSTART_HEALTH_BUDGET_SECONDS}s. Retry setup once the platform "
                "LaunchAgent has finished starting."
            ),
        )
    listed = solet_call(
        request,
        runtime,
        "service_interface::session_ledger_service::list_sources",
        {},
    )
    source_ids = _registered_source_ids(listed, absolute_rows)
    if source_ids is None:
        return _failed_outcome(request, listed, "session_source_register_failed")
    for source_id in source_ids:
        backfill = solet_call(
            request,
            runtime,
            "service_interface::session_ledger_service::poll_source",
            {"source_id": source_id},
        )
        if not solet_call_succeeded(backfill):
            return _failed_outcome(request, backfill, "session_backfill_failed")
    return result(request, status="applied", retry_safe=True)


def _registered_source_ids(
    outcome: CommandOutcome,
    absolute_rows: list[tuple[str, Path]],
) -> list[str] | None:
    """Read back enabled boot-registered sources before polling them.

    Setup writes the allow-list and restarts the target; boot then registers the
    declared pulling sources through the trusted internal seam.  The setup
    adapter is an external bridge caller, so it must verify those rows rather
    than re-invoking the authorization-gated public registration verb.
    """
    payload = _solet_success_payload(outcome)
    if payload is None:
        return None
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return None
    source_ids: dict[tuple[str, str], str] = {}
    expected = [
        (source_kind, canonicalize_root_uri_for_storage(str(root)))
        for source_kind, root in absolute_rows
    ]
    for source in sources:
        entry = _source_entry_id(source)
        if entry is None:
            continue
        key, source_id = entry
        if key in expected and key not in source_ids:
            source_ids[key] = source_id
    if len(source_ids) != len(expected):
        return None
    return [source_ids[key] for key in expected]


def _source_entry_id(source: object) -> tuple[tuple[str, str], str] | None:
    if not isinstance(source, dict):
        return None
    source_kind = source.get("source_kind")
    root_uri = source.get("root_uri")
    source_id = source.get("source_id")
    if not isinstance(source_kind, str) or not isinstance(root_uri, str):
        return None
    if not isinstance(source_id, str) or not source_id:
        return None
    if source.get("enabled") is not True:
        return None
    return (source_kind, root_uri), source_id
