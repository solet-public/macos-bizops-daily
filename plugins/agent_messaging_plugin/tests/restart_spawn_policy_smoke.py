#!/usr/bin/env python3
"""Unit smoke for restart_session's spawn-leg policy resolution (2026-08-10
fix). Live-measured defect: ``_run_restart_session_job`` used to build a
``SpawnSessionRequest`` directly and call ``lifecycle_spawn_session``,
bypassing every config-resolution step ``spawn_session()`` itself runs
(model/effort defaults, tool allowlist, permission mode, transport) —
``managed_session`` has no columns for ``permission_mode``/``allowed_tools``/
``transport``, so a restarted worker silently lost all three every time. The
visible failure was ``host_cannot_spawn`` (no permission mode configured) on
a real tmux-hosted restart. The fix routes the restart's spawn through the
SAME shared builder (``_spawn_session_request_from_params``) and policy
function (``_apply_spawn_session_policy``) ``spawn_session()`` uses, so a
future policy step can never again land in one caller and not the other.

Covers ``AgentMessagingPlugin._build_restart_spawn_params`` (the ledger-row
carry-forward) and the full pipeline
(``_build_restart_spawn_params`` -> ``_spawn_session_request_from_params`` ->
``_apply_spawn_session_policy``) end to end, including the mutation-proof
leg: a request built via the restart path must inherit a POLICY-CONFIGURED
permission_mode exactly as ``spawn_session()`` would, not the
``SpawnSessionRequest`` dataclass's own empty-string default.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/restart_spawn_policy_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _apply_spawn_session_policy,
    _SessionLifecyclePolicyConfig,
    _spawn_session_request_from_params,
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


def _plugin() -> AgentMessagingPlugin:
    return AgentMessagingPlugin.__new__(AgentMessagingPlugin)


def _old_row(**overrides: str) -> dict[str, str]:
    row = {
        "host": "tmux",
        "brief_ref": "workbench/some-brief.md",
        "unit_id": "unt-restart-01234567",
        "work_class": "read_only",
        "budget_line": "some-budget-line",
        "visibility": "visible",
        "model": "haiku",
        "effort": "low",
    }
    row.update(overrides)
    return row


def test_build_restart_spawn_params_carries_ledger_fields() -> None:
    plugin = _plugin()
    params = plugin._build_restart_spawn_params(  # noqa: SLF001 -- testing the builder directly
        _old_row(), role_class="project", lane_id="lane-1", role_name="some-role",
    )
    _check(
        params["role_class"] == "project"
        and params["lane_id"] == "lane-1"
        and params["unit_id"] == "unt-restart-01234567"
        and params["role_name"] == "some-role"
        and params["host"] == "tmux"
        and params["work_class"] == "read_only"
        and params["budget_line"] == "some-budget-line"
        and params["model"] == "haiku"
        and params["effort"] == "low",
        "_build_restart_spawn_params carries every ledger-backed field, including unit_id, forward",
    )
    _check(
        params["spawned_by_role"] == AgentMessagingPlugin._CHOREOGRAPHY_DIRECTED_BY,
        "_build_restart_spawn_params stamps spawned_by_role with the "
        "choreography's own directed_by constant",
    )


def test_build_restart_spawn_params_omits_permission_mode_allowed_tools_transport() -> None:
    plugin = _plugin()
    params = plugin._build_restart_spawn_params(  # noqa: SLF001 -- testing the builder directly
        _old_row(), role_class="project", lane_id="lane-1", role_name="some-role",
    )
    _check(
        "permission_mode" not in params and "allowed_tools" not in params
        and "transport" not in params,
        "the raw params never claim a permission_mode/allowed_tools/transport "
        "the ledger row cannot actually supply -- these three are LEFT for "
        "_apply_spawn_session_policy to fill, never faked here",
    )


def test_restart_spawn_pipeline_resolves_permission_mode_end_to_end() -> None:
    """The mutation-proof leg: if a future change reverts the restart path
    to skip ``_apply_spawn_session_policy`` (or reintroduces a private,
    unpatched copy of the resolution steps), this assertion reds -- a
    restart-rebuilt request must inherit a policy-configured permission_mode
    exactly as ``spawn_session()``'s own path would, never the
    ``SpawnSessionRequest`` dataclass's raw empty-string default."""
    plugin = _plugin()
    raw_params = plugin._build_restart_spawn_params(  # noqa: SLF001 -- testing the builder directly
        _old_row(), role_class="project", lane_id="lane-1", role_name="some-role",
    )
    req = _spawn_session_request_from_params(raw_params, plugin._CHOREOGRAPHY_DIRECTED_BY)
    _check(
        req.permission_mode == "",
        "sanity check: BEFORE policy application the request's permission_mode "
        "is still empty, exactly the live-measured host_cannot_spawn shape",
    )
    policy = _SessionLifecyclePolicyConfig(
        work_class_defaults={}, work_class_tool_allowlists={},
        headless_permission_mode="bypassPermissions", default_fleet_transport="watch",
    )
    resolved = _apply_spawn_session_policy(req, policy)
    _check(
        resolved.permission_mode == "bypassPermissions",
        "AFTER _apply_spawn_session_policy the restart-rebuilt request "
        "inherits the policy's headless_permission_mode -- the exact "
        "resolution step the live host_cannot_spawn failure was missing",
    )
    _check(
        resolved.transport == "watch",
        "the restart-rebuilt request also inherits default_fleet_transport "
        "through the same shared policy function (allowed_tools/transport "
        "were the same-shape latent gaps named alongside permission_mode)",
    )
    _check(
        resolved.directed_by == plugin._CHOREOGRAPHY_DIRECTED_BY  # noqa: SLF001
        and resolved.spawned_by_role == plugin._CHOREOGRAPHY_DIRECTED_BY,  # noqa: SLF001
        "directed_by/spawned_by_role are preserved through the shared "
        "params+policy pipeline, unchanged from the choreography's own "
        "convention",
    )


