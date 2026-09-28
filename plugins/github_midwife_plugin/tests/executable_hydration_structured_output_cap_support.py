"""Structured bridge-output cap regressions for executable hydration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _public_observed_values(evidence: Any) -> dict[str, Any] | None:
    if not isinstance(evidence, list) or not evidence or not isinstance(evidence[0], dict):
        return None
    observed = evidence[0].get("observed")
    if not isinstance(observed, list) or not all(isinstance(item, str) for item in observed):
        return None
    return {
        key: json.loads(value)
        for item in observed
        if "=" in item
        for key, value in [item.split("=", maxsplit=1)]
    }


def _assert_evidence_digest(result: Any, check: Callable[[object, str], None]) -> None:
    evidence = result["evidence"][0]
    observed = evidence["observed"]
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    check(
        isinstance(observed, bool) and evidence["digest"] == expected_digest,
        "knowledge readiness evidence keeps a public scalar observation with a matching digest",
    )


def _assert_pre_call_timeout(
    timeout_probe: Any,
    runtime: Any,
    wait_for_knowledge: Callable[..., Any],
    check: Callable[[object, str], None],
) -> None:
    class PreCallDriftClock:
        def __init__(self) -> None:
            self.calls = 0

        def monotonic(self) -> float:
            self.calls += 1
            return 0.0 if self.calls == 1 else 0.001

        def sleep(self, seconds: float) -> None:
            raise AssertionError(f"pre-call timeout must not sleep: {seconds}")

    command_count = len(runtime.commands)
    clock = PreCallDriftClock()
    timeout = wait_for_knowledge(
        timeout_probe, runtime, monotonic=clock.monotonic, sleep=clock.sleep
    )
    check(
        timeout["checkpoint_status"] == "blocked"
        and timeout["error_kind"] == "knowledge_retrieval_timeout"
        and len(runtime.commands) == command_count,
        "pre-call subsecond budget is a timeout, not a malformed response",
    )


def _assert_parent_budget_cap(
    target: Path,
    runtime: Any,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    nonempty: Any,
    vector: tuple[str, ...],
    wait_for_knowledge: Callable[..., Any],
) -> None:
    class TimeoutRecordingRuntime:
        def __init__(self, base: Any) -> None:
            self.home = base.home
            self._base = base
            self.timeouts: list[int] = []

        def run(self, *args: Any, **kwargs: Any) -> Any:
            self.timeouts.append(kwargs["timeout_seconds"])
            return self._base.run(*args, **kwargs)

    probe = request(
        target,
        operation_id="knowledge_retrieval_succeeds",
        operation_ref="service_interface::knowledge_service.search",
        probe_purpose="stage_exit",
        timeout_seconds=120,
    )
    runtime.response_sequences[vector] = [nonempty]
    capped_runtime = TimeoutRecordingRuntime(runtime)
    result = wait_for_knowledge(probe, capped_runtime)
    check(
        result["checkpoint_status"] == "verified" and capped_runtime.timeouts == [60],
        "knowledge poll caps a 120-second parent budget to a 60-second governed call",
    )


def run_knowledge_output_cap(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
    structured_output_limit: int,
) -> None:
    """Prove a measured over-cap knowledge response remains readable."""

    from github_midwife_plugin.setup_adapter import dispatch_request

    probe = request(
        target,
        operation_id="knowledge_retrieval_succeeds",
        operation_ref="service_interface::knowledge_service.search",
        probe_purpose="completion",
    )
    vector = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::knowledge_service::search",
        '{"query":"session start orientation","top_k":1}',
    )
    runtime.responses[vector] = command_outcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "error": None,
                    "data": {
                        "count": 1,
                        "results": [
                            {
                                "content": "x" * 25_022,
                                "knowledge_base": "fixture",
                                "file_path": "fixture.md",
                                "score": 1.0,
                                "tier": "semantic",
                                "memory_id": "fixture-memory",
                            }
                        ],
                    },
                }
            }
        ),
        "",
    )
    result = dispatch_request(probe, runtime)
    check(
        result["checkpoint_status"] == "verified",
        "M-KNOWLEDGE-CAP: over-cap structured knowledge output is green",
    )
    check(
        runtime.command_output_limits[-1] == structured_output_limit,
        "structured knowledge output_limit reaches the production cap",
    )


def run_knowledge_readiness_poll(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
) -> None:
    """Exercise successful-empty, malformed, and deadline-bound retrieval states."""

    from github_midwife_plugin.installation_state_doctor import _wait_for_knowledge_retrieval

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    class SlowRuntime:
        def __init__(self, base: Any, clock: Clock) -> None:
            self.home = base.home
            self._base = base
            self._clock = clock

        def run(self, *args: Any, **kwargs: Any) -> Any:
            self._clock.now += 0.75
            return self._base.run(*args, **kwargs)

    probe = request(
        target,
        operation_id="knowledge_retrieval_succeeds",
        operation_ref="service_interface::knowledge_service.search",
        probe_purpose="stage_exit",
        timeout_seconds=9,
    )
    vector = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::knowledge_service::search",
        '{"query":"session start orientation","top_k":1}',
    )
    empty = command_outcome(
        0, False, 1, '{"result":{"success":true,"data":{"count":0,"results":[]}}}', ""
    )
    result_row = {
        "content": "fixture result",
        "knowledge_base": "fixture",
        "file_path": "fixture.md",
        "score": 1.0,
        "tier": "semantic",
        "memory_id": "fixture-memory",
    }
    nonempty = command_outcome(
        0,
        False,
        1,
        json.dumps({"result": {"success": True, "data": {"count": 1, "results": [result_row]}}}),
        "",
    )
    runtime.response_sequences[vector] = [empty, nonempty]
    clock = Clock()
    result = _wait_for_knowledge_retrieval(
        probe, runtime, monotonic=clock.monotonic, sleep=clock.sleep
    )
    check(
        result["checkpoint_status"] == "verified" and clock.sleeps == [0.5],
        "stage-exit knowledge readiness polls until measured retrieval is nonempty",
    )
    _assert_evidence_digest(result, check)
    invalid_envelopes = (
        ("missing fields", '{"result":{"success":true,"data":{}}}'),
        (
            "count mismatch with no rows",
            '{"result":{"success":true,"data":{"count":1,"results":[]}}}',
        ),
        (
            "count mismatch with a row",
            '{"result":{"success":true,"data":{"count":0,"results":[{}]}}}',
        ),
        (
            "non-object row",
            '{"result":{"success":true,"data":{"count":1,"results":["not-an-object"]}}}',
        ),
        (
            "row missing canonical fields",
            '{"result":{"success":true,"data":{"count":1,"results":[{}]}}}',
        ),
        (
            "successful envelope with an error",
            json.dumps(
                {
                    "result": {
                        "success": True,
                        "error": {"message": "boom"},
                        "data": {"count": 1, "results": [result_row]},
                    }
                }
            ),
        ),
    )
    for label, envelope in invalid_envelopes:
        runtime.response_sequences[vector] = [
            command_outcome(0, False, 1, envelope, ""),
            empty,
        ]
        malformed_clock = Clock()
        malformed = _wait_for_knowledge_retrieval(
            probe, runtime, monotonic=malformed_clock.monotonic, sleep=malformed_clock.sleep
        )
        check(
            malformed["error_kind"] == "knowledge_retrieval_protocol_invalid"
            and not malformed_clock.sleeps
            and len(runtime.response_sequences[vector]) == 1,
            f"{label} successful knowledge envelope fails immediately without a retry",
        )
    runtime.response_sequences[vector] = [
        command_outcome(
            1,
            True,
            1,
            json.dumps({"result": {"success": True, "data": {"count": 1, "results": [result_row]}}}),
            "",
        ),
        empty,
    ]
    timed_out_clock = Clock()
    timed_out = _wait_for_knowledge_retrieval(
        probe, runtime, monotonic=timed_out_clock.monotonic, sleep=timed_out_clock.sleep
    )
    check(
        timed_out["checkpoint_status"] == "blocked"
        and timed_out["error_kind"] == "knowledge_retrieval_timeout"
        and not timed_out_clock.sleeps
        and len(runtime.response_sequences[vector]) == 1,
        "governed knowledge process timeout is an immediate timeout, not a protocol error",
    )
    timeout_probe = request(
        target,
        operation_id="knowledge_retrieval_succeeds",
        operation_ref="service_interface::knowledge_service.search",
        probe_purpose="stage_exit",
        timeout_seconds=6,
    )
    runtime.response_sequences[vector] = [empty, empty]
    deadline_clock = Clock()
    deadline = _wait_for_knowledge_retrieval(
        timeout_probe,
        SlowRuntime(runtime, deadline_clock),
        monotonic=deadline_clock.monotonic,
        sleep=deadline_clock.sleep,
    )
    check(
        deadline["checkpoint_status"] == "blocked"
        and deadline_clock.now == 1.0
        and len(runtime.response_sequences[vector]) == 1,
        "knowledge poll never starts a governed call beyond its remaining deadline",
    )
    _assert_pre_call_timeout(timeout_probe, runtime, _wait_for_knowledge_retrieval, check)
    _assert_parent_budget_cap(
        target, runtime, request, check, nonempty, vector, _wait_for_knowledge_retrieval
    )
    _assert_extended_readiness(target, runtime, request, check, command_outcome, vector, empty, nonempty)
    runtime.response_sequences.pop(vector)



def _assert_extended_readiness(
    target: Path,
    runtime: Any,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
    vector: tuple[str, ...],
    empty: Any,
    nonempty: Any,
) -> None:
    from github_midwife_plugin.installation_state_doctor import _wait_for_knowledge_retrieval

    class TimedRuntime:
        def __init__(self, responses: list[Any], durations: list[float]) -> None:
            self.home = runtime.home
            self.responses = responses
            self.durations = durations
            self.now = 0.0
            self.timeouts: list[int] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

        def run(self, *args: Any, **kwargs: Any) -> Any:
            self.timeouts.append(kwargs["timeout_seconds"])
            self.now += self.durations.pop(0)
            return self.responses.pop(0)

    def run(responses: list[Any], durations: list[float], budget: int = 120) -> tuple[Any, TimedRuntime]:
        timed = TimedRuntime(responses, durations)
        probe = request(target, operation_id="knowledge_retrieval_succeeds",
                        operation_ref="service_interface::knowledge_service.search",
                        probe_purpose="stage_exit", timeout_seconds=budget)
        response = _wait_for_knowledge_retrieval(
            probe, timed, monotonic=timed.monotonic, sleep=timed.sleep
        )
        return response, timed

    response, timed = run([empty, nonempty], [40.0, 1.0])
    check(response["checkpoint_status"] == "verified" and timed.now == 41.5,
          "readiness beyond the old 30-second budget verifies before the new deadline")
    response, timed = run([empty, nonempty], [60.0, 54.0])
    check(response["checkpoint_status"] == "verified" and timed.timeouts == [60, 54]
          and timed.now < 115, "remaining child budget preserves parent launch/result reserve")
    response, timed = run([nonempty], [115.0])
    check(response["error_kind"] == "knowledge_retrieval_timeout",
          "success at the inner deadline cannot verify")
    response, timed = run([empty], [1.0], 6)
    check(response["error_kind"] == "knowledge_retrieval_timeout" and timed.now < 6,
          "persistent empty emits a structured retry-safe timeout before parent termination")
    response, timed = run([], [], 5)
    check(response["error_kind"] == "knowledge_retrieval_timeout" and not timed.timeouts,
          "exhausted reserve before first call never launches")
    _assert_extended_failures(run, command_outcome, nonempty, check)
    # An ordinary completion probe stays single-shot, including successful empty.
    from github_midwife_plugin.installation_state_doctor import knowledge
    probe = request(target, operation_id="knowledge_retrieval_succeeds",
                    operation_ref="service_interface::knowledge_service.search",
                    probe_purpose="completion", timeout_seconds=30)
    runtime.response_sequences[vector] = [empty, nonempty]
    response = knowledge(probe, runtime)
    check(response["checkpoint_status"] != "verified"
          and len(runtime.response_sequences[vector]) == 1,
          "ordinary completion remains single-shot")


def _assert_extended_failures(
    run: Callable[..., Any], command_outcome: Any, nonempty: Any,
    check: Callable[[object, str], None],
) -> None:
    from dataclasses import replace
    negatives = [
        (replace(nonempty, executable_missing=True), "knowledge_retrieval_executable_missing"),
        (replace(nonempty, returncode=1), "knowledge_retrieval_transport_failed"),
        (replace(nonempty, timed_out=True), "knowledge_retrieval_timeout"),
        (replace(nonempty, stdout_truncated=True, stdout_bytes=nonempty.stdout_bytes + 1), "output_truncated"),
        (replace(nonempty, stderr_truncated=True, stderr_bytes=1), "output_truncated"),
    ]
    negatives.append((command_outcome(0, False, 1,
                      '{"result":{"success":false,"error":{"message":"private-error"}}}', ""),
                      "knowledge_retrieval_service_failed"))
    for envelope in (
        "not-json", "[]",
        '{"result":{"success":true,"data":{"count":true,"results":[]}}}',
        '{"result":{"success":true,"data":{"count":-1,"results":[]}}}',
        '{"result":{"success":true,"data":{"count":0,"results":{}}}}',
        '{"result":{"success":true,"action_status":"failed","data":{"count":0,"results":[]}}}',
    ):
        negatives.append((command_outcome(0, False, 1, envelope, ""),
                          "knowledge_retrieval_protocol_invalid"))
    for field, value in (("score", True), ("score", float("nan")), ("content", None)):
        payload = json.loads(nonempty.stdout)
        payload["result"]["data"]["results"][0][field] = value
        negatives.append((command_outcome(0, False, 1, json.dumps(payload), ""),
                          "knowledge_retrieval_protocol_invalid"))
    for field, value in (("status", "failed"), ("error_message", "private-error"), ("status", [])):
        payload = json.loads(nonempty.stdout)
        payload[field] = value
        negatives.append((command_outcome(0, False, 1, json.dumps(payload), ""),
                          "knowledge_retrieval_protocol_invalid"))
    for outcome, error in negatives:
        response, timed = run([outcome, nonempty], [0.0, 0.0])
        check(response["error_kind"] == error and len(timed.timeouts) == 1,
              f"{error} fails immediately without retry or late success")
        check("private-error" not in json.dumps(response), "terminal evidence excludes raw payloads")

def _evidence_observed(response: Any, evidence_id: str) -> object:
    matches = [item for item in response["evidence"] if item["id"] == evidence_id]
    return matches[0]["observed"] if len(matches) == 1 else None


def run_plugin_roster_output_cap(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
    default_output_limit: int,
    structured_output_limit: int,
) -> None:
    """Exercise roster capture, truncation, and evidence through FakeRuntime."""

    from github_midwife_plugin.setup_adapter import dispatch_request

    manifest = target / "profile/config/manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    probe = request(
        target,
        operation_id="plugin_roster_matches_plan",
        operation_ref="service_interface::lifecycle_management_service.list_plugins",
        probe_purpose="stage_exit",
    )
    vector = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::lifecycle_management_service::list_plugins",
        "{}",
    )

    def row(
        name: str, *, status: str = "ready", padding: str = "", **optional: object,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "name": name,
            "status": status,
            "enabled": True,
            "lifecycle_managed": False,
            "is_running": False,
            "version": "1.2.3",
            "priority": 100,
            "process_count": 1,
        }
        if padding:
            value["metadata"] = padding
        value.update(optional)
        return value

    def roster(rows: list[dict[str, object]]) -> Any:
        payload = {"result": {"success": True, "error": None, "data": {"plugins": rows}}}
        return command_outcome(0, False, 1, json.dumps(payload), "")

    production_rows = [row(f"fixture_plugin_{index:02d}") for index in range(34)]
    manifest.write_text(
        "profile_name: fixture\nplugins:\n"
        + "".join(f"- {entry['name']}\n" for entry in production_rows),
        encoding="utf-8",
    )
    check(
        len(roster(production_rows).stdout.encode("utf-8")) > default_output_limit,
        "production-shaped roster exceeds the default command cap",
    )

    runtime.responses[vector] = roster(production_rows[:33])
    check(
        dispatch_request(probe, runtime)["checkpoint_status"] != "verified",
        "strict-subset roster is non-green",
    )
    runtime.responses[vector] = roster(production_rows + [row("unexpected_plugin")])
    check(
        dispatch_request(probe, runtime)["checkpoint_status"] != "verified",
        "strict-superset roster is non-green",
    )
    runtime.responses[vector] = roster(production_rows)
    exact = dispatch_request(probe, runtime)
    check(
        exact["checkpoint_status"] == "verified",
        "M-ROSTER-STRUCTURED-CAP-4096: exact production roster is green",
    )
    check(
        runtime.command_output_limits[-1] == structured_output_limit,
        "roster output_limit reaches _cap_stream",
    )

    dormant_rows = list(production_rows)
    dormant_rows[-1] = row(str(production_rows[-1]["name"]), status="uninitialized")
    runtime.responses[vector] = roster(dormant_rows)
    dormant = dispatch_request(probe, runtime)
    check(
        dormant["checkpoint_status"] != "verified",
        "unconfigured roster member is non-green",
    )
    last_name = str(production_rows[-1]["name"])
    check(
        _evidence_observed(dormant, "plugin_roster_unready")
        == [f"{last_name}: uninitialized: no last_error reported"],
        "iss_4b22fdeb: unready evidence names the plugin and its status",
    )

    torn = "Embedding is nonfinite or not normalized"
    erroring_rows = list(production_rows)
    erroring_rows[-1] = row(last_name, status="error", last_error=torn)
    runtime.responses[vector] = roster(erroring_rows)
    erroring = dispatch_request(probe, runtime)
    check(
        erroring["error_kind"] == "plugin_roster_unready"
        and _evidence_observed(erroring, "plugin_roster_unready") == [f"{last_name}: error: {torn}"],
        "iss_4b22fdeb: unready evidence carries each plugin's last_error",
    )
    check(
        f"{last_name}: error: {torn}" in str(erroring["repair"]),
        "iss_4b22fdeb: repair text names the unready plugin",
    )
    check(
        _evidence_observed(exact, "plugin_roster_unready") == [],
        "a green roster reports an empty unready list",
    )

    unavailable = "Apple Foundation Models unavailable: DEVICE_NOT_ELIGIBLE"
    degraded_rows = list(production_rows)
    degraded_rows[-1] = row(last_name, warning=unavailable)
    runtime.responses[vector] = roster(degraded_rows)
    degraded = dispatch_request(probe, runtime)
    warnings = [item for item in degraded["evidence"] if item["id"] == "plugin_roster_warnings"]
    check(
        degraded["checkpoint_status"] == "verified",
        "iss_7f4ce644: a ready-but-degraded plugin satisfies the roster",
    )
    check(
        len(warnings) == 1 and warnings[0]["status"] == "warning"
        and warnings[0]["observed"] == [f"{last_name}: {unavailable}"],
        "iss_7f4ce644: the degraded plugin's warning stays visible in doctor evidence",
    )

    for bad in ({"last_error": 7}, {"warning": ""}):
        malformed_rows = list(production_rows)
        malformed_rows[-1] = row(last_name, **bad)
        runtime.responses[vector] = roster(malformed_rows)
        check(
            dispatch_request(probe, runtime)["error_kind"] == "plugin_roster_invalid",
            f"a mistyped optional roster field is invalid, not silently dropped: {sorted(bad)}",
        )

    runtime.responses[vector] = roster(
        [row("oversized_plugin", padding="x" * structured_output_limit)]
    )
    truncated = dispatch_request(probe, runtime)
    check(
        truncated["error_kind"] == "output_truncated",
        "M-ROSTER-DROP-TRUNCATION-BRANCH: truncated roster is explicit",
    )
    evidence = truncated["evidence"]
    observed_values = _public_observed_values(evidence)
    check(
        observed_values is not None
        and observed_values.get("stdout_bytes", 0) > structured_output_limit,
        "truncated roster carries byte-count evidence",
    )
