"""W-VAULT-CALLER-ENFORCE smoke — sub-2 success-criteria suite.

P0 Tier 2 sub-2 (state-service consolidation campaign). Covers the 13
smokes specified in `workbench/2026-06-07_tier_2_w_vault_caller_enforce_brief.md`
§6, plus the §6.1 operator-MCP entry-path verification (Codex
correction #6).

Each smoke is a standalone function named ``_smoke_NN_<short_name>``.
``main()`` runs them in order and prints PASS/FAIL per smoke. Exit
status 0 when all pass, 1 otherwise — matches the pattern used by
``plugins/secrets_manager_vault_plugin/tests/secrets_manager_vault_smoke.py``.

Most smokes operate against the enforcement primitives in
:mod:`ananta.services.vault_service.enforcement` directly + tiny mock
``CallContext`` instances. Smokes 2/3/5/6/9/12 build a minimal real
``MacosVaultPlugin`` instance via ``object.__new__`` (no DB) and
exercise the structural methods. Smoke 11 is a static filesystem check.
Smoke 13 introspects both concrete plugin classes' source files.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from ananta.core.process_registry.invocation_schema_generator import (
    InvocationSchemaGenerator,
)
from ananta.core.process_registry.service_interface_scanner import (
    ServiceInterfaceScanner,
)
from ananta.core.services.call_context import (
    CallContext,
    VaultAccessDeniedError,
    VaultKeyMalformedError,
)
from ananta.services.vault_service.enforcement import (
    enforce_namespace,
    requires_operator_principal,
)
from ananta.services.vault_service.interfaces.public import VaultServiceAPI
from ananta.services.vault_service.vault_service_proxy import (
    OPERATOR_ONLY_METHODS,
    VaultServiceProxy,
)

_passed: int = 0
_failed: list[str] = []


def _check(ok: bool, message: str) -> None:
    """Record a single smoke result."""
    global _passed
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {message}")
    if ok:
        _passed += 1
    else:
        _failed.append(message)


def _expect_raises(
    label: str,
    exc_type: type[BaseException],
    fn: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Helper: assert that calling fn raises exc_type."""
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        _check(True, f"{label}: raised {exc_type.__name__} ({exc})")
        return
    except BaseException as exc:  # noqa: BLE001  — smoke test wants the full picture
        _check(False, f"{label}: expected {exc_type.__name__}, got {type(exc).__name__} ({exc})")
        return
    _check(False, f"{label}: expected {exc_type.__name__}, no exception raised")


# ─── Smoke 1: registry-metadata (requires_call_context survives scan) ──


def _smoke_01_registry_metadata_requires_call_context() -> None:
    print("\nSmoke 1: registry-metadata smoke — requires_call_context survives scanner")
    scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
    processes: dict[str, object] = {}
    scanner._scan_class_methods(VaultServiceAPI, processes)  # noqa: SLF001
    process_key = "service_interface::vault_service::retrieve"
    entry = processes.get(process_key)
    if not isinstance(entry, dict):
        _check(False, f"{process_key} missing from scanner output")
        return
    _check(
        entry.get("requires_call_context") is True,
        f"{process_key} carries requires_call_context=True after scan "
        f"(actual: {entry.get('requires_call_context')!r})",
    )
    # Also verify a sample of OTHER vault verbs carry the flag — guards
    # against an accidental single-method opt-in.
    for verb in ("store", "rename", "vault_init", "oauth_client_list"):
        full = f"service_interface::vault_service::{verb}"
        e = processes.get(full)
        if not isinstance(e, dict):
            _check(False, f"{full} missing from scanner output")
            continue
        _check(
            e.get("requires_call_context") is True,
            f"{full} carries requires_call_context=True",
        )


# ─── Helpers for plugin-instance smokes (smokes 2/3/5/6/9/12) ──────────


