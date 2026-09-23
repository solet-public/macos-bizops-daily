"""Shared data and result rendering for target-start readiness probes."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .setup_adapter_contract import AdapterRequest, JsonObject, evidence, result


@dataclass(frozen=True, slots=True)
class ReadinessBudget:
    contract_version: int
    contract_digest: str
    source_artifact: str
    budget_source: str
    budget_unit: str
    semantic_scope: str
    release_signal: str
    consumer_probe_purposes: tuple[str, ...]
    consumer_probe_refs: tuple[str, ...]
    parent_budget_seconds: int
    governed_process_call_seconds: int

    @property
    def effective_wait_seconds(self) -> int:
        return self.parent_budget_seconds - self.governed_process_call_seconds


@dataclass(frozen=True, slots=True)
class ReadinessObservation:
    budget: ReadinessBudget
    monotonic_origin: float
    monotonic_deadline: float
    observed_at: float
    last_status: str
    probe_count: int


def readiness_timeout_result(
    request: AdapterRequest,
    *,
    budget: ReadinessBudget,
    monotonic_origin: float,
    monotonic_deadline: float,
    observed_at: float,
    last_status: str,
    probe_count: int,
) -> JsonObject:
    health_command = f"{request.target}/.venv/bin/solet-bridge health"
    observation = ReadinessObservation(
        budget=budget,
        monotonic_origin=monotonic_origin,
        monotonic_deadline=monotonic_deadline,
        observed_at=observed_at,
        last_status=last_status,
        probe_count=probe_count,
    )
    elapsed_seconds = max(0.0, observed_at - monotonic_origin)
    return result(
        request,
        status="blocked",
        error_kind="target_readiness_timeout",
        retry_safe=True,
        duration_ms=int(elapsed_seconds * 1000),
        evidence_items=[readiness_evidence(request, observation, process_called=False)],
        repair=(
            f"Waited {budget.effective_wait_seconds} seconds for {request.name!r} "
            f"target-local health status=healthy under {budget.source_artifact}:"
            f"{budget.budget_source}; last status was {last_status}. The governed "
            f"process was not called. Run `{health_command}`, inspect the target startup "
            "log if it remains non-healthy, then retry create."
        ),
    )


def readiness_evidence(
    request: AdapterRequest,
    observation: ReadinessObservation,
    *,
    process_called: bool,
) -> JsonObject:
    budget = observation.budget
    executable = str(request.target / ".venv/bin/solet-bridge")
    elapsed = max(0.0, observation.observed_at - observation.monotonic_origin)
    remaining = max(0.0, observation.monotonic_deadline - observation.observed_at)
    observed: JsonObject = {
        "target_name": request.name,
        "target_root": str(request.target),
        "target_executable": executable,
        "signal_source": "target-local-cli",
        "release_signal": budget.release_signal,
        "last_health_status": observation.last_status,
        "health_probe_count": observation.probe_count,
        "source_artifact": budget.source_artifact,
        "budget_source": budget.budget_source,
        "budget_unit": budget.budget_unit,
        "semantic_scope": budget.semantic_scope,
        "contract_version": budget.contract_version,
        "contract_fingerprint": budget.contract_digest,
        "parent_budget_seconds": budget.parent_budget_seconds,
        "governed_process_call_reservation_seconds": budget.governed_process_call_seconds,
        "derived_wait_seconds": budget.effective_wait_seconds,
        "monotonic_origin_seconds": observation.monotonic_origin,
        "monotonic_deadline_seconds": observation.monotonic_deadline,
        "observed_at_monotonic_seconds": observation.observed_at,
        "elapsed_seconds": elapsed,
        "remaining_margin_seconds": remaining,
        "governed_process_called": process_called,
    }
    observed_items = [
        f"{key}={json.dumps(value, sort_keys=True, separators=(',', ':'))}"
        for key, value in sorted(observed.items())
    ]
    return evidence(
        evidence_id="target_bridge_ready",
        kind="readiness",
        status="verified" if process_called else "blocked",
        summary=(
            "exact target-local health released the governed process"
            if process_called
            else "exact target-local health did not release the governed process"
        ),
        observed=observed_items,
        expected="healthy_before_absolute_deadline",
        source=f"command:{executable} health;SOLET_NAME={request.name}",
    )
