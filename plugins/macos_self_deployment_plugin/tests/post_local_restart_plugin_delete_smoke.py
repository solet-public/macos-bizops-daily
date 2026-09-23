#!/usr/bin/env python3
"""Cycle 6 post-delete smoke for ``local_restart_plugin`` retirement.

Validates the static + library-level contract surfaces that Cycle 6 (the
local_restart_plugin retirement) could have broken. The live blue/green
cutover assertions per design memo §2.3 (b)–(f) — ``restart_with_manifest``
contract envelope, ``stop_self`` contract envelope, blue/green router
swap completes, the solet healthy post-cutover — are STRUCTURALLY DEFERRED to
the Goal 1 GOAL-VALIDATED checkpoint (per Coordinator-Day 2026-06-16 PT
concur), matching the F3 ``install_plugin_from_path_smoke.py`` A4
precedent + the ``root_manifest_smoke.py`` §7.3 b/c/d precedent that
multi-process integration territory beyond the per-cycle scope is owned
by a follow-on live-solet smoke. The C4 new-pip-deps smoke + crash-recovery
smoke + manifest diagnostic on polluted root at the Goal 1 checkpoint
will exercise the swap machinery end-to-end.

What this smoke covers (positive assertions per memo §2.3):
  (a) plugin registry / entry-points post-delete does NOT contain
      ``local_restart_plugin``.
  (a') ``plugins/local_restart_plugin/`` directory absent on disk.
  (a'') repo-wide source grep returns ZERO ``local_restart_plugin`` /
       ``LocalRestartPlugin`` references in production code paths
       (plugins/ ananta/ deployment/ initialization/).
  (a''') both ``local.yaml`` profile artifacts (midwife KB template +
        ``initialization/profiles/local.yaml``) parse cleanly post-edit,
        omit ``local_restart_plugin`` from the ``plugins:`` list, and
        keep ``service_bindings.self_deployment_service``
        → ``macos_self_deployment_plugin``.
  (a'''') library-level ``PluginManager`` rediscovery against a
         post-cleanup ``allowed_plugins`` set finds
         ``macos_self_deployment_plugin`` in ``plugin_manager.plugins``
         and does NOT find ``local_restart_plugin``.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402

_DELETED_PLUGIN_NAME = "local_restart_plugin"
_DELETED_PLUGIN_CLASS = "LocalRestartPlugin"
_CANONICAL_PROVIDER = "macos_self_deployment_plugin"
_ENTRY_POINT_GROUP = "ananta.plugins"

_MIDWIFE_TEMPLATE = (
    REPO_ROOT
    / "plugins"
    / "macos_midwife_plugin"
    / "knowledge_base"
    / "profile_templates"
    / "local.yaml"
)
_OPERATOR_PROFILE = REPO_ROOT / "initialization" / "profiles" / "local.yaml"

_PRODUCTION_GREP_ROOTS = (
    REPO_ROOT / "plugins",
    REPO_ROOT / "ananta",
    REPO_ROOT / "deployment",
    REPO_ROOT / "initialization",
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


def _entry_point_names() -> set[str]:
    return {
        ep.name
        for ep in importlib.metadata.entry_points().select(group=_ENTRY_POINT_GROUP)
    }


def _refresh_entry_points_cache() -> None:
    """Drop importlib caches so a stale ``local_restart_plugin`` entry from
    a prior install in this process is not still surfaced. The 6-D step
    ran ``pip uninstall``; this drops the in-process metadata snapshot
    so the smoke observes the on-disk truth.
    """
    importlib.invalidate_caches()
    importlib.metadata.MetadataPathFinder.invalidate_caches()


def _load_profile_yaml(path: Path) -> Any:
    """Parse the profile YAML — return type intentionally ``Any`` so the
    smoke can assert the top-level shape (mapping vs list vs None) as
    part of its positive-outcome contract.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _case_a_entry_points_no_legacy() -> None:
    """A: entry_points(group='ananta.plugins') no longer carries the plugin."""
    print("\nCase A: entry_points post-uninstall does NOT carry the deleted plugin")
    _refresh_entry_points_cache()
    _check(
        _DELETED_PLUGIN_NAME not in _entry_point_names(),
        f"  importlib.metadata.entry_points() omits {_DELETED_PLUGIN_NAME!r}",
    )


def _case_a_prime_directory_absent() -> None:
    """A': source-tree directory removed by 6-D."""
    print("\nCase A': plugin source directory absent on disk")
    target = REPO_ROOT / "plugins" / _DELETED_PLUGIN_NAME
    _check(
        not target.exists(),
        f"  {target.relative_to(REPO_ROOT)} does not exist",
    )