def _make_minimal_macos_vault_plugin() -> Any:
    """Build a MacosVaultPlugin minimal enough to exercise structural
    methods. Skips PluginBase __init__ + DB wiring; the methods we
    exercise (retrieve / get_public_key / unlock) reach for impls /
    helpers that we monkeypatch."""
    # The plugin's constants resolve scoped keys eagerly and fail closed when
    # the caller has not supplied SOLET_NAME.
    from macos_vault_plugin.plugin import MacosVaultPlugin

    plugin = object.__new__(MacosVaultPlugin)
    plugin.logger = logging.getLogger("w_vault_caller_enforce_smoke.vault")

    # Stub the substrate calls so structural methods reach the impl
    # paths without needing a real vault. Any of these may be called by
    # smokes 2-12; we make them safe no-ops that return a `not_found`
    # action result. The enforcement happens BEFORE these impls run, so
    # an operator-positive call that passes enforcement will land here
    # and return not_found — which is the expected "no auth denial"
    # outcome.
    def _not_found(key: str) -> dict[str, Any]:
        return {
            "action_status": "error",
            "data": {},
            "error": {"code": "not_found", "message": f"{key} not found"},
            "timestamp": "",
        }

    plugin._retrieve_impl = lambda key: _not_found(key)
    plugin._store_impl = lambda key, _value, _tags, _metadata: {
        "action_status": "completed",
        "data": {"key": key},
        "error": None,
        "timestamp": "",
    }
    plugin._exists_impl = lambda key: {
        "action_status": "completed",
        "data": {"key": key, "exists": False},
        "error": None,
        "timestamp": "",
    }
    plugin._delete_impl = lambda key: _not_found(key)

    class _StubKeyMgr:
        def unlock(self, passphrase: str) -> None:
            del passphrase

        def lock(self) -> None:
            pass

        def get_status(self) -> dict[str, Any]:
            return {"initialized": True, "unlocked": True}

    plugin._get_key_manager = lambda: _StubKeyMgr()
    plugin._crypto = None
    plugin._success = lambda data: {
        "action_status": "completed",
        "data": dict(data),
        "error": None,
        "timestamp": "",
    }
    return plugin


# ─── Smoke 2: SC-A positive both surfaces ──────────────────────────────


def _smoke_02_sc_a_positive_both_surfaces() -> None:
    print("\nSmoke 2: SC-A positive both surfaces — owner plugin can read its own namespace")
    plugin = _make_minimal_macos_vault_plugin()
    # Direct surface (queued-action equivalent: ActionProcessor builds
    # the CallContext and injects via kwarg; we simulate that here).
    ctx = CallContext.for_plugin("plugin_A")
    try:
        plugin.retrieve("example.plugin_A.token", call_context=ctx)
        _check(True, "direct: retrieve('example.plugin_A.token') with for_plugin('plugin_A') passes enforcement (hit substrate)")
    except VaultAccessDeniedError as e:
        _check(False, f"direct: should NOT raise VaultAccessDeniedError on own-namespace key: {e}")
    # Proxy surface (bound-service: the proxy injects the context).
    proxy = VaultServiceProxy(plugin, "plugin_A")
    try:
        proxy.retrieve("example.plugin_A.token")
        _check(True, "proxy: retrieve('example.plugin_A.token') from caller_plugin='plugin_A' passes enforcement (hit substrate)")
    except VaultAccessDeniedError as e:
        _check(False, f"proxy: should NOT raise VaultAccessDeniedError on own-namespace key: {e}")


# ─── Smoke 3: SC-B cross-plugin denial both surfaces ───────────────────


def _smoke_03_sc_b_cross_plugin_denial_both_surfaces() -> None:
    print("\nSmoke 3: SC-B cross-plugin denial both surfaces")
    plugin = _make_minimal_macos_vault_plugin()
    ctx = CallContext.for_plugin("plugin_A")
    _expect_raises(
        "direct: plugin_A reading plugin_B's key",
        VaultAccessDeniedError,
        plugin.retrieve, "example.plugin_B.token", call_context=ctx,
    )
    proxy = VaultServiceProxy(plugin, "plugin_A")
    _expect_raises(
        "proxy: plugin_A reading plugin_B's key",
        VaultAccessDeniedError,
        proxy.retrieve, "example.plugin_B.token",
    )


# ─── Smoke 4: operator-principal positive ──────────────────────────────


