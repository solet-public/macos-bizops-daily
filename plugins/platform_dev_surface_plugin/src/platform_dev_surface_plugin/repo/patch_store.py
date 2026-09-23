"""propose_patch artifact store (design §2.4) — state-interface, own namespace.

propose_patch is ARTIFACT-ONLY: it durably stores a unified-diff proposal + a
read-only ``git apply --check`` clean-applies flag, and returns a ``patch_id``.
APPLY IS NOT A VERB — the operator / Git-Controller reads the artifact and
applies it through the normal 5-step handoff. There is no model-callable
apply/commit path anywhere in this plugin.

Persistence is declarative: the table is declared via ``get_schema_definitions``
(SchemaProvider) and auto-installed at startup by the plugin_initializer. The
only DB touch is a single ``write_state`` into the plugin's OWN namespace — no
raw SQL, no joins (State Interface mandate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

PATCH_NAMESPACE = "platform_dev_surface_plugin"
PATCH_TABLE = "patch_proposal"
_PATCH_ID_PREFIX = "patch"


def get_patch_proposal_schema() -> SchemaDefinition:
    """Declarative schema for the ``platform_dev_surface_plugin__patch_proposal`` table.

    Dev-surface artifact content (diffs, paths) is public (0.0) — it is the
    operator's own proposed change, never user data, and is never returned to
    the model (propose_patch returns only the patch_id + flags).
    """
    return SchemaDefinition(
        namespace=PATCH_NAMESPACE,
        description="platform_dev_surface_plugin repo_service artifacts.",
        tables={
            PATCH_TABLE: TableSchema(
                table_name=PATCH_TABLE,
                id_prefix=_PATCH_ID_PREFIX,
                description="A stored unified-diff proposal awaiting operator/Git-Controller apply.",
                data_sensitivity=0.0,
                columns={
                    "unified_diff": ColumnDefinition(
                        type=ColumnType.TEXT, not_null=True, data_sensitivity=0.0,
                        description="The proposed unified diff (artifact only; never auto-applied).",
                    ),
                    "path_count": ColumnDefinition(
                        type=ColumnType.INTEGER, not_null=True, data_sensitivity=0.0,
                        description="Number of repo-root-confined paths the diff touches.",
                    ),
                    "applies_cleanly": ColumnDefinition(
                        type=ColumnType.BOOLEAN, not_null=True, data_sensitivity=0.0,
                        description="Result of a read-only `git apply --check` at store time.",
                    ),
                    "paths": ColumnDefinition(
                        type=ColumnType.JSON, data_sensitivity=0.0,
                        description="The confined target paths extracted from the diff.",
                    ),
                    "principal": ColumnDefinition(
                        type=ColumnType.TEXT, data_sensitivity=0.0,
                        description="Server-built CallContext principal that proposed the patch (audit).",
                    ),
                },
            ),
        },
    )


class PatchStore:
    """Write patch-proposal artifacts via the state interface (own namespace)."""

    def __init__(self, state_service: StateManagementInterface) -> None:
        self._state = state_service

    def store(
        self,
        *,
        unified_diff: str,
        paths: list[str],
        applies_cleanly: bool,
        principal: str,
    ) -> str:
        """Persist one proposal; return the auto-generated ``patch_id`` (fail-loud)."""
        result = self._state.write_state(
            PATCH_NAMESPACE,
            {
                "table": PATCH_TABLE,
                "record": {
                    "unified_diff": unified_diff,
                    "path_count": len(paths),
                    "applies_cleanly": applies_cleanly,
                    "paths": paths,
                    "principal": principal,
                },
            },
        )
        # write_state ActionResult.data = {"namespace", "result": {"generated_id", "inserted"}}
        data = result.get("data")
        inner = data.get("result") if isinstance(data, dict) else None
        patch_id = inner.get("generated_id") if isinstance(inner, dict) else None
        if not isinstance(patch_id, str) or not patch_id:
            raise RuntimeError(f"patch_proposal write returned no generated_id: {result!r}")
        return patch_id
