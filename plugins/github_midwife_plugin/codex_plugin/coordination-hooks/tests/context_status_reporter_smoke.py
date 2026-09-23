#!/usr/bin/env python3
"""Prove the stock-Codex Stop reporter emits only fresh native measurements."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from _harness import Results, preflight, run_hook

_REPO_ROOT = Path(__file__).resolve().parents[5]
_AGENT_MESSAGING_SRC = _REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"
_AGENT_MESSAGING_TESTS = _REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests"
for _path in (_AGENT_MESSAGING_SRC, _AGENT_MESSAGING_TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _real_state_fake import RealShapeState  # noqa: E402

from agent_messaging_plugin.session_context_status_store import (  # noqa: E402
    StaleContextReadingError,
    read_session_context_status,
    read_session_context_status_history,
    upsert_session_context_status,
)
from agent_messaging_plugin.session_sweep import _gauge_stale_prose  # noqa: E402


class _NaiveDatetimeReadbackState(RealShapeState):
    """State double whose reads match the UTC-naive ``DATETIME`` column."""

    object_readback = False

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        result = super().query_state(namespace, query)
        records = result.get("data", {}).get("records", [])
        for record in records:
            value = record.get("reading_at")
            if isinstance(value, str) and value.endswith("+00:00"):
                record["reading_at"] = value.removesuffix("+00:00")
            if self.object_readback and record.get("reading_at") == "2026-09-01T17:00:01":
                record["reading_at"] = datetime(2026, 9, 1, 17, 0, 1)
        return result


def _event(timestamp: str, event_type: str, **payload: Any) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": event_type, "payload": payload}


def _transcript(
    path: Path,
    *,
    after_compaction: bool = True,
    first_post_boundary_timestamp: str = "2026-08-22T20:00:04.000Z",
) -> None:
    rows = [
        _event(
            "2026-08-22T20:00:00.000Z",
            "session_meta",
            id="thread-1",
            model_provider="openai",
        ),
        _event(
            "2026-08-22T20:00:01.000Z",
            "turn_context",
            model="gpt-5.6-sol",
            effort="xhigh",
        ),
        _event(
            "2026-08-22T20:00:02.000Z",
            "event_msg",
            type="token_count",
            info={
                "last_token_usage": {
                    "input_tokens": 200_000,
                    "cached_input_tokens": 190_000,
                    "cache_write_input_tokens": 0,
                },
                "model_context_window": 258_400,
            },
        ),
        _event("2026-08-22T20:00:03.000Z", "event_msg", type="context_compacted"),
    ]
    if after_compaction:
        rows.append(
            _event(
                first_post_boundary_timestamp,
                "event_msg",
                type="token_count",
                info={
                    "last_token_usage": {
                        "input_tokens": 25_844,
                        "cached_input_tokens": 24_576,
                        "cache_write_input_tokens": 0,
                    },
                    "model_context_window": 258_400,
                },
            )
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _append_newer_same_generation_measurement(path: Path) -> None:
    row = _event(
        "2026-08-22T20:00:05.000Z",
        "event_msg",
        type="token_count",
        info={
            "last_token_usage": {
                "input_tokens": 31_337,
                "cached_input_tokens": 30_000,
                "cache_write_input_tokens": 0,
            },
            "model_context_window": 258_400,
        },
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _fake_cli(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

capture_path = Path(os.environ["REPORT_CAPTURE_PATH"])
state_path = Path(os.environ["REPORT_STATE_PATH"])
process_key = sys.argv[2]
parameters = json.loads(sys.argv[3])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None

if process_key == "plugin::agent_messaging_plugin::session_context_status_history":
    entries = [] if state is None else [state]
    data = {"resolved": bool(entries), "entries": entries}
elif process_key == "plugin::agent_messaging_plugin::report_context_status":
    captures = (
        json.loads(capture_path.read_text(encoding="utf-8"))
        if capture_path.exists()
        else []
    )
    captures.append(parameters)
    capture_path.write_text(json.dumps(captures), encoding="utf-8")
    state_path.write_text(json.dumps({
        "claude_session_id": parameters["runtime_session_id"],
        "reading_at": parameters.get("reading_at", "").removesuffix("Z"),
    }), encoding="utf-8")
    data = {"status": "recorded"}
else:
    raise SystemExit(f"unexpected process key: {process_key}")

print(json.dumps({
    "status": "completed",
    "result": {"success": True, "data": data},
}))
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o700)


def _fresh_clear_transcript(path: Path) -> None:
    rows = [
        _event(
            "2026-08-22T20:10:00.000Z",
            "session_meta",
            id="thread-1",
            model_provider="openai",
        ),
        _event(
            "2026-08-22T20:10:01.000Z",
            "turn_context",
            model="gpt-5.6-sol",
            effort="xhigh",
        ),
        _event(
            "2026-08-22T20:10:02.000Z",
            "event_msg",
            type="token_count",
            info={
                "last_token_usage": {
                    "input_tokens": 18_000,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                },
                "model_context_window": 258_400,
            },
        ),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _payload(transcript: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "Stop",
            "session_id": "thread-1",
            "transcript_path": str(transcript),
            "model": "gpt-5.6-sol",
        }
    )


def _env(fake_cli: Path, capture: Path, state: Path) -> dict[str, str]:
    return {
        "AGENT_INSTANCE_ID": "agi-test",
        "AGENT_SESSION_ID": "ases-test",
        "AGENT_WAKE_CLI": str(fake_cli),
        "REPORT_CAPTURE_PATH": str(capture),
        "REPORT_STATE_PATH": str(state),
    }


def _reported_parameters(capture: Path, index: int = -1) -> dict[str, Any]:
    captured = json.loads(capture.read_text(encoding="utf-8"))
    value: Any = captured[index]
    assert isinstance(value, dict)
    return value


def _report_count(capture: Path) -> int:
    if not capture.exists():
        return 0
    value = json.loads(capture.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return len(value)


def _write_store(state: _NaiveDatetimeReadbackState, *, reading_at: str, tokens: int) -> bool:
    return upsert_session_context_status(
        cast(Any, state),
        agent_instance_id="agi-naive-readback",
        claude_session_id="runtime-naive-readback",
        model="gpt-5.6-terra",
        current_tokens=tokens,
        ceiling=258_400,
        measured_at="2026-09-01T17:00:00+00:00",
        reading_at=reading_at,
    )


def _check_store_series(res: Results) -> _NaiveDatetimeReadbackState:
    """M1, M3, M4, M6-M7: the persisted and incoming clocks stay distinct."""
    state = _NaiveDatetimeReadbackState()
    first = _write_store(state, reading_at="2026-09-01T17:00:00+00:00", tokens=1)
    stored = read_session_context_status(cast(Any, state), "agi-naive-readback")
    res.check(first is True, "M1 first aware reading lands")
    res.check(
        stored is not None and stored.get("reading_at") == "2026-09-01T17:00:00",
        "M6 the state double returns the real UTC-naive DATETIME shape",
        repr(None if stored is None else stored.get("reading_at")),
    )
    second = _write_store(state, reading_at="2026-09-01T17:00:01+00:00", tokens=2)
    rows, truncated = read_session_context_status_history(
        cast(Any, state), "agi-naive-readback",
    )
    res.check(second is True, "M1 a newer aware reading follows naive stored readback")
    res.check(
        len(rows) == 2 and not truncated and {row.get("claude_session_id") for row in rows} == {"runtime-naive-readback"},
        "M7 one runtime session produces a two-row history series",
        repr(rows),
    )
    replay = _write_store(state, reading_at="2026-09-01T17:00:01+00:00", tokens=2)
    rows_after_replay, _ = read_session_context_status_history(cast(Any, state), "agi-naive-readback")
    res.check(replay is False and len(rows_after_replay) == 2, "M3 equal instant replay is an idempotent no-op")
    try:
        _write_store(state, reading_at="2026-09-01T17:00:00+00:00", tokens=0)
    except StaleContextReadingError as exc:
        res.check("regressed" in str(exc), "M4 older incoming reading fails for regression, not timezone parsing", str(exc))
    else:
        res.check(False, "M4 older incoming reading fails loud")
    return state


def _check_incoming_strictness(res: Results, state: _NaiveDatetimeReadbackState) -> None:
    """M2: a reporter cannot supply an ambiguous clock."""
    try:
        _write_store(state, reading_at="2026-09-01T17:00:02", tokens=3)
    except StaleContextReadingError as exc:
        res.check("incoming reading_at must be timezone-aware" in str(exc), "M2 naive incoming reading remains strict", str(exc))
    else:
        res.check(False, "M2 naive incoming reading is rejected")


def _check_stored_datetime_object(res: Results, state: _NaiveDatetimeReadbackState) -> None:
    """A provider returning a datetime object has the same stored-UTC contract."""
    state.object_readback = True
    object_readback = _write_store(state, reading_at="2026-09-01T17:00:02+00:00", tokens=3)
    res.check(object_readback is True, "stored datetime objects normalize as UTC before comparison")


def _check_gauge_stale_prose(res: Results) -> None:
    """The stale notice guides readers to the correct runtime and evidence."""
    prose = _gauge_stale_prose(
        agent_instance_id="agi-naive-readback",
        row={"lane_id": "lane-cdx-fix-gauge-naive-readback"},
        last_alive=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
        measured_at=datetime(2026, 9, 1, 17, 0, tzinfo=UTC),
        clock=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
    )
    res.check(
        "session_context_status_history" in prose and "upsert-only and keeps no history" not in prose,
        "notice points readers to GAU-15 history instead of claiming history is absent",
        prose,
    )
    res.check(
        "PostToolUse" not in prose and "rotation_due_watch" not in prose and "gauge reporter" in prose,
        "notice stays runtime-neutral and names the per-runtime gauge reporter",
        prose,
    )


def _check_naive_readback_regressions(res: Results) -> None:
    state = _check_store_series(res)
    _check_incoming_strictness(res, state)
    _check_stored_datetime_object(res, state)
    _check_gauge_stale_prose(res)


def main() -> int:
    preflight()
    res = Results("Codex context-status reporter")
    with tempfile.TemporaryDirectory(prefix="codex-context-reporter-") as raw_tmp:
        tmp = Path(raw_tmp)
        transcript = tmp / "transcript.jsonl"
        fake_cli = tmp / "solet"
        capture = tmp / "capture.json"
        state = tmp / "state.json"
        _fake_cli(fake_cli)

        _transcript(transcript)
        proc = run_hook(
            "context_status_reporter.py",
            env=_env(fake_cli, capture, state),
            stdin=_payload(transcript),
        )
        res.check(proc.returncode == 0, "fresh post-compaction report succeeds", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "successful Stop output is inert JSON", proc.stdout)
        res.check(capture.is_file(), "fresh measurement invokes the reporting verb")
        if capture.is_file():
            params = _reported_parameters(capture)
            expected = {
                "agent_instance_id": "agi-test",
                "agent_session_id": "ases-test",
                "runtime_session_id": "thread-1",
                "provider": "openai",
                "runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "current_tokens": 25_844,
                "ceiling": 258_400,
                "reading_at": "2026-08-22T20:00:04.000Z",
                "cache_read_tokens": 24_576,
                "cache_write_tokens": 0,
                "reporter_surface": "vendored",
                "reporter_generation": 2,
            }
            for key, value in expected.items():
                res.check(params.get(key) == value, f"report carries native {key}", repr(params.get(key)))
            res.check("cache_cold" not in params, "one cache counter never invents cold state")
            res.check("cache_overage_signature" not in params, "one call never invents overage")
            measured_at = params.get("measured_at")
            res.check(
                isinstance(measured_at, str) and measured_at.endswith("+00:00"),
                "report carries an explicit UTC measurement clock",
                repr(measured_at),
            )
        if state.exists():
            stored_reading = json.loads(state.read_text(encoding="utf-8")).get("reading_at")
            res.check(
                stored_reading == "2026-08-22T20:00:04.000",
                "M6 Codex history fake emits the UTC-naive DATETIME readback",
                repr(stored_reading),
            )

        proc = run_hook(
            "context_status_reporter.py",
            env=_env(fake_cli, capture, state),
            stdin=_payload(transcript),
        )
        res.check(proc.returncode == 0, "unchanged later Stop is a safe no-op", proc.stderr)
        res.check(
            _report_count(capture) == 1,
            "unchanged later Stop never republishes the consumed reading identity",
            f"report calls={_report_count(capture)}",
        )

        _append_newer_same_generation_measurement(transcript)
        proc = run_hook(
            "context_status_reporter.py",
            env=_env(fake_cli, capture, state),
            stdin=_payload(transcript),
        )
        res.check(proc.returncode == 0, "newer same-generation Stop succeeds", proc.stderr)
        res.check(
            _report_count(capture) == 2,
            "newer same-generation reading invokes a second report",
            f"report calls={_report_count(capture)}",
        )
        if _report_count(capture) == 2:
            newer_params = _reported_parameters(capture)
            res.check(
                newer_params.get("current_tokens") == 31_337,
                "newer same-generation report carries the latest current_tokens",
                repr(newer_params.get("current_tokens")),
            )
            res.check(
                newer_params.get("reading_at") == "2026-08-22T20:00:05.000Z",
                "newer same-generation report carries the latest reading_at",
                repr(newer_params.get("reading_at")),
            )

        proc = run_hook(
            "context_status_reporter.py",
            env=_env(fake_cli, capture, state),
            stdin=_payload(transcript),
        )
        res.check(proc.returncode == 0, "unchanged latest Stop remains a safe no-op", proc.stderr)
        res.check(
            _report_count(capture) == 2,
            "unchanged latest Stop never republishes the newest reading identity",
            f"report calls={_report_count(capture)}",
        )

        capture.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        _transcript(transcript, after_compaction=False)
        proc = run_hook(
            "context_status_reporter.py",
            env=_env(fake_cli, capture, state),
            stdin=_payload(transcript),
        )
        res.check(proc.returncode == 0, "pre-compaction-only measurement is a safe no-op", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "stale-only Stop output is inert JSON", proc.stdout)
        res.check(not capture.exists(), "stale pre-compaction measurement is never reported")

        _fresh_clear_transcript(transcript)
        proc = run_hook(
            "context_status_reporter.py",
            env=_env(fake_cli, capture, state),
            stdin=_payload(transcript),
        )
        res.check(proc.returncode == 0, "fresh post-clear transcript reports", proc.stderr)
        res.check(capture.is_file(), "first positive post-clear reading invokes the verb")
        if capture.is_file():
            clear_params = _reported_parameters(capture)
            res.check(clear_params.get("current_tokens") == 18_000, "post-clear C is native")
            res.check(clear_params.get("cache_read_tokens") == 0, "cold-by-construction counter stays data")
            res.check("cache_cold" not in clear_params, "post-clear does not misclassify cache expiry")

        for bad_clock in ("garbage-clock", "2026-08-22T20:00:04"):
            capture.unlink(missing_ok=True)
            state.unlink(missing_ok=True)
            _transcript(transcript, first_post_boundary_timestamp=bad_clock)
            proc = run_hook(
                "context_status_reporter.py",
                env=_env(fake_cli, capture, state),
                stdin=_payload(transcript),
            )
            res.check(
                proc.returncode != 0,
                f"empty history rejects malformed first reading clock {bad_clock!r}",
                proc.stderr,
            )
            res.check(
                not capture.exists(),
                f"empty history never delivers malformed first reading clock {bad_clock!r}",
            )

            state.write_text(
                json.dumps({
                    "claude_session_id": "previous-runtime",
                    "reading_at": "2026-08-22T20:00:03.000Z",
                }),
                encoding="utf-8",
            )
            proc = run_hook(
                "context_status_reporter.py",
                env=_env(fake_cli, capture, state),
                stdin=_payload(transcript),
            )
            res.check(
                proc.returncode != 0,
                f"new runtime rejects malformed reading clock {bad_clock!r}",
                proc.stderr,
            )
            res.check(
                not capture.exists(),
                f"new runtime never delivers malformed reading clock {bad_clock!r}",
            )

        capture.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        proc = run_hook("context_status_reporter.py", stdin=_payload(transcript))
        res.check(proc.returncode == 0, "non-fleet session is deliberately unarmed", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "unarmed Stop output is inert JSON", proc.stdout)
        res.check(not capture.exists(), "unarmed hook never calls the platform")

        _transcript(transcript)
        bad_payload = json.loads(_payload(transcript))
        bad_payload["session_id"] = "wrong-thread"
        proc = run_hook(
            "context_status_reporter.py",
            env=_env(fake_cli, capture, state),
            stdin=json.dumps(bad_payload),
        )
        res.check(proc.returncode != 0, "session/transcript identity mismatch fails loud")
        res.check(not capture.exists(), "identity mismatch never reports another transcript")

        _check_naive_readback_regressions(res)

    return res.finish()


if __name__ == "__main__":
    raise SystemExit(main())
