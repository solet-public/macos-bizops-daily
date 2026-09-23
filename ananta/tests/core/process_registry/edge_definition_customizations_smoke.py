#!/usr/bin/env python3
"""Plugin EdgeProcessDefinition contract smoke (no pytest).

Covers the PLUGIN half of the EDGE contract: every ``EdgeProcessProvider``
plugin's ``get_edge_process_definitions()`` against its decorated EDGE
``@platform_process`` methods. (The former service-interface sibling,
``edge_customizations_smoke.py``, statically mirrored the both-blocks boot
FATAL and retired with it in the 2026-07-15 relax.)

WHY THIS EXISTS. The boot-only validator
``plugin_registration_validator.validate_edge_process_provider`` enforces,
at ``process_registry`` build time, that every plugin EDGE process is
declared in ``get_edge_process_definitions()`` AND decorated with
``@platform_process`` (decorated<->declared parity, error codes
``process_registry.edge_process_mismatch`` /
``process_registry.edge_process_not_declared``).

Customizations and field_sensitivities are OPTIONAL since the 2026-07-15
frontier-first relax (the former ``edge_process_missing_customizations`` and
``edge_process_missing_sensitivity`` FATALs were dropped — see
``workbench/2026-07-15_frontier_first_result_processing_consolidation.md``);
this smoke proves the relax red-first: a no-customizations definition, which
FATALed before, must now validate cleanly.

That check runs only at boot. The INF-01 ``set_autonomic_slot`` fatality
(2026-07-03) was exactly this class: a new plugin EDGE verb whose
decorated<->declared parity was wrong passed every gate green and died at
green-boot. This smoke runs the SAME real validator at gate time, over
every installed plugin, so the class is caught before a commit lands
rather than at cutover.

WHY GATE-TIME AND NOT PREFLIGHT (the honest-record note). The blue/green
``manifest_preflight`` already runs this validator pre-cutover
(``check_instantiation_and_decorators`` ->
``validate_edge_process_provider``), but preflight runs IN-PROCESS in the
live "blue" platform, so an ``importlib`` re-import returns blue's STALE
cached module — a net-new verb added by the very commit being deployed is
invisible to it. See the "Cache-poisoning limit" section of
``ananta/src/ananta/services/lifecycle_management_service/manifest_preflight.py``.
``run_smokes.py`` runs a FRESH python process with no stale cache, so a
gate-time sweep is the durable guard for the net-new-verb class: the
service half is ``edge_customizations_smoke.py``, the plugin half is here.
(The deploy-time, post-cache-poison fix — a subprocess/L2 fresh-source
preflight probe — is tracked separately as backlog GTE-06.)

Coverage: reuses the real ``PluginRegistrationValidator`` (no logic
duplication, no mock). Entry points that are registered but not installed
in this environment (e.g. a renamed or cloud-only plugin) are surfaced and
skipped, never silently dropped — and the skip is only tolerated when the
plugin directory is genuinely absent (``_orphaned_skips``), so an unloadable
entry point whose source IS present still fails.

The provider population is DERIVED from the installed classes
(``_declared_provider_names``), never hardcoded: this smoke ships in seed
bundles, which carry a pruned plugin subset, and a fixed count is wrong in
every tree but the one it was written against.

Red-first: a synthetic negative fixture proves the validator still fires on
a decorated<->declared parity mismatch, and a no-customizations fixture
proves the 2026-07-15 relax is real (it FATALed before); plus a parity pin
on the real ``set_autonomic_slot`` (the INF-01 green-boot fatality verb —
that fatality was a parity failure).

Run from repo root:
    .venv/bin/python3 ananta/tests/core/process_registry/edge_definition_customizations_smoke.py
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_metadata import (  # noqa: E402
    ActionMetadata,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
)
from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.core.plugins.plugin_base import PluginBase  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import (  # noqa: E402
    PluginRegistrationValidator,
)
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.interfaces.edge_process_provider import (  # noqa: E402
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

_ENTRY_POINT_GROUP = "ananta.plugins"

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


def _error_code(exc: FrameworkError) -> str:
    return str(getattr(exc, "error_code", ""))


def _fixture_action(name: str) -> ActionMetadata:
    """A real (unmocked) ActionMetadata for one decorated EDGE verb."""
    return ActionMetadata(
        name=name,
        display_name=name,
        description=f"Negative-fixture EDGE verb {name} for the red-first pin.",
        plugin="_edge_definition_fixture",
        function=name,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
    )


class _NoCustomizationsProvider:
    """EDGE verb declared+decorated with NO customization blocks at all.

    Before the 2026-07-15 relax this construction FATALed with
    ``edge_process_missing_customizations``; under the relaxed contract it
    must validate cleanly.
    """

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "bare_verb": EdgeProcessDefinition(
                name="bare_verb",
                result_processor_template_customizations=None,
                error_processor_template_customizations=None,
            ),
        }


class _ParityMismatchProvider:
    """Declares an EDGE definition with no matching decorated method."""

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "ghost_verb": EdgeProcessDefinition(
                name="ghost_verb",
                result_processor_template_customizations=(
                    MergeResultProcessorCustomizations(
                    )
                ),
                error_processor_template_customizations=(
                    MergeErrorProcessorCustomizations(retryable=False)
                ),
            ),
        }


def _installed_plugin_classes() -> tuple[dict[str, type[PluginBase]], list[tuple[str, str]]]:
    """Load every installed ``ananta.plugins`` entry-point plugin class.

    Returns (loaded classes by name, skipped (name, detail) pairs). A
    registered-but-uninstalled entry point (renamed/cloud-only plugin) is
    surfaced in the skip list, never silently dropped.
    """
    classes: dict[str, type[PluginBase]] = {}
    skipped: list[tuple[str, str]] = []
    for ep in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP):
        try:
            loaded = ep.load()
        except Exception as exc:  # noqa: BLE001 — uninstalled entry point
            skipped.append((ep.name, f"{type(exc).__name__}: {exc}"))
            continue
        if isinstance(loaded, type) and issubclass(loaded, PluginBase):
            classes[ep.name] = loaded
    return classes, skipped


def _sweep_plugin_providers(
    classes: dict[str, type[PluginBase]],
) -> tuple[int, list[str], list[str]]:
    """(providers checked, violation labels, instantiation-gap labels)."""
    validator = PluginRegistrationValidator()

    checked = 0
    violations: list[str] = []
    gaps: list[str] = []

    for plugin_name, plugin_class in sorted(classes.items()):
        try:
            instance = plugin_class()
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"{plugin_name}: __init__ raised {type(exc).__name__}: {exc}")
            continue
        if not isinstance(instance, EdgeProcessProvider):
            continue
        try:
            actions = instance.get_available_actions()
        except Exception as exc:  # noqa: BLE001
            gaps.append(
                f"{plugin_name}: get_available_actions raised "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        checked += 1
        try:
            validator.validate_edge_process_provider(plugin_name, instance, actions)
        except FrameworkError as exc:
            violations.append(f"{plugin_name}: {_error_code(exc)}: {exc.message}")
    return checked, violations, gaps


def _declared_provider_names(classes: dict[str, type[PluginBase]]) -> list[str]:
    """Installed plugin classes that DECLARE the provider method.

    The sweep's own membership test is ``isinstance(instance, EdgeProcessProvider)``,
    and ``EdgeProcessProvider`` is a ``@runtime_checkable`` Protocol — i.e. presence of
    ``get_edge_process_definitions``. Deriving the expected population from the same
    attribute, over the INSTALLED classes, gives a floor that scales with whatever tree
    it runs in (the full checkout or a pruned seed bundle) instead of a hardcoded count.
    """
    return sorted(
        name for name, cls in classes.items() if hasattr(cls, "get_edge_process_definitions")
    )


def _orphaned_skips(skipped: list[tuple[str, str]]) -> list[str]:
    """Unloadable entry points whose plugin directory IS present on disk.

    A registered entry point that fails to import is EXPECTED when the plugin is not
    part of this tree at all — a rename left the metadata behind (origin), or the seed
    manifest pruned the plugin (bundle). It is a genuine PACKAGING break when the source
    is sitting right there and the module still will not load, so that case — and only
    that case — is a failure.
    """
    plugins_dir = _REPO_ROOT / "plugins"
    return [entry[0] for entry in skipped if (plugins_dir / entry[0]).is_dir()]


def _run_real_plugin_sweep(
    classes: dict[str, type[PluginBase]], skipped: list[tuple[str, str]],
) -> None:
    checked, violations, gaps = _sweep_plugin_providers(classes)
    declared = _declared_provider_names(classes)
    orphans = _orphaned_skips(skipped)
    print(
        f"\n  swept {checked} EdgeProcessProvider plugins "
        f"({len(classes)} installed, {len(declared)} declaring the provider method, "
        f"{len(skipped)} entry points not installed)"
    )
    for name, detail in skipped:
        print(f"  SKIP (not installed)  {name}: {detail}")
    for gap in gaps:
        print(f"  GAP  {gap}")
    for violation in violations:
        print(f"  VIOLATION  {violation}")
    for orphan in orphans:
        print(f"  ORPHAN  {orphan}: entry point will not load but plugins/{orphan}/ exists")

    _check(bool(classes), "the ananta.plugins entry-point group resolved installed plugins")
    _check(
        checked == len(declared),
        f"every installed EdgeProcessProvider was swept ({checked} of {len(declared)} declared)",
    )
    _check(checked > 0, "the sweep was not vacuous (at least one provider scanned)")
    _check(
        not orphans,
        "every unloadable entry point is a stale registration, not a packaging break "
        f"(present-on-disk but unloadable: {orphans})",
    )
    _check(not gaps, "every installed provider instantiated and scanned")
    _check(
        not violations,
        "every plugin EDGE process passes the EdgeProcessProvider contract "
        "(decorated<->declared parity; customizations optional since 2026-07-15)",
    )


def _run_negative_fixtures() -> None:
    validator = PluginRegistrationValidator()

    # The fixtures duck-type the EdgeProcessProvider protocol the validator
    # exercises; cast past the PluginBase annotation deliberately.
    raised_bare = None
    try:
        validator.validate_edge_process_provider(
            "_edge_definition_fixture",
            cast(PluginBase, _NoCustomizationsProvider()),
            [_fixture_action("bare_verb")],
        )
    except FrameworkError as exc:
        raised_bare = _error_code(exc)
    _check(
        raised_bare is None,
        "relax red-first: a no-customizations EDGE definition validates cleanly "
        f"(pre-2026-07-15 this FATALed; got {raised_bare!r})",
    )

    raised_parity = None
    try:
        validator.validate_edge_process_provider(
            "_edge_definition_fixture",
            cast(PluginBase, _ParityMismatchProvider()),
            [],
        )
    except FrameworkError as exc:
        raised_parity = _error_code(exc)
    _check(
        raised_parity == "process_registry.edge_process_mismatch",
        "red-first: a declared-but-undecorated EDGE definition is rejected "
        f"(got {raised_parity!r})",
    )


def _run_inf01_fatality_pin(classes: dict[str, type[PluginBase]]) -> None:
    """Positive pin on the INF-01 set_autonomic_slot green-boot fatality verb.

    The 2026-07-03 fatality was a decorated<->declared PARITY failure, so the
    pin asserts the verb is declared in get_edge_process_definitions() —
    customization blocks are optional since the 2026-07-15 relax.
    """
    agent_messaging_cls = classes.get("agent_messaging_plugin")
    _check(
        agent_messaging_cls is not None,
        "agent_messaging_plugin is installed (host of the INF-01 pin)",
    )
    if agent_messaging_cls is None:
        return
    definitions = agent_messaging_cls().get_edge_process_definitions()
    _check(
        "set_autonomic_slot" in definitions,
        "set_autonomic_slot is declared in get_edge_process_definitions() "
        "(the 2026-07-03 INF-01 green-boot parity-fatality pin)",
    )


def main() -> int:
    print("Plugin EdgeProcessDefinition contract smoke")
    classes, skipped = _installed_plugin_classes()
    _run_real_plugin_sweep(classes, skipped)
    _run_negative_fixtures()
    _run_inf01_fatality_pin(classes)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
