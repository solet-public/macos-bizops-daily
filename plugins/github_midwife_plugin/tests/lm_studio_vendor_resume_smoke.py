"""Bounded vendor resume and reviewed-byte controls at the setup adapter seam."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/github_midwife_plugin/src"))
sys.path.insert(0, str(ROOT / "solet_cli/src"))

from github_midwife_plugin import lm_studio_provisioning, lm_studio_pull  # noqa: E402
from github_midwife_plugin.lm_studio_deadline import ServedDeadline  # noqa: E402
from github_midwife_plugin.lm_studio_models import ModelArtifact, artifact_present, cli_path  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from lm_studio_provisioning_smoke import FakeClock, FixtureRuntime, fixture_bytes, outcome, request, write_artifact  # noqa: E402
from solet_manager.adapter_protocol import OperationRequest, OperationResult  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.operation_records import normalize_apply_result  # noqa: E402

PULL = "setup::lm_studio.pull_inference"
VENDOR_ERROR = "Download failed: Timed-out. Please try to resume."


@dataclass
class Daemon:
    """The vendor daemon's transfer, advanced once per adapter poll.

    ``step`` bytes land every ``period`` ticks while fewer than ``active_ticks``
    ticks have grown. At the reviewed size the partial is renamed onto the
    final path; provenance lands ``provenance_delay`` ticks later (never when
    None), and ``touch`` rewrites the final mtime once on the next tick.
    """

    step: int = 0
    period: int = 1
    active_ticks: int = 1_000_000
    provenance_delay: int | None = 0
    touch: bool = False
    corrupt: bool = False
    ticks: int = 0
    renamed_at: int | None = None


class ResumeRuntime(FixtureRuntime):
    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.scenario: str | None = None
        self.pull_timeouts: list[int] = []
        self.clock = FakeClock()
        self.clock.on_tick = self._tick
        self.daemon = Daemon()
        self.cli_seconds = 0.0
        self.daemon_after_get: Daemon | None = None
        self.seed_on_get = False

    def _partial(self) -> Path:
        model = self.models["inference"]
        return model.path(self.home).with_name(f"downloading_{model.filename}.part")

    def seed_partial(self, size: int = 17) -> None:
        self._partial().parent.mkdir(parents=True, exist_ok=True)
        self._partial().write_bytes(fixture_bytes("inference")[:size])

    def _tick(self) -> None:
        daemon, model, partial = self.daemon, self.models["inference"], self._partial()
        daemon.ticks += 1
        if daemon.renamed_at is not None:
            self._settle(model, daemon)
            return
        if not partial.is_file() or partial.is_symlink() or daemon.step == 0 or daemon.ticks > daemon.active_ticks or daemon.ticks % daemon.period:
            return
        size = min(model.size_bytes, partial.stat().st_size + daemon.step)
        partial.write_bytes(fixture_bytes("inference")[:size])
        if size == model.size_bytes:
            self._rename(model, partial, daemon)

    def _rename(self, model: ModelArtifact, partial: Path, daemon: Daemon) -> None:
        data = bytearray(fixture_bytes("inference"))
        if daemon.corrupt:
            data[5] = ord("X")
        model.path(self.home).write_bytes(bytes(data))
        partial.unlink()
        daemon.renamed_at = daemon.ticks
        if daemon.provenance_delay == 0:
            _record_provenance(self.home, model)

    def _settle(self, model: ModelArtifact, daemon: Daemon) -> None:
        assert daemon.renamed_at is not None
        age = daemon.ticks - daemon.renamed_at
        if daemon.touch and age == 1:
            stamp = model.path(self.home).stat().st_mtime_ns + 1_000_000
            os.utime(model.path(self.home), ns=(stamp, stamp))
        if daemon.provenance_delay is not None and age == daemon.provenance_delay:
            _record_provenance(self.home, model)

    def _pull(self, args: tuple[str, ...], timeout: int) -> CommandOutcome:
        self._daemon_side_of_get(timeout)
        model = next(item for item in self.models.values() if item.get_argv == args)
        scenario = self.scenario
        if scenario in _FIRST_EXIT_THEN:
            self.scenario = _FIRST_EXIT_THEN[scenario]
            if not self._partial().exists():
                self.seed_partial()
            return outcome(code=1, error=VENDOR_ERROR)
        if scenario in {"vendor_corrupt", "success_corrupt"}:
            return self._corrupt_pull(model, scenario)
        if scenario in _FIXED_OUTCOMES:
            return _FIXED_OUTCOMES[scenario]()
        return super()._pull(args, timeout)

    def _daemon_side_of_get(self, timeout: int) -> None:
        """The CLI runs for ``cli_seconds``; it may start or restart the daemon's transfer."""

        self.pull_timeouts.append(timeout)
        self.clock.now += self.cli_seconds
        if self.daemon_after_get is not None and len(self.pull_timeouts) > 1:
            self.daemon, self.daemon_after_get = self.daemon_after_get, None
        if self.seed_on_get and not self._partial().exists():
            self.seed_partial()

    def _corrupt_pull(self, model: ModelArtifact, scenario: str) -> CommandOutcome:
        write_artifact(self.home, model)
        with model.path(self.home).open("r+b") as stream:
            stream.seek(5)
            stream.write(b"X")
        return outcome() if scenario == "success_corrupt" else outcome(code=1, error=VENDOR_ERROR)


