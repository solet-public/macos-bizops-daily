"""§3 schema-snapshot collector (B1) — snapshot an arbitrary code tree's schema.

The §3 preflight gate (``schema_preflight.py``) classifies a candidate-vs-current
schema diff as additive (rollback-safe) or not. It needs a **canonical schema
snapshot** of a code tree's DECLARED schemas. This module is that collector: it
discovers every declared ``SchemaDefinition`` (core + manifest-gated plugins),
reduces them to the canonical ``namespace → table → column → attrs`` snapshot,
and prints it as JSON to stdout.

It is launched as a **subprocess by FILE PATH** (NOT ``-m``) by
``schema_snapshot_producer``, with ``PYTHONPATH`` pointed at a target code tree
``T`` (the frozen candidate clone, or an old release's ``code/`` for the B1·1
baseline derive). Run-by-path means the collector LOGIC always comes from the
source/new tree, while ``import ananta`` and every plugin module resolve to ``T``
via ``PYTHONPATH``. That is why this module imports ONLY ``ananta.*`` + stdlib:
an OLD ``T`` (e.g. a pre-producer release) has no ``schema_snapshot_collector``
of its own, and may lack any plugin-local helper — so the collector cannot
depend on ``macos_self_deployment_plugin.*`` or it would ImportError against the
old tree. The canonical reduce is therefore INLINED here (drift vs
``schema_preflight.schemas_to_snapshot`` is caught by a current-tree smoke).

Two fail-closed asserts protect the cross-version derive (a snapshot of the
WRONG tree silently defeats the gate):

- **provenance** — every introspected module's ``__file__`` (``ananta`` + each
  discovered plugin INSTANCE; NOT this collector, which is from source) MUST
  resolve under ``EXPECT_ROOT`` (= ``T``). Catches a ``.pth`` that loaded a
  plugin from live source instead of ``T``.
- **completeness** — the discovered plugin set MUST be a superset of the profile
  manifest set. Catches silent under-collection (an old ``T`` missing a manifest
  plugin, which would otherwise shrink the snapshot). Which manifest that is
  matters: see ``MANIFEST_PLUGIN_OVERRIDE`` below and :func:`_resolve_manifest`.

Either assert raises → non-zero exit → the producer raises ``ReleaseManagerError``
→ the deploy is refused. Requires ``SOLET_NAME`` + ``APP_HOME`` (profile
manifest gating) + ``EXPECT_ROOT`` (provenance root); the producer sets all three.

``MANIFEST_PLUGIN_OVERRIDE`` (optional, JSON array of plugin names) names the
manifest that governed ``T`` itself, and exists because ``APP_HOME``'s live
manifest describes the INCOMING deploy, not the old tree the derive path points
at (BLG-02). Set by the producer for the derive call site only; the candidate
call site leaves it unset and reads the live manifest, which is correct there.

Namespace merge: multiple ``SchemaDefinition`` objects can share one namespace
(e.g. several core schemas under ``core``), each contributing distinct tables.
A naive ``{s.namespace: s}`` would DROP all but the last per namespace, silently
shrinking the snapshot. So tables are merged per namespace; a genuine duplicate
``(namespace, table)`` across two definitions is a build-time conflict and fails
loud.

DB-free: discovery instantiates plugins to read ``get_schema_definitions`` but
never opens a connection or runs ``prepare_for_readiness`` — measured ~2s for the
full plugin set.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ananta.config.core_schemas import CoreSchemaDefinitions
from ananta.core.plugins.capabilities import collect_schemas
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
from ananta.types.schema_types import SchemaDefinition

import ananta

# Canonical snapshot column-attribute keys. DUPLICATED from
# ``schema_preflight`` ON PURPOSE: this module must not import the plugin
# package (it runs against an OLD tree that may lack the symbol). A current-tree
# drift-guard smoke asserts these literals + the reduce below stay byte-identical
# to ``schema_preflight.schemas_to_snapshot``.
_ATTR_TYPE = "type"
_ATTR_TYPE_PARAMS = "type_params"
_ATTR_NOT_NULL = "not_null"
_ATTR_HAS_DEFAULT = "has_default"
_ATTR_PRIMARY_KEY = "primary_key"
_ATTR_UNIQUE = "unique"


def _reduce_to_snapshot(
    schemas: dict[str, SchemaDefinition],
) -> dict[str, dict[str, dict[str, dict[str, object]]]]:
    """Reduce typed ``SchemaDefinition``s to the canonical comparable snapshot.

    INLINE twin of ``schema_preflight.schemas_to_snapshot`` (see the keys note
    above). Only rollback-relevant attributes are retained, so the snapshot is
    small, JSON-able, and stable across cosmetic edits.
    """
    snapshot: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
    for namespace, schema in schemas.items():
        tables: dict[str, dict[str, dict[str, object]]] = {}
        for table_key, table in schema.tables.items():
            columns: dict[str, dict[str, object]] = {}
            for column_name, column in table.columns.items():
                columns[column_name] = {
                    _ATTR_TYPE: str(column.type),
                    _ATTR_TYPE_PARAMS: dict(column.type_params or {}),
                    _ATTR_NOT_NULL: bool(column.not_null),
                    _ATTR_HAS_DEFAULT: column.default is not None,
                    _ATTR_PRIMARY_KEY: bool(column.primary_key),
                    _ATTR_UNIQUE: bool(column.unique),
                }
            tables[table_key] = columns
        snapshot[namespace] = tables
    return snapshot


def _assert_under_root(module_file: str | None, expect_root: Path, label: str) -> str | None:
    """Return an offender string if ``module_file`` is missing or outside ``expect_root``."""
    if module_file is None:
        return f"{label}: no __file__ (cannot verify provenance)"
    if not Path(module_file).resolve().is_relative_to(expect_root):
        return f"{label}: {module_file} not under {expect_root}"
    return None


def _assert_provenance(manager: PluginManager, expect_root: Path) -> None:
    """Fail-closed unless ``ananta`` + every discovered plugin instance loads from ``T``.

    The collector's OWN module is deliberately NOT checked: under run-by-path it
    comes from source, not ``T`` (the derive case), which is correct and expected.
    """
    offenders: list[str] = []
    ananta_offender = _assert_under_root(getattr(ananta, "__file__", None), expect_root, "ananta")
    if ananta_offender is not None:
        offenders.append(ananta_offender)
    for name, plugin in manager.plugins.items():
        module = sys.modules.get(type(plugin).__module__)
        offender = _assert_under_root(getattr(module, "__file__", None), expect_root, name)
        if offender is not None:
            offenders.append(offender)
    if offenders:
        msg = (
            f"§3 collector provenance violation (EXPECT_ROOT={expect_root}): "
            + "; ".join(offenders)
        )
        raise ValueError(msg)


def _assert_completeness(manager: PluginManager, manifest: set[str] | None) -> None:
    """Fail-closed unless the discovered plugin set ⊇ the profile manifest set.

    ``manifest is None`` means "load everything installed" (no gating) — nothing
    to satisfy. Otherwise a manifest plugin absent from the target tree is a
    silent under-collection and the snapshot cannot be trusted.
    """
    if manifest is None:
        return
    missing = set(manifest) - set(manager.plugins)
    if missing:
        msg = (
            "§3 collector completeness violation: manifest plugins not discovered "
            f"in target tree: {sorted(missing)}"
        )
        raise ValueError(msg)


def _resolve_manifest(app_home: str) -> set[str] | None:
    """Return the plugin set that governs THIS target tree.

    ``MANIFEST_PLUGIN_OVERRIDE`` (JSON array of plugin names) is the derive
    path's answer to BLG-02: when snapshotting an OLD release's ``code/``, the
    LIVE manifest at ``app_home`` has already been overwritten with the
    INCOMING deploy's manifest, so reading it would gate an old tree by a new
    profile. A deploy that GROWS the manifest then trips
    :func:`_assert_completeness` on a plugin the old tree never had — a
    false-REFUSE of a perfectly safe deploy. The producer passes the OLD
    release's own captured set instead.

    Absent override → read the live manifest, which is CORRECT for the
    candidate call site (``apply_manifest`` writes the new manifest before
    delegating a restart, so live IS the candidate's own manifest) and is also
    the compat rung for an old release built before per-release capture landed:
    those carry no captured set, and falling back reproduces today's exact
    behaviour rather than inventing a more permissive one.

    An override that is present but not a JSON array of strings is a
    programming error in the producer, never adopter input, so it raises
    rather than falling back — a silent fallback here would gate an old tree
    by the wrong manifest, which is the very defect this exists to fix.
    """
    override_raw = os.environ.get("MANIFEST_PLUGIN_OVERRIDE")
    if override_raw is None:
        return load_manifest_plugin_set(app_home)
    try:
        decoded = json.loads(override_raw)
    except json.JSONDecodeError as exc:
        msg = f"MANIFEST_PLUGIN_OVERRIDE is not valid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(decoded, list) or not all(isinstance(name, str) for name in decoded):
        msg = (
            "MANIFEST_PLUGIN_OVERRIDE must be a JSON array of plugin names, got: "
            f"{type(decoded).__name__}"
        )
        raise ValueError(msg)
    if not decoded:
        # Independently guarded here, not just upstream, because this process
        # reads the variable across a subprocess boundary and is the last thing
        # standing between it and the gate. An EMPTY allow-set discovers zero
        # plugins (the unresolved-plugin check is a non-fatal warning), passes
        # the completeness assert vacuously, and produces an empty snapshot —
        # which ``classify_snapshot_diff`` reports as unconditionally additive
        # because it iterates the OLD side. That is the gate failing OPEN.
        msg = (
            "MANIFEST_PLUGIN_OVERRIDE is an EMPTY array — refusing, because an empty "
            "plugin set collapses the snapshot to nothing and would pass the additive "
            "check vacuously. Omit the variable to read the live manifest instead."
        )
        raise ValueError(msg)
    return set(decoded)


def collect_declared_schemas() -> list[SchemaDefinition]:
    """Discover + provenance/completeness-verify the target tree, return its schemas.

    DB-free: discovery instantiates plugins to call ``get_schema_definitions``
    but never opens a pool or runs readiness.
    """
    app_home = os.environ["APP_HOME"]
    expect_root_env = os.environ.get("EXPECT_ROOT")
    if not expect_root_env:
        msg = "EXPECT_ROOT not set — refusing to snapshot without a provenance root"
        raise ValueError(msg)
    expect_root = Path(expect_root_env).resolve()
    manifest = _resolve_manifest(app_home)
    manager = PluginManager()
    manager.discover_plugins(allowed_plugins=manifest)
    _assert_provenance(manager, expect_root)
    _assert_completeness(manager, manifest)
    schemas: list[SchemaDefinition] = list(CoreSchemaDefinitions.get_all_core_schemas())
    schemas.extend(collect_schemas(manager.plugins))
    return schemas


def build_snapshot(schemas: list[SchemaDefinition]) -> dict[str, dict[str, dict[str, dict[str, object]]]]:
    """Reduce schemas to the canonical snapshot, MERGING tables per namespace.

    Fails loud on a genuine duplicate ``(namespace, table)`` across two
    definitions — that is a build-time schema conflict, not a snapshot concern.
    """
    merged: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
    for schema in schemas:
        part = _reduce_to_snapshot({schema.namespace: schema})
        for namespace, tables in part.items():
            ns_tables = merged.setdefault(namespace, {})
            for table_name, columns in tables.items():
                if table_name in ns_tables:
                    msg = (
                        f"duplicate declared table {namespace}.{table_name} across "
                        "two SchemaDefinitions — schema conflict, refusing to snapshot"
                    )
                    raise ValueError(msg)
                ns_tables[table_name] = columns
    return merged


def main() -> int:
    snapshot = build_snapshot(collect_declared_schemas())
    json.dump(snapshot, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
