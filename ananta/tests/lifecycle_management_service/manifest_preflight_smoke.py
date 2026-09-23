#!/usr/bin/env python3
"""Standalone smoke for the L1 apply_manifest pre-flight (no pytest).

Demonstrates that the three new checks land in the same place as the four
real-world bug classes from the post-Phase-A fire (per dispatch acceptance
N5). The smoke uses synthetic fixture plugin modules written to a tempdir
so we exercise the real validator code path without depending on entry-
point installation or any specific live plugin's current state.

Bug-class fixtures:

* **#4 ParameterType stale enum** — module body references
  ``ParameterType.NONEXISTENT``. Caught at L1.1 import-time as an
  ``AttributeError`` when the module is loaded.
* **#5 field_sensitivities missing** — ``@platform_process`` EDGE method
  with no ``field_sensitivities`` and no ``table_name``. NO LONGER a bug
  class: the ``edge_process_missing_sensitivity`` FATAL was relaxed
  2026-07-15 (frontier-first consolidation — sensitivities are optional),
  so the fixture now proves L1.2 passes it cleanly (relax red-first).
* **#6/#7 KB JSON missing** — ``@platform_process`` method + matching
  ``EdgeProcessDefinition`` entry but NO companion
  ``knowledge_base/processes/<name>.json`` file. Caught at L1.2 by
  :class:`KnowledgeBaseOverlayLoader.apply` (``JSON file not found``).

Run:
    .venv/bin/python3 ananta/tests/lifecycle_management_service/manifest_preflight_smoke.py
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.lifecycle_management_service.manifest_preflight import (  # noqa: E402
    check_instantiation_and_decorators,
    check_kb_overlay_and_collisions,
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


def _write_fixture_plugin(
    fixtures_dir: Path,
    plugin_name: str,
    plugin_body: str,
    *,
    write_kb_json: bool = True,
    kb_json_body: str | None = None,
    edge_function_name: str = "ping",
) -> Path:
    plugin_root = fixtures_dir / plugin_name
    (plugin_root / "src" / plugin_name).mkdir(parents=True)
    (plugin_root / "src" / plugin_name / "__init__.py").write_text("")
    (plugin_root / "src" / plugin_name / "plugin.py").write_text(plugin_body)
    if write_kb_json:
        kb_dir = plugin_root / "knowledge_base" / "processes"
        kb_dir.mkdir(parents=True)
        if kb_json_body is None:
            kb_json_body = (
                '{"process_key": "plugin::' + plugin_name + '::'
                + edge_function_name + '",\n'
                ' "display_name": "fixture",\n'
                ' "description": "fixture",\n'
                ' "embedding_description": "fixture"}\n'
            )
        (kb_dir / f"{edge_function_name}.json").write_text(kb_json_body)
    return plugin_root


def _import_fixture(
    plugin_name: str, fixtures_dir: Path,
) -> type | None:
    src_path = fixtures_dir / plugin_name / "src"
    sys.path.insert(0, str(src_path))
    try:
        module = importlib.import_module(f"{plugin_name}.plugin")
    finally:
        sys.path.remove(str(src_path))
    plugin_cls_name = "".join(part.capitalize() for part in plugin_name.split("_"))
    return getattr(module, plugin_cls_name, None)


# ─── Fixtures ──────────────────────────────────────────────────────────────


_FIXTURE_BUG4_BODY = textwrap.dedent('''
"""Bug-class #4 fixture: stale enum at decoration time."""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)