_FIRST_EXIT_THEN: dict[str, str | None] = {"resume": None, "second_other": "other_error", "second_vendor": "vendor_timeout", "second_timeout": "timeout", "budget": None}
_FIXED_OUTCOMES: dict[str, Callable[[], CommandOutcome]] = {
    "vendor_timeout": lambda: outcome(code=1, error=VENDOR_ERROR),
    "other_error": lambda: outcome(code=1, error="download request rejected"),
    "timeout": lambda: outcome(code=None, timeout=True),
}


def _record_provenance(home: Path, model: ModelArtifact) -> None:
    metadata = home / ".lmstudio/.internal/model-data.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(metadata.read_text()) if metadata.exists() else {"json": []}
    owner, repo = model.repository.split("/", 1)
    data["json"].append([f"{model.repository}/{model.filename}", {"source": {"type": "huggingface", "owner": owner, "repo": repo, "file": model.filename}}])
    metadata.write_text(json.dumps(data))


def _run_pull(runtime: ResumeRuntime) -> dict[str, object]:
    with runtime.clock.patched():
        return dispatch_request(request(PULL, phase="apply", timeout=900), runtime)


def _counted_pull(runtime: ResumeRuntime) -> tuple[dict[str, object], list[bool]]:
    """Run a pull and record, per ``artifact_present`` call, whether a final file existed."""

    calls: list[bool] = []

    def counted(model: ModelArtifact, home: Path, deadline: ServedDeadline | None = None) -> bool:
        calls.append(model.path(home).exists())
        return artifact_present(model, home, deadline)

    with patch.object(lm_studio_pull, "artifact_present", counted):
        return _run_pull(runtime), calls


def _installed_runtime(home: Path) -> ResumeRuntime:
    runtime = ResumeRuntime(home)
    with patch.object(lm_studio_provisioning, "reviewed_models", return_value=runtime.models):
        assert dispatch_request(request("setup::lm_studio.install", phase="apply"), runtime)["checkpoint_status"] == "applied"
    return runtime


def _gets(runtime: ResumeRuntime) -> int:
    return sum(1 for argv in runtime.commands if len(argv) > 1 and argv[1] == "get")


def _ids(response: dict[str, object]) -> set[str]:
    items = response["evidence"]
    assert isinstance(items, list)
    return {str(item["id"]) for item in items if isinstance(item, dict)}


