"""Parent-origin budgets across Manager, bootstrap and plugin input boundaries."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "solet_cli/src"), str(ROOT / "plugins/github_midwife_plugin/src"), str(ROOT / "plugins/github_midwife_plugin/tests")]

from github_midwife_plugin import lm_studio_deadline, lm_studio_models, lm_studio_provisioning  # noqa: E402
from github_midwife_plugin.lm_studio_deadline import DeadlineExpiredError, ServedDeadline  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, result  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from lm_studio_index_smoke import INDEX_REF, LOAD_REF, MODEL_KEY, SERVED_PROBE, IndexRuntime, call  # noqa: E402
from lm_studio_provisioning_smoke import FixtureRuntime, request, write_artifact  # noqa: E402
from solet_manager import adapters  # noqa: E402
from solet_manager.adapter_protocol import OperationRequest  # noqa: E402

from bootstrap_adapter import routes  # noqa: E402

KEY = "lm_studio_parent_deadline_ns"
ROUTES = (
    ("load_lm_studio_embedding_model", "load_embedding"),
    ("load_lm_studio_inference_model", "load_inference"),
    ("lm_studio_embedding_model_served", "embedding_model_served"),
    ("lm_studio_inference_model_served", "inference_model_served"),
)


def manager_request(operation_id: str, suffix: str) -> OperationRequest:
    apply = suffix.startswith("load_")
    return OperationRequest(
        request_id="11111111-1111-4111-8111-111111111111", operation_id=operation_id,
        operation_ref=f"setup::lm_studio.{suffix}", phase="apply" if apply else "probe",
        probe_purpose=None if apply else "stage_exit", attempt=1, name="fixture", target=str(ROOT),
        flow_id="macos.repository_setup", flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64, approval_fingerprint="sha256:" + "c" * 64 if apply else None,
        dry_run=not apply, timeout_seconds=300 if apply else 30,
        public_inputs={"embeddings_implementation": "lm_studio", "inference_implementation": "lm_studio", "lm_studio_base_url": "http://127.0.0.1:1234/v1"},
    )


def check_envelopes() -> None:
    clock = FakeClock()
    with tempfile.TemporaryDirectory(prefix="served-envelope-") as temporary, patch.object(lm_studio_deadline.time, "monotonic_ns", clock.now):
        runtime = FixtureRuntime(Path(temporary))
        runtime.server = True
        binary = lm_studio_models.cli_path(runtime.home)
        binary.parent.mkdir(parents=True)
        binary.write_text("fixture")
        binary.chmod(0o700)
        for model in runtime.models.values():
            write_artifact(runtime.home, model)
            runtime.loaded.add(model.api_identifier)
        with patch.object(lm_studio_provisioning, "reviewed_models", return_value=runtime.models), patch.object(routes, "lm_studio_route", side_effect=lambda raw, _runtime: dispatch_request(AdapterRequest.from_dict(dict(raw)), runtime)):
            for operation_id, suffix in ROUTES:
                req = manager_request(operation_id, suffix)
                raw = req.to_dict()
                raw["public_inputs"] = {**req.public_inputs, KEY: clock.now() + req.timeout_seconds * 1_000_000_000}
                answer = routes.execute_adapter_request(raw)
                assert answer["checkpoint_status"] in {"verified", "applied"}, (suffix, answer)
                _check_invalid_deadlines(raw, req, clock)
            unrelated = manager_request("lm_studio_cli_available", "cli_available")
            bad = replace(unrelated, public_inputs={**unrelated.public_inputs, KEY: clock.now() + 30_000_000_000})
            assert routes.execute_adapter_request(bad.to_dict())["error_kind"] == "adapter_protocol_error"
            assert dispatch_request(AdapterRequest.from_dict(bad.to_dict()), runtime)["error_kind"] == "adapter_protocol_error"
            req = manager_request(*ROUTES[2])
            bad = replace(req, public_inputs={**req.public_inputs, "unrelated_extra": 1})
            assert routes.execute_adapter_request(bad.to_dict())["error_kind"] == "adapter_protocol_error"
            assert dispatch_request(AdapterRequest.from_dict(bad.to_dict()), runtime)["error_kind"] == "adapter_protocol_error"


def _check_invalid_deadlines(raw: dict[str, object], req: OperationRequest, clock: FakeClock) -> None:
    for value in (True, "invalid", None, -1, 0):
        raw["public_inputs"][KEY] = value
        assert routes.execute_adapter_request(raw)["checkpoint_status"] == "blocked", value
    raw["public_inputs"] = dict(req.public_inputs)
    assert routes.execute_adapter_request(raw)["error_kind"] == "lm_studio_deadline_contract_invalid"
    raw["public_inputs"][KEY] = clock.now() + (req.timeout_seconds + 1) * 1_000_000_000
    assert routes.execute_adapter_request(raw)["error_kind"] == "lm_studio_deadline_contract_invalid"
    raw["public_inputs"][KEY] = clock.now() - 1
    assert routes.execute_adapter_request(raw)["error_kind"] == "lm_studio_served_budget_exhausted"


def check_manager_clock() -> None:
    clock = FakeClock()
    req = manager_request(*ROUTES[3])
    req = replace(req, public_inputs={**req.public_inputs, KEY: 999999999999999})
    registry = adapters.AdapterRegistry(target=ROOT)
    seen: list[dict[str, object]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raw = json.loads(str(kwargs["input"]))
        seen.append(raw)
        assert raw["public_inputs"][KEY] == 1030_000_000_000
        assert kwargs["timeout"] == 28
        return subprocess.CompletedProcess(command, 0, json.dumps(result(AdapterRequest.from_dict(raw), status="verified")), "")

    def capture(request: OperationRequest, kind: str, envelope: object) -> None:
        del request, envelope
        if kind == "request":
            clock.sleep(2)

    with patch.object(adapters.time, "monotonic_ns", clock.now), patch.object(adapters, "_capture_envelope", side_effect=capture), patch.object(registry, "command_for", return_value=(sys.executable, "fixture")), patch.object(adapters.subprocess, "run", side_effect=run):
        answer = adapters.invoke_adapter(registry, runner="bootstrap", request=req)
        assert answer.checkpoint_status.value == "verified"
    assert req.public_inputs[KEY] == 999999999999999 and len(seen) == 1
    clock.seconds = 1000

    def late(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        clock.sleep(30)
        raw = json.loads(str(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(result(AdapterRequest.from_dict(raw), status="verified")), "")

    with patch.object(adapters.time, "monotonic_ns", clock.now), patch.object(registry, "command_for", return_value=(sys.executable, "fixture")), patch.object(adapters.subprocess, "run", side_effect=late):
        assert adapters.invoke_adapter(registry, runner="bootstrap", request=req).error_kind == "adapter_timeout"
    clock.seconds = 1000
    with patch.object(adapters.time, "monotonic_ns", clock.now), patch.object(adapters, "_capture_envelope", side_effect=lambda *_: clock.sleep(30)), patch.object(registry, "command_for", return_value=(sys.executable, "fixture")), patch.object(adapters.subprocess, "run") as launch:
        assert adapters.invoke_adapter(registry, runner="bootstrap", request=req).error_kind == "adapter_timeout"
        launch.assert_not_called()


def check_hash_and_boundaries() -> None:
    clock = FakeClock()
    with patch.object(lm_studio_deadline.time, "monotonic_ns", clock.now):
        deadline = ServedDeadline(clock.now(), clock.now() + 25_000_000_000)
        clock.sleep(24.1)
        try:
            deadline.call_budget(10)
        except DeadlineExpiredError:
            pass
        else:
            raise AssertionError("subsecond budget was rounded up")
        for elapsed, expected in ((20, 250), (269, 1), (270, None)):
            clock.seconds = 1000 + elapsed
            deadline = ServedDeadline(1000_000_000_000, 1295_000_000_000)
            try:
                budget = deadline.call_budget(290, reserve=25)
            except DeadlineExpiredError:
                assert expected is None
            else:
                assert budget == expected
        clock.seconds = 1000
        deadline = ServedDeadline(clock.now(), clock.now() + 25_000_000_000)

        class SlowStream(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                if size == 1024 * 1024:
                    clock.sleep(25)
                return super().read(size)

        try:
            lm_studio_models._hash_model_stream(SlowStream(b"GGUF" + b"x" * 50), deadline)
        except DeadlineExpiredError:
            pass
        else:
            raise AssertionError("late hash accepted")


def check_parent_hard_stop() -> None:
    req = replace(manager_request(*ROUTES[3]), timeout_seconds=1)
    registry = adapters.AdapterRegistry(target=ROOT)
    with patch.object(registry, "command_for", return_value=(sys.executable, "-c", "import time; time.sleep(10)")):
        assert adapters.invoke_adapter(registry, runner="bootstrap", request=req).error_kind == "adapter_timeout"


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 1000.0

    def now(self) -> int:
        return int(self.seconds * 1_000_000_000)

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds
        assert self.seconds <= 2000, "polling reset the shared deadline"


class DelayedRuntime(IndexRuntime):
    def __init__(self, home: Path, clock: FakeClock, scenario: str) -> None:
        super().__init__(home)
        self.clock = clock
        self.scenario = scenario
        self.reads = 0
        self.budgets: list[int] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: int, cwd: Path | None = None, extra_env: dict[str, str] | None = None, input_text: str | None = None, output_limit: int = 4096) -> CommandOutcome:
        self.budgets.append(timeout_seconds)
        if self.scenario == "slow_commands" and argv[1] in {"ls", "ps"}:
            self.clock.sleep(timeout_seconds)
        return super().run(argv, timeout_seconds=timeout_seconds, cwd=cwd, extra_env=extra_env, input_text=input_text, output_limit=output_limit)

    def http_json(self, url: str, *, timeout_seconds: int, payload: JsonObject | None = None) -> tuple[int, JsonValue]:
        if "/api/v0/" not in url:
            return super().http_json(url, timeout_seconds=timeout_seconds, payload=payload)
        self.reads += 1
        if self.scenario == "transport":
            raise ConnectionRefusedError
        if self.scenario == "malformed":
            return 200, {"data": [{"id": "qwen3-14b", "state": "bad"}]}
        if self.scenario == "opposite":
            return 200, {"data": [{"id": "qwen3-14b", "state": "loaded"}]}
        if self.scenario == "late":
            self.clock.sleep(25)
        if self.scenario == "source_change":
            self.source.write_bytes(self.source.read_bytes()[:-1] + b"X")
        if self.scenario in {"delay", "load_delay"} and self.reads < 3:
            return 200, {"data": []}
        return super().http_json(url, timeout_seconds=timeout_seconds, payload=payload)


def _deadline_case(scenario: str, *, phase: str = "probe", purpose: str = "stage_exit") -> tuple[JsonObject, int, int, float]:
    clock = FakeClock()
    with tempfile.TemporaryDirectory(prefix="served-deadline-") as temporary, patch.object(lm_studio_deadline.time, "monotonic_ns", clock.now), patch.object(lm_studio_deadline.time, "sleep", clock.sleep):
        runtime = DelayedRuntime(Path(temporary), clock, scenario)
        assert call(runtime, INDEX_REF, phase="apply")["checkpoint_status"] == "applied"
        if scenario not in {"absent", "opposite", "load_delay"} and phase != "apply":
            runtime.loaded_by_id["qwen3-14b"] = MODEL_KEY
            runtime.loaded.add("qwen3-14b")
        req = request(LOAD_REF if phase == "apply" else SERVED_PROBE, phase=phase, timeout=300 if phase == "apply" else 30)
        req = replace(req, probe_purpose=purpose if phase == "probe" else None)
        started = clock.seconds
        with patch.object(lm_studio_provisioning, "reviewed_models", return_value=runtime.models):
            answer = dispatch_request(req, runtime)
        assert all(budget >= 1 for budget in runtime.budgets)
        return answer, runtime.load_count, runtime.reads, clock.seconds - started


def check_deadline_controls() -> None:
    for scenario in ("immediate", "delay"):
        answer, loads, reads, elapsed = _deadline_case(scenario)
        assert answer["checkpoint_status"] == "verified" and loads == 0, (scenario, answer)
        assert reads == (1 if scenario == "immediate" else 3) and elapsed < 25
    answer, loads, reads, elapsed = _deadline_case("load_delay", phase="apply")
    assert answer["checkpoint_status"] == "applied" and loads == 1 and reads == 3, answer
    _check_deadline_failures()
    _check_deadline_one_shots()


def _check_deadline_failures() -> None:
    for scenario in ("absent", "transport", "late", "slow_commands"):
        answer, loads, _, elapsed = _deadline_case(scenario)
        assert answer["checkpoint_status"] == "blocked" and answer["error_kind"] == "lm_studio_served_budget_exhausted" and loads == 0, (scenario, answer)
        assert elapsed <= 25, (scenario, elapsed)
    for scenario in ("malformed", "opposite", "source_change"):
        answer, loads, reads, _ = _deadline_case(scenario)
        assert answer["checkpoint_status"] == "blocked" and loads == 0 and reads == 1, (scenario, answer)


def _check_deadline_one_shots() -> None:
    for purpose in ("pre_apply", "preview", "stage_entry"):
        answer, loads, reads, elapsed = _deadline_case("delay", purpose=purpose)
        assert answer["checkpoint_status"] != "verified" and loads == 0 and reads == 1 and elapsed == 0, answer
    answer, loads, reads, _ = _deadline_case("transport", phase="apply")
    assert answer["checkpoint_status"] == "blocked" and loads == 0 and reads == 1


def main() -> None:
    check_deadline_controls()
    check_envelopes()
    check_manager_clock()
    check_hash_and_boundaries()
    check_parent_hard_stop()
    print("PASS: real bootstrap/plugin envelope, Manager overwrite/launch/late refusal, parent hard stop, shared load budget and cooperative hash")


if __name__ == "__main__":
    main()
