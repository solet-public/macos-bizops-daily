"""Plugin-schema lifecycle implementation for the postgres backing.

Implements ``PluginSchemaServiceInterface`` against a real PostgreSQL
provider using the new Postgres-native renderer and the transactional
connection. Each verb is a thin orchestrator over four small components:

  1. Hydrate JSON → ``SchemaDefinition`` (``serialization.from_json``).
  2. Standardize via ``SchemaStandardizer`` so the snapshot stored in
     ownership matches what's actually applied to Postgres.
  3. Render DDL ops (``ddl_renderer.emit_create_table_ops``).
  4. Apply ops + ownership writes inside a single transaction
     (``provider.get_transactional_connection``).

Scope of this initial implementation (v1):

  * ``install_plugin_schema``: unknown-namespace, no-live-tables path
    (CREATE everything, record ownership). Identical-shape re-install path
    (no-op + bump ``updated_at``). Reactivate-from-inactive path.
  * ``uninstall_plugin_schema``: logical (status=inactive). Tables intact.
  * ``purge_plugin_schema``: drop tables + ownership rows. Refuses against
    active unless ``force=True``.
  * ``get_installed_schema``: read-only introspection.
  * ``update_plugin_schema``: out of scope here — the diff layer is its own
    next step (v8 plan step 8). Calling it raises ``NotImplementedError``.
  * Adoption against existing live tables: out of scope here — the
    legacy-normalization four-step subroutine is its own step (v8 step 10).
    For now, install on an unknown namespace whose tables already exist
    raises a clear error pointing at the future adoption path.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ananta.interfaces.plugin_schema_service_interface import (
    PluginSchemaServiceInterface,
)
from ananta.services.plugin_schema_service.serialization import from_json, to_json
from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import SchemaDefinition, TableSchema
from psycopg import sql

from .adoption import (
    introspect_table_columns,
    introspect_table_indexes,
    plan_column_normalizations,
    plan_index_reconciliation,
    preflight_normalization,
)
from .ddl_renderer import emit_create_table_ops, emit_drop_table_op
from .schema_diff import diff_schema
from .utils import build_table_name

if TYPE_CHECKING:
    from .provider import PostgresProvider

logger = logging.getLogger(__name__)


def _install_outcome_status(created: list[str], adopted: list[str]) -> str:
    """Pick a status string from the per-table create/adopt counts."""
    if not adopted:
        return "installed"
    if not created:
        return "adopted"
    return "installed_and_adopted"


def _strip_derived_fields(table_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Drop derived fields (``physical_name`` on indexes) for shape comparison.

    Snapshots stored in ownership are enriched with the resolved physical
    index name; freshly-built snapshots from a declared SchemaDefinition
    don't have that field. Comparing them directly would always say they
    differ. Strip the derived fields before equality checks.
    """
    out = dict(table_snapshot)
    indexes = out.get("indexes")
    if isinstance(indexes, list):
        out["indexes"] = [
            {k: v for k, v in idx.items() if k != "physical_name"} if isinstance(idx, dict) else idx
            for idx in indexes
        ]
    return out