def check_resume(runtime: ResumeRuntime) -> None:
    """Fix A kept: a stalled partial gets one identical re-run inside the budget."""

    runtime.scenario = "resume"
    model = runtime.models["inference"]
    response = _run_pull(runtime)
    assert response["checkpoint_status"] == "applied", response
    # The re-run now waits out the 90 s stall window first (was [890, 845]).
    assert runtime.pull_timeouts == [890, 800]
    _check_resume_evidence(runtime, response)
    assert artifact_present(model, runtime.home)
    assert dispatch_request(request("setup::lm_studio.inference_artifact_present"), runtime)["checkpoint_status"] == "verified"
    assert dispatch_request(request("setup::lm_studio.start_server", phase="apply"), runtime)["checkpoint_status"] == "applied"
    assert dispatch_request(request("setup::lm_studio.load_inference", phase="apply"), runtime)["checkpoint_status"] == "applied"


def _check_resume_evidence(runtime: ResumeRuntime, response: dict[str, object]) -> None:
    model = runtime.models["inference"]
    pulls = [argv for argv in runtime.commands if len(argv) > 1 and argv[1] == "get"]
    assert pulls == [(str(cli_path(runtime.home)), *model.get_argv)] * 2
    assert _ids(response) >= {"lm_studio_initial_pull_outcome", "lm_studio_download_watch", "lm_studio_resume_pull_outcome", "lm_studio_inference_artifact_sha256"}
    assert "timed_out=False" in response["evidence"][0]["observed"]


def check_cli_dies_daemon_finishes(runtime: ResumeRuntime) -> None:
    """R44 fresh round 2: the CLI exits at 84 s while the daemon completes."""

    runtime.scenario, runtime.cli_seconds, runtime.seed_on_get = "vendor_timeout", 84.0, True
    runtime.daemon = Daemon(step=10)
    runtime.pull_timeouts.clear()
    response, calls = _counted_pull(runtime)
    assert response["checkpoint_status"] == "applied", response
    assert _gets(runtime) == 1 and calls.count(True) == 1
    assert _ids(response) >= {"lm_studio_initial_pull_outcome", "lm_studio_download_watch", "lm_studio_inference_artifact_sha256"}
    assert response["timed_out"] is False and runtime.clock.now < 890


def check_stall_then_resumed_growth(runtime: ResumeRuntime) -> None:
    """Stall 90 s, one identical re-run, the daemon resumes, and the watch applies."""

    runtime.scenario, runtime.cli_seconds = "second_vendor", 84.0
    runtime.daemon_after_get = Daemon(step=10)
    response = _run_pull(runtime)
    assert response["checkpoint_status"] == "applied", response
    assert _gets(runtime) == 2 and artifact_present(runtime.models["inference"], runtime.home)


def check_active_transfer(runtime: ResumeRuntime) -> None:
    """A re-apply over a growing partial observes it and issues no ``lms get``."""

    runtime.seed_partial()
    runtime.daemon = Daemon(step=10)
    response = _run_pull(runtime)
    assert response["checkpoint_status"] == "applied", response
    assert _gets(runtime) == 0 and "lm_studio_download_watch" in _ids(response)


def check_completion_race(runtime: ResumeRuntime) -> None:
    """Provenance lands after the rename and the final is touched once: digest once, after settling."""

    runtime.scenario, runtime.cli_seconds, runtime.seed_on_get = "vendor_timeout", 84.0, True
    runtime.daemon = Daemon(step=10, provenance_delay=3, touch=True)
    response, calls = _counted_pull(runtime)
    assert response["checkpoint_status"] == "applied", response
    assert calls.count(True) == 1 and calls[-1] is True


