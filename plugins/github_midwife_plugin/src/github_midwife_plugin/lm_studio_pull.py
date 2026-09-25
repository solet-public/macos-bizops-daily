"""Reviewed LM Studio model pull: the outcome follows the daemon's observed bytes.

A vendor CLI exit is not the download's outcome. After the pinned vendor
timeout the pull watches the daemon's partial and final files, verifies the
settled artifact once, and fails closed on a stall or the verify reserve.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .lm_studio_models import (
    VERIFY_RESERVE_SECONDS,
    DownloadWatch,
    ModelArtifact,
    artifact_present,
    download_observation,
    resumable_partial,
    rewatchable,
    transfer_active,
    watch_download,
)
from .setup_adapter_contract import AdapterRequest, JsonObject, evidence, result
from .setup_adapter_runtime import CommandOutcome, Runtime

_VENDOR_DOWNLOAD_TIMEOUT = "Download failed: Timed-out. Please try to resume."


_RESUME_MINIMUM_SECONDS = 30


def repair_hint(request: AdapterRequest) -> str:
    return f"For incomplete setup, run solet create {request.name} --dry-run --json and review the new fingerprint before resuming. After restoring a completed host, run solet doctor {request.name} --json."


def pull_model(pull: PullContext) -> JsonObject:
    """Observe an active transfer first; otherwise run the reviewed ``lms get``."""

    active = _active_transfer_outcome(pull)
    if active is not None:
        return active
    outcome = pull.runtime.run(pull.command, timeout_seconds=max(1, int(pull.deadline - time.monotonic())))
    if outcome.timed_out:
        return download_pending(pull.request, pull.runtime, pull.model, pull.command, outcome)
    if outcome.ok:
        if artifact_present(pull.model, pull.runtime.home):
            return result(pull.request, status="applied", duration_ms=outcome.duration_ms, evidence_items=[artifact_identity_evidence(pull.model)])
        return result(pull.request, status="failed", error_kind=f"lm_studio_{pull.suffix}_artifact_invalid", retry_safe=False, duration_ms=outcome.duration_ms, evidence_items=[command_outcome_evidence(outcome, pull.command)], repair=repair_hint(pull.request))
    category = vendor_failure_category(outcome)
    if category is not None:
        return _vendor_pull_outcome(pull, outcome, category)
    return command_failure(pull.request, outcome, f"lm_studio_{pull.suffix}_failed", pull.command)


def _vendor_pull_outcome(pull: PullContext, outcome: CommandOutcome, category: str) -> JsonObject:
    request, model, home = pull.request, pull.model, pull.runtime.home
    first = command_outcome_evidence(outcome, pull.command, category)
    if model.path(home).exists() or model.path(home).is_symlink():
        return _verified_result(pull, [first], outcome.duration_ms, outcome)
    if not resumable_partial(model, home) or pull.deadline - time.monotonic() < VERIFY_RESERVE_SECONDS:
        return command_failure(request, outcome, "lm_studio_vendor_download_timeout", pull.command, category)
    # The CLI's exit is not the download's outcome: the daemon keeps writing.
    initial = command_outcome_evidence(outcome, pull.command, category, "lm_studio_initial_pull_outcome")
    return _watched_outcome(pull, watch_download(model, home, pull.deadline), [initial], outcome.duration_ms, last=outcome, first=outcome)


def _resume_pull(pull: PullContext, first: CommandOutcome, evidence_items: list[JsonObject]) -> JsonObject:
    """Retry the exact reviewed command once inside the original adapter budget."""

    request, runtime, model, command = pull.request, pull.runtime, pull.model, pull.command
    second = runtime.run(command, timeout_seconds=max(1, int(pull.deadline - time.monotonic())))
    category = vendor_failure_category(second)
    evidence_items = [*evidence_items, command_outcome_evidence(second, command, category, "lm_studio_resume_pull_outcome")]
    duration_ms = first.duration_ms + second.duration_ms
    if second.timed_out:
        evidence_items.append(partial_evidence(model, runtime))
        return result(request, status="pending", error_kind="lm_studio_download_pending", retry_safe=True, timed_out=True, exit_code=None, duration_ms=duration_ms, evidence_items=evidence_items, reason=command_failure_reason(second), repair=repair_hint(request))
    if (second.ok or category is not None) and artifact_present(model, runtime.home):
        return result(request, status="applied", exit_code=second.returncode, duration_ms=duration_ms, evidence_items=[*evidence_items, artifact_identity_evidence(model)], reason=None if second.ok else command_failure_reason(second))
    if category is not None and rewatchable(model, runtime.home, pull.deadline):
        return _watched_outcome(pull, watch_download(model, runtime.home, pull.deadline), evidence_items, duration_ms, last=second, first=None)
    error_kind = f"lm_studio_{pull.suffix}_artifact_invalid" if second.ok else "lm_studio_vendor_download_timeout" if category is not None else f"lm_studio_{pull.suffix}_failed"
    return result(request, status="failed", error_kind=error_kind, retry_safe=not second.ok, exit_code=second.returncode, duration_ms=duration_ms, evidence_items=evidence_items, reason=command_failure_reason(second), repair=repair_hint(request))


@dataclass(frozen=True)
class PullContext:
    request: AdapterRequest
    runtime: Runtime
    suffix: str
    model: ModelArtifact
    command: tuple[str, ...]
    deadline: float


def _watched_outcome(
    pull: PullContext,
    watch: DownloadWatch,
    evidence_items: list[JsonObject],
    command_ms: int,
    *,
    last: CommandOutcome | None,
    first: CommandOutcome | None,
) -> JsonObject:
    """Decide from the observed transfer; every non-settled exit fails closed.

    ``last`` is the most recent ``lms get``; it is None when the watch began
    over an already active transfer and this apply ran no command.
    """

    model = pull.model
    evidence_items = [*evidence_items, watch.evidence()]
    duration_ms = command_ms + watch.elapsed_ms
    if watch.exit == "settled":
        return _verified_result(pull, evidence_items, duration_ms, last)
    if first is not None and watch.exit == "stalled" and resumable_partial(model, pull.runtime.home) and pull.deadline - time.monotonic() >= _RESUME_MINIMUM_SECONDS:
        return _resume_pull(pull, first, evidence_items)
    return _unsettled_failure(pull, watch, [*evidence_items, partial_evidence(model, pull.runtime)], duration_ms, last)


def _verified_result(pull: PullContext, evidence_items: list[JsonObject], duration_ms: int, last: CommandOutcome | None) -> JsonObject:
    """One full digest decides: applied, or a non-retryable invalid artifact."""

    exit_code = None if last is None else last.returncode
    reason = None if last is None or last.ok else command_failure_reason(last)
    if artifact_present(pull.model, pull.runtime.home):
        return result(pull.request, status="applied", exit_code=exit_code, duration_ms=duration_ms, evidence_items=[*evidence_items, artifact_identity_evidence(pull.model)], reason=reason)
    return result(pull.request, status="failed", error_kind=f"lm_studio_{pull.suffix}_artifact_invalid", retry_safe=False, exit_code=exit_code, duration_ms=duration_ms, evidence_items=evidence_items, reason=reason, repair=repair_hint(pull.request))


def _unsettled_failure(pull: PullContext, watch: DownloadWatch, evidence_items: list[JsonObject], duration_ms: int, last: CommandOutcome | None) -> JsonObject:
    """Fail closed and retry-safe: a stall, or a reserve reached while still growing."""

    request = pull.request
    progressing = watch.exit == "reserve"
    error_kind = "lm_studio_download_still_progressing" if progressing else "lm_studio_vendor_download_timeout"
    repair = f"The LM Studio daemon is still downloading; re-run solet create {request.name} --dry-run --json, review, and resume. The pull will observe and not restart the transfer." if progressing else repair_hint(request)
    exit_code, reason = (None, None) if last is None else (last.returncode, command_failure_reason(last))
    return result(request, status="failed", error_kind=error_kind, retry_safe=True, timed_out=False, exit_code=exit_code, duration_ms=duration_ms, evidence_items=evidence_items, reason=reason, repair=repair)


def _active_transfer_outcome(pull: PullContext) -> JsonObject | None:
    """Watch, without a new ``lms get``, a transfer the daemon is already running."""

    if not transfer_active(pull.model, pull.runtime.home):
        return None
    watch = watch_download(pull.model, pull.runtime.home, pull.deadline)
    return None if watch.exit == "stalled" else _watched_outcome(pull, watch, [], 0, last=None, first=None)


def artifact_identity_evidence(model: ModelArtifact) -> JsonObject:
    return evidence(evidence_id=f"lm_studio_{model.role}_artifact_sha256", kind="filesystem", status="verified", summary="Complete reviewed LM Studio artifact matches published SHA-256 and source provenance", observed=model.sha256, expected=model.sha256, source=f"https://huggingface.co/{model.repository}/blob/main/{model.filename}")


def partial_evidence(model: ModelArtifact, runtime: Runtime) -> JsonObject:
    return evidence(evidence_id="lm_studio_partial_bytes", kind="filesystem", status="pending", summary="Partial model download retained for an identical resumable retry", observed=download_observation(model, runtime.home)[0], expected=model.size_bytes, source=str(model.path(runtime.home)))


def download_pending(
    request: AdapterRequest,
    runtime: Runtime,
    model: ModelArtifact,
    command: tuple[str, ...],
    outcome: CommandOutcome,
) -> JsonObject:
    return result(
        request,
        status="pending",
        error_kind="lm_studio_download_pending",
        timed_out=True,
        exit_code=None,
        duration_ms=outcome.duration_ms,
        retry_safe=True,
        evidence_items=[
            command_outcome_evidence(outcome, command),
            partial_evidence(model, runtime),
        ],
        reason=command_failure_reason(outcome),
        repair=f"Run solet create {request.name} again after reviewing its dry run. The model pull resumes existing bytes; preserve the partial file.",
    )


def command_failure(
    request: AdapterRequest,
    outcome: CommandOutcome,
    error_kind: str,
    argv: tuple[str, ...],
    vendor_category: str | None = None,
) -> JsonObject:
    return result(
        request,
        status="failed",
        error_kind=error_kind,
        retry_safe=True,
        exit_code=outcome.returncode,
        timed_out=outcome.timed_out,
        duration_ms=outcome.duration_ms,
        evidence_items=[command_outcome_evidence(outcome, argv, vendor_category)],
        reason=command_failure_reason(outcome),
        repair=repair_hint(request),
    )


def command_outcome_evidence(
    outcome: CommandOutcome,
    argv: tuple[str, ...],
    vendor_category: str | None = None,
    evidence_id: str = "lm_studio_command_outcome",
) -> JsonObject:
    """Expose reviewed command identity and bounded outcome facts, never streams."""

    category = vendor_category or "unclassified"
    return evidence(
        evidence_id=evidence_id,
        kind="command",
        status="observed",
        summary=(
            "LM Studio reviewed command outcome: "
            f"exit_code={outcome.returncode}; timed_out={outcome.timed_out}; "
            f"stdout_bytes={outcome.stdout_bytes}; stderr_bytes={outcome.stderr_bytes}."
        ),
        observed=[
            f"argv={' '.join(argv)}",
            f"exit_code={outcome.returncode}",
            f"timed_out={outcome.timed_out}",
            f"duration_ms={outcome.duration_ms}",
            f"stdout_bytes={outcome.stdout_bytes}",
            f"stderr_bytes={outcome.stderr_bytes}",
            f"stdout_truncated={outcome.stdout_truncated}",
            f"stderr_truncated={outcome.stderr_truncated}",
            f"vendor_failure_category={category}",
        ],
        expected="reviewed command completes without a timeout or nonzero exit",
        source="reviewed_lm_studio_registry",
    )


def command_failure_reason(outcome: CommandOutcome) -> JsonObject:
    if outcome.timed_out:
        outcome_class = "timeout"
    elif outcome.executable_missing:
        outcome_class = "executable_missing"
    elif outcome.launch_error is not None:
        outcome_class = "launch_error"
    else:
        outcome_class = "nonzero_exit"
    return {
        "outcome_class": outcome_class,
        "exit_code": outcome.returncode,
        "duration_ms": outcome.duration_ms,
        "timed_out": outcome.timed_out,
        "stdout_bytes": outcome.stdout_bytes,
        "stderr_bytes": outcome.stderr_bytes,
        "stdout_truncated": outcome.stdout_truncated,
        "stderr_truncated": outcome.stderr_truncated,
    }


def vendor_failure_category(outcome: CommandOutcome) -> str | None:
    """Recognize only the pinned vendor diagnostic on a failed model pull."""

    if (
        not outcome.timed_out
        and outcome.returncode not in (None, 0)
        and _VENDOR_DOWNLOAD_TIMEOUT in outcome.stderr
    ):
        return "vendor_download_timeout"
    return None
