"""Deterministic full-stack scenarios for the derived readiness deadline."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

_SEALED_PRODUCTION_SUBJECT = os.environ.get("SOLET_SEALED_SUBJECT_PRODUCTION")
_REPO = (
    Path(_SEALED_PRODUCTION_SUBJECT).resolve(strict=True)
    if _SEALED_PRODUCTION_SUBJECT is not None
    else Path(__file__).resolve().parents[3]
)
sys.path.insert(0, str(_REPO / "solet_cli/src"))
sys.path.insert(0, str(_REPO / "plugins/github_midwife_plugin/tests"))
sys.path.insert(
    0,
    str(_REPO / "plugins/github_midwife_plugin/src"),
)

from github_midwife_plugin import installation_doctor  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import (  # noqa: E402
    AdapterRequest,
    JsonObject,
    JsonValue,
)
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from readiness_deadline_runtime_support import (  # noqa: E402
    VirtualClock,
    VirtualRuntime,
)
from solet_manager import completion_verifier, stage_boundaries  # noqa: E402
from solet_manager.adapter_protocol import (  # noqa: E402
    OperationRequest,
    OperationResult,
)
from solet_manager.adapter_validation import validate_evidence  # noqa: E402
from solet_manager.contracts import (  # noqa: E402
    ContractBundle,
    startup_readiness_budget,
)
from solet_manager.errors import AdapterProtocolError, ContractError  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402

_CONTRACTS = _REPO / "plugins/github_midwife_plugin/knowledge_base"
_PROBE_ID = "embedding_request_succeeds"


def _target(root: Path, name: str = "newborn") -> Path:
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    target = target.resolve(strict=True)
    launcher = target / ".venv/bin/solet-bridge"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f"#!{target}/.venv/bin/python3\n", encoding="utf-8")
    launcher.chmod(0o755)
    return target


def _capture_requests(
    bundle: ContractBundle,
    target: Path,
    root: Path,
) -> dict[str, OperationRequest]:
    captured: dict[str, OperationRequest] = {}

    def capture(
        registry: object,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        del registry, runner
        purpose = request.probe_purpose
        if not isinstance(purpose, str):
            raise AssertionError("captured readiness probe must have a purpose")
        captured[purpose] = request
        return OperationResult.blocked(
            request,
            error_kind="fixture_capture",
            repair="fixture only",
        )

    transaction = Transaction.create(
        name="newborn",
        target=target,
        input_fingerprint="sha256:" + "1" * 64,
        answers={},
        seed=SeedLock(
            "https://example.invalid/readiness-fixture.git",
            "release-fixture",
            "a" * 40,
            "b" * 40,
            "c" * 64,
            "readiness-fixture",
        ),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=bundle.completion_probe_ids,
    )
    placeholder = SimpleNamespace(
        name="newborn",
        target=str(target),
        answers_fingerprint=transaction.answers_fingerprint,
    )
    with patch.object(stage_boundaries, "invoke_adapter", capture):
        stage_boundaries._invoke_boundary_probe(
            bundle=bundle,
            transaction=cast(Any, placeholder),
            registry=cast(Any, object()),
            stage_id="models",
            boundary="exit",
            probe_id=_PROBE_ID,
            answers={},
            attempt=1,
        )
    paths = ManagerPaths.resolve(
        environ={},
        home=root / "home",
        explicit_home=root / "manager",
    )
    with patch.object(completion_verifier, "invoke_adapter", capture):
        completion_verifier._run_completion_probe(
            bundle=bundle,
            transaction=transaction,
            registry=cast(Any, object()),
            paths=paths,
            probe_id=_PROBE_ID,
        )
    return captured


def _plugin_request(request: OperationRequest) -> AdapterRequest:
    raw = cast(dict[str, object], request.to_dict())
    return AdapterRequest.from_dict(raw)


def _observe_foreign_health(runtime: VirtualRuntime) -> None:
    for foreign, vector in zip(runtime.foreign.targets, runtime.foreign.vectors, strict=True):
        observation = runtime.run(
            vector,
            timeout_seconds=5,
            cwd=foreign,
            extra_env={"SOLET_NAME": foreign.name},
        )
        assert installation_doctor._bridge_health_status(observation) == "healthy"


def _target_readiness_evidence(response: JsonObject) -> JsonObject:
    evidence = cast(list[JsonObject], response.get("evidence", []))
    return next(
        (item for item in evidence if item.get("id") == "target_bridge_ready"),
        {},
    )


def _assert_closed_readiness_observed_shape(response: JsonObject) -> None:
    readiness_evidence = _target_readiness_evidence(response)
    if not readiness_evidence:
        return
    observed = readiness_evidence.get("observed")
    if not isinstance(observed, list) or not all(isinstance(item, str) for item in observed):
        raise AssertionError("readiness evidence producer emits the closed v1 string-array shape")
    observed_items = cast(list[str], observed)
    assert observed_items == sorted(set(observed_items)), (
        "readiness evidence producer emits deterministic unique key=value strings"
    )
    dict_observed_red_mutation = {**readiness_evidence, "observed": {"red": "mutation"}}
    try:
        validate_evidence(dict_observed_red_mutation)
    except AdapterProtocolError:
        pass
    else:
        raise AssertionError(
            "readiness evidence dict red mutation must be rejected by the frozen consumer"
        )


def _arm_result(
    response: JsonObject,
    runtime: VirtualRuntime,
    clock: VirtualClock,
    release_at: float | None,
    ambient_name_valid: bool,
    ambient_cwd_valid: bool,
) -> dict[str, JsonValue]:
    _assert_closed_readiness_observed_shape(response)
    readiness_evidence = _target_readiness_evidence(response)
    return {
        "status": response["checkpoint_status"],
        "error_kind": response["error_kind"],
        "process_calls": runtime.process_calls,
        "probe_before_release": bool(
            release_at is not None
            and any(process_time < release_at for process_time in runtime.process_times)
        ),
        "process_timeouts": runtime.process_timeouts,
        "health_count": runtime.health_count,
        "foreign_health_calls": runtime.foreign.calls,
        "foreign_health_observations": runtime.foreign.observations,
        "target_redirects_valid": bool(runtime.target_redirects) and all(runtime.target_redirects),
        "ambient_name_challenged": ambient_name_valid,
        "ambient_cwd_challenged": ambient_cwd_valid,
        "monotonic_end": clock.monotonic_now,
        "wall_end": clock.wall_now,
        "trace": runtime.trace,
        "trace_sha256": hashlib.sha256(
            json.dumps(runtime.trace, separators=(",", ":")).encode()
        ).hexdigest(),
        "readiness_observed": readiness_evidence.get("observed"),
    }


def _arm(
    request: OperationRequest,
    root: Path,
    target: Path,
    *,
    release_at: float | None,
    wall_jump_per_sleep: float = 0.0,
    foreign_first: bool = False,
    ambient_name: str | None = None,
    ambient_cwd: Path | None = None,
) -> dict[str, JsonValue]:
    clock = VirtualClock(wall_jump_per_sleep=wall_jump_per_sleep)
    foreign_targets = (
        _target(target.parent, "foreign-primary"),
        _target(target.parent, "foreign-secondary"),
    )
    runtime = VirtualRuntime(
        root,
        target,
        clock,
        release_at,
        foreign_targets,
        request.name,
    )
    real_wait = installation_doctor._wait_for_target_readiness

    if foreign_first:
        _observe_foreign_health(runtime)

    def virtual_wait(adapter_request: AdapterRequest, adapter_runtime: VirtualRuntime) -> object:
        return real_wait(
            adapter_request,
            adapter_runtime,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    challenge_name = ambient_name or request.name
    challenge_cwd = ambient_cwd or root
    challenge_cwd.mkdir(parents=True, exist_ok=True)
    with (
        patch.dict(os.environ, {"SOLET_NAME": challenge_name}, clear=False),
        chdir(challenge_cwd),
        patch.object(
            installation_doctor,
            "_wait_for_target_readiness",
            virtual_wait,
        ),
    ):
        ambient_name_observed = os.environ.get("SOLET_NAME")
        ambient_cwd_observed = Path.cwd()
        response = dispatch_request(_plugin_request(request), runtime)
    return _arm_result(
        response,
        runtime,
        clock,
        release_at,
        ambient_name_observed == challenge_name and challenge_name != request.name,
        ambient_cwd_observed == challenge_cwd.resolve(strict=True)
        and ambient_cwd_observed != target,
    )


def _deterministic_report(
    root: Path,
) -> tuple[dict[str, JsonValue], dict[str, OperationRequest], Path]:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    target = _target(root)
    requests = _capture_requests(bundle, target, root)
    arms: dict[str, JsonValue] = {}
    for purpose in ("stage_exit", "completion"):
        outcomes = [
            _arm(
                requests[purpose],
                root / f"{purpose}-{attempt}",
                target,
                release_at=33.0,
            )
            for attempt in range(10)
        ]
        arms[purpose] = {
            "statuses": [item["status"] for item in outcomes],
            "errors": [item["error_kind"] for item in outcomes],
            "process_calls": [item["process_calls"] for item in outcomes],
            "probe_before_release": [item["probe_before_release"] for item in outcomes],
            "trace_hashes": [item["trace_sha256"] for item in outcomes],
            "trace": outcomes[0]["trace"],
            "timeout_seconds": requests[purpose].timeout_seconds,
            "public_inputs": requests[purpose].public_inputs,
        }
    return arms, requests, target


def _assert_deterministic_green(report: dict[str, JsonValue]) -> None:
    for purpose in ("stage_exit", "completion"):
        arm = cast(dict[str, JsonValue], report[purpose])
        assert arm["statuses"] == ["verified"] * 10, (
            "derived readiness schedule must pass stage_exit and completion 10/10"
        )
        assert arm["process_calls"] == [1] * 10, (
            "governed process must run exactly once after release"
        )
        assert arm["probe_before_release"] == [False] * 10, (
            "governed process must never run while the controlled latch is closed"
        )
        hashes = cast(list[str], arm["trace_hashes"])
        assert len(set(hashes)) == 1, "normalized traces must be identical 10/10"


def _assert_nonconsumer_completion_reaches_process(
    request: OperationRequest,
    root: Path,
) -> None:
    synthetic = replace(
        request,
        operation_id="generic_completion_fixture",
        operation_ref="plugin::g_suite_plugin.test_connection",
        public_inputs={},
    )
    calls: list[str] = []

    def successful_call(
        adapter_request: AdapterRequest,
        runtime: object,
        process_key: str,
        arguments: JsonObject,
        *,
        timeout_seconds: int | None = None,
    ) -> CommandOutcome:
        del adapter_request, runtime, arguments, timeout_seconds
        calls.append(process_key)
        return CommandOutcome(
            0,
            False,
            1,
            '{"result": {"success": true, "error": null, "data": {}}}',
            "",
        )

    with patch.object(installation_doctor, "_solet_call", successful_call):
        response = dispatch_request(
            _plugin_request(synthetic),
            VirtualRuntime(
                root,
                synthetic.target,
                VirtualClock(),
                0.0,
                (_target(root, "completion-foreign-a"), _target(root, "completion-foreign-b")),
                synthetic.name,
            ),
        )
    assert response["checkpoint_status"] == "verified"
    assert calls == ["plugin::g_suite_plugin::test_connection"]


def _boundary_controls(
    requests: dict[str, OperationRequest],
    root: Path,
    target: Path,
) -> dict[str, JsonValue]:
    request = requests["stage_exit"]
    values = request.public_inputs
    parent = cast(int, values["startup_readiness_parent_budget_seconds"])
    reserve = cast(
        int,
        values["startup_readiness_governed_process_call_seconds"],
    )
    wait = parent - reserve
    controls = {
        "just_before": _arm(request, root / "just-before", target, release_at=wait - 0.5),
        "just_after": _arm(request, root / "just-after", target, release_at=wait + 0.5),
        "boundary": _arm(request, root / "boundary", target, release_at=float(wait)),
        "never": _arm(request, root / "never", target, release_at=None),
        "wall_jump": _arm(
            request,
            root / "wall-jump",
            target,
            release_at=None,
            wall_jump_per_sleep=10_000.0,
        ),
    }
    assert controls["just_before"]["status"] == "verified"
    for key in ("just_after", "boundary", "never", "wall_jump"):
        assert controls[key]["error_kind"] == "target_readiness_timeout"
        assert controls[key]["process_calls"] == 0
        assert controls[key]["monotonic_end"] == wait
    assert controls["wall_jump"]["wall_end"] != controls["never"]["wall_end"]
    return cast(dict[str, JsonValue], controls)


def _foreign_first_outcomes(
    request: OperationRequest,
    root: Path,
    target: Path,
) -> list[dict[str, JsonValue]]:
    return [
        _arm(
            request,
            root / f"foreign-{attempt}",
            target,
            release_at=2.0,
            foreign_first=True,
            ambient_name="ambient-foreign",
            ambient_cwd=root / f"ambient-cwd-{attempt}",
        )
        for attempt in range(10)
    ]


def _identity_failure_controls(
    request: OperationRequest,
    root: Path,
    target: Path,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    foreign = _target(root, "foreign-target")
    symlink = root / "symlink-newborn"
    symlink.symlink_to(foreign, target_is_directory=True)
    symlink_request = replace(request, target=str(symlink))
    symlink_result = _arm(
        symlink_request,
        root / "symlink-arm",
        symlink,
        release_at=0.0,
    )
    assert symlink_result["error_kind"] == "target_readiness_identity_invalid"
    assert symlink_result["process_calls"] == 0

    launcher = target / ".venv/bin/solet-bridge"
    original = launcher.read_text(encoding="utf-8")
    launcher.write_text(f"#!{foreign}/.venv/bin/python3\n", encoding="utf-8")
    copied_result = _arm(
        request,
        root / "copied-launcher-arm",
        target,
        release_at=0.0,
    )
    launcher.write_text(original, encoding="utf-8")
    launcher.chmod(0o755)
    assert copied_result["error_kind"] == "target_readiness_identity_invalid"
    assert copied_result["process_calls"] == 0
    return symlink_result, copied_result


def _values(
    outcomes: list[dict[str, JsonValue]],
    key: str,
) -> list[JsonValue]:
    return [item[key] for item in outcomes]


def _foreign_and_identity_controls(
    request: OperationRequest,
    root: Path,
    target: Path,
) -> dict[str, JsonValue]:
    foreign_first = _foreign_first_outcomes(request, root, target)
    _assert_foreign_isolation(foreign_first)
    symlink_result, copied_result = _identity_failure_controls(request, root, target)
    return {
        "foreign_trace_sha256": foreign_first[0]["trace_sha256"],
        "foreign_runs": len(foreign_first),
        "foreign_health_calls": _values(foreign_first, "foreign_health_calls"),
        "foreign_health_observations": _values(foreign_first, "foreign_health_observations"),
        "governed_call_before_newborn_release": _values(foreign_first, "probe_before_release"),
        "target_redirects_valid": _values(foreign_first, "target_redirects_valid"),
        "ambient_name_challenged": _values(foreign_first, "ambient_name_challenged"),
        "ambient_cwd_challenged": _values(foreign_first, "ambient_cwd_challenged"),
        "symlink_error": symlink_result["error_kind"],
        "copied_launcher_error": copied_result["error_kind"],
    }


def _assert_foreign_isolation(foreign_first: list[dict[str, JsonValue]]) -> None:
    assert _values(foreign_first, "status") == ["verified"] * 10
    assert _values(foreign_first, "process_calls") == [1] * 10
    assert _values(foreign_first, "probe_before_release") == [False] * 10, (
        "foreign-first control allowed a governed call before newborn release"
    )
    assert _values(foreign_first, "foreign_health_calls") == [2] * 10
    observations = cast(
        list[list[JsonObject]],
        _values(foreign_first, "foreign_health_observations"),
    )
    _assert_foreign_observations(observations)
    assert _values(foreign_first, "target_redirects_valid") == [True] * 10
    assert _values(foreign_first, "ambient_name_challenged") == [True] * 10
    assert _values(foreign_first, "ambient_cwd_challenged") == [True] * 10
    assert len(set(cast(list[str], _values(foreign_first, "trace_sha256")))) == 1


def _assert_foreign_observations(observations: list[list[JsonObject]]) -> None:
    expected_names = ["foreign-primary", "foreign-secondary"]
    for run in observations:
        assert [observation["name"] for observation in run] == expected_names
        for observation in run:
            assert observation["status"] == "healthy"
            assert observation["before_newborn_release"] is True
            assert observation["redirected"] is True


def _write_mutated_contracts(
    root: Path,
    name: str,
    mutation: Any,
) -> Path:
    directory = root / name
    shutil.copytree(_CONTRACTS, directory)
    flow_path = directory / "macos_setup_flow.json"
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    mutation(flow)
    flow_path.write_text(json.dumps(flow), encoding="utf-8")
    return directory


def _authority_controls(root: Path, target: Path) -> dict[str, JsonValue]:
    changed = _write_mutated_contracts(
        root,
        "changed-authority",
        lambda flow: flow["executor_contracts"]["start_command"].update({"timeout_seconds": 140}),
    )
    changed_bundle = ContractBundle.load(source_revision="a" * 40, directory=changed)
    changed_requests = _capture_requests(changed_bundle, target, root / "changed-capture")
    assert {request.timeout_seconds for request in changed_requests.values()} == {140}
    assert {
        request.public_inputs["startup_readiness_parent_budget_seconds"]
        for request in changed_requests.values()
    } == {140}

    unrelated = root / "unrelated-timeout"
    shutil.copytree(_CONTRACTS, unrelated)
    adapter_schema_path = unrelated / "setup_adapter_envelope.schema.json"
    adapter_schema = json.loads(adapter_schema_path.read_text(encoding="utf-8"))
    _replace_first_timeout_maximum(adapter_schema)
    adapter_schema_path.write_text(json.dumps(adapter_schema), encoding="utf-8")
    unrelated_bundle = ContractBundle.load(source_revision="a" * 40, directory=unrelated)
    assert startup_readiness_budget(unrelated_bundle).effective_wait_seconds == 90

    invalid_errors: list[str] = []
    mutations = (
        lambda flow: flow["executor_contracts"]["start_command"].pop("startup_readiness"),
        lambda flow: flow["executor_contracts"]["start_command"]["startup_readiness"].update(
            {"budget_unit": "milliseconds"}
        ),
        lambda flow: flow["executor_contracts"]["start_command"]["startup_readiness"][
            "downstream_reservations"
        ].update({"governed_process_call_seconds": -1}),
        lambda flow: flow["executor_contracts"]["start_command"]["startup_readiness"][
            "downstream_reservations"
        ].update({"governed_process_call_seconds": 120}),
        lambda flow: flow["executor_contracts"]["start_command"].update(
            {"timeout_seconds": float("nan")}
        ),
        lambda flow: flow["executor_contracts"]["start_command"].update({"timeout_seconds": "120"}),
    )
    for index, mutation in enumerate(mutations):
        directory = _write_mutated_contracts(root, f"invalid-{index}", mutation)
        try:
            ContractBundle.load(source_revision="a" * 40, directory=directory)
        except ContractError as exc:
            invalid_errors.append(str(exc))
        else:
            raise AssertionError("invalid readiness authority must fail closed")
    assert len(invalid_errors) == len(mutations)
    assert all("executor_contracts.start_command" in error for error in invalid_errors)
    return {
        "changed_parent_seconds": 140,
        "changed_wait_seconds": 110,
        "unrelated_timeout_wait_seconds": 90,
        "invalid_cases": len(invalid_errors),
    }


def _replace_first_timeout_maximum(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "integer" and value.get("maximum") == 900:
            value["maximum"] = 899
            return True
        return any(_replace_first_timeout_maximum(item) for item in value.values())
    if isinstance(value, list):
        return any(_replace_first_timeout_maximum(item) for item in value)
    return False


def _deterministic_evidence(report: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        purpose: {
            "statuses": cast(dict[str, JsonValue], report[purpose])["statuses"],
            "errors": cast(dict[str, JsonValue], report[purpose])["errors"],
            "process_calls": cast(dict[str, JsonValue], report[purpose])["process_calls"],
            "probe_before_release": cast(dict[str, JsonValue], report[purpose])[
                "probe_before_release"
            ],
            "trace_hashes": cast(dict[str, JsonValue], report[purpose])["trace_hashes"],
            "timeout_seconds": cast(dict[str, JsonValue], report[purpose])["timeout_seconds"],
        }
        for purpose in ("stage_exit", "completion")
    }


def _control_evidence(controls: dict[str, JsonValue]) -> dict[str, JsonValue]:
    boundaries = cast(dict[str, dict[str, JsonValue]], controls["boundaries"])
    return {
        "boundaries": {
            key: {
                "status": value["status"],
                "error_kind": value["error_kind"],
                "process_calls": value["process_calls"],
                "monotonic_end": value["monotonic_end"],
            }
            for key, value in boundaries.items()
        },
        "isolation": controls["isolation"],
        "authority": controls["authority"],
    }


def run_scenarios(*, emit: bool) -> dict[str, JsonValue]:
    with tempfile.TemporaryDirectory(prefix="readiness-deadline-") as temporary:
        root = Path(temporary).resolve(strict=True)
        production_source = Path(installation_doctor.__file__).resolve(strict=True)
        if emit:
            print(
                json.dumps(
                    {
                        "production_subject": {
                            "installation_doctor": str(production_source),
                            "installation_doctor_sha256": hashlib.sha256(
                                production_source.read_bytes()
                            ).hexdigest(),
                        }
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        report, requests, target = _deterministic_report(root)
        isolation = _foreign_and_identity_controls(
            requests["stage_exit"],
            root,
            target,
        )
        deterministic = _deterministic_evidence(report)
        if emit:
            print(json.dumps({"deterministic": deterministic}, sort_keys=True), flush=True)
        _assert_deterministic_green(report)
        _assert_nonconsumer_completion_reaches_process(
            requests["completion"], root / "nonconsumer-completion"
        )
        controls = {
            "boundaries": _boundary_controls(requests, root, target),
            "isolation": isolation,
            "authority": _authority_controls(root, target),
        }
        compact_controls = _control_evidence(controls)
        if emit:
            print(json.dumps({"controls": compact_controls}, sort_keys=True), flush=True)
        return {
            "deterministic": deterministic,
            "controls": compact_controls,
        }


def main() -> int:
    run_scenarios(emit=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
