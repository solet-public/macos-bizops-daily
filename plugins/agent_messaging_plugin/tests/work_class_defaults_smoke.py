#!/usr/bin/env python3
"""Unit smoke for the §6 L3 rule 1 config substrate — per-``work_class``
spawn defaults read from ``plugin.yaml``'s ``work_class_defaults`` block
(operator policy DATA, not a code default: which model is "cheapest
capable" per work_class is a governance call this module does not invent,
same posture as ``FLEET_HEADLESS_PERMISSION_MODE``).

Covers the two pure module-level helpers in ``plugin.py``
(``_as_work_class_defaults`` — yaml-shape coercion, tolerant of a malformed
entry; ``_apply_work_class_defaults`` — never overrides an explicit caller
value) plus the builder method that wires a real ``ConfigProvider`` through
to those helpers.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/work_class_defaults_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.config.config_provider import ConfigProvider  # noqa: E402

from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _apply_tool_allowlist,
    _apply_work_class_defaults,
    _as_work_class_defaults,
    _as_work_class_tool_allowlists,
    _resolve_permission_mode,
    _resolve_transport,
)
from agent_messaging_plugin.session_lifecycle_verbs import SpawnSessionRequest  # noqa: E402

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


def _req(*, work_class: str, model: str = "", effort: str = "") -> SpawnSessionRequest:
    return SpawnSessionRequest(
        role_class="ephemeral", lane_id="lane-1", brief_ref="brief-1",
        work_class=work_class, budget_line="budget-1", model=model, effort=effort,
    )


def test_as_work_class_defaults_coerces_valid_shape() -> None:
    result = _as_work_class_defaults(
        {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}},
    )
    _check(
        result == {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}},
        "a well-formed work_class_defaults block round-trips exactly",
    )


def test_as_work_class_defaults_skips_malformed_entries() -> None:
    result = _as_work_class_defaults(
        {
            "read_only": {"model": "claude-haiku-4-5", "effort": "low"},
            "analysis_deliverable": "not-a-mapping",
            123: {"model": "x", "effort": "y"},
        },
    )
    _check(
        result == {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}},
        "a malformed entry (non-dict value, non-str key) is skipped, "
        "the well-formed sibling survives -- one operator typo never "
        "poisons the whole config build",
    )


def test_as_work_class_defaults_drops_unknown_keys_within_an_entry() -> None:
    result = _as_work_class_defaults(
        {"read_only": {"model": "claude-haiku-4-5", "effort": "low", "extra": "ignored"}},
    )
    _check(
        result == {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}},
        "only 'model'/'effort' keys survive within an entry",
    )


def test_as_work_class_defaults_non_dict_input_returns_empty() -> None:
    _check(_as_work_class_defaults(None) == {}, "None input -> {} (today's behavior)")
    _check(_as_work_class_defaults([]) == {}, "list input -> {} (wrong shape, never crashes)")
    _check(
        _as_work_class_defaults("bogus") == {},
        "string input -> {} (wrong shape, never crashes)",
    )


def test_apply_work_class_defaults_fills_omitted_fields() -> None:
    req = _req(work_class="read_only")
    defaults = {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}}
    result = _apply_work_class_defaults(req, defaults)
    _check(
        result.model == "claude-haiku-4-5" and result.effort == "low",
        "both model and effort are filled from the configured default "
        "when the caller supplied neither",
    )


def test_apply_work_class_defaults_never_overrides_explicit_values() -> None:
    req = _req(work_class="read_only", model="claude-opus-5", effort="high")
    defaults = {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}}
    result = _apply_work_class_defaults(req, defaults)
    _check(
        result.model == "claude-opus-5" and result.effort == "high",
        "an explicit caller model/effort is NEVER overridden by the "
        "configured default, even when a default exists for this work_class",
    )


def test_apply_work_class_defaults_partial_fields() -> None:
    req = _req(work_class="read_only", model="claude-opus-5")
    defaults = {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}}
    result = _apply_work_class_defaults(req, defaults)
    _check(
        result.model == "claude-opus-5" and result.effort == "low",
        "an explicit model survives while an omitted effort is still "
        "filled from the default -- the two fields are independent",
    )


def test_apply_work_class_defaults_noop_for_unconfigured_work_class() -> None:
    req = _req(work_class="production_mutation")
    defaults = {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}}
    result = _apply_work_class_defaults(req, defaults)
    _check(
        result is req,
        "a work_class with no configured entry returns the SAME request "
        "object untouched (the shipped-empty-config case: no defaults "
        "applied, spawn_session behaves exactly as it did before this "
        "config existed)",
    )


def test_apply_work_class_defaults_noop_for_empty_config() -> None:
    req = _req(work_class="read_only")
    result = _apply_work_class_defaults(req, {})
    _check(result is req, "an empty (shipped-default) config block is a pure no-op")


def test_build_session_lifecycle_policy_config_reads_provider() -> None:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider(
        "agent_messaging_plugin",
        {"work_class_defaults": {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}}},
    )
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.work_class_defaults == {"read_only": {"model": "claude-haiku-4-5", "effort": "low"}},
        "_build_session_lifecycle_policy_config reads a real ConfigProvider's "
        "work_class_defaults key end-to-end through _provider_get",
    )


def test_build_session_lifecycle_policy_config_defaults_to_empty() -> None:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider("agent_messaging_plugin", {})
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.work_class_defaults == {},
        "an absent config key resolves to {} -- the shipped plugin.yaml "
        "default, and spawn_session's model/effort resolution is untouched",
    )


def test_as_work_class_tool_allowlists_coerces_valid_shape() -> None:
    result = _as_work_class_tool_allowlists(
        {"read_only": ["mcp__testhom__peer_register", "mcp__testhom__process_call"]},
    )
    _check(
        result == {"read_only": ("mcp__testhom__peer_register", "mcp__testhom__process_call")},
        "a well-formed work_class_tool_allowlists block round-trips as a tuple",
    )


def test_as_work_class_tool_allowlists_skips_malformed_entries() -> None:
    result = _as_work_class_tool_allowlists(
        {
            "read_only": ["mcp__testhom__process_call"],
            "analysis_deliverable": "not-a-list",
            123: ["x"],
        },
    )
    _check(
        result == {"read_only": ("mcp__testhom__process_call",)},
        "a malformed entry (non-list value, non-str key) is skipped, the "
        "well-formed sibling survives",
    )


def test_as_work_class_tool_allowlists_non_dict_input_returns_empty() -> None:
    _check(
        _as_work_class_tool_allowlists(None) == {},
        "None input -> {} (every spawn still gated, empty)",
    )
    _check(
        _as_work_class_tool_allowlists("bogus") == {},
        "wrong-shaped input -> {}, never crashes",
    )


def _spawn_req(*, work_class: str, allowed_tools: tuple[str, ...] = ()) -> SpawnSessionRequest:
    return SpawnSessionRequest(
        role_class="ephemeral", lane_id="lane-1", brief_ref="brief-1",
        work_class=work_class, budget_line="budget-1", allowed_tools=allowed_tools,
    )


def test_apply_tool_allowlist_fills_omitted_allowlist() -> None:
    req = _spawn_req(work_class="read_only")
    allowlists = {"read_only": ("mcp__testhom__peer_register", "mcp__testhom__process_call")}
    result = _apply_tool_allowlist(req, allowlists)
    _check(
        result.allowed_tools == ("mcp__testhom__peer_register", "mcp__testhom__process_call"),
        "an omitted allowed_tools is filled from the configured per-work_class allowlist",
    )


def test_apply_tool_allowlist_never_overrides_explicit_value() -> None:
    req = _spawn_req(work_class="read_only", allowed_tools=("Read",))
    allowlists = {"read_only": ("mcp__testhom__peer_register", "mcp__testhom__process_call")}
    result = _apply_tool_allowlist(req, allowlists)
    _check(
        result.allowed_tools == ("Read",),
        "an explicit caller allowed_tools is NEVER overridden by the configured default",
    )


def test_apply_tool_allowlist_noop_for_unconfigured_work_class() -> None:
    req = _spawn_req(work_class="production_mutation")
    allowlists = {"read_only": ("mcp__testhom__process_call",)}
    result = _apply_tool_allowlist(req, allowlists)
    _check(
        result is req,
        "a work_class with no configured allowlist entry returns the SAME "
        "request object untouched (shipped-empty-config case)",
    )


def test_apply_tool_allowlist_noop_for_empty_config() -> None:
    req = _spawn_req(work_class="read_only")
    result = _apply_tool_allowlist(req, {})
    _check(result is req, "an empty (shipped-default) allowlist config block is a pure no-op")


def test_build_session_lifecycle_policy_config_reads_tool_allowlists() -> None:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider(
        "agent_messaging_plugin",
        {"work_class_tool_allowlists": {"read_only": ["mcp__testhom__process_call"]}},
    )
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.work_class_tool_allowlists == {"read_only": ("mcp__testhom__process_call",)},
        "_build_session_lifecycle_policy_config reads work_class_tool_allowlists "
        "end-to-end through _provider_get",
    )


def test_build_session_lifecycle_policy_config_defaults_permission_mode() -> None:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider("agent_messaging_plugin", {})
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.headless_permission_mode == "bypassPermissions",
        "an absent headless_permission_mode config key resolves to the "
        "shipped plugin.yaml default 'bypassPermissions' (flipped from "
        "'default' -- D2 finding: an unattended spawn under Claude Code's "
        "own 'default' interactive-approval mode gets empty effective "
        "grants, since no human exists to approve a prompt), not empty",
    )


def test_build_session_lifecycle_policy_config_reads_permission_mode() -> None:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider(
        "agent_messaging_plugin", {"headless_permission_mode": "manual"},
    )
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.headless_permission_mode == "manual",
        "_build_session_lifecycle_policy_config reads a configured "
        "headless_permission_mode end-to-end through _provider_get",
    )


def test_build_session_lifecycle_policy_config_defaults_fleet_transport() -> None:
    """fleet-watch-transport-migration phase-2 slice 2: an absent
    default_fleet_transport config key resolves to 'watch', per the
    operator's verbatim charter ('non-MCP should be the default right
    now') -- not 'mcp', and not empty."""
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider("agent_messaging_plugin", {})
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.default_fleet_transport == "watch",
        "an absent default_fleet_transport config key resolves to the "
        "shipped plugin.yaml default 'watch' (the operator charter's "
        "non-MCP-primary directive), not 'mcp' and not empty",
    )


