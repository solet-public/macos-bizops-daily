"""Approved PostgreSQL install actions and fresh service-readiness confirmation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .homebrew import CommandExecutionError, CommandOutcome, run_homebrew_install_required
from .models import AdapterRuntime, PostgresObservation
from .postgres import postgres_observation, run_required, wait_for_postgres_ready


@dataclass(frozen=True)
class PostgresServiceStart:
    """The command receipt and fresh state observed after a service start attempt."""

    outcome: CommandOutcome
    ready: bool
    observation: PostgresObservation


def _start_service(runtime: AdapterRuntime, brew: str) -> PostgresServiceStart:
    """Start the service, then prove readiness with a fresh full observation."""

    try:
        outcome = run_required(runtime, [brew, "services", "start", "postgresql@17"], "PostgreSQL service start")
    except CommandExecutionError as exc:
        outcome = exc.outcome
    polled_ready = wait_for_postgres_ready(runtime)
    observation = postgres_observation(runtime)
    return PostgresServiceStart(outcome, polled_ready and observation.ready, observation)


def apply_postgres_install_actions(
    runtime: AdapterRuntime,
    brew: str,
    actions: Sequence[dict[str, Any]],
) -> PostgresServiceStart | None:
    """Apply the approved package/service plan without retaining stale readiness."""

    packages = {
        "postgres.install_homebrew_formula": ("postgresql@17", "PostgreSQL install"),
        "postgres.install_pgvector_formula": ("pgvector", "pgvector install"),
    }
    refreshed: PostgresObservation | None = None
    for item in actions:
        action_id = str(item["id"])
        if action_id == "postgres.start_homebrew_service":
            transition = _start_service(runtime, brew)
            refreshed = transition.observation
            if not transition.ready:
                return transition
            continue
        if action_id == "postgres.install_pgvector_formula" and (refreshed is not None and refreshed.pgvector_available is True):
            continue
        package, label = packages[action_id]
        run_homebrew_install_required(runtime, brew, package, label)
    return None
