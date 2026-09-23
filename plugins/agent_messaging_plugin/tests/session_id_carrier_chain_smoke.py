#!/usr/bin/env python3
"""Smoke for the stdio-bridge `agent_session_id` PER-AGENT-KIND carrier chain.

Covers `_resolve_agent_session_id(agent_id)` / `SESSION_ID_ENV_VARS_BY_AGENT` /
`DEFAULT_SESSION_ID_ENV_VARS` in `agent_messaging_plugin.mcp_bridge.__main__`.

The stable logical-session key drives the reconnect self-refresh CAS +
`peer_claim_role`. `refresh_role_binding_cas` re-points roles filtered on
`agent_session_id` ALONE, so resolution MUST be keyed on the bridge's agent
kind: a bridge that adopted a foreign kind's inherited/leaked session id would
re-point the WRONG session's roles. Hence:

  - a `codex` bridge prefers its own `CODEX_THREAD_ID` (authoritative — this
    Codex conversation, never stale for a Codex child), then
    `AGENT_SESSION_ID`;
  - EVERY OTHER kind uses `AGENT_SESSION_ID` only and NEVER adopts
    `CODEX_THREAD_ID` (an unknown kind takes the default, non-codex chain).

Contract (per Coordinator-Day fold 2026-07-16, Codex Finding 1):
1. codex + both carriers      -> CODEX_THREAD_ID wins;
2. codex + CODEX_THREAD_ID    → resolves it;
3. codex + solet carrier → resolves it;
4. codex + neither           → "" (degraded);
5. claude_code + leaked CODEX_THREAD_ID only → "" NOT adopted  (reverse-nesting
   pin — the load-bearing case);
6. claude_code + solet carrier → resolves it;
7. claude_code + both        → solet carrier (CODEX_THREAD_ID ignored);
8. claude_code + neither     → "";
9. unknown kind + CODEX_THREAD_ID only → "" (default chain refuses the codex
   carrier);
10. the mapping/default constants have the expected shape.

Run:

    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_id_carrier_chain_smoke.py
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Generator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from agent_messaging_plugin.mcp_bridge.__main__ import (  # noqa: E402
    AGENT_SESSION_ID_ENV,
    CODEX_AGENT_ID,
    DEFAULT_AGENT_ID,
    DEFAULT_SESSION_ID_ENV_VARS,
    SESSION_ID_ENV_VARS_BY_AGENT,
    _resolve_agent_session_id,
)

_UNKNOWN_AGENT_ID = "some_other_agent"

# Every carrier env-var name that appears in ANY chain — cleared per test so a
# carrier set in the real launching environment cannot leak into an assertion.
_ALL_CARRIERS = frozenset(
    name
    for chain in (*SESSION_ID_ENV_VARS_BY_AGENT.values(), DEFAULT_SESSION_ID_ENV_VARS)
    for name in chain
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


@contextlib.contextmanager
def _isolated_session_env(**values: str) -> Generator[None]:
    """Fully control every carrier for one test.

    Saves and clears ALL `_ALL_CARRIERS`, applies `values`, restores on exit —
    so a carrier set in the real launching environment (this session may itself
    export `AGENT_SESSION_ID` / `CODEX_THREAD_ID`) cannot leak in.
    """
    saved = {name: os.environ.get(name) for name in _ALL_CARRIERS}
    try:
        for name in _ALL_CARRIERS:
            os.environ.pop(name, None)
        for name, value in values.items():
            os.environ[name] = value
        yield
    finally:
        for name in _ALL_CARRIERS:
            original = saved[name]
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original


# ─── codex agent kind ───────────────────────────────────────────────────


def test_codex_prefers_thread_id_over_export() -> None:
    with _isolated_session_env(
        CODEX_THREAD_ID="thread-authoritative",
        AGENT_SESSION_ID="sess-inherited-from-parent",
    ):
        resolved = _resolve_agent_session_id(CODEX_AGENT_ID)
    _check(
        resolved == "thread-authoritative",
        f"codex: CODEX_THREAD_ID wins over an inherited solet session id "
        f"(got {resolved!r})",
    )


def test_codex_thread_id_only() -> None:
    with _isolated_session_env(CODEX_THREAD_ID="thread-abc123"):
        resolved = _resolve_agent_session_id(CODEX_AGENT_ID)
    _check(
        resolved == "thread-abc123",
        f"codex: CODEX_THREAD_ID resolves (non-empty → self-refresh-eligible) "
        f"(got {resolved!r})",
    )


def test_codex_uses_solet_carrier_when_no_thread_id() -> None:
    with _isolated_session_env(AGENT_SESSION_ID="sess-exported"):
        resolved = _resolve_agent_session_id(CODEX_AGENT_ID)
    _check(
        resolved == "sess-exported",
        f"codex: AGENT_SESSION_ID resolves when no CODEX_THREAD_ID "
        f"(got {resolved!r})",
    )


def test_codex_neither_degraded() -> None:
    with _isolated_session_env():
        resolved = _resolve_agent_session_id(CODEX_AGENT_ID)
    _check(
        resolved == "",
        f"codex: neither carrier → '' (degraded) (got {resolved!r})",
    )


# ─── claude_code (default) agent kind ────────────────────────────────────


def test_claude_code_refuses_leaked_codex_thread_id() -> None:
    # Reverse-nesting hazard: a claude_code bridge spawned under a Codex parent
    # inherits a leaked CODEX_THREAD_ID. It MUST NOT be adopted — the CAS keys on
    # agent_session_id alone, so adoption would re-point the Codex parent's roles.
    with _isolated_session_env(CODEX_THREAD_ID="thread-leaked-from-parent"):
        resolved = _resolve_agent_session_id(DEFAULT_AGENT_ID)
    _check(
        resolved == "",
        f"claude_code: a LEAKED CODEX_THREAD_ID is NOT adopted → '' degraded "
        f"(reverse-nesting pin) (got {resolved!r})",
    )


def test_claude_code_exported_resolves() -> None:
    with _isolated_session_env(AGENT_SESSION_ID="sess-exported"):
        resolved = _resolve_agent_session_id(DEFAULT_AGENT_ID)
    _check(
        resolved == "sess-exported",
        f"claude_code: exported AGENT_SESSION_ID resolves "
        f"(got {resolved!r})",
    )


def test_claude_code_export_wins_ignores_codex_carrier() -> None:
    with _isolated_session_env(
        AGENT_SESSION_ID="sess-exported",
        CODEX_THREAD_ID="thread-should-be-ignored",
    ):
        resolved = _resolve_agent_session_id(DEFAULT_AGENT_ID)
    _check(
        resolved == "sess-exported",
        f"claude_code: AGENT_SESSION_ID resolves; CODEX_THREAD_ID ignored "
        f"even when set (got {resolved!r})",
    )


def test_claude_code_neither_degraded() -> None:
    with _isolated_session_env():
        resolved = _resolve_agent_session_id(DEFAULT_AGENT_ID)
    _check(
        resolved == "",
        f"claude_code: neither carrier → '' "
        f"(got {resolved!r})",
    )


# ─── unknown agent kind → default (non-codex) chain ──────────────────────


def test_unknown_agent_kind_refuses_codex_carrier() -> None:
    with _isolated_session_env(CODEX_THREAD_ID="thread-leaked"):
        resolved = _resolve_agent_session_id(_UNKNOWN_AGENT_ID)
    _check(
        resolved == "",
        f"unknown kind: takes the default chain, refuses CODEX_THREAD_ID → '' "
        f"(got {resolved!r})",
    )


# ─── structural pins on the carrier constants ────────────────────────────


def test_carrier_chain_shapes() -> None:
    _check(
        SESSION_ID_ENV_VARS_BY_AGENT.get(CODEX_AGENT_ID)
        == ("CODEX_THREAD_ID", AGENT_SESSION_ID_ENV),
        f"codex chain is (CODEX_THREAD_ID, AGENT_SESSION_ID) "
        f"(got {SESSION_ID_ENV_VARS_BY_AGENT.get(CODEX_AGENT_ID)!r})",
    )
    _check(
        DEFAULT_SESSION_ID_ENV_VARS == (AGENT_SESSION_ID_ENV,),
        f"default chain is (AGENT_SESSION_ID,) "
        f"(got {DEFAULT_SESSION_ID_ENV_VARS!r})",
    )
    _check(
        "CODEX_THREAD_ID" not in DEFAULT_SESSION_ID_ENV_VARS,
        "default (non-codex) chain never contains CODEX_THREAD_ID",
    )


def main() -> int:
    print(
        "plugins/agent_messaging_plugin/tests/session_id_carrier_chain_smoke.py",
    )
    test_codex_prefers_thread_id_over_export()
    test_codex_thread_id_only()
    test_codex_uses_solet_carrier_when_no_thread_id()
    test_codex_neither_degraded()
    test_claude_code_refuses_leaked_codex_thread_id()
    test_claude_code_exported_resolves()
    test_claude_code_export_wins_ignores_codex_carrier()
    test_claude_code_neither_degraded()
    test_unknown_agent_kind_refuses_codex_carrier()
    test_carrier_chain_shapes()

    print()
    print(f"passed: {_passed}")
    if _failed:
        print(f"failed: {len(_failed)}")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