class FixtureBug4(PluginBase, EdgeProcessProvider):
    name = "fixture_bug4"

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {}

    @platform_process(
        name="broken",
        parameters={
            "x": ParameterMetadata(
                type=ParameterType.NONEXISTENT,  # <- stale enum
                required=True,
                description="invalid type ref",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="", properties={},
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="broken",
        ),
    )
    def broken(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {}
''')


_FIXTURE_BUG5_BODY = textwrap.dedent('''
"""Bug-class #5 fixture: EDGE @platform_process with no field_sensitivities."""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
    MergeResultProcessorCustomizations as EdgeMergeResult,
    MergeErrorProcessorCustomizations as EdgeMergeError,
)


class FixtureBug5(PluginBase, EdgeProcessProvider):
    name = "fixture_bug5"

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "ping": EdgeProcessDefinition(
                name="ping",
                result_processor_template_customizations=EdgeMergeResult(
                    result_type="fixture_bug5_result",
                    # No field_sensitivities — optional since the 2026-07-15 relax.
                ),
                error_processor_template_customizations=EdgeMergeError(retryable=False),
            ),
        }

    @platform_process(
        name="ping",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="", properties={},
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="fixture_bug5_result",
            # No field_sensitivities here either — optional since 2026-07-15.
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def ping(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {"action_status": "completed", "data": {}, "actions": [], "error": None, "timestamp": ""}
''')


_FIXTURE_BUG6_BODY = textwrap.dedent('''
"""Bug-class #6/#7 fixture: @platform_process with NO companion JSON file."""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
    MergeResultProcessorCustomizations as EdgeMergeResult,
    MergeErrorProcessorCustomizations as EdgeMergeError,
)


class FixtureBug6(PluginBase, EdgeProcessProvider):
    name = "fixture_bug6"

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "ping": EdgeProcessDefinition(
                name="ping",
                result_processor_template_customizations=EdgeMergeResult(
                    result_type="fixture_bug6_result",
                ),
                error_processor_template_customizations=EdgeMergeError(retryable=False),
            ),
        }

    @platform_process(
        name="ping",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="", properties={},
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="fixture_bug6_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def ping(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {"action_status": "completed", "data": {}, "actions": [], "error": None, "timestamp": ""}
''')


_FIXTURE_CLEAN_BODY = _FIXTURE_BUG6_BODY.replace(
    "class FixtureBug6", "class FixtureClean",
).replace("fixture_bug6", "fixture_clean")


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_bug4_stale_enum_caught_at_import() -> None:
    """§2.1 — stale ParameterType.NONEXISTENT raises AttributeError on import."""
    with tempfile.TemporaryDirectory(prefix="preflight_smoke_") as tmpdir:
        fixtures_dir = Path(tmpdir)
        _write_fixture_plugin(fixtures_dir, "fixture_bug4", _FIXTURE_BUG4_BODY)
        src_path = fixtures_dir / "fixture_bug4" / "src"
        sys.path.insert(0, str(src_path))
        captured_class = None
        captured_error: Exception | None = None
        try:
            try:
                module = importlib.import_module("fixture_bug4.plugin")
                captured_class = getattr(module, "FixtureBug4", None)
            except Exception as exc:  # noqa: BLE001
                captured_error = exc
        finally:
            sys.path.remove(str(src_path))
            sys.modules.pop("fixture_bug4.plugin", None)
            sys.modules.pop("fixture_bug4", None)
        _check(
            captured_class is None and captured_error is not None,
            "bug #4 stale-enum module fails to import",
        )
        _check(
            isinstance(captured_error, AttributeError)
            and "NONEXISTENT" in str(captured_error),
            "bug #4 error message names the bad ParameterType attribute",
        )


def test_bug5_field_sensitivities_missing_passes_relaxed_validator() -> None:
    """§2.2 — EDGE process without field_sensitivities passes L1.2 cleanly.

    Relax red-first (2026-07-15 frontier-first consolidation): this exact
    fixture produced an ``edge_process_missing_sensitivity`` L1.2 failure
    before the relax, so a zero-failure run proves the relax is live rather
    than the check being vacuous.
    """
    with tempfile.TemporaryDirectory(prefix="preflight_smoke_") as tmpdir:
        fixtures_dir = Path(tmpdir)
        _write_fixture_plugin(fixtures_dir, "fixture_bug5", _FIXTURE_BUG5_BODY)
        plugin_cls = _import_fixture("fixture_bug5", fixtures_dir)
        try:
            assert plugin_cls is not None
            _instances, failures = check_instantiation_and_decorators(
                {"fixture_bug5": plugin_cls},
            )
            _check(
                len(failures) == 0,
                "relaxed contract: a sensitivity-less EDGE verb passes preflight "
                f"(got {len(failures)} failures: "
                f"{[f.message for f in failures]})",
            )
        finally:
            sys.modules.pop("fixture_bug5.plugin", None)
            sys.modules.pop("fixture_bug5", None)


def test_bug6_missing_kb_json_caught_by_kb_overlay() -> None:
    """§2.2 (kb_overlay path) — missing companion JSON file is rejected."""
    with tempfile.TemporaryDirectory(prefix="preflight_smoke_") as tmpdir:
        fixtures_dir = Path(tmpdir)
        _write_fixture_plugin(
            fixtures_dir, "fixture_bug6", _FIXTURE_BUG6_BODY,
            write_kb_json=False,  # <- the bug: NO JSON file lands.
        )
        plugin_cls = _import_fixture("fixture_bug6", fixtures_dir)
        try:
            assert plugin_cls is not None
            instances, instantiation_failures = check_instantiation_and_decorators(
                {"fixture_bug6": plugin_cls},
            )
            _check(
                not instantiation_failures,
                "bug #6 fixture passes §2.2 registration_validator (well-formed decorator)",
            )
            _check(
                "fixture_bug6" in instances,
                "bug #6 fixture instance created",
            )
            kb_failures = check_kb_overlay_and_collisions(instances)
            _check(
                bool(kb_failures),
                "bug #6 missing JSON file is caught at L1.2 kb_overlay",
            )
            joined = "\n".join(f.message for f in kb_failures)
            _check(
                "knowledge base process definitions" in joined.lower()
                or "JSON file not found" in joined,
                "bug #6 kb_overlay failure message names the missing JSON",
            )
        finally:
            sys.modules.pop("fixture_bug6.plugin", None)
            sys.modules.pop("fixture_bug6", None)


def test_clean_plugin_passes_all_three_checks() -> None:
    """Clean fixture with valid decorators + matching JSON passes §2.2 + §2.3.

    Establishes a baseline that the gate isn't producing false positives.
    """
    with tempfile.TemporaryDirectory(prefix="preflight_smoke_") as tmpdir:
        fixtures_dir = Path(tmpdir)
        _write_fixture_plugin(fixtures_dir, "fixture_clean", _FIXTURE_CLEAN_BODY)
        plugin_cls = _import_fixture("fixture_clean", fixtures_dir)
        try:
            assert plugin_cls is not None
            instances, failures = check_instantiation_and_decorators(
                {"fixture_clean": plugin_cls},
            )
            _check(
                not failures,
                f"clean fixture passes §2.2 (got {len(failures)} failures)",
            )
            kb_failures = check_kb_overlay_and_collisions(instances)
            # kb_overlay also walks every loaded service-interface module's
            # process; the fixture's own process key has a JSON file. The
            # smoke can't easily isolate the platform's existing keys from
            # the fixture's, so we assert ONLY on the fixture's row.
            joined = "\n".join(f.message for f in kb_failures)
            _check(
                "fixture_clean" not in joined,
                "clean fixture is not flagged by §2.2 kb_overlay",
            )
        finally:
            sys.modules.pop("fixture_clean.plugin", None)
            sys.modules.pop("fixture_clean", None)


_FIXTURE_CACHE_POISON_BUGGY_BODY = textwrap.dedent('''
"""Cache-poisoning fixture v1: decorator present, entry MISSING from defs.

Simulates the pre-fix on-disk state that, when imported into the live
platform, caches a class whose `@platform_process` decorator-set declares
``ping`` as an EDGE process but whose ``get_edge_process_definitions()``
returns an empty dict.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
    MergeResultProcessorCustomizations as EdgeMergeResult,
    MergeErrorProcessorCustomizations as EdgeMergeError,
)


class FixtureCachePoison(PluginBase, EdgeProcessProvider):
    name = "fixture_cache_poison"

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {}

    @platform_process(
        name="ping",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="", properties={},
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="fixture_cache_poison_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def ping(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        del params, state
        return {"ok": True}
''')


_FIXTURE_CACHE_POISON_FIXED_BODY = textwrap.dedent('''
"""Cache-poisoning fixture v2: decorator AND entry present (fixed on disk).

Same import shape as v1; the only difference is that
``get_edge_process_definitions`` now declares ``ping``. The AST-fallback
in ``_validate_edge_methods_have_definitions`` should detect this disk
state and decline to raise on a class object loaded from v1.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
    MergeResultProcessorCustomizations as EdgeMergeResult,
    MergeErrorProcessorCustomizations as EdgeMergeError,
)


