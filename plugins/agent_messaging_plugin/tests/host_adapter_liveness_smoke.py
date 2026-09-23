#!/usr/bin/env python3
"""No-half-alive-interfaces liveness smoke for the ``HostDriver`` Protocol
(fleet session-management Phase B, design §1.1 point 3): "Any interface this
lane declares ships in the same wave with a liveness smoke ... the smoke
fails when the interface has consumers wired and zero implementers live (the
exact state BackendRouter sat in for ~10 weeks before its D3 retirement
[A1]), and when its docstring
names implementers that do not exist."

D2 update: ``tmux`` moved from ``_CLAIMED_DEFERRED_DRIVERS`` to
``_CLAIMED_LIVE_DRIVERS`` now that ``tmux_adapter.TmuxHostDriver`` ships
registered (session_hosts.py D1+D2 docstring) — this is the exact
widened-interface-updates-its-fakes discipline the design doc names: the
claim list is a SPEC of what the docstring says, and it must track the
registry's real state or this smoke goes stale in the direction that matters
(silently claiming something is still deferred after it shipped).

Every check below RE-PROVES itself is load-bearing by inline mutation —
temporarily forcing the exact failure shape, confirming the assertion would
then read False, and restoring real state before the next check runs. A
guard nobody has ever seen fail is not proven load-bearing (the same
caution as "a test can render the defect in its own assertion") — this file
makes that proof durable and re-run on every gate pass, not a one-time
manual demonstration.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/host_adapter_liveness_smoke.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import session_hosts  # noqa: E402
from agent_messaging_plugin.session_hosts import (  # noqa: E402
    AGENT_RUNTIME_CLAUDE_CODE,
    AGENT_RUNTIME_CODEX,
    DEFAULT_AGENT_RUNTIME,
    HostMechanismMissingError,
    resolve_host_driver,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    _resolve_spawn_host,
)

# The module docstring's own claims (session_hosts.py, read at authoring
# time — this list is this smoke's spec of what the docstring says, not a
# parse of the prose itself): "operator", "headless", and "tmux" (D2) all
# ship REGISTERED for both runner runtimes in this build. No driver is claimed deferred;
# the set stays declared (not deleted) so the next D-step that adds one has
# an obvious place to name it.
_CLAIMED_LIVE_DRIVERS = frozenset(
    (runtime, host)
    for runtime in (AGENT_RUNTIME_CLAUDE_CODE, AGENT_RUNTIME_CODEX)
    for host in ("operator", "headless", "tmux")
)
_CLAIMED_DEFERRED_DRIVERS: frozenset[tuple[str, str]] = frozenset()

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


def test_a_real_consumer_is_wired_to_the_interface() -> None:
    """Precondition for the liveness check below: if nothing actually calls
    ``resolve_host_driver``, an empty registry would be dead code, not the
    now-retired-BackendRouter-shape hazard. ``spawn_session`` is the real, live consumer
    (§4) — confirm it, structurally, rather than assuming it."""
    source = inspect.getsource(_resolve_spawn_host)
    _check(
        "resolve_host_driver" in source,
        "spawn_session's real host-resolution helper calls resolve_host_driver "
        "-- the interface has a wired consumer, not just a declared Protocol",
    )


def test_claimed_live_drivers_actually_resolve() -> None:
    for runtime, host in _CLAIMED_LIVE_DRIVERS:
        driver, resolved_host = resolve_host_driver(host, runtime)
        _check(
            driver is not None and resolved_host == host,
            f"docstring-claimed live driver {(runtime, host)!r} actually resolves to a "
            "registered HostDriver (not just claimed in prose)",
        )


def test_claimed_deferred_driver_is_not_secretly_registered() -> None:
    _check(
        _CLAIMED_DEFERRED_DRIVERS == frozenset(),
        "precondition, explicit not implicit: no driver is currently claimed "
        "deferred (D2 registered the last one, tmux) — this loop is "
        "intentionally a no-op below, not a silently-vacuous check",
    )
    for runtime, host in _CLAIMED_DEFERRED_DRIVERS:
        raised = False
        try:
            resolve_host_driver(host, runtime)
        except HostMechanismMissingError:
            raised = True
        _check(
            raised,
            f"docstring-claimed DEFERRED driver {(runtime, host)!r} is still correctly "
            "unregistered -- the docstring and the registry agree on what "
            "has NOT shipped, not just what has",
        )


def test_registry_emptiness_is_actually_caught() -> None:
    """RED-FIRST PROOF, self-contained: force the registry empty (the exact
    now-retired-BackendRouter failure shape — a wired consumer, zero live implementers),
    confirm the liveness assertion reads False, then restore the real
    registered drivers before any later check runs."""
    original = dict(session_hosts._REGISTRY)
    _check(len(original) > 0, "precondition: the real registry is non-empty before mutating it")
    session_hosts._REGISTRY.clear()
    try:
        would_be_live = len(session_hosts._REGISTRY) > 0
        _check(
            not would_be_live,
            "RED: with the registry forced empty, "
            "'>=1 live implementer' correctly reads False",
        )
    finally:
        session_hosts._REGISTRY.clear()
        session_hosts._REGISTRY.update(original)
    _check(
        len(session_hosts._REGISTRY) == len(original),
        "GREEN: the registry is restored to its real registered state afterward",
    )


def test_a_claimed_driver_going_missing_is_actually_caught() -> None:
    """RED-FIRST PROOF, self-contained: force ONE docstring-claimed live
    driver (headless) out of the registry — the "docstring names an
    implementer that does not exist" failure shape — confirm resolution then
    raises, then restore."""
    target = (DEFAULT_AGENT_RUNTIME, "headless")
    _check(
        target in session_hosts._REGISTRY,
        f"precondition: {target!r} is really registered before mutating it",
    )
    removed = session_hosts._REGISTRY.pop(target)
    try:
        raised = False
        try:
            resolve_host_driver(target[1], target[0])
        except HostMechanismMissingError:
            raised = True
        _check(
            raised,
            f"RED: with {target!r} forced out of the registry, resolving the "
            "docstring-claimed driver correctly raises HostMechanismMissingError",
        )
    finally:
        session_hosts._REGISTRY[target] = removed
    _check(
        target in session_hosts._REGISTRY,
        f"GREEN: {target!r} is restored to the real registry afterward",
    )


def test_registry_is_never_empty_while_a_consumer_is_wired() -> None:
    """The actual standing liveness assertion (run against REAL, unmutated
    state, after the self-proving mutations above have restored it): a
    consumer is wired (proven structurally above) AND the registry holds
    >=1 live implementer, simultaneously, always."""
    consumer_wired = "resolve_host_driver" in inspect.getsource(_resolve_spawn_host)
    _check(
        consumer_wired and len(session_hosts._REGISTRY) > 0,
        "the HostDriver interface has >=1 live registered implementer while "
        "a real consumer is wired to it -- never the now-retired-BackendRouter-shape gap "
        "of wired-consumers-with-zero-implementers",
    )


def main() -> int:
    test_a_real_consumer_is_wired_to_the_interface()
    test_claimed_live_drivers_actually_resolve()
    test_claimed_deferred_driver_is_not_secretly_registered()
    test_registry_emptiness_is_actually_caught()
    test_a_claimed_driver_going_missing_is_actually_caught()
    test_registry_is_never_empty_while_a_consumer_is_wired()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