def test_build_session_lifecycle_policy_config_reads_fleet_transport() -> None:
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider(
        "agent_messaging_plugin", {"default_fleet_transport": "mcp"},
    )
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.default_fleet_transport == "mcp",
        "_build_session_lifecycle_policy_config reads a configured "
        "default_fleet_transport end-to-end through _provider_get -- the "
        "charter's own 'easy to change later' requirement",
    )


def test_build_session_lifecycle_policy_config_empty_fleet_transport_falls_back() -> None:
    """_as_str treats an empty string the same as absent -- an operator who
    sets default_fleet_transport: "" in plugin.yaml gets the charter default
    back, not a silently-empty transport no consumer can act on."""
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    plugin.config_provider = ConfigProvider(
        "agent_messaging_plugin", {"default_fleet_transport": ""},
    )
    policy = plugin._build_session_lifecycle_policy_config()  # noqa: SLF001 -- testing the builder directly
    _check(
        policy.default_fleet_transport == "watch",
        "an explicit empty-string default_fleet_transport falls back to "
        "'watch', matching _as_str's documented empty-is-missing behavior",
    )


def test_resolve_permission_mode_fills_omitted_from_policy() -> None:
    req = _req(work_class="read_only")
    resolved = _resolve_permission_mode(req, "default")
    _check(
        resolved.permission_mode == "default",
        "an omitted permission_mode is filled from the policy value",
    )