def _smoke_04_operator_principal_positive() -> None:
    print("\nSmoke 4: operator-principal positive — operator bypasses namespace check")
    plugin = _make_minimal_macos_vault_plugin()
    op_ctx = CallContext.for_operator()
    try:
        plugin.retrieve("example.any_plugin.any_key", call_context=op_ctx)
        _check(True, "operator: retrieve('example.any_plugin.any_key') passes (no VaultAccessDeniedError)")
    except VaultAccessDeniedError as e:
        _check(False, f"operator should bypass namespace check: {e}")
    # Operator can also unlock (operator-only verb).
    try:
        plugin.unlock("test", call_context=op_ctx)
        _check(True, "operator: unlock passes (operator-only verb allows operator)")
    except VaultAccessDeniedError as e:
        _check(False, f"operator should be allowed to unlock: {e}")


# ─── Smoke 5: plugin-principal calling operator-only method ────────────


def _smoke_05_plugin_principal_calling_operator_only() -> None:
    print("\nSmoke 5: plugin-principal calling operator-only method (unlock)")
    plugin = _make_minimal_macos_vault_plugin()
    plugin_ctx = CallContext.for_plugin("plugin_A")
    _expect_raises(
        "plugin-principal calling unlock",
        VaultAccessDeniedError,
        plugin.unlock, "test", call_context=plugin_ctx,
    )


# ─── Smoke 6: get_public_key operator-only ─────────────────────────────


def _smoke_06_get_public_key_operator_only() -> None:
    print("\nSmoke 6: get_public_key operator-only")
    plugin = _make_minimal_macos_vault_plugin()
    # Non-operator caller → denied.
    plugin_ctx = CallContext.for_plugin("plugin_A")
    _expect_raises(
        "plugin-principal calling get_public_key",
        VaultAccessDeniedError,
        plugin.get_public_key, call_context=plugin_ctx,
    )
    # External caller → denied.
    ext_ctx = CallContext.for_external("client_X")
    _expect_raises(
        "external-principal calling get_public_key",
        VaultAccessDeniedError,
        plugin.get_public_key, call_context=ext_ctx,
    )
    # The operator-positive case isn't unit-testable on this stubbed
    # plugin (the impl reaches into substrate). It's covered by the
    # decorator's pass-through behavior verified in smoke 13.


# ─── Smoke 7: external-bridge smoke ────────────────────────────────────


def _smoke_07_external_bridge_principal_kind() -> None:
    print("\nSmoke 7: external-bridge smoke — external principal_kind not operator")
    ctx = CallContext.for_external("test_client_id")
    _check(
        ctx.principal_kind == "external",
        f"for_external builds principal_kind='external' (actual: {ctx.principal_kind!r})",
    )
    _check(
        ctx.is_operator_principal is False,
        f"for_external().is_operator_principal is False (actual: {ctx.is_operator_principal!r})",
    )
    # External principal calling a plugin-scoped key without a bound
    # calling_plugin should be denied.
    plugin = _make_minimal_macos_vault_plugin()
    _expect_raises(
        "external calling 'example.plugin_A.token'",
        VaultAccessDeniedError,
        plugin.retrieve, "example.plugin_A.token", call_context=ctx,
    )


# ─── Smoke 8: operator-MCP entry path verification ─────────────────────


def _smoke_08_operator_mcp_principal_stamping() -> None:
    print("\nSmoke 8: operator-MCP direct path — ActionProcessor stamps operator")
    # Brief §6.1: ActionProcessor._build_call_context should default to
    # CallContext.for_operator() for direct operator MCP entry
    # (source_plugin=None AND no flow / no authenticated principal).
    # Inspect the path directly rather than spinning up ActionProcessor.
    from ananta.core.actions.action_processor import ActionProcessor

    class _FakeAction:
        source_plugin: str | None = None
        flow_id: str | None = None
        id: str = "test-action"

    fake_self: Any = object.__new__(ActionProcessor)
    fake_self._get_flow_trigger_data = lambda _flow_id: {}

    action_no_plugin: Any = _FakeAction()
    ctx = ActionProcessor._build_call_context(fake_self, action_no_plugin)  # type: ignore[arg-type]
    _check(
        ctx.principal_kind == "operator" and ctx.calling_plugin is None,
        f"direct operator MCP defaults to for_operator() "
        f"(actual: principal_kind={ctx.principal_kind!r}, "
        f"calling_plugin={ctx.calling_plugin!r})",
    )

    class _FakeActionFromPlugin:
        source_plugin: str | None = "plugin_X"
        flow_id: str | None = "flow-1"
        id: str = "test-2"

    action_with_plugin: Any = _FakeActionFromPlugin()
    ctx2 = ActionProcessor._build_call_context(fake_self, action_with_plugin)  # type: ignore[arg-type]
    _check(
        ctx2.principal_kind == "plugin" and ctx2.calling_plugin == "plugin_X",
        f"queued action with source_plugin stamps for_plugin "
        f"(actual: {ctx2.principal_kind!r}, calling_plugin={ctx2.calling_plugin!r})",
    )