class FixtureCachePoison(PluginBase, EdgeProcessProvider):
    name = "fixture_cache_poison"

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "ping": EdgeProcessDefinition(
                name="ping",
                result_processor_template_customizations=EdgeMergeResult(
                    result_type="fixture_cache_poison_result",
                ),
                error_processor_template_customizations=EdgeMergeError(retryable=False),
            ),
        }

    @platform_process(
        name="ping",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="", properties={},
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="fixture_cache_poison_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def ping(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        del params, state
        return {"ok": True}
''')


def test_cache_poison_strict_raise_when_disk_also_lacks() -> None:
    """v1 disk source + v1 cached class → real bug, validator raises."""
    with tempfile.TemporaryDirectory(prefix="preflight_smoke_") as tmpdir:
        fixtures_dir = Path(tmpdir)
        _write_fixture_plugin(
            fixtures_dir, "fixture_cache_poison",
            _FIXTURE_CACHE_POISON_BUGGY_BODY,
        )
        plugin_cls = _import_fixture("fixture_cache_poison", fixtures_dir)
        try:
            assert plugin_cls is not None
            _, failures = check_instantiation_and_decorators(
                {"fixture_cache_poison": plugin_cls},
            )
            _check(
                len(failures) == 1,
                f"cache-poison v1 disk produces 1 preflight failure (got {len(failures)})",
            )
            if failures:
                _check(
                    failures[0].check == "L1.2_registration_validator",
                    "cache-poison v1 failure attributed to registration_validator",
                )
                _check(
                    "not declared in get_edge_process_definitions" in failures[0].message,
                    "cache-poison v1 failure message names the contract",
                )
        finally:
            sys.modules.pop("fixture_cache_poison.plugin", None)
            sys.modules.pop("fixture_cache_poison", None)


def test_cache_poison_ast_fallback_when_disk_declares_entry() -> None:
    """v1 cached class + v2 disk source → AST fallback, validator does NOT raise."""
    with tempfile.TemporaryDirectory(prefix="preflight_smoke_") as tmpdir:
        fixtures_dir = Path(tmpdir)
        plugin_root = _write_fixture_plugin(
            fixtures_dir, "fixture_cache_poison",
            _FIXTURE_CACHE_POISON_BUGGY_BODY,
        )
        plugin_cls = _import_fixture("fixture_cache_poison", fixtures_dir)
        try:
            assert plugin_cls is not None
            # Overwrite on-disk source with the fixed version. The class
            # object cached above retains the v1 method tables; on-disk
            # source now declares the missing entry.
            (plugin_root / "src" / "fixture_cache_poison" / "plugin.py").write_text(
                _FIXTURE_CACHE_POISON_FIXED_BODY,
            )
            _, failures = check_instantiation_and_decorators(
                {"fixture_cache_poison": plugin_cls},
            )
            _check(
                not failures,
                f"cache-poison v2 disk → AST fallback skips raise (got {len(failures)} failures)",
            )
        finally:
            sys.modules.pop("fixture_cache_poison.plugin", None)
            sys.modules.pop("fixture_cache_poison", None)


def main() -> int:
    print("=== manifest_preflight_smoke (L1 retroactive-catch demo) ===")
    test_bug4_stale_enum_caught_at_import()
    test_bug5_field_sensitivities_missing_passes_relaxed_validator()
    test_bug6_missing_kb_json_caught_by_kb_overlay()
    test_clean_plugin_passes_all_three_checks()
    test_cache_poison_strict_raise_when_disk_also_lacks()
    test_cache_poison_ast_fallback_when_disk_declares_entry()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
