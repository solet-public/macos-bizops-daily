#!/usr/bin/env python3
"""Phase 2 / POR §1.3 ◆R2 — EMBEDDING_SERVICE is a REQUIRED service (offline, no pytest).

The embedder is a required, inference-INDEPENDENT service: discovery/retrieval and
the context_service briefing depend on it regardless of whether any reasoner is
bound. This smoke asserts the bind-or-error invariant is enforced BY DECLARATION —
``EMBEDDING_SERVICE`` is in ``REQUIRED_SERVICES`` and ``validate_required_services``
raises when it is unbound.

Run:
    .venv/bin/python3 ananta/tests/core/context_service/required_services_embedding_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.orchestration.service_bindings import (  # noqa: E402
    REQUIRED_SERVICES,
    BindingSource,
    ServiceBinding,
    ServiceBindingError,
    ServiceBindings,
    ServiceName,
)

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


def _bindings_covering(omit: ServiceName | None) -> ServiceBindings:
    """A loaded ServiceBindings binding every REQUIRED service except ``omit``."""
    sb = ServiceBindings("/unused")
    sb._bindings = {  # noqa: SLF001 (white-box: avoid env/config-file flakiness)
        svc: ServiceBinding(service_name=svc, plugin_name="p", source=BindingSource.CONFIG)
        for svc in REQUIRED_SERVICES
        if svc is not omit
    }
    sb._loaded = True  # noqa: SLF001
    return sb


def test_embedding_is_declared_required() -> None:
    _check(
        ServiceName.EMBEDDING_SERVICE in REQUIRED_SERVICES,
        "EMBEDDING_SERVICE is declared in REQUIRED_SERVICES",
    )


def test_validation_fails_when_embedding_unbound() -> None:
    sb = _bindings_covering(omit=ServiceName.EMBEDDING_SERVICE)
    try:
        sb.validate_required_services()
    except ServiceBindingError as exc:
        _check(
            "embedding_service" in str(exc),
            f"validate_required_services raises naming embedding_service (got {exc})",
        )
    else:
        _check(False, "validate_required_services did NOT raise with embedding unbound")


def test_validation_passes_when_all_required_bound() -> None:
    sb = _bindings_covering(omit=None)
    try:
        sb.validate_required_services()
        _check(True, "validation passes when every required service (incl. embedding) is bound")
    except ServiceBindingError as exc:
        _check(False, f"validation unexpectedly failed with all required bound: {exc}")


def main() -> int:
    print("=== EMBEDDING_SERVICE required-service enforcement (Phase 2 / POR §1.3) ===")
    test_embedding_is_declared_required()
    test_validation_fails_when_embedding_unbound()
    test_validation_passes_when_all_required_bound()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