def check_provenance_never(runtime: ResumeRuntime) -> None:
    runtime.scenario, runtime.cli_seconds, runtime.seed_on_get = "vendor_timeout", 84.0, True
    runtime.daemon = Daemon(step=10, provenance_delay=None)
    response, calls = _counted_pull(runtime)
    assert response["checkpoint_status"] == "failed" and response["error_kind"] == "lm_studio_vendor_download_timeout", response
    assert response["retry_safe"] is True and response["timed_out"] is False and calls.count(True) == 0


def check_wrong_digest_at_size(runtime: ResumeRuntime) -> None:
    runtime.scenario, runtime.cli_seconds, runtime.seed_on_get = "vendor_timeout", 84.0, True
    runtime.daemon = Daemon(step=10, corrupt=True)
    response = _run_pull(runtime)
    assert response["checkpoint_status"] == "failed" and response["error_kind"] == "lm_studio_pull_inference_artifact_invalid", response
    assert response["retry_safe"] is False


def check_still_progressing(runtime: ResumeRuntime) -> None:
    """Growth past the verify reserve fails retry-safe without spoofing a timeout."""

    runtime.scenario, runtime.cli_seconds, runtime.seed_on_get = "vendor_timeout", 84.0, True
    runtime.daemon = Daemon(step=1, period=2)
    response = _run_pull(runtime)
    assert response["checkpoint_status"] == "failed" and response["error_kind"] == "lm_studio_download_still_progressing", response
    assert response["retry_safe"] is True and response["timed_out"] is False and _gets(runtime) == 1
    assert 890 - 60 - 5 <= runtime.clock.now <= 890


def check_no_sleep_without_partial(runtime: ResumeRuntime) -> None:
    """Pulls with no partial present never sample or sleep before ``lms get``."""

    for reference in ("setup::lm_studio.pull_embedding", PULL):
        with runtime.clock.patched():
            assert dispatch_request(request(reference, phase="apply", timeout=900), runtime)["checkpoint_status"] == "applied"
    assert runtime.clock.sleeps == []


def check_failures(runtime: ResumeRuntime) -> None:
    model = runtime.models["inference"]
    partial = model.path(runtime.home).with_name(f"downloading_{model.filename}.part")
    for scenario, error_kind, calls in (
        ("second_other", "lm_studio_pull_inference_failed", 2),
        # Previously asserted failure after two CLI exits with nothing observed,
        # which encoded the defect. It is now a stalled daemon that fails
        # closed within two stall windows of the first exit.
        ("second_vendor", "lm_studio_vendor_download_timeout", 2),
        ("vendor_timeout", "lm_studio_vendor_download_timeout", 1),
        ("vendor_corrupt", "lm_studio_pull_inference_artifact_invalid", 1),
        ("success_corrupt", "lm_studio_pull_inference_artifact_invalid", 1),
        ("other_error", "lm_studio_pull_inference_failed", 1),
    ):
        partial.unlink(missing_ok=True)
        model.path(runtime.home).unlink(missing_ok=True)
        _check_failure_case(runtime, scenario, error_kind, calls)
    runtime.scenario, runtime.cli_seconds, runtime.clock.now = "budget", 865.0, 0.0
    exhausted = _run_pull(runtime)
    assert exhausted["checkpoint_status"] == "failed" and runtime.pull_timeouts[-1] == 890
    _check_unwatched_partials(runtime, partial)


def _check_failure_case(runtime: ResumeRuntime, scenario: str, error_kind: str, calls: int) -> None:
    runtime.scenario, runtime.cli_seconds, runtime.clock.now = scenario, 84.0, 0.0
    before = len(runtime.pull_timeouts)
    response = _run_pull(runtime)
    assert response["checkpoint_status"] == "failed" and response["error_kind"] == error_kind, response
    assert response["timed_out"] is False and len(runtime.pull_timeouts) - before == calls
    assert runtime.clock.now <= 84 * calls + 2 * 90 + 10
    assert dispatch_request(request("setup::lm_studio.inference_artifact_present"), runtime)["checkpoint_status"] != "verified"