# ─── Smoke 9: missing CallContext (server-side bug check) ──────────────


def _smoke_09_missing_call_context() -> None:
    print("\nSmoke 9: missing CallContext — server-side bug check")
    plugin = _make_minimal_macos_vault_plugin()
    _expect_raises(
        "retrieve(key, call_context=None)",
        VaultAccessDeniedError,
        plugin.retrieve, "example.plugin_A.token", call_context=None,
    )


# ─── Smoke 10: malformed key (less than 3 segments) ────────────────────


def _smoke_10_malformed_key() -> None:
    print("\nSmoke 10: malformed key — less than 3 segments raises VaultKeyMalformedError")
    plugin = _make_minimal_macos_vault_plugin()
    ctx = CallContext.for_plugin("only_two")
    _expect_raises(
        "retrieve('only_two.parts') from plugin-principal",
        VaultKeyMalformedError,
        plugin.retrieve, "only_two.parts", call_context=ctx,
    )
    # Helper-direct check too — defense against the decorator-stack
    # accidentally swallowing it.
    _expect_raises(
        "enforce_namespace('a.b', plugin-ctx) direct",
        VaultKeyMalformedError,
        enforce_namespace, "a.b", ctx,
    )


# ─── Smoke 11: removed plugin-process surface inventory ────────────────


def _smoke_11_removed_plugin_process_surface() -> None:
    print("\nSmoke 11: removed plugin-process surface inventory — no JSONs left, no decorators left")
    # File path:
    # <repo>/ananta/src/ananta/services/vault_service/tests/this_file.py
    # parents[0]=tests, [1]=vault_service, [2]=services, [3]=ananta,
    # [4]=src, [5]=ananta (outer), [6]=<repo>.
    repo_root = Path(__file__).resolve().parents[6]
    targets = [
        repo_root / "plugins" / "macos_vault_plugin" / "knowledge_base" / "processes",
        repo_root / "plugins" / "secrets_manager_vault_plugin" / "knowledge_base" / "processes",
    ]
    for target in targets:
        json_count = (
            len(list(target.glob("*.json"))) if target.exists() else 0
        )
        _check(
            json_count == 0,
            f"{target.relative_to(repo_root)}: {json_count} JSON files left (expected 0)",
        )
    # Source-grep: zero @platform_process decorators in either plugin.py
    for plugin_path in [
        repo_root / "plugins" / "macos_vault_plugin" / "src"
        / "macos_vault_plugin" / "plugin.py",
        repo_root / "plugins" / "secrets_manager_vault_plugin" / "src"
        / "secrets_manager_vault_plugin" / "plugin.py",
    ]:
        # ROSTER INTERSECTION. The plugin roster here is hardcoded, but which
        # plugins ship is resolved PER CAPABILITY BUNDLE:
        # secrets_manager_vault_plugin is in no bundle, so it is absent from every
        # seed while macos_vault_plugin ships in all of them. This is the repair
        # the seed manifest's ★ standard prescribes for a hardcoded roster, and
        # the same one applied to hydration_guidance_convention_smoke. The
        # assertion is a REMOVAL proof, so an absent plugin satisfies it
        # vacuously — there is no surface left to carry a decorator. Measured on
        # the r21 born-clone verdict (sealed 254700866ab0…): this smoke was 1 of
        # 14 BLOCKING, raising FileNotFoundError on exactly this path after ten
        # enforcement smokes had passed.
        if not plugin_path.is_file():
            print(f"  SKIP    {plugin_path.relative_to(repo_root)}: plugin not in this profile's set")
            continue
        source = plugin_path.read_text(encoding="utf-8")
        count = sum(
            1 for line in source.splitlines()
            if line.startswith("    @platform_process(")
        )
        _check(
            count == 0,
            f"{plugin_path.relative_to(repo_root)}: {count} @platform_process decorators left (expected 0)",
        )


