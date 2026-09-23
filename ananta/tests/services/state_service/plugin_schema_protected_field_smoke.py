#!/usr/bin/env python3
"""Fleet-wide boot replay: no plugin schema redeclares a platform-managed column.

iss_2bc76075 (2026-09-20): ``seed_factory_plugin`` landed a table that declared
its own ``created_at``. ``SchemaStandardizer._validate_no_protected_field_overrides``
refuses that at ``schema_manager.initialize_schemas`` — which runs at candidate
boot, so EVERY blue-green candidate crashed within seconds of spawn and every
swap since 09-14 rolled back silently. Every static gate was green: the defect
is only visible by running the standardizer, and nothing ran it before a boot.
A second table (``release_runs``, ``created_at`` + ``updated_at``) was hiding
behind the first, because ``initialize_schemas`` stops at its first failure.

This smoke closes that class: it enumerates EVERY plugin that provides schemas
(``plugins/*/src/*/plugin.py`` defining ``get_schema_definitions``), calls it
without booting the platform, and replays the standardizer over every table
ONE AT A TIME so every offender is named, not just the first. Offline: no
solet, no database — the standardizer is pure.

Enumeration completeness is itself checked: a plugin whose module cannot be
imported or whose ``get_schema_definitions`` cannot be called is a FAILURE,
never a skip (a silently-empty enumeration is exactly how this class stays
invisible). The RED control feeds the checker the defect's own shape and
requires it to be refused.

Run:
    .venv/bin/python3 ananta/tests/services/state_service/plugin_schema_protected_field_smoke.py
"""

from __future__ import annotations

# ruff: noqa: E402
import importlib
import inspect
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.types.column_types import ColumnType
from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema

_PLUGIN_GLOB = "*/src/*/plugin.py"
_PROVIDER_MARKER = "def get_schema_definitions"

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


def _boot_replay_failures(schema: SchemaDefinition, standardizer: SchemaStandardizer) -> list[str]:
    """Replay the exact ``initialize_schemas`` step per table; return every refusal."""
    refusals: list[str] = []
    for table_name, table in schema.tables.items():
        try:
            standardized = standardizer.standardize_schema(SchemaDefinition(namespace=schema.namespace, tables={table_name: table}))
        except ValueError as exc:
            refusals.append(f"{schema.namespace}.{table_name}: {exc}")
            continue
        errors = standardized.validate()
        if errors:
            refusals.append(f"{schema.namespace}.{table_name}: validate() -> {errors}")
    return refusals


def _provider_classes(module: object) -> list[type]:
    module_name = getattr(module, "__name__", "")
    return [cls for _, cls in inspect.getmembers(module, inspect.isclass) if cls.__module__ == module_name and "get_schema_definitions" in vars(cls)]


def _schemas_of(cls: type) -> list[SchemaDefinition]:
    """Call ``get_schema_definitions`` on a bare instance — no ``__init__``, no
    platform. ``name`` mirrors ``PluginBase.__init__`` (the class name), the one
    attribute a provider reads for its namespace."""
    instance = object.__new__(cls)
    instance.name = cls.__name__  # type: ignore[attr-defined]  # PluginBase.__init__ sets exactly this
    schemas = cls.get_schema_definitions(instance)
    if not isinstance(schemas, list) or not all(isinstance(s, SchemaDefinition) for s in schemas):
        raise TypeError(f"{cls.__name__}.get_schema_definitions returned {type(schemas).__name__}, not list[SchemaDefinition]")
    return schemas


