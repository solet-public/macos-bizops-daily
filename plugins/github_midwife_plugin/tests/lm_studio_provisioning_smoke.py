"""Hermetic production-seam controls for the complete LM Studio setup flow."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/github_midwife_plugin/src"))
sys.path.insert(0, str(ROOT / "solet_cli/src"))

from github_midwife_plugin.lm_studio_login_agent import LABEL  # noqa: E402
from github_midwife_plugin.lm_studio_models import ModelArtifact, artifact_present, cli_path, model_loaded, reviewed_models  # noqa: E402
from github_midwife_plugin.lm_studio_provisioning import operation_handlers, probe_handlers  # noqa: E402
from github_midwife_plugin.lm_studio_settings import disable_jit, jit_disabled, settings_path  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject, JsonValue  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, SystemRuntime  # noqa: E402
from solet_manager.adapter_protocol import OperationRequest, OperationResult  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.operation_records import normalize_apply_result  # noqa: E402


def outcome(*, text: str = "", error: str = "", code: int | None = 0, timeout: bool = False) -> CommandOutcome:
    return CommandOutcome(code, timeout, 1, text, error)


class FixtureRuntime(SystemRuntime):
    def __init__(self, home: Path) -> None:
        super().__init__(home=home)
        self.commands: list[tuple[str, ...]] = []
        self.loaded: set[str] = set()
        self.server = False
        self.login = False
        self.unknown = False
        self.timeout_pull = False
        self.pull_failure: str | None = None
        self.rewrite_first_start = False
        self.models = reviewed_models(ROOT)

    def run(self, argv: tuple[str, ...], *, timeout_seconds: int, cwd: Path | None = None, extra_env: dict[str, str] | None = None, input_text: str | None = None, output_limit: int = 4096) -> CommandOutcome:
        del cwd, extra_env, output_limit
        self.commands.append(argv)
        if argv[0] == "/usr/bin/curl":
            return outcome(text='APP_VERSION="0.0.24-1"\nprintf installed\n')
        if argv[0] == "/bin/sh":
            assert input_text is not None and 'APP_VERSION="0.0.23-1"' in input_text
            path = cli_path(self.home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture executable\n")
            path.chmod(0o700)
            return outcome(text="installer consumed pinned script\n")
        if argv[0] == "/bin/launchctl":
            return self._launchctl(argv)
        assert argv[0] == str(cli_path(self.home)), argv
        return self._lms(argv[1:], timeout_seconds)

    def _launchctl(self, argv: tuple[str, ...]) -> CommandOutcome:
        if argv[1] == "print":
            return outcome(text=f"{LABEL} = {{\n state = not running\n}}") if self.login else outcome(code=113, error=f'Could not find service "{LABEL}"')
        assert argv[1] in {"enable", "bootstrap"}
        if argv[1] == "bootstrap":
            self.login = True
        return outcome()

    def _lms(self, args: tuple[str, ...], timeout: int) -> CommandOutcome:
        if args[0] in {"daemon", "server"}:
            return self._lifecycle(args)
        if args[0] == "get":
            return self._pull(args, timeout)
        assert args[0] == "load", args
        model = next(item for item in self.models.values() if item.load_argv == args)
        assert artifact_present(model, self.home)
        self.loaded.add(model.api_identifier)
        return outcome()

    def _lifecycle(self, args: tuple[str, ...]) -> CommandOutcome:
        if args[:2] in (("daemon", "up"), ("server", "start")):
            assert jit_disabled(self.home)
            if args[0] == "server":
                self.server = True
                self._materialize_first_start()
        elif args[:2] in (("server", "stop"), ("daemon", "down")):
            self.server = False
        else:
            raise AssertionError(args)
        return outcome()

    def _materialize_first_start(self) -> None:
        if self.rewrite_first_start:
            settings_path(self.home).write_text('{"port":1234,"cors":true,"justInTimeModelLoading":true}')
            self.rewrite_first_start = False

    def _pull(self, args: tuple[str, ...], timeout: int) -> CommandOutcome:
        model = next(item for item in self.models.values() if item.get_argv == args)
        if self.pull_failure == "vendor_timeout":
            return outcome(code=1, error="Download failed: Timed-out. Please try to resume.")
        if self.pull_failure == "vendor_timeout_complete":
            write_artifact(self.home, model)
            return outcome(code=1, error="Download failed: Timed-out. Please try to resume.")
        if self.pull_failure == "other_error":
            return outcome(code=1, error="download request rejected")
        if self.timeout_pull:
            assert timeout == 890
            path = model.path(self.home).with_name(f"downloading_{model.filename}.part")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"preserved partial")
            return outcome(code=None, timeout=True)
        write_artifact(self.home, model)
        return outcome()

    def http_json(self, url: str, *, timeout_seconds: int, payload: JsonObject | None = None) -> tuple[int, JsonValue]:
        assert timeout_seconds <= 2 and payload is None
        if not self.server:
            return 503, None
        if url.endswith("/api/v0/models"):
            return 200, {"data": [{"id": item.api_identifier, "state": "unexpected" if self.unknown else "loaded" if item.api_identifier in self.loaded else "not-loaded"} for item in self.models.values()]}
        assert url.endswith("/v1/models")
        # Deliberately reproduce JIT's discovery list even while nothing is loaded.
        return 200, {"object": "list", "data": [{"id": item.api_identifier} for item in self.models.values()]}


def write_artifact(home: Path, model: ModelArtifact) -> None:
    path = model.path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(b"GGUF")
        stream.truncate(model.size_bytes)
    metadata = home / ".lmstudio/.internal/model-data.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(metadata.read_text()) if metadata.exists() else {"json": []}
    owner, repo = model.repository.split("/", 1)
    data["json"].append([f"{model.repository}/{model.filename}", {"source": {"type": "huggingface", "owner": owner, "repo": repo, "file": model.filename}}])
    metadata.write_text(json.dumps(data))


def request(reference: str, *, phase: str = "probe", timeout: int = 300) -> AdapterRequest:
    return AdapterRequest("11111111-1111-4111-8111-111111111111", "lm_studio_fixture", reference, phase, "preview" if phase == "probe" else None, 1, "fixture", ROOT, "a" * 40, "sha256:" + "b" * 64, None if phase == "probe" else "sha256:" + "c" * 64, phase == "probe", timeout, {"embeddings_implementation": "lm_studio", "inference_implementation": "lm_studio", "lm_studio_base_url": "http://127.0.0.1:1234/v1"})


def check_complete_sequence(runtime: FixtureRuntime) -> None:
    for reference in operation_handlers():
        preview = dispatch_request(request(reference), runtime)
        assert preview["checkpoint_status"] == "pending" and preview["planned_actions"], reference
        timeout = 900 if ".pull_" in reference else 300
        applied = dispatch_request(request(reference, phase="apply", timeout=timeout), runtime)
        assert applied["checkpoint_status"] == "applied", (reference, applied)
    for reference in probe_handlers():
        checked = dispatch_request(request(reference), runtime)
        assert checked["checkpoint_status"] == "verified", (reference, checked)
    before = len(runtime.commands)
    for reference in operation_handlers():
        assert dispatch_request(request(reference, phase="apply"), runtime)["checkpoint_status"] == "applied"
    assert all(command[:2] == ("/bin/launchctl", "print") for command in runtime.commands[before:])


def check_jit_and_unknown(runtime: FixtureRuntime) -> None:
    runtime.server = True
    runtime.loaded.clear()
    assert model_loaded(runtime, "qwen3-14b") is False
    load = dispatch_request(request("setup::lm_studio.load_inference"), runtime)
    assert load["checkpoint_status"] == "pending" and load["planned_actions"]
    runtime.unknown = True
    checked = dispatch_request(request("setup::lm_studio.inference_model_served"), runtime)
    assert checked["checkpoint_status"] == "blocked" and checked["error_kind"] == "lm_studio_inference_model_served_unknown"
    runtime.unknown = False


def check_partial_retry(runtime: FixtureRuntime) -> None:
    model = runtime.models["inference"]
    runtime.timeout_pull = True
    raw = dispatch_request(request("setup::lm_studio.pull_inference", phase="apply", timeout=900), runtime)
    assert raw["checkpoint_status"] == "pending" and raw["timed_out"] is True and raw["retry_safe"] is True
    partial_evidence = next(item for item in raw["evidence"] if item["id"] == "lm_studio_partial_bytes")
    assert partial_evidence["observed"] == len(b"preserved partial")
    adapter_request = request("setup::lm_studio.pull_inference", phase="apply", timeout=900)
    manager_request = OperationRequest(adapter_request.request_id, adapter_request.operation_id, adapter_request.operation_ref, "apply", None, 1, adapter_request.name, str(adapter_request.target), "macos.repository_setup", adapter_request.flow_source_revision, adapter_request.answers_fingerprint, adapter_request.approval_fingerprint, False, 900, adapter_request.public_inputs)
    typed = OperationResult.from_dict(raw, manager_request)
    assert normalize_apply_result(typed) is typed
    assert normalize_apply_result(replace(typed, retry_safe=False)).checkpoint_status == CheckpointStatus.FAILED
    partial = model.path(runtime.home).with_name(f"downloading_{model.filename}.part")
    runtime.timeout_pull = False
    assert dispatch_request(request("setup::lm_studio.pull_inference", phase="apply", timeout=900), runtime)["checkpoint_status"] == "applied"
    assert partial.read_bytes() == b"preserved partial"


def check_pull_failure_evidence(runtime: FixtureRuntime) -> None:
    """Exercise each pull through the real adapter route, not a replacement handler."""

    dispatch_request(request("setup::lm_studio.install", phase="apply"), runtime)
    for suffix in ("embedding", "inference"):
        reference = f"setup::lm_studio.pull_{suffix}"
        _check_vendor_timeout(runtime, reference)
        _check_outer_timeout(runtime, reference)
        _check_other_pull_failure(runtime, reference, suffix)
        _check_completed_vendor_timeout(runtime, reference, suffix)


def _check_vendor_timeout(runtime: FixtureRuntime, reference: str) -> None:
    runtime.pull_failure = "vendor_timeout"
    vendor = dispatch_request(request(reference, phase="apply", timeout=900), runtime)
    assert vendor["checkpoint_status"] == "failed", vendor
    assert vendor["error_kind"] == "lm_studio_vendor_download_timeout"
    assert vendor["timed_out"] is False
    assert vendor["exit_code"] == 1
    assert vendor["reason"] == _expected_nonzero_reason("Download failed: Timed-out. Please try to resume.")
    assert "vendor_failure_category=vendor_download_timeout" in vendor["evidence"][0]["observed"]
    assert "Download failed" not in json.dumps(vendor)


def _check_outer_timeout(runtime: FixtureRuntime, reference: str) -> None:
    runtime.timeout_pull = True
    runtime.pull_failure = None
    outer_timeout = dispatch_request(request(reference, phase="apply", timeout=900), runtime)
    assert outer_timeout["checkpoint_status"] == "pending", outer_timeout
    assert outer_timeout["error_kind"] == "lm_studio_download_pending"
    assert outer_timeout["timed_out"] is True
    assert "vendor_failure_category=unclassified" in outer_timeout["evidence"][0]["observed"]


def _check_other_pull_failure(runtime: FixtureRuntime, reference: str, suffix: str) -> None:
    runtime.timeout_pull = False
    runtime.pull_failure = "other_error"
    other = dispatch_request(request(reference, phase="apply", timeout=900), runtime)
    assert other["checkpoint_status"] == "failed", other
    assert other["error_kind"] == f"lm_studio_pull_{suffix}_failed"
    assert other["timed_out"] is False
    assert other["exit_code"] == 1
    assert "vendor_failure_category=unclassified" in other["evidence"][0]["observed"]


def _check_completed_vendor_timeout(runtime: FixtureRuntime, reference: str, suffix: str) -> None:
    runtime.pull_failure = "vendor_timeout_complete"
    recovered = dispatch_request(request(reference, phase="apply", timeout=900), runtime)
    assert recovered["checkpoint_status"] == "applied", recovered
    assert recovered["exit_code"] == 1
    assert recovered["timed_out"] is False
    assert "vendor_failure_category=vendor_download_timeout" in recovered["evidence"][0]["observed"]
    model = runtime.models["embeddings" if suffix == "embedding" else "inference"]
    model.path(runtime.home).unlink()
    (runtime.home / ".lmstudio/.internal/model-data.json").unlink()


def _expected_nonzero_reason(stderr: str) -> JsonObject:
    return {
        "outcome_class": "nonzero_exit",
        "exit_code": 1,
        "duration_ms": 1,
        "timed_out": False,
        "stdout_bytes": 0,
        "stderr_bytes": len(stderr),
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def check_settings(runtime: FixtureRuntime) -> None:
    path = settings_path(runtime.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"port":1234,"cors":true,"extra":{"preserve":1},"justInTimeModelLoading":true}')
    assert disable_jit(runtime)
    assert json.loads(path.read_text()) == {"port": 1234, "cors": True, "extra": {"preserve": 1}, "justInTimeModelLoading": False}
    before = path.read_bytes()
    assert disable_jit(runtime) and path.read_bytes() == before
    path.write_text("{broken")
    assert not disable_jit(runtime) and path.read_text() == "{broken"


def check_wrong_artifact(runtime: FixtureRuntime) -> None:
    model = runtime.models["embeddings"]
    wrong = replace(model, api_identifier="text-embedding-nomic-embed-text-v1.5", repository="nomic-ai/nomic-embed-text-v1.5-GGUF", filename="nomic-embed-text-v1.5.Q4_K_M.gguf", size_bytes=84110000)
    write_artifact(runtime.home, wrong)
    assert not artifact_present(model, runtime.home)
    write_artifact(runtime.home, model)
    assert artifact_present(model, runtime.home)
    with model.path(runtime.home).open("r+b") as stream:
        stream.truncate(42)
    assert not artifact_present(model, runtime.home)


def check_first_start_order(runtime: FixtureRuntime) -> None:
    dispatch_request(request("setup::lm_studio.install", phase="apply"), runtime)
    runtime.rewrite_first_start = True
    result = dispatch_request(request("setup::lm_studio.start_server", phase="apply"), runtime)
    assert result["checkpoint_status"] == "applied", result
    lifecycle = [command[1:3] for command in runtime.commands if command[0] == str(cli_path(runtime.home))]
    assert lifecycle == [("daemon", "up"), ("server", "start"), ("server", "stop"), ("server", "start")]
    assert jit_disabled(runtime.home)
    assert json.loads(settings_path(runtime.home).read_text())["cors"] is True


def main() -> int:
    checks = (check_complete_sequence, check_jit_and_unknown, check_partial_retry, check_pull_failure_evidence, check_settings, check_wrong_artifact, check_first_start_order)
    for check in checks:
        with tempfile.TemporaryDirectory(prefix="lm-studio-provisioning-") as temporary:
            runtime = FixtureRuntime(Path(temporary))
            if check == check_partial_retry:
                dispatch_request(request("setup::lm_studio.install", phase="apply"), runtime)
            check(runtime)
    print("lm_studio_provisioning_smoke: complete sequence, idempotence, JIT, pull failure evidence, partial retry and exact artifact controls passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
