#!/usr/bin/env python3
"""ThinkingService ABC↔wrapper parity smoke (no pytest).

Kills the 4-layer-rule bug class caught live on 2026-07-02: the Phase 3
Seam A verbs existed on the ABC (``ThinkingServiceAPI``), the plugin, and
the KB JSONs, but the CONCRETE ``ThinkingService`` wrapper had no delegate
methods — so a real registry dispatch failed with ``'ThinkingService'
object has no attribute ...`` while every offline test (which drove the
plugin layer directly) stayed green.

Mirror of the dual-layer-concreteness pattern from
``plugins/actr_memory_plugin/tests/cron_remediation_smoke.py`` (which
asserts ``not cls.__abstractmethods__`` on every concrete
``MemoryServiceInterface`` subclass), adapted for this service:
``ThinkingService`` does NOT inherit the ABC (Slice 11C deleted the
concrete interface; the wrapper delegates to an Any-typed plugin through
the ``ThinkingProvider`` protocol), so abstract-method coverage cannot
catch the miss here. Instead: every ``@service_interface_process``-
decorated method on ``ThinkingServiceAPI`` must exist as a callable on
BOTH the wrapper (``ThinkingService`` — what registry dispatch hits) and
the bound plugin class (``DefaultThinkingPlugin`` — what the wrapper
delegates to).

Offline: pure introspection, no live solet / LM Studio / Postgres.

Run:
    .venv/bin/python3 ananta/tests/core/thinking_service/wrapper_parity_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from ananta.services.thinking_service import ThinkingService  # noqa: E402
from ananta.services.thinking_service.interfaces.public import (  # noqa: E402
    ThinkingServiceAPI,
)
from default_thinking_plugin.plugin import DefaultThinkingPlugin  # noqa: E402

# The decorator stamps this attribute on every registered verb
# (service_interface_decorator.py) — it is the enumeration key, so a new
# decorated verb is covered here automatically the moment it is added.
_DECORATOR_MARKER = "_service_interface_metadata"


def decorated_verb_names() -> list[str]:
    """Every @service_interface_process-decorated method on the ABC."""
    names = [
        name
        for name in dir(ThinkingServiceAPI)
        if hasattr(getattr(ThinkingServiceAPI, name), _DECORATOR_MARKER)
    ]
    if not names:
        msg = (
            "found ZERO decorated verbs on ThinkingServiceAPI — the "
            "decorator marker attribute has moved; update _DECORATOR_MARKER"
        )
        raise AssertionError(msg)
    return sorted(names)


def missing_callables(concrete: type) -> list[str]:
    """Decorated ABC verbs with no callable counterpart on *concrete*."""
    return [
        name
        for name in decorated_verb_names()
        if not callable(getattr(concrete, name, None))
    ]


def main() -> int:
    verbs = decorated_verb_names()
    print(f"{len(verbs)} decorated verbs on ThinkingServiceAPI")

    failures: list[str] = []
    for concrete in (ThinkingService, DefaultThinkingPlugin):
        missing = missing_callables(concrete)
        if missing:
            failures.append(
                f"{concrete.__name__} is missing callable(s) for decorated "
                f"verb(s): {missing}",
            )
            print(f"  FAIL  {concrete.__name__}: missing {missing}")
        else:
            print(f"  PASS  {concrete.__name__}: all {len(verbs)} verbs callable")

    if failures:
        print(f"\n{len(failures)} parity failure(s)")
        return 1
    print("\nparity OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
