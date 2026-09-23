#!/usr/bin/env python3
"""Report one fresh stock-Codex context measurement at an async Stop boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROCESS_KEY = "plugin::agent_messaging_plugin::report_context_status"
HISTORY_PROCESS_KEY = "plugin::agent_messaging_plugin::session_context_status_history"
REPORTER_GENERATION = 2
RUNTIME = "codex"


class ReporterError(RuntimeError):
    """An armed reporter could not produce or deliver a trustworthy reading."""


@dataclass(frozen=True)
class Measurement:
    runtime_session_id: str
    provider: str
    model: str
    effort: str
    current_tokens: int
    ceiling: int
    reading_at: str
    cache_read_tokens: int | None
    cache_write_tokens: int | None


@dataclass(frozen=True)
class TranscriptIdentity:
    runtime_session_id: str
    provider: str
    last_compaction_index: int


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReporterError(f"{label} must be a JSON object")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReporterError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_count(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReporterError(f"{label} must be a non-negative integer when present")
    return value


def _load_rows(transcript_path: Path) -> list[dict[str, Any]]:
    if not transcript_path.is_file():
        raise ReporterError(f"transcript_path is not a file: {transcript_path}")
    raw = transcript_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                # Codex appends the transcript concurrently. A partial final
                # record is not evidence and must never displace the last
                # complete record.
                continue
            raise ReporterError(
                f"transcript row {index + 1} is malformed JSON: {exc.msg}"
            ) from exc
        rows.append(_object(value, f"transcript row {index + 1}"))
    return rows


def _event_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else None


def _turn_identity(payload: dict[str, Any]) -> tuple[str, str]:
    model = _required_text(payload.get("model"), "turn_context.model")
    direct_effort = payload.get("effort")
    nested_effort: object = None
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict):
            nested_effort = settings.get("reasoning_effort")
    candidates = [
        value.strip()
        for value in (direct_effort, nested_effort)
        if isinstance(value, str) and value.strip()
    ]
    if not candidates:
        raise ReporterError("turn_context carries no reasoning effort")
    if len(set(candidates)) != 1:
        raise ReporterError("turn_context effort fields disagree")
    return model, candidates[0]


def _is_compaction(row: dict[str, Any]) -> bool:
    payload = _event_payload(row)
    return (
        row.get("type") == "event_msg"
        and payload is not None
        and payload.get("type") == "context_compacted"
    )


def _transcript_identity(rows: list[dict[str, Any]]) -> TranscriptIdentity:
    session_ids: set[str] = set()
    providers: set[str] = set()
    for row in rows:
        payload = _event_payload(row)
        if row.get("type") != "session_meta" or payload is None:
            continue
        session_ids.add(_required_text(payload.get("id"), "session_meta.id"))
        providers.add(
            _required_text(payload.get("model_provider"), "session_meta.model_provider")
        )
    if len(session_ids) != 1 or len(providers) != 1:
        raise ReporterError("transcript must carry one session_meta identity and provider")
    last_compaction = max(
        (index for index, row in enumerate(rows) if _is_compaction(row)),
        default=-1,
    )
    return TranscriptIdentity(
        runtime_session_id=next(iter(session_ids)),
        provider=next(iter(providers)),
        last_compaction_index=last_compaction,
    )


def _token_usage_after_boundary(
    row: dict[str, Any], *, index: int, boundary: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    payload = _event_payload(row)
    if (
        index <= boundary
        or row.get("type") != "event_msg"
        or payload is None
        or payload.get("type") != "token_count"
    ):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage")
    return (info, usage) if isinstance(usage, dict) else None


def _positive_token_counts(
    info: dict[str, Any], usage: dict[str, Any]
) -> tuple[int, int] | None:
    current_tokens = usage.get("input_tokens")
    ceiling = info.get("model_context_window")
    if current_tokens in (None, 0) or ceiling in (None, 0):
        return None
    if (
        isinstance(current_tokens, bool)
        or not isinstance(current_tokens, int)
        or current_tokens < 0
    ):
        raise ReporterError("last_token_usage.input_tokens must be a positive integer")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
        raise ReporterError("model_context_window must be a positive integer")
    return current_tokens, ceiling


def _token_measurement(
    *,
    row: dict[str, Any],
    info: dict[str, Any],
    usage: dict[str, Any],
    identity: TranscriptIdentity,
    turn_model: str | None,
    effort: str | None,
    expected_model: str,
) -> Measurement | None:
    counts = _positive_token_counts(info, usage)
    if counts is None:
        return None
    if turn_model is None or effort is None:
        raise ReporterError("token_count has no preceding turn_context identity")
    if turn_model != expected_model:
        raise ReporterError("hook model does not match the transcript turn_context model")
    reading_at = _required_text(row.get("timestamp"), "token_count.timestamp")
    _reading_clock(reading_at, "token_count.timestamp")
    current_tokens, ceiling = counts
    return Measurement(
        runtime_session_id=identity.runtime_session_id,
        provider=identity.provider,
        model=turn_model,
        effort=effort,
        current_tokens=current_tokens,
        ceiling=ceiling,
        reading_at=reading_at,
        cache_read_tokens=_optional_count(
            usage.get("cached_input_tokens"),
            "last_token_usage.cached_input_tokens",
        ),
        cache_write_tokens=_optional_count(
            usage.get("cache_write_input_tokens"),
            "last_token_usage.cache_write_input_tokens",
        ),
    )


def _measurement(
    rows: list[dict[str, Any]], *, expected_session_id: str, expected_model: str
) -> Measurement | None:
    identity = _transcript_identity(rows)
    if identity.runtime_session_id != expected_session_id:
        raise ReporterError(
            "hook session_id does not match the transcript session_meta identity"
        )
    turn_model: str | None = None
    effort: str | None = None
    latest_candidate: Measurement | None = None
    for index, row in enumerate(rows):
        payload = _event_payload(row)
        if payload is None:
            continue
        if row.get("type") == "turn_context":
            turn_model, effort = _turn_identity(payload)
            continue
        token_usage = _token_usage_after_boundary(
            row,
            index=index,
            boundary=identity.last_compaction_index,
        )
        if token_usage is None:
            continue
        info, usage = token_usage
        candidate = _token_measurement(
            row=row,
            info=info,
            usage=usage,
            identity=identity,
            turn_model=turn_model,
            effort=effort,
            expected_model=expected_model,
        )
        if candidate is not None:
            latest_candidate = candidate
    return latest_candidate


def _reporter_surface(path: Path) -> str:
    normalized = path.resolve().as_posix()
    if "/.codex/plugins/cache/" in normalized:
        return "plugin_cache"
    if "/.ananta/releases/" in normalized:
        return "release"
    if "/plugins/github_midwife_plugin/codex_plugin/" in normalized:
        return "vendored"
    if "/.claude/hooks/" in normalized or "/.codex/hooks/" in normalized:
        return "checkout"
    return "unknown"


def _fleet_environment() -> tuple[str, str, Path] | None:
    values = {
        "AGENT_INSTANCE_ID": os.environ.get("AGENT_INSTANCE_ID", "").strip(),
        "AGENT_SESSION_ID": os.environ.get("AGENT_SESSION_ID", "").strip(),
        "AGENT_WAKE_CLI": os.environ.get("AGENT_WAKE_CLI", "").strip(),
    }
    present = {name for name, value in values.items() if value}
    if not present:
        return None
    if len(present) != len(values):
        missing = sorted(set(values) - present)
        raise ReporterError("partially armed fleet reporter is missing " + ", ".join(missing))
    cli = Path(values["AGENT_WAKE_CLI"])
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ReporterError(f"AGENT_WAKE_CLI is not an executable file: {cli}")
    return values["AGENT_INSTANCE_ID"], values["AGENT_SESSION_ID"], cli


def _process_call(
    cli: Path,
    process_key: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                str(cli),
                "call",
                process_key,
                json.dumps(parameters, separators=(",", ":"), sort_keys=True),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReporterError(f"context-status process call failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise ReporterError(f"context-status process call failed: {detail}")
    try:
        response: Any = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ReporterError("context-status process call returned non-JSON output") from exc
    outer = _object(response, f"{process_key} process response")
    result = outer.get("result")
    if outer.get("status") != "completed" or not isinstance(result, dict) or result.get("success") is not True:
        raise ReporterError(f"{process_key} process call did not report success")
    return _object(result.get("data"), f"{process_key} result.data")


def _deliver(cli: Path, parameters: dict[str, Any]) -> None:
    _process_call(cli, PROCESS_KEY, parameters)


def _reading_clock(raw: str, label: str) -> datetime:
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReporterError(f"{label} is not an ISO-8601 timestamp: {raw!r}") from exc
    if value.tzinfo is None:
        raise ReporterError(f"{label} must be timezone-aware")
    return value


def _stored_reading_clock(raw: str, label: str) -> datetime:
    """Parse a history timestamp, restoring UTC from the DATETIME readback."""
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReporterError(f"{label} is not an ISO-8601 timestamp: {raw!r}") from exc
    if value.tzinfo is not None:
        return value
    return datetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        tzinfo=UTC,
        fold=value.fold,
    )


def _already_consumed(
    cli: Path,
    *,
    agent_instance_id: str,
    measurement: Measurement,
) -> bool:
    candidate = _reading_clock(measurement.reading_at, "candidate reading_at")
    data = _process_call(
        cli,
        HISTORY_PROCESS_KEY,
        {"agent_instance_id": agent_instance_id, "limit": 1},
    )
    entries = data.get("entries")
    if entries in (None, []):
        return False
    if not isinstance(entries, list) or not isinstance(entries[0], dict):
        raise ReporterError("context-status history returned malformed entries")
    latest = entries[0]
    if str(latest.get("claude_session_id") or "") != measurement.runtime_session_id:
        return False
    prior_raw = latest.get("reading_at")
    if prior_raw is None:
        return False
    prior = _stored_reading_clock(
        _required_text(prior_raw, "stored reading_at"),
        "stored reading_at",
    )
    if candidate == prior:
        return True
    if candidate < prior:
        raise ReporterError(
            f"candidate reading_at regressed: {candidate.isoformat()} < {prior.isoformat()}"
        )
    return False


def _run() -> None:
    fleet = _fleet_environment()
    if fleet is None:
        print("{}")
        return
    agent_instance_id, agent_session_id, cli = fleet
    try:
        hook_input: Any = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ReporterError(f"hook stdin is malformed JSON: {exc.msg}") from exc
    payload = _object(hook_input, "hook stdin")
    if _required_text(payload.get("hook_event_name"), "hook_event_name") != "Stop":
        raise ReporterError("context-status reporter is bound only to Stop")
    if payload.get("stop_hook_active") is True:
        print("{}")
        return
    runtime_session_id = _required_text(payload.get("session_id"), "session_id")
    transcript_path = Path(
        _required_text(payload.get("transcript_path"), "transcript_path")
    )
    model = _required_text(payload.get("model"), "model")
    measurement = _measurement(
        _load_rows(transcript_path),
        expected_session_id=runtime_session_id,
        expected_model=model,
    )
    if measurement is None:
        # Immediately after compaction there may be no positive post-boundary
        # token_count yet. Reporting the older value would be worse than an
        # explicit no-op; the next Stop supplies the first valid reading.
        print("{}")
        return
    if _already_consumed(
        cli,
        agent_instance_id=agent_instance_id,
        measurement=measurement,
    ):
        print("{}")
        return
    parameters: dict[str, Any] = {
        "agent_instance_id": agent_instance_id,
        "agent_session_id": agent_session_id,
        "runtime_session_id": measurement.runtime_session_id,
        "provider": measurement.provider,
        "runtime": RUNTIME,
        "model": measurement.model,
        "effort": measurement.effort,
        "current_tokens": measurement.current_tokens,
        "ceiling": measurement.ceiling,
        "reading_at": measurement.reading_at,
        "measured_at": datetime.now(UTC).isoformat(),
        "reporter_surface": _reporter_surface(Path(__file__)),
        "reporter_generation": REPORTER_GENERATION,
    }
    optional = {
        "cache_read_tokens": measurement.cache_read_tokens,
        "cache_write_tokens": measurement.cache_write_tokens,
    }
    parameters.update({key: value for key, value in optional.items() if value is not None})
    _deliver(cli, parameters)
    print("{}")


def main() -> int:
    try:
        _run()
    except ReporterError as exc:
        print(f"context_status_reporter: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