def _check_unwatched_partials(runtime: ResumeRuntime, partial: Path) -> None:
    """Zero-byte, oversized, and symlinked partials keep today's single failure, unwatched."""

    runtime.cli_seconds = 0.0
    for size in (0, runtime.models["inference"].size_bytes + 1, -1):
        partial.unlink(missing_ok=True)
        partial.symlink_to(runtime.models["inference"].path(runtime.home)) if size < 0 else partial.write_bytes(b"X" * size)
        runtime.scenario = "vendor_timeout"
        before, sleeps = len(runtime.pull_timeouts), len(runtime.clock.sleeps)
        response = _run_pull(runtime)
        assert response["checkpoint_status"] == "failed" and len(runtime.pull_timeouts) - before == 1
        assert len(runtime.clock.sleeps) == sleeps, "an invalid partial must never be watched"


def check_identity(runtime: ResumeRuntime) -> None:
    model = runtime.models["inference"]
    write_artifact(runtime.home, model)
    assert artifact_present(model, runtime.home)
    metadata = runtime.home / ".lmstudio/.internal/model-data.json"
    valid_metadata = metadata.read_text()
    metadata.unlink()
    assert not artifact_present(model, runtime.home)
    for old, new in ((model.repository, "wrong/repository"), (model.filename, "wrong.gguf")):
        metadata.write_text(valid_metadata.replace(old, new))
        assert not artifact_present(model, runtime.home)
    write_artifact(runtime.home, model)
    with model.path(runtime.home).open("r+b") as stream:
        stream.seek(5)
        stream.write(b"X")
    assert not artifact_present(model, runtime.home)
    assert dispatch_request(request("setup::lm_studio.inference_artifact_present"), runtime)["checkpoint_status"] != "verified"
    partial = model.path(runtime.home).with_name(f"downloading_{model.filename}.part")
    partial.write_bytes(b"preserved partial")
    runtime.scenario = "vendor_timeout"
    before = len(runtime.pull_timeouts)
    assert _run_pull(runtime)["checkpoint_status"] == "failed" and len(runtime.pull_timeouts) - before == 1


def check_manager_failure(runtime: ResumeRuntime) -> None:
    runtime.scenario = "second_vendor"
    raw = _run_pull(runtime)
    adapter_request = request(PULL, phase="apply", timeout=900)
    manager_request = OperationRequest(adapter_request.request_id, adapter_request.operation_id, adapter_request.operation_ref, "apply", None, 1, adapter_request.name, str(adapter_request.target), "macos.repository_setup", adapter_request.flow_source_revision, adapter_request.answers_fingerprint, adapter_request.approval_fingerprint, False, 900, adapter_request.public_inputs)
    typed = OperationResult.from_dict(raw, manager_request)
    assert normalize_apply_result(typed).checkpoint_status == CheckpointStatus.FAILED
    assert raw["timed_out"] is False and raw["exit_code"] == 1
    assert "Download failed" not in json.dumps(raw)
    runtime.scenario = "second_timeout"
    timed = _run_pull(runtime)
    assert timed["checkpoint_status"] == "pending" and timed["timed_out"] is True
    assert timed["exit_code"] is None


def main() -> int:
    checks = (
        check_resume,
        check_cli_dies_daemon_finishes,
        check_stall_then_resumed_growth,
        check_active_transfer,
        check_completion_race,
        check_provenance_never,
        check_wrong_digest_at_size,
        check_still_progressing,
        check_no_sleep_without_partial,
        check_failures,
        check_identity,
        check_manager_failure,
    )
    for check in checks:
        with tempfile.TemporaryDirectory(prefix="lm-studio-vendor-resume-") as temporary:
            runtime = _installed_runtime(Path(temporary))
            with patch.object(lm_studio_provisioning, "reviewed_models", return_value=runtime.models):
                check(runtime)
    print("lm_studio_vendor_resume_smoke: daemon-observed pull, bounded resume, digest identity, and negative controls passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
