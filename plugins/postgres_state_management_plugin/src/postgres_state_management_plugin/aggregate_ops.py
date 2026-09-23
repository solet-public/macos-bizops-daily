"""Aggregate operation helpers for PostgreSQL state plugin.

Mirrors ``write_ops``/``key_value_ops`` (module-level ``(provider, …)`` helpers)
so the autocommit aggregate facade methods stay thin. Lockstep with the rds
twin's ``rds_crud._state_aggregate``.
"""

import logging

import psycopg
from ananta.core.domain.types import ActionResult

from postgres_state_management_plugin.postgres_backend.provider import PostgresProvider

from .result_helpers import create_error_result, create_success_result

logger = logging.getLogger(__name__)


def run_aggregate(
    provider: PostgresProvider,
    namespace: str,
    data: dict[str, object],
    *,
    op: str,
    requires_column: bool,
    error_ns: str,
) -> ActionResult:
    """Shared autocommit aggregate facade — validate, run, wrap the envelope.

    ``count`` REJECTS a ``column``; ``max``/``min`` REQUIRE one (fail-fast,
    mirroring ``acquire_lease.invalid_*`` error codes). The scalar is surfaced
    VERBATIM at ``data.result.value`` (no coercion — the F1 TZ seam). NO auto
    ``is_deleted`` exclusion.
    """
    try:
        table = data.get("table")
        if not isinstance(table, str):
            return create_error_result(
                "Missing or invalid 'table' in data",
                error_code=f"{error_ns}.invalid_table",
            )
        filters = data.get("filters", {})
        if not isinstance(filters, dict):
            return create_error_result(
                "'filters' must be a dictionary",
                error_code=f"{error_ns}.invalid_filters",
            )
        column = data.get("column")
        agg_column: str | None
        if requires_column:
            if not isinstance(column, str):
                return create_error_result(
                    "Missing or invalid 'column' in data",
                    error_code=f"{error_ns}.invalid_column",
                )
            agg_column = column
        else:
            if column is not None:
                return create_error_result(
                    "'count' does not accept a 'column'",
                    error_code=f"{error_ns}.unexpected_column",
                )
            agg_column = None
        value = provider.aggregate(
            namespace=namespace, table=table, op=op,
            column=agg_column, filters=filters,
        )
        return create_success_result(
            {"namespace": namespace, "result": {"value": value}}
        )
    except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
        logger.exception("Failed to compute %s aggregate", error_ns)
        return create_error_result(
            str(e), error_code=f"state.{error_ns}_failed",
            details={"namespace": namespace},
        )