# ─── Smoke 12: compat-mode dropped (legacy flat names rejected) ────────


def _smoke_12_compat_mode_dropped() -> None:
    print("\nSmoke 12: compat-mode dropped — legacy flat name rejected with VaultKeyMalformedError")
    plugin = _make_minimal_macos_vault_plugin()
    ctx = CallContext.for_plugin("anything")
    _expect_raises(
        "retrieve('legacy_flat_name') from plugin-principal",
        VaultKeyMalformedError,
        plugin.retrieve, "legacy_flat_name", call_context=ctx,
    )


# ─── Smoke 13: lockstep parity smoke ───────────────────────────────────


def _smoke_13_lockstep_parity() -> None:
    print("\nSmoke 13: lockstep parity — decorated methods match OPERATOR_ONLY_METHODS")
    repo_root = Path(__file__).resolve().parents[6]
    for plugin_path, label in [
        (
            repo_root / "plugins" / "macos_vault_plugin" / "src"
            / "macos_vault_plugin" / "plugin.py",
            "macos_vault_plugin",
        ),
        (
            repo_root / "plugins" / "secrets_manager_vault_plugin" / "src"
            / "secrets_manager_vault_plugin" / "plugin.py",
            "secrets_manager_vault_plugin",
        ),
    ]:
        # Same roster intersection as smoke 11 above, same reason.
        if not plugin_path.is_file():
            print(f"  SKIP    {label}: plugin not in this profile's set")
            continue
        source = plugin_path.read_text(encoding="utf-8")
        lines = source.splitlines()
        decorated: set[str] = set()
        for idx, line in enumerate(lines):
            if line.strip() != "@requires_operator_principal":
                continue
            # Find the next `    def <name>(` line.
            for follow in lines[idx + 1: idx + 6]:
                stripped = follow.lstrip()
                if stripped.startswith("def "):
                    name = stripped.split("def ", 1)[1].split("(", 1)[0].strip()
                    decorated.add(name)
                    break
        # frozenset and set with the same string members compare equal.
        expected = set(OPERATOR_ONLY_METHODS)
        _check(
            decorated == expected,
            f"{label}: decorated set ({sorted(decorated)}) == OPERATOR_ONLY_METHODS "
            f"({sorted(expected)})",
        )

    # Pass-through behavior: the decorator forwards args + return value
    # unchanged when the principal is operator. This is the only smoke
    # path that exercises the decorator end-to-end without a real
    # plugin instance — guards against a wrapper bug.
    captured: dict[str, Any] = {}

    @requires_operator_principal
    def _example(
        instance_label: str,
        x: int,
        *,
        call_context: CallContext | None = None,
    ) -> int:
        captured["self"] = instance_label
        captured["x"] = x
        captured["ctx"] = call_context
        return x * 2

    result = _example("instance", 21, call_context=CallContext.for_operator())
    _check(
        result == 42 and captured["x"] == 21 and captured["self"] == "instance",
        f"requires_operator_principal forwards args + return (got {result})",
    )


def main() -> int:
    print("W-VAULT-CALLER-ENFORCE (Tier 2 sub-2) smoke suite")
    print("=" * 60)
    _smoke_01_registry_metadata_requires_call_context()
    _smoke_02_sc_a_positive_both_surfaces()
    _smoke_03_sc_b_cross_plugin_denial_both_surfaces()
    _smoke_04_operator_principal_positive()
    _smoke_05_plugin_principal_calling_operator_only()
    _smoke_06_get_public_key_operator_only()
    _smoke_07_external_bridge_principal_kind()
    _smoke_08_operator_mcp_principal_stamping()
    _smoke_09_missing_call_context()
    _smoke_10_malformed_key()
    _smoke_11_removed_plugin_process_surface()
    _smoke_12_compat_mode_dropped()
    _smoke_13_lockstep_parity()
    print("\n" + "=" * 60)
    print(f"  {_passed} passed, {len(_failed)} failed")
    if _failed:
        for f in _failed:
            print(f"    FAIL: {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