def test_resolve_permission_mode_never_overrides_explicit_value() -> None:
    req = SpawnSessionRequest(
        role_class="ephemeral", lane_id="lane-1", brief_ref="brief-1",
        work_class="read_only", budget_line="budget-1", permission_mode="manual",
    )
    resolved = _resolve_permission_mode(req, "acceptEdits")
    _check(
        resolved.permission_mode == "manual",
        "an explicit caller permission_mode is NEVER overridden by the policy value",
    )


def test_resolve_permission_mode_accepts_bypass_from_policy() -> None:
    """Operator ruling, 2026-08-03 ('we don't have any restrictions now'):
    no value is rejected here anymore -- including bypassPermissions."""
    req = _req(work_class="read_only")
    resolved = _resolve_permission_mode(req, "bypassPermissions")
    _check(
        resolved.permission_mode == "bypassPermissions",
        "a policy-configured bypassPermissions now resolves through untouched, "
        "per the operator's 'no restrictions' ruling",
    )


def test_resolve_permission_mode_stays_empty_when_no_policy_value() -> None:
    req = _req(work_class="read_only")
    result = _resolve_permission_mode(req, "")
    _check(
        result.permission_mode == "",
        "an empty policy value with no explicit caller value resolves to empty "
        "-- headless_adapter.py.verify_config() is the actual floor that refuses "
        "a spawn resolving to nothing at all",
    )


