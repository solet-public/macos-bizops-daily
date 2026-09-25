"""One Manager-origin monotonic budget for passive LM Studio readiness."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .setup_adapter_contract import AdapterRequest, JsonObject, evidence, result
from .setup_adapter_runtime import CommandOutcome

PARENT_DEADLINE_KEY = "lm_studio_parent_deadline_ns"
SERVED_REFS = frozenset(f"setup::lm_studio.{suffix}" for suffix in (
    "load_embedding", "load_inference", "embedding_model_served", "inference_model_served",
))
WAIT_PURPOSES = frozenset({"post_apply", "stage_exit", "completion"})
_NS = 1_000_000_000


class DeadlineExpiredError(Exception):
    """Budget exhaustion is neither corrupt source bytes nor protocol failure."""


@dataclass
class ServedDeadline:
    started_ns: int
    expires_ns: int
    attempts: int = 0
    load_succeeded: bool = False
    terminal_class: str = "not_observed"
    last_command: tuple[str, ...] = ()

    def check(self) -> None:
        if time.monotonic_ns() >= self.expires_ns:
            raise DeadlineExpiredError

    def call_budget(self, cap: int, *, reserve: int = 0) -> int:
        remaining = (self.expires_ns - time.monotonic_ns()) // _NS - reserve
        if remaining < 1:
            raise DeadlineExpiredError
        return min(cap, remaining)

    def pause(self) -> None:
        self.check()
        time.sleep(min(1.0, (self.expires_ns - time.monotonic_ns()) / _NS))
        self.check()

    def record_command(self, command: CommandOutcome) -> None:
        self.last_command = (f"exit_code={command.returncode}", f"timed_out={command.timed_out}",
                             f"stdout_truncated={command.stdout_truncated}", f"stderr_truncated={command.stderr_truncated}",
                             f"duration_ms={command.duration_ms}")

    def facts(self) -> JsonObject:
        now = time.monotonic_ns()
        return {"attempt_count": self.attempts, "elapsed_ms": (now - self.started_ns) // 1_000_000,
                "remaining_ms": max(0, (self.expires_ns - now) // 1_000_000),
                "terminal_class": self.terminal_class, "load_command_succeeded": self.load_succeeded, "last_command": list(self.last_command)}


def deadline_for_request(request: AdapterRequest) -> ServedDeadline | None:
    raw = request.public_inputs.get(PARENT_DEADLINE_KEY)
    required = request.phase == "apply" or request.probe_purpose in WAIT_PURPOSES
    if raw is None and not required:
        return None
    now = time.monotonic_ns()
    if request.operation_ref not in SERVED_REFS or type(raw) is not int or raw <= 0:
        raise ValueError("invalid LM Studio parent deadline contract")
    if raw - now > request.timeout_seconds * _NS:
        raise ValueError("LM Studio parent deadline exceeds declared timeout")
    deadline = ServedDeadline(raw - request.timeout_seconds * _NS, raw - 5 * _NS)
    return deadline


def check_deadline(deadline: ServedDeadline | None) -> None:
    if deadline is not None:
        deadline.check()


def call_budget(deadline: ServedDeadline | None, cap: int) -> int:
    return cap if deadline is None else deadline.call_budget(cap)


def run_with_deadline(request: AdapterRequest, operation: Callable[[ServedDeadline | None], JsonObject], repair: str) -> JsonObject:
    deadline: ServedDeadline | None = None
    try:
        deadline = deadline_for_request(request)
    except ValueError:
        return result(request, status="blocked", error_kind="lm_studio_deadline_contract_invalid", retry_safe=False, repair=repair)
    except DeadlineExpiredError:
        return result(request, status="blocked", error_kind="lm_studio_served_budget_exhausted", repair=repair)
    try:
        check_deadline(deadline)
        answer = operation(deadline)
        check_deadline(deadline)
        if deadline is not None:
            items = answer["evidence"]
            assert isinstance(items, list)
            items.append(_deadline_evidence(deadline))
        check_deadline(deadline)
        return answer
    except DeadlineExpiredError:
        return result(request, status="blocked", error_kind="lm_studio_served_budget_exhausted", retry_safe=True,
                      evidence_items=[] if deadline is None else [_deadline_evidence(deadline)],
                      repair="Re-probe served state before any further mutation; preserve the model and review a fresh dry run.")


def _deadline_evidence(deadline: ServedDeadline) -> JsonObject:
    return evidence(evidence_id="lm_studio_served_deadline", kind="host", status="observed",
                    summary="Shared parent-origin readiness budget", observed=[f"{key}={value}" for key, value in deadline.facts().items()],
                    expected="strict identity before deadline", source="Manager monotonic envelope")




def record_command(deadline: ServedDeadline | None, command: CommandOutcome) -> None:
    if deadline is not None:
        deadline.record_command(command)
