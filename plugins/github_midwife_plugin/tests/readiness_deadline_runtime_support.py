"""Deterministic runtime support for readiness-deadline scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from github_midwife_plugin.setup_adapter_contract import JsonObject, JsonValue
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome

_PROCESS_KEY = "service_interface::embedding_service::get_embedding_dimension"


@dataclass(slots=True)
class VirtualClock:
    monotonic_now: float = 0.0
    wall_now: float = 1_000_000.0
    wall_jump_per_sleep: float = 0.0

    def monotonic(self) -> float:
        return self.monotonic_now

    def sleep(self, seconds: float) -> None:
        self.monotonic_now += seconds
        self.wall_now += seconds + self.wall_jump_per_sleep


@dataclass(slots=True)
class ForeignHealthState:
    targets: tuple[Path, Path]
    vectors: tuple[tuple[str, str], tuple[str, str]]
    by_vector: dict[tuple[str, str], Path]
    observations: list[JsonObject] = field(default_factory=list)
    calls: int = 0

    @classmethod
    def build(cls, targets: tuple[Path, Path]) -> ForeignHealthState:
        vectors = tuple((str(target / ".venv/bin/solet-bridge"), "health") for target in targets)
        return cls(targets, vectors, dict(zip(vectors, targets, strict=True)))


class VirtualRuntime:
    """Closed target runtime with foreign instances healthy first."""

    def __init__(
        self,
        root: Path,
        target: Path,
        clock: VirtualClock,
        release_at: float | None,
        foreign_targets: tuple[Path, Path],
        expected_name: str,
    ) -> None:
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.target = target
        self.clock = clock
        self.release_at = release_at
        self.expected_name = expected_name
        self.trace: list[str] = []
        self.process_calls = 0
        self.process_times: list[float] = []
        self.process_timeouts: list[int] = []
        self.health_count = 0
        self.foreign = ForeignHealthState.build(foreign_targets)
        self.target_redirects: list[bool] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = 4096,
    ) -> CommandOutcome:
        del input_text, output_limit
        health = (str(self.target / ".venv/bin/solet-bridge"), "health")
        process = (str(self.target / ".venv/bin/solet-bridge"), "call", _PROCESS_KEY, "{}")
        now = self.clock.monotonic()
        if argv in self.foreign.vectors:
            return self._run_foreign_health(argv, cwd, extra_env, now)
        if argv == health:
            return self._run_target_health(cwd, extra_env, now)
        if argv == process:
            return self._run_process(cwd, extra_env, now, timeout_seconds)
        self.trace.append(f"unexpected:{argv}@{now:.3f}")
        return CommandOutcome(1, False, 1, "", "unexpected command")

    def _run_foreign_health(
        self,
        argv: tuple[str, ...],
        cwd: Path | None,
        extra_env: dict[str, str] | None,
        now: float,
    ) -> CommandOutcome:
        foreign = self.foreign.by_vector[argv]
        redirected = cwd == foreign and (extra_env or {}).get("SOLET_NAME") == foreign.name
        self.foreign.calls += 1
        self.foreign.observations.append(
            {
                "name": foreign.name,
                "status": "healthy",
                "at": now,
                "before_newborn_release": self.release_at is None or now < self.release_at,
                "redirected": redirected,
            }
        )
        self.trace.append(
            f"foreign-health:{foreign.name}:healthy@{now:.3f}:redirected={redirected}"
        )
        return _health("healthy")

    def _target_redirected(
        self,
        cwd: Path | None,
        extra_env: dict[str, str] | None,
    ) -> bool:
        return cwd == self.target and (extra_env or {}).get("SOLET_NAME") == self.expected_name

    def _run_target_health(
        self,
        cwd: Path | None,
        extra_env: dict[str, str] | None,
        now: float,
    ) -> CommandOutcome:
        redirected = self._target_redirected(cwd, extra_env)
        self.target_redirects.append(redirected)
        state, outcome = self._health_outcome(now)
        self.trace.append(f"health:{state}@{now:.3f}:redirected={redirected}")
        self.health_count += 1
        return outcome

    def _run_process(
        self,
        cwd: Path | None,
        extra_env: dict[str, str] | None,
        now: float,
        timeout_seconds: int,
    ) -> CommandOutcome:
        redirected = self._target_redirected(cwd, extra_env)
        self.target_redirects.append(redirected)
        self.process_calls += 1
        self.process_times.append(now)
        self.process_timeouts.append(timeout_seconds)
        self.trace.append(f"process@{now:.3f}:redirected={redirected}")
        return CommandOutcome(
            0,
            False,
            1,
            json.dumps(
                {
                    "result": {
                        "success": True,
                        "error": None,
                        "data": {"result": {"dimension": 768}},
                    }
                }
            ),
            "",
        )

    def _health_outcome(self, now: float) -> tuple[str, CommandOutcome]:
        if self.release_at is not None and now >= self.release_at:
            return "healthy", _health("healthy")
        state = ("unreachable", "invalid", "starting", "degraded")[min(self.health_count, 3)]
        if state == "unreachable":
            return state, CommandOutcome(1, False, 1, "", "starting")
        if state == "invalid":
            return state, CommandOutcome(0, False, 1, "{", "")
        return state, _health(state)

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonValue]:
        del url, timeout_seconds, payload
        return 503, None

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        del path, content, mode
        raise AssertionError("readiness scenario may not write through the runtime")


def _health(status: str) -> CommandOutcome:
    return CommandOutcome(0, False, 1, json.dumps({"status": status}), "")