def test_resolve_transport_fills_omitted_from_policy() -> None:
    """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06): mirrors
    _resolve_permission_mode's test exactly, one field later."""
    req = _req(work_class="read_only")
    resolved = _resolve_transport(req, "watch")
    _check(
        resolved.transport == "watch",
        "an omitted transport is filled from the policy value (default_fleet_transport)",
    )


def test_resolve_transport_never_overrides_explicit_value() -> None:
    req = SpawnSessionRequest(
        role_class="ephemeral", lane_id="lane-1", brief_ref="brief-1",
        work_class="read_only", budget_line="budget-1", transport="mcp",
    )
    resolved = _resolve_transport(req, "watch")
    _check(
        resolved.transport == "mcp",
        "an explicit caller transport is NEVER overridden by the policy value",
    )


def test_resolve_transport_stays_empty_when_no_policy_value() -> None:
    req = _req(work_class="read_only")
    result = _resolve_transport(req, "")
    _check(
        result.transport == "",
        "an empty policy value with no explicit caller value resolves to empty "
        "-- the host driver's own hardcoded 'watch' floor is the actual last "
        "resort, not this pure resolver",
    )


def main() -> int:
    test_as_work_class_defaults_coerces_valid_shape()
    test_as_work_class_defaults_skips_malformed_entries()
    test_as_work_class_defaults_drops_unknown_keys_within_an_entry()
    test_as_work_class_defaults_non_dict_input_returns_empty()
    test_apply_work_class_defaults_fills_omitted_fields()
    test_apply_work_class_defaults_never_overrides_explicit_values()
    test_apply_work_class_defaults_partial_fields()
    test_apply_work_class_defaults_noop_for_unconfigured_work_class()
    test_apply_work_class_defaults_noop_for_empty_config()
    test_build_session_lifecycle_policy_config_reads_provider()
    test_build_session_lifecycle_policy_config_defaults_to_empty()
    test_as_work_class_tool_allowlists_coerces_valid_shape()
    test_as_work_class_tool_allowlists_skips_malformed_entries()
    test_as_work_class_tool_allowlists_non_dict_input_returns_empty()
    test_apply_tool_allowlist_fills_omitted_allowlist()
    test_apply_tool_allowlist_never_overrides_explicit_value()
    test_apply_tool_allowlist_noop_for_unconfigured_work_class()
    test_apply_tool_allowlist_noop_for_empty_config()
    test_build_session_lifecycle_policy_config_reads_tool_allowlists()
    test_build_session_lifecycle_policy_config_defaults_permission_mode()
    test_build_session_lifecycle_policy_config_reads_permission_mode()
    test_build_session_lifecycle_policy_config_defaults_fleet_transport()
    test_build_session_lifecycle_policy_config_reads_fleet_transport()
    test_build_session_lifecycle_policy_config_empty_fleet_transport_falls_back()
    test_resolve_permission_mode_fills_omitted_from_policy()
    test_resolve_permission_mode_never_overrides_explicit_value()
    test_resolve_permission_mode_accepts_bypass_from_policy()
    test_resolve_permission_mode_stays_empty_when_no_policy_value()
    test_resolve_transport_fills_omitted_from_policy()
    test_resolve_transport_never_overrides_explicit_value()
    test_resolve_transport_stays_empty_when_no_policy_value()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
