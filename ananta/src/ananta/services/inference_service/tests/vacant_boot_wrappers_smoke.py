#!/usr/bin/env python3
"""INF-03 boot-integration smoke — `_create_service_wrappers` under vacancy.

Reviewer-A's B1 (2026-07-05) proved the unit-level vacancy smoke cannot see
boot regressions: the constructor stopped raising, but the boot step's very
next call (`get_context_management_config` feeding DiscoveryService +
ContextService) raised the typed vacancy error and killed startup anyway.
This smoke runs the REAL `_create_service_wrappers` startup step against a
fake orchestrator whose `inference_service` binding is ABSENT, and asserts
the whole step completes — the regression class the isolation smoke misses
by construction (the same boot-only-check class as the set_autonomic_slot
EDGE-declaration incident).

Offline: fake orchestrator/bindings/plugin-manager; no live solet, no DB.

Run from repo root:
    .venv/bin/python3 ananta/src/ananta/services/inference_service/tests/vacant_boot_wrappers_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.orchestration.service_bindings import ServiceName  # noqa: E402
from ananta.core.orchestration.startup_sequence import (  # noqa: E402
    _create_service_wrappers,
)
from ananta.interfaces.memory_service_interface import (  # noqa: E402
    MemoryServiceInterface,
)
from ananta.services.context_management.config import (  # noqa: E402
    VACANT_PROVIDER_CONTEXT_CONFIG,
)


class _FakeMemoryPlugin:
    """Structural stand-in; virtual subclass so the wrapper's isinstance passes."""

    def is_ready(self) -> bool:
        return True


MemoryServiceInterface.register(_FakeMemoryPlugin)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# Every service EXCEPT inference gets a (never-validated-at-construction)
# fake binding; inference is ABSENT = the INF-03 declared-vacant profile.
_BINDINGS: dict[ServiceName, str | None] = {
    ServiceName.VECTOR_SERVICE: "fake_vector_plugin",
    ServiceName.EMBEDDING_SERVICE: "fake_embedding_plugin",
    ServiceName.INFERENCE_SERVICE: None,
    ServiceName.MEMORY_SERVICE: "fake_memory_plugin",
    ServiceName.KNOWLEDGE_SERVICE: None,
    ServiceName.THINKING_SERVICE: None,
}


class _FakeServiceBindings:
    def get_plugin_name(self, service_name: ServiceName) -> str | None:
        return _BINDINGS.get(service_name)


class _FakePluginManager:
    """Serves benign plugin stand-ins for the NON-inference wrappers.

    Other wrappers (MemoryService resolves at construction) may
    legitimately touch their plugins here; the assertion this smoke owns
    is that the INFERENCE path never resolves a plugin under vacancy —
    tracked via ``resolved`` and asserted in main().
    """

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def get_plugin(self, plugin_name: str) -> object:
        self.resolved.append(plugin_name)
        if plugin_name == "fake_memory_plugin":
            return _FakeMemoryPlugin()
        return object()


class _FakeOrch:
    def __init__(self, app_home: str) -> None:
        self.APP_HOME = app_home
        self.service_bindings = _FakeServiceBindings()
        self.plugin_manager = _FakePluginManager()
        self.state_service = object()


def main() -> int:
    os.environ.pop("ANANTA_INFERENCE_PROVIDER", None)
    with tempfile.TemporaryDirectory() as app_home:
        orch = _FakeOrch(app_home)
        try:
            _create_service_wrappers(orch)
        except Exception as exc:  # noqa: BLE001
            print(
                "FAIL: _create_service_wrappers raised under a vacant "
                f"inference binding — B1 regressed: {type(exc).__name__}: {exc}"
            )
            return 1
        _check(True, "boot step _create_service_wrappers completes under vacancy")
        _check(
            getattr(orch, "inference_service", None) is not None,
            "vacant InferenceService wrapper installed on the orchestrator",
        )
        _check(
            getattr(orch, "discovery_service", None) is not None,
            "DiscoveryService constructed from the vacant-state config",
        )
        threshold = orch.discovery_service._min_similarity_threshold  # noqa: SLF001
        _check(
            threshold == VACANT_PROVIDER_CONTEXT_CONFIG.discovery_min_similarity_threshold,
            "DiscoveryService threshold sourced from VACANT_PROVIDER_CONTEXT_CONFIG",
        )
        _check(
            getattr(orch, "context_service", None) is not None,
            "ContextService constructed with the vacant-state config",
        )
        _check(
            getattr(orch, "memory_service", None) is not None
            and orch.knowledge_service is None
            and orch.thinking_service is None,
            "remaining wrappers follow their own bound/optional postures",
        )
        _check(
            not any("inference" in name for name in orch.plugin_manager.resolved),
            "no inference plugin resolution attempted anywhere in the vacant boot step",
        )

    print(f"\nvacant_boot_wrappers_smoke: {_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