def test_restart_spawn_pipeline_never_overrides_an_explicit_ledger_value() -> None:
    """_apply_spawn_session_policy must still be a fill-never-override
    resolver on the restart path -- a work_class the operator explicitly
    configured a default model/effort for should not clobber a ledger row
    that already carries non-empty model/effort (mirrors work_class_defaults_
    smoke.py's own coverage of this property for the direct spawn_session()
    path; this test proves the SAME property survives through the restart's
    extra _build_restart_spawn_params/_spawn_session_request_from_params hop)."""
    plugin = _plugin()
    raw_params = plugin._build_restart_spawn_params(  # noqa: SLF001 -- testing the builder directly
        _old_row(model="claude-opus-5", effort="high"),
        role_class="project", lane_id="lane-1", role_name="some-role",
    )
    req = _spawn_session_request_from_params(raw_params, plugin._CHOREOGRAPHY_DIRECTED_BY)
    policy = _SessionLifecyclePolicyConfig(
        work_class_defaults={"read_only": {"model": "claude-haiku-4-5", "effort": "low"}},
        work_class_tool_allowlists={}, headless_permission_mode="bypassPermissions",
        default_fleet_transport="watch",
    )
    resolved = _apply_spawn_session_policy(req, policy)
    _check(
        resolved.model == "claude-opus-5" and resolved.effort == "high",
        "the old row's own model/effort survive the restart pipeline "
        "unclobbered by a configured work_class default",
    )


def main() -> int:
    test_build_restart_spawn_params_carries_ledger_fields()
    test_build_restart_spawn_params_omits_permission_mode_allowed_tools_transport()
    test_restart_spawn_pipeline_resolves_permission_mode_end_to_end()
    test_restart_spawn_pipeline_never_overrides_an_explicit_ledger_value()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
