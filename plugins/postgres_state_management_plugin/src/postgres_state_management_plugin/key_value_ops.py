"""Key-value store operations for PostgreSQL state plugin."""

import json
import logging
from typing import Any

import psycopg
from ananta.core.domain.types import ActionResult

from postgres_state_management_plugin.postgres_backend.provider import PostgresProvider

from .result_helpers import create_error_result, create_success_result

logger = logging.getLogger(__name__)


def kv_set(
    provider: PostgresProvider,
    namespace: str,
    key: str,
    value: object,
    scope: str = "GLOBAL",
) -> ActionResult:
    try:
        serialized_value = json.dumps(value) if isinstance(value, dict | list) else str(value)
        data = {
            "id": provider.generate_id("core__key_value_store"),
            "namespace": namespace,
            "key": key,
            "value": serialized_value,
            "scope": scope,
        }
        record_id = provider.upsert(
            namespace="core",
            table="key_value_store",
            data=data,
            conflict_columns=["namespace", "key", "scope"],
        )
        return create_success_result(
            {"namespace": namespace, "key": key, "scope": scope, "id": record_id}
        )
    except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
        logger.exception("Failed to set key-value")
        return create_error_result(
            str(e), error_code="kv.set_failed", details={"namespace": namespace, "key": key}
        )


def kv_get(
    provider: PostgresProvider, namespace: str, key: str, scope: str = "GLOBAL"
) -> ActionResult:
    try:
        rows = provider.select(
            namespace="core",
            table="key_value_store",
            conditions={"namespace": namespace, "key": key, "scope": scope},
            limit=1,
        )
        if not rows:
            return create_success_result(
                {"namespace": namespace, "key": key, "scope": scope, "value": None, "found": False}
            )
        return create_success_result(
            {
                "namespace": namespace,
                "key": key,
                "scope": scope,
                "value": rows[0].get("value"),
                "found": True,
            }
        )
    except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
        logger.exception("Failed to get key-value")
        return create_error_result(
            str(e), error_code="kv.get_failed", details={"namespace": namespace, "key": key}
        )


def kv_delete(
    provider: PostgresProvider, namespace: str, key: str, scope: str = "GLOBAL"
) -> ActionResult:
    try:
        deleted_count = provider.delete(
            namespace="core",
            table="key_value_store",
            conditions={"namespace": namespace, "key": key, "scope": scope},
            soft_delete=False,
        )
        return create_success_result(
            {"namespace": namespace, "key": key, "scope": scope, "deleted": deleted_count}
        )
    except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
        logger.exception("Failed to delete key-value")
        return create_error_result(
            str(e),
            error_code="kv.delete_failed",
            details={"namespace": namespace, "key": key},
        )


def kv_clear(
    provider: PostgresProvider,
    namespace: str | None = None,
    scope: str | None = None,
) -> ActionResult:
    try:
        conditions: dict[str, Any] = {}
        if namespace:
            conditions["namespace"] = namespace
        if scope:
            conditions["scope"] = scope
        deleted_count = provider.delete(
            namespace="core",
            table="key_value_store",
            conditions=conditions,
            soft_delete=False,
        )
        return create_success_result(
            {"namespace": namespace, "scope": scope, "deleted": deleted_count}
        )
    except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
        logger.exception("Failed to clear key-values")
        return create_error_result(
            str(e),
            error_code="kv.clear_failed",
            details={"namespace": namespace, "scope": scope},
        )


def kv_list(
    provider: PostgresProvider,
    namespace: str | None = None,
    scope: str | None = None,
) -> ActionResult:
    try:
        conditions: dict[str, Any] = {}
        if namespace:
            conditions["namespace"] = namespace
        if scope:
            conditions["scope"] = scope
        rows = provider.select(
            namespace="core",
            table="key_value_store",
            conditions=conditions if conditions else None,
        )
        return create_success_result(
            {"namespace": namespace, "scope": scope, "values": rows, "count": len(rows)}
        )
    except (psycopg.Error, OSError, RuntimeError, ValueError) as e:
        logger.exception("Failed to list key-values")
        return create_error_result(
            str(e),
            error_code="kv.list_failed",
            details={"namespace": namespace, "scope": scope},
        )
