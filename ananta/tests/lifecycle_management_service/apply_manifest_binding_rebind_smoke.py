#!/usr/bin/env python3
"""Standalone smoke for apply_manifest v1.1 binding-mutation extension.

Verifies the W-VAULT-LOCAL-KEYCHAIN Tier 3 §1.1 contract:
``_v1_synthesize_manifest`` accepts caller-supplied ``service_bindings``;
the binding-validator rejects plugin-list-only renames as
``binding_provider_missing``; the same rename WITH caller-supplied
rebind passes and the diff reports ``rebound_services``.

Pure-function tests against ``validate_bindings_satisfied`` + ``diff_manifest``
+ ``_v1_synthesize_manifest``. No live orchestrator, no Postgres, no
filesystem CAS. Exercises the smallest possible surface that proves the
v1.1 behavior in a hermetic standalone run.

Run:
    .venv/bin/python3 ananta/tests/lifecycle_management_service/apply_manifest_binding_rebind_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.lifecycle_management_service.binding_validator import (  # noqa: E402
    validate_bindings_satisfied,
)
from ananta.services.lifecycle_management_service.manifest_writer import (  # noqa: E402
    CurrentManifestState,
    diff_manifest,
)
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
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


def _make_service() -> LifecycleManagementService:
    """Service instance for synthesizer + envelope tests. Orchestrator unused by these paths."""
    return LifecycleManagementService(orchestrator_ref=None)


def _current_state_with_default_vault() -> CurrentManifestState:
    """Synthetic pre-rename state: plugin set includes ``default_vault_plugin``; vault_service bound to it."""
    return CurrentManifestState(
        plugins=("default_vault_plugin", "state_service_plugin", "macos_self_deployment_plugin"),
        service_bindings={
            "vault_service": "default_vault_plugin",
            "state_service": "state_service_plugin",
            "self_deployment_service": "macos_self_deployment_plugin",
        },
        manifest_bytes=b"# synthetic\n",
        bindings_bytes=b"{}\n",
        etag="synthetic-etag",
    )


# ─────────────────────────────────────────────────────────────────────────
# SC-10: Plugin-list-only rename REJECTED as binding_provider_missing
# ─────────────────────────────────────────────────────────────────────────

def test_sc10_plugin_list_only_rename_rejected() -> None:
    """No caller-supplied bindings; new plugins lacks the old provider; validator returns satisfied=False."""
    svc = _make_service()
    current = _current_state_with_default_vault()
    new_manifest = {
        "plugins": ["macos_vault_plugin", "state_service_plugin", "macos_self_deployment_plugin"],
        "profile_name": "local",
    }
    synth = svc._v1_synthesize_manifest(new_manifest, current)
    _check(
        "effective_manifest" in synth,
        "SC-10: synthesizer accepts plugin-list-only call (does not reject upfront)",
    )
    if "effective_manifest" not in synth:
        return
    effective = synth["effective_manifest"]
    _check(
        effective["service_bindings"]["vault_service"] == "default_vault_plugin",
        "SC-10: effective bindings carry over stale current vault_service binding",
    )
    result = validate_bindings_satisfied(
        new_manifest=effective,
        current_bindings=current.service_bindings,
    )
    _check(
        not result.satisfied,
        "SC-10: binding-validator rejects (satisfied=False) when bound provider absent from new plugins",
    )
    missing_services = [mb.service for mb in result.missing_bindings]
    _check(
        "vault_service" in missing_services,
        "SC-10: rejection cites vault_service as missing binding",
    )


# ─────────────────────────────────────────────────────────────────────────
# SC-11: Rename + caller-supplied rebind PASSES; diff reports rebound_services
# ─────────────────────────────────────────────────────────────────────────

def test_sc11_rename_with_rebind_passes_and_reports_rebound() -> None:
    """Caller supplies new service_bindings rebinding vault_service to the new plugin; validator passes; diff reports rebound."""
    svc = _make_service()
    current = _current_state_with_default_vault()
    new_manifest = {
        "plugins": ["macos_vault_plugin", "state_service_plugin", "macos_self_deployment_plugin"],
        "profile_name": "local",
        "service_bindings": {
            "vault_service": "macos_vault_plugin",
        },
    }
    synth = svc._v1_synthesize_manifest(new_manifest, current)
    _check(
        "effective_manifest" in synth,
        "SC-11: synthesizer accepts caller-supplied service_bindings",
    )
    if "effective_manifest" not in synth:
        return
    effective = synth["effective_manifest"]
    _check(
        effective["service_bindings"]["vault_service"] == "macos_vault_plugin",
        "SC-11: effective bindings reflect the caller-supplied rebind",
    )
    _check(
        effective["service_bindings"]["state_service"] == "state_service_plugin",
        "SC-11: untouched current bindings (state_service) survive the merge",
    )
    result = validate_bindings_satisfied(
        new_manifest=effective,
        current_bindings=current.service_bindings,
    )
    _check(
        result.satisfied,
        "SC-11: binding-validator passes (satisfied=True) when rebind matches new plugins list",
    )
    diff = diff_manifest(current, effective)
    _check(
        "vault_service" in diff.rebound_services,
        "SC-11: diff reports vault_service in rebound_services",
    )
    _check(
        "default_vault_plugin" in diff.removed_plugins,
        "SC-11: diff reports default_vault_plugin in removed_plugins",
    )
    _check(
        "macos_vault_plugin" in diff.added_plugins,
        "SC-11: diff reports macos_vault_plugin in added_plugins",
    )


# ─────────────────────────────────────────────────────────────────────────
# SC-10b: plugin_config_overrides still rejected (regression guard)
# ─────────────────────────────────────────────────────────────────────────

def test_plugin_config_overrides_still_rejected() -> None:
    """The v1 ban on plugin_config_overrides is preserved by the v1.1 surface."""
    svc = _make_service()
    current = _current_state_with_default_vault()
    new_manifest = {
        "plugins": ["macos_vault_plugin", "state_service_plugin", "macos_self_deployment_plugin"],
        "service_bindings": {"vault_service": "macos_vault_plugin"},
        "plugin_config_overrides": {"macos_vault_plugin": {"foo": "bar"}},
    }
    synth = svc._v1_synthesize_manifest(new_manifest, current)
    _check(
        "rejection_envelope" in synth,
        "SC-10b: plugin_config_overrides triggers v1 rejection",
    )


# ─────────────────────────────────────────────────────────────────────────
# SC-10c: malformed service_bindings shape rejected before any disk write
# ─────────────────────────────────────────────────────────────────────────

def test_malformed_service_bindings_rejected() -> None:
    """service_bindings must be a dict; passing a list raises a structured rejection."""
    svc = _make_service()
    current = _current_state_with_default_vault()
    new_manifest = {
        "plugins": ["macos_vault_plugin"],
        "service_bindings": ["vault_service", "macos_vault_plugin"],
    }
    synth = svc._v1_synthesize_manifest(new_manifest, current)
    _check(
        "rejection_envelope" in synth,
        "SC-10c: malformed service_bindings shape rejected upfront",
    )


_SMOKES = [
    ("SC-10  Plugin-list-only rename rejected", test_sc10_plugin_list_only_rename_rejected),
    ("SC-11  Rename + caller rebind passes + diff reports rebound", test_sc11_rename_with_rebind_passes_and_reports_rebound),
    ("SC-10b plugin_config_overrides still rejected", test_plugin_config_overrides_still_rejected),
    ("SC-10c malformed service_bindings rejected", test_malformed_service_bindings_rejected),
]


def main() -> int:
    for label, fn in _SMOKES:
        print(f"\n=== {label} ===")
        try:
            fn()
        except Exception as exc:
            _failed.append(f"{label}: crashed with {exc!r}")
            print(f"  CRASH  {exc!r}")
    print()
    if _failed:
        print(f"FAILURES ({len(_failed)}):")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print(f"All {_passed} checks passed across {len(_SMOKES)} smokes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