class PluginSchemaLifecycle(PluginSchemaServiceInterface):
    """Postgres-backed plugin-schema lifecycle.

    Bound to ``PluginSchemaServiceInterface`` by the postgres state management
    plugin. Owns the four-step apply path (hydrate → standardize → render →
    apply transactionally) and the ownership-table writes.
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider
        self._standardizer = SchemaStandardizer()

    # --- public verbs ------------------------------------------------------

    def install_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        declared = self._hydrate_and_standardize(plugin_namespace, declared_schema_json)
        existing = self._read_ownership(plugin_namespace)

        if not existing:
            return self._install_fresh(plugin_namespace, declared)

        return self._install_against_existing(plugin_namespace, declared, existing)

    def _install_against_existing(
        self,
        plugin_namespace: str,
        declared: SchemaDefinition,
        existing: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Dispatch when ownership rows already exist for the namespace.

        Three cases: all-inactive → reactivate; declared subset matches its
        recorded shape → no-op + bump updated_at; otherwise → diff-and-update.
        """
        existing_active = {t: row for t, row in existing.items() if row["status"] == "active"}
        existing_inactive = {t: row for t, row in existing.items() if row["status"] == "inactive"}

        if existing_inactive and not existing_active:
            return self._reactivate(plugin_namespace, declared, existing_inactive)

        # Match check is scoped to tables this declaration knows about, so
        # piecewise installation of a namespace across multiple SchemaProvider
        # calls works correctly (declared subset matches its subset of ownership).
        relevant_active = {t: row for t, row in existing_active.items() if t in declared.tables}
        if self._declared_subset_matches(declared, existing_active, relevant_active):
            return self._touch_updated_at(plugin_namespace, relevant_active)

        return self._apply_update(plugin_namespace, declared, existing_active)

    def _declared_subset_matches(
        self,
        declared: SchemaDefinition,
        existing_active: dict[str, dict[str, Any]],
        relevant_active: dict[str, dict[str, Any]],
    ) -> bool:
        """True when every declared table is in active ownership AND shapes match."""
        if not set(declared.tables) <= set(existing_active):
            return False
        return self._snapshot_matches(declared, relevant_active)

    def update_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        declared = self._hydrate_and_standardize(plugin_namespace, declared_schema_json)
        existing = self._read_ownership(plugin_namespace)
        if not existing:
            raise ValueError(
                f"update_plugin_schema for {plugin_namespace}: namespace is not installed. "
                "Call install_plugin_schema first."
            )
        active = {t: row for t, row in existing.items() if row["status"] == "active"}
        if not active:
            raise ValueError(
                f"update_plugin_schema for {plugin_namespace}: namespace is uninstalled "
                "(all ownership rows inactive). Call install_plugin_schema to reactivate first."
            )
        return self._apply_update(plugin_namespace, declared, active)

    def uninstall_plugin_schema(self, plugin_namespace: str) -> dict[str, Any]:
        existing = self._read_ownership(plugin_namespace)
        if not existing:
            return {"status": "not_installed", "namespace": plugin_namespace, "tables": []}

        with self._provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE platform__plugin_schema_ownership "
                "SET status = 'inactive', uninstalled_at = (NOW() AT TIME ZONE 'UTC'), "
                "updated_at = (NOW() AT TIME ZONE 'UTC') "
                "WHERE plugin_namespace = %s AND status = 'active'",
                (plugin_namespace,),
            )

        return {
            "status": "uninstalled",
            "namespace": plugin_namespace,
            "tables": list(existing.keys()),
            "data_preserved": True,
        }

    def purge_plugin_schema(
        self, plugin_namespace: str, force: bool = False
    ) -> dict[str, Any]:
        existing = self._read_ownership(plugin_namespace)
        if not existing:
            return {"status": "not_installed", "namespace": plugin_namespace, "tables": []}

        any_active = any(row["status"] == "active" for row in existing.values())
        if any_active and not force:
            raise ValueError(
                f"purge_plugin_schema refused for {plugin_namespace}: namespace is "
                "active. Call uninstall_plugin_schema first, or pass force=True to "
                "override (destructive)."
            )

        schema_name = self._provider.config.schema_name
        ops: list[sql.Composed] = [
            emit_drop_table_op(plugin_namespace, table_name, schema_name)
            for table_name in existing
        ]

        with self._provider.get_transactional_connection() as conn, conn.cursor() as cur:
            # Provider owns DDL execution + id_prefix-cache scrubbing
            # atomically; lifecycle keeps ownership-row management.
            self._provider.apply_schema_purge_ops(
                cur, plugin_namespace, list(existing.keys()), ops,
            )
            cur.execute(
                "DELETE FROM platform__plugin_schema_ownership WHERE plugin_namespace = %s",
                (plugin_namespace,),
            )

        logger.info(
            "purge_plugin_schema: dropped %d tables and ownership rows for %s",
            len(existing),
            plugin_namespace,
        )
        return {
            "status": "purged",
            "namespace": plugin_namespace,
            "tables": list(existing.keys()),
            "force": force,
        }

    def get_installed_schema(self, plugin_namespace: str) -> dict[str, Any]:
        existing = self._read_ownership(plugin_namespace)
        return {
            "namespace": plugin_namespace,
            "tables": {
                t: {
                    "status": row["status"],
                    "schema_snapshot": row["schema_snapshot_json"],
                    "installed_at": row["installed_at"].isoformat() if row.get("installed_at") else None,
                    "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
                    "uninstalled_at": (
                        row["uninstalled_at"].isoformat()
                        if row.get("uninstalled_at")
                        else None
                    ),
                }
                for t, row in existing.items()
            },
        }

    # --- internal helpers --------------------------------------------------

    def _hydrate_and_standardize(
        self, plugin_namespace: str, declared_json: dict[str, Any]
    ) -> SchemaDefinition:
        declared = from_json(declared_json)
        if declared.namespace != plugin_namespace:
            raise ValueError(
                f"Namespace mismatch: caller passed {plugin_namespace!r} but declared "
                f"schema namespace is {declared.namespace!r}"
            )
        # Idempotent standardization: if the schema already has the platform
        # standard fields (caller pre-standardized — e.g., SchemaManager), skip
        # the standardizer call. Re-standardizing would trigger
        # ``_validate_no_protected_field_overrides`` and fail because the
        # standard fields are now "redefined" by the previous pass.
        if not self._is_already_standardized(declared):
            declared = self._standardizer.standardize_schema(declared)
        errors = declared.validate()
        if errors:
            raise ValueError(f"SchemaDefinition.validate() failed: {errors}")
        return declared

    @staticmethod
    def _is_already_standardized(schema: SchemaDefinition) -> bool:
        """Heuristic: if every table has the protected standard fields, treat
        the schema as already standardized.
        """
        if not schema.tables:
            return False
        marker_fields = {"id", "namespace", "created_at", "updated_at", "is_deleted"}
        return all(
            marker_fields.issubset(table.columns.keys()) for table in schema.tables.values()
        )

    def _install_fresh(
        self, plugin_namespace: str, declared: SchemaDefinition
    ) -> dict[str, Any]:
        """Install on a namespace with no ownership rows.

        Per-table dispatch: tables not on disk → CREATE; tables already on
        disk → adopt (column-type normalize + index reconcile + record).
        Both classes of work happen in a single transaction along with the
        ownership-row inserts.
        """
        schema_name = self._provider.config.schema_name
        snapshot = to_json(declared)["tables"]
        plan, adoption_results = self._plan_install_per_table(
            plugin_namespace, declared, schema_name
        )
        self._apply_install_plan(plugin_namespace, declared, plan, snapshot)

        created = [t for t, s in adoption_results.items() if s == "created"]
        adopted = [t for t, s in adoption_results.items() if s == "adopted"]
        logger.info(
            "install_plugin_schema: %s — %d created, %d adopted (%d total)",
            plugin_namespace, len(created), len(adopted), len(declared.tables),
        )
        return {
            "status": _install_outcome_status(created, adopted),
            "namespace": plugin_namespace,
            "tables": list(declared.tables.keys()),
            "created_tables": [t for t, s in adoption_results.items() if s == "created"],
            "adopted_tables": [t for t, s in adoption_results.items() if s == "adopted"],
        }

    def _plan_install_per_table(
        self,
        plugin_namespace: str,
        declared: SchemaDefinition,
        schema_name: str,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """Pre-classify each declared table as create-or-adopt and produce its plan.

        Adoption introspection runs in the autocommit pool here so the read-only
        catalog reads stay outside the transaction's lock footprint. The transaction
        opens later in ``_apply_install_plan``.
        """
        plan: dict[str, dict[str, Any]] = {}
        results: dict[str, str] = {}
        for table_name, table in declared.tables.items():
            if self._table_exists_on_disk(plugin_namespace, table_name):
                plan[table_name] = self._plan_adoption(plugin_namespace, table, schema_name)
                results[table_name] = "adopted"
            else:
                plan[table_name] = self._plan_fresh_create(plugin_namespace, table, schema_name)
                results[table_name] = "created"
        return plan, results

    def _plan_fresh_create(
        self,
        plugin_namespace: str,
        table: TableSchema,
        schema_name: str,
    ) -> dict[str, Any]:
        """Plan structure for a brand-new table: full CREATE ops, no preflights."""
        return {
            "ops": list(emit_create_table_ops(plugin_namespace, table, schema_name)),
            "physical_index_names": {
                idx.name: f"{plugin_namespace}__{table.table_name}__{idx.name}"
                for idx in table.indexes
            },
            "preflight_specs": [],
            "table_name": table.table_name,
        }

    def _apply_install_plan(
        self,
        plugin_namespace: str,
        declared: SchemaDefinition,
        plan: dict[str, dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> None:
        """Run preflights, DDL ops, and ownership inserts in one transaction."""
        schema_name = self._provider.config.schema_name
        with self._provider.get_transactional_connection() as conn:
            for table_plan in plan.values():
                self._run_preflights(conn, plugin_namespace, table_plan, schema_name)

            with conn.cursor() as cur:
                # All DDL goes through the provider chokepoint so the
                # id_prefix cache stays consistent with on-disk reality.
                all_ops = (op for table_plan in plan.values() for op in table_plan["ops"])
                self._provider.apply_schema_change_ops(cur, declared, all_ops)
                self._insert_ownership_rows(cur, plugin_namespace, plan, snapshot)

    def _run_preflights(
        self,
        conn: Any,
        plugin_namespace: str,
        table_plan: dict[str, Any],
        schema_name: str,
    ) -> None:
        """Run all preflight specs for one table inside the transaction."""
        for col_name, declared_type, live_type in table_plan["preflight_specs"]:
            preflight_normalization(
                conn, plugin_namespace, table_plan["table_name"],
                col_name, declared_type, live_type, schema_name,
            )

    def _insert_ownership_rows(
        self,
        cur: Any,
        plugin_namespace: str,
        plan: dict[str, dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> None:
        """INSERT one ownership row per declared table, with resolved index names."""
        for table_name, table_snapshot in snapshot.items():
            enriched = self._enrich_snapshot_with_physical_names(
                table_snapshot, plan[table_name]["physical_index_names"]
            )
            cur.execute(
                "INSERT INTO platform__plugin_schema_ownership "
                "(plugin_namespace, table_name, schema_snapshot_json, status) "
                "VALUES (%s, %s, %s, 'active')",
                (plugin_namespace, table_name, json.dumps(enriched)),
            )

    def _plan_adoption(
        self,
        plugin_namespace: str,
        table: TableSchema,
        schema_name: str,
    ) -> dict[str, Any]:
        """Read live table state and compute the adoption ops + preflight specs.

        Introspection runs through the autocommit pool. Returns the ops and
        a list of ``(col_name, declared_type, live_type)`` preflight specs —
        the lifecycle's transactional connection runs preflights first
        (inside the tx), then the ops.
        """
        full_table_name = build_table_name(plugin_namespace, table.table_name)
        with self._provider.get_connection() as conn:
            live_columns = introspect_table_columns(conn, schema_name, full_table_name)
            live_indexes = introspect_table_indexes(conn, schema_name, full_table_name)

        column_ops, preflight_specs = plan_column_normalizations(
            plugin_namespace, table, live_columns, schema_name
        )
        index_ops, physical_names = plan_index_reconciliation(
            plugin_namespace, table, live_indexes, schema_name
        )

        return {
            "ops": [*column_ops, *index_ops],
            "physical_index_names": physical_names,
            "preflight_specs": preflight_specs,
            "table_name": table.table_name,
        }

    def _enrich_snapshot_with_physical_names(
        self, table_snapshot: dict[str, Any], physical_names: dict[str, str]
    ) -> dict[str, Any]:
        """Embed resolved physical index names into the per-index snapshot entries.

        The diff path uses these on subsequent updates so DROP INDEX targets
        the actual on-disk name (which differs from the resolved-name formula
        for adoption-renamed indexes only by happenstance, but the snapshot
        is the authoritative record either way).
        """
        if not physical_names:
            return table_snapshot
        enriched = dict(table_snapshot)
        enriched_indexes = []
        for idx in enriched.get("indexes", []):
            idx = dict(idx)
            name = idx.get("name")
            if isinstance(name, str):
                phys = physical_names.get(name)
                if phys:
                    idx["physical_name"] = phys
            enriched_indexes.append(idx)
        enriched["indexes"] = enriched_indexes
        return enriched

    def _reactivate(
        self,
        plugin_namespace: str,
        declared: SchemaDefinition,
        inactive_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot = to_json(declared)["tables"]

        for table_name in declared.tables:
            if table_name not in inactive_rows:
                raise NotImplementedError(
                    f"install_plugin_schema for {plugin_namespace}: declared table "
                    f"{table_name!r} is not in the inactive ownership snapshot. "
                    "Cross-shape reactivation requires the diff path (v8 step 8)."
                )

        for table_name, table_snapshot in snapshot.items():
            recorded = inactive_rows[table_name]["schema_snapshot_json"]
            if _strip_derived_fields(recorded) != _strip_derived_fields(table_snapshot):
                raise NotImplementedError(
                    f"install_plugin_schema for {plugin_namespace}.{table_name}: "
                    "declared shape differs from inactive snapshot. The diff path "
                    "is not yet implemented (v8 step 8)."
                )

        with self._provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE platform__plugin_schema_ownership "
                "SET status = 'active', uninstalled_at = NULL, "
                "updated_at = (NOW() AT TIME ZONE 'UTC') "
                "WHERE plugin_namespace = %s",
                (plugin_namespace,),
            )

        logger.info(
            "install_plugin_schema: reactivated %s (%d tables, identical shape, data preserved)",
            plugin_namespace,
            len(inactive_rows),
        )
        return {
            "status": "reactivated",
            "namespace": plugin_namespace,
            "tables": list(inactive_rows.keys()),
            "data_preserved": True,
        }

    def _apply_update(
        self,
        plugin_namespace: str,
        declared: SchemaDefinition,
        existing_active: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Diff declared vs. recorded snapshot, apply DDL, refresh snapshot.

        Tables added → CREATE. Columns added → ALTER ADD. Columns removed →
        ALTER DROP. Index changes → drop-then-create on resolved physical
        names. Column type changes → refused (use v1 drop-and-readd).

        **Table-level semantics: additive only.** Tables in ownership but
        absent from this declaration are left alone. A single namespace can
        be declared piecewise across multiple ``SchemaProvider`` calls (the
        platform does this at startup — ``core`` namespace gets installed
        once per table group), and we don't want a partial declaration to
        look like a table-removal request. To actually remove a table,
        ``uninstall_plugin_schema`` (logical, namespace-wide, data preserved)
        or ``purge_plugin_schema`` (destructive, namespace-wide).
        """
        # Reconstruct the recorded SchemaDefinition for ONLY the tables this
        # declaration knows about. Ownership rows for other tables in the
        # namespace are intentionally invisible to this diff so they don't
        # appear as removals.
        relevant_existing = {
            t: row for t, row in existing_active.items() if t in declared.tables
        }
        current_namespace_payload: dict[str, Any] = {
            "namespace": plugin_namespace,
            "tables": {t: row["schema_snapshot_json"] for t, row in relevant_existing.items()},
        }
        current = from_json(current_namespace_payload)

        ops = diff_schema(
            namespace=plugin_namespace,
            current=current,
            declared=declared,
            mode="update",
            schema_name=self._provider.config.schema_name,
        )

        if not ops:
            return self._touch_updated_at(plugin_namespace, existing_active)

        # New snapshot for ownership rows (declared-shape, post-update)
        new_snapshot = to_json(declared)["tables"]

        with self._provider.get_transactional_connection() as conn, conn.cursor() as cur:
            # All DDL goes through the provider chokepoint so id_prefix
            # cache entries for newly-added tables get registered atomically
            # with the schema change.
            self._provider.apply_schema_change_ops(cur, declared, ops)
            # New tables: INSERT ownership row.
            # Existing tables: UPDATE snapshot.
            for table_name, snap in new_snapshot.items():
                if table_name in existing_active:
                    cur.execute(
                        "UPDATE platform__plugin_schema_ownership "
                        "SET schema_snapshot_json = %s, "
                        "    updated_at = (NOW() AT TIME ZONE 'UTC') "
                        "WHERE plugin_namespace = %s AND table_name = %s",
                        (json.dumps(snap), plugin_namespace, table_name),
                    )
                else:
                    cur.execute(
                        "INSERT INTO platform__plugin_schema_ownership "
                        "(plugin_namespace, table_name, schema_snapshot_json, status) "
                        "VALUES (%s, %s, %s, 'active')",
                        (plugin_namespace, table_name, json.dumps(snap)),
                    )

        logger.info(
            "update_plugin_schema: applied %d ops to %s (added/dropped columns and indexes)",
            len(ops),
            plugin_namespace,
        )
        return {
            "status": "updated",
            "namespace": plugin_namespace,
            "tables": list(declared.tables.keys()),
            "ops_applied": len(ops),
        }

    def _snapshot_matches(
        self, declared: SchemaDefinition, recorded: dict[str, dict[str, Any]]
    ) -> bool:
        if set(declared.tables) != set(recorded):
            return False
        snapshot = to_json(declared)["tables"]
        return all(
            _strip_derived_fields(recorded[t]["schema_snapshot_json"])
            == _strip_derived_fields(snapshot[t])
            for t in declared.tables
        )

    def _touch_updated_at(
        self, plugin_namespace: str, recorded: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        with self._provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE platform__plugin_schema_ownership "
                "SET updated_at = (NOW() AT TIME ZONE 'UTC') "
                "WHERE plugin_namespace = %s",
                (plugin_namespace,),
            )
        return {
            "status": "no_op",
            "namespace": plugin_namespace,
            "tables": list(recorded.keys()),
            "reason": "identical shape already installed",
        }

    def _read_ownership(self, plugin_namespace: str) -> dict[str, dict[str, Any]]:
        with self._provider.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, schema_snapshot_json, status, "
                "installed_at, updated_at, uninstalled_at "
                "FROM platform__plugin_schema_ownership "
                "WHERE plugin_namespace = %s",
                (plugin_namespace,),
            )
            return {row["table_name"]: dict(row) for row in cur.fetchall()}

    def _table_exists_on_disk(self, plugin_namespace: str, table_name: str) -> bool:
        return self._provider.table_exists(plugin_namespace, table_name)