def _case_a_double_prime_grep_clean() -> None:
    """A'': production-tree grep returns zero name references.

    The smoke file itself carries ``_DELETED_PLUGIN_NAME`` /
    ``_DELETED_PLUGIN_CLASS`` as constants — exclude it so the
    assertion exercises the cascade-clean contract instead of
    catching its own self-reference.

    ``shipped_smoke_contract.yaml`` is excluded for the same reason and no other:
    it is a PATH-KEYED REGISTRATION surface, so it necessarily contains this
    smoke's own filename as a key, and that filename contains the retired plugin
    name as a substring. That is a registration, not a reference to the deleted
    plugin — exactly the relationship ``quality_gates/gate_smokes.txt`` has, which
    is why the register is not among ``_PRODUCTION_GREP_ROOTS`` either. The
    exclusion is by FILENAME, so a real code reference to the retired plugin
    anywhere else under the grep roots is still caught.
    """
    print("\nCase A'': production-tree grep returns zero name references")
    pattern = f"{_DELETED_PLUGIN_NAME}|{_DELETED_PLUGIN_CLASS}"
    completed = subprocess.run(
        ["grep", "-rnE",
         "--exclude-dir=__pycache__", "--exclude-dir=.ruff_cache",
         f"--exclude={Path(__file__).name}",
         "--exclude=shipped_smoke_contract.yaml",
         pattern,
         *(str(p) for p in _PRODUCTION_GREP_ROOTS if p.exists())],
        capture_output=True, text=True, check=False,
    )
    matches = [line for line in completed.stdout.splitlines() if line.strip()]
    _check(
        not matches,
        f"  zero production-tree references (found {len(matches)})",
    )
    if matches:
        for line in matches[:10]:
            print(f"      LEFTOVER: {line}")


def _assert_profile_yaml_clean(path: Path, label_prefix: str) -> None:
    doc = _load_profile_yaml(path)
    _check(
        isinstance(doc, dict),
        f"  {label_prefix} parses as a YAML mapping",
    )
    if not isinstance(doc, dict):
        return

    plugin_list = doc.get("plugins") or []
    _check(
        _DELETED_PLUGIN_NAME not in plugin_list,
        f"  {label_prefix} plugins: list omits {_DELETED_PLUGIN_NAME!r}",
    )
    _check(
        _CANONICAL_PROVIDER in plugin_list,
        f"  {label_prefix} plugins: list still carries {_CANONICAL_PROVIDER!r}",
    )

    bindings = doc.get("service_bindings") or {}
    self_deployment = bindings.get("self_deployment_service")
    _check(
        self_deployment == _CANONICAL_PROVIDER,
        f"  {label_prefix} service_bindings.self_deployment_service "
        f"= {_CANONICAL_PROVIDER!r} (got {self_deployment!r})",
    )


def _case_a_triple_prime_profiles_parse_clean() -> None:
    """A''': both local.yaml artifacts parse + omit deleted + keep canonical binding."""
    print("\nCase A''': both local.yaml artifacts parse cleanly post-cleanup")
    # BOTH artifacts in this case are ORIGIN-ONLY, each for its own measured
    # reason, so each is guarded separately rather than the case being dropped:
    # macos_midwife_plugin is in NO capability bundle (github_midwife_plugin is
    # the birth spine), and `initialization/` is absent from the seed manifest's
    # `copy:` allowlist entirely. A born solet's profile comes from the midwife
    # templates that DO ship, so neither artifact describes an adopter. Every
    # other case here — the production-tree grep, the PluginManager rediscovery —
    # is adopter-relevant and still runs. Measured on the r21 born-clone verdict
    # (sealed 254700866ab0…): 1 of 14 BLOCKING, raising FileNotFoundError on the
    # midwife template right after Case A'' had printed PASS.
    for path, label in ((_MIDWIFE_TEMPLATE, "midwife KB template"),
                        (_OPERATOR_PROFILE, "operator profile")):
        if not path.is_file():
            print(f"  SKIP    {label}: origin-only artifact, not shipped in a seed")
            continue
        _assert_profile_yaml_clean(path, label)


def _case_a_quad_prime_plugin_manager_rediscover() -> None:
    """A'''': library-level PluginManager rediscovery against post-cleanup allowlist."""
    print("\nCase A'''': library-level PluginManager rediscovery")
    _refresh_entry_points_cache()
    plugin_manager = PluginManager()
    allowed = {_CANONICAL_PROVIDER}
    plugin_manager.discover_plugins(allowed_plugins=allowed)

    _check(
        _CANONICAL_PROVIDER in plugin_manager.plugins,
        f"  rediscovery loads {_CANONICAL_PROVIDER!r} into plugin_manager.plugins",
    )
    _check(
        _DELETED_PLUGIN_NAME not in plugin_manager.plugins,
        f"  rediscovery does NOT load {_DELETED_PLUGIN_NAME!r}",
    )


def _summary_and_exit() -> None:
    total = _passed + len(_failed)
    print(f"\n--- Summary: {_passed}/{total} passed ---")
    if _failed:
        print("Failures:")
        for label in _failed:
            print(f"  - {label}")
        sys.exit(1)
    sys.exit(0)


def main() -> None:
    print(
        f"Running Cycle 6 post-delete smoke for {_DELETED_PLUGIN_NAME!r} "
        f"against {REPO_ROOT}"
    )
    _case_a_entry_points_no_legacy()
    _case_a_prime_directory_absent()
    _case_a_double_prime_grep_clean()
    _case_a_triple_prime_profiles_parse_clean()
    _case_a_quad_prime_plugin_manager_rediscover()
    _summary_and_exit()


if __name__ == "__main__":
    main()