def _replay_one_plugin(plugin_py: Path, standardizer: SchemaStandardizer, providers_seen: set[str]) -> int:
    """Import one candidate plugin module and replay its schema(s) one table at a
    time. Returns the number of tables replayed (0 on an import/call failure,
    already reported via ``_check``)."""
    package = plugin_py.parent.name
    src_dir = str(plugin_py.parents[1])
    sys.path.insert(0, src_dir)
    try:
        module = importlib.import_module(f"{package}.plugin")
    except Exception:
        _check(False, f"{package}: plugin module imports (the platform imports it at boot)\n{traceback.format_exc()}")
        sys.path.remove(src_dir)
        return 0
    classes = _provider_classes(module)
    _check(len(classes) > 0, f"{package}: enumeration resolved its schema-provider class(es)")
    tables_seen = 0
    for cls in classes:
        try:
            schemas = _schemas_of(cls)
        except Exception:
            _check(False, f"{package}.{cls.__name__}: get_schema_definitions() callable without a booted platform\n{traceback.format_exc()}")
            continue
        providers_seen.add(f"{package}.{cls.__name__}")
        for schema in schemas:
            tables_seen += len(schema.tables)
            refusals = _boot_replay_failures(schema, standardizer)
            _check(not refusals, f"{package}.{cls.__name__} namespace {schema.namespace!r}: {len(schema.tables)} table(s) pass the initialize_schemas standardizer replay" + ("".join(f"\n        {r}" for r in refusals)))
    sys.path.remove(src_dir)
    return tables_seen


def check_every_plugin_schema_boots() -> None:
    standardizer = SchemaStandardizer()
    candidates = sorted(p for p in (REPO_ROOT / "plugins").glob(_PLUGIN_GLOB) if _PROVIDER_MARKER in p.read_text(encoding="utf-8"))
    _check(len(candidates) > 0, f"enumeration found schema-providing plugins under plugins/ ({len(candidates)} plugin.py files)")
    tables_seen = 0
    providers_seen: set[str] = set()
    for plugin_py in candidates:
        tables_seen += _replay_one_plugin(plugin_py, standardizer, providers_seen)
    # seed_factory_plugin is structurally absent from every assembled seed
    # bundle (assemble_seed's NO-FACTORY invariant fail-loud-excludes it), and
    # this smoke ships into every bundle (ananta/tests/ is a seed_manifest.yaml
    # copy entry) -- so the iss_2bc76075 regression guard below only applies
    # when seed_factory_plugin/ was actually enumerated, same as candidates is
    # already derived from what's actually present rather than a fixed roster.
    seed_factory_present = any(plugin_py.parent.name == "seed_factory_plugin" for plugin_py in candidates)
    if seed_factory_present:
        _check("seed_factory_plugin.SeedFactoryPlugin" in providers_seen, "seed_factory_plugin (iss_2bc76075's origin) was among the providers replayed")
    else:
        print("  SKIP  seed_factory_plugin (iss_2bc76075's origin) was among the providers replayed -- absent by the NO-FACTORY invariant (not the dev checkout)")
    _check(tables_seen >= len(candidates), f"replayed {tables_seen} tables across {len(providers_seen)} providers")


def check_red_control() -> None:
    """The defect's own shape must be refused by the same checker, or the
    green above proves nothing."""
    standardizer = SchemaStandardizer()
    for column in sorted(SchemaStandardizer.PROTECTED_STANDARD_FIELDS):
        bad = SchemaDefinition(namespace="control", tables={"bad": TableSchema(table_name="bad", columns={"k": ColumnDefinition(type=ColumnType.TEXT, not_null=True), column: ColumnDefinition(type=ColumnType.TEXT)}, id_prefix="bad")})
        refusals = _boot_replay_failures(bad, standardizer)
        _check(len(refusals) == 1 and column in refusals[0], f"CONTROL: a table redeclaring {column!r} is refused by the replay")
    good = SchemaDefinition(namespace="control", tables={"good": TableSchema(table_name="good", columns={"k": ColumnDefinition(type=ColumnType.TEXT, not_null=True)}, id_prefix="good")})
    _check(_boot_replay_failures(good, standardizer) == [], "CONTROL: a table declaring only its own columns passes the replay")


def main() -> int:
    print("plugin_schema_protected_field_smoke")
    check_red_control()
    check_every_plugin_schema_boots()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
