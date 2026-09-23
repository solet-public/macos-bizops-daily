"""Address book plugin state service query helpers."""

from __future__ import annotations

from typing import Any

from ananta.interfaces.state_service_protocol import StateServiceProtocol


def get_by_name(
    state_service: StateServiceProtocol,
    namespace: str,
    name: str,
) -> dict[str, Any] | None:
    # Scope name lookup to LIVE rows. read_state does not auto-exclude
    # soft-deleted rows, so without is_deleted=0 a tombstone would (a) block
    # re-registering a deleted name (name_exists) and (b) resolve as if live.
    result = state_service.read_state(
        namespace=namespace,
        query={"table": "address", "filters": {"name": name, "is_deleted": 0}},
    )
    if isinstance(result, dict):  # type: ignore[reportUnnecessaryIsInstance]
        data = result.get("data", {})
        if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
            rows = data.get("records", [])
            if isinstance(rows, list) and rows:
                row = rows[0]
                if isinstance(row, dict):
                    return row
    return None


def get_entries_for_address(
    state_service: StateServiceProtocol,
    namespace: str,
    address_id: str,
) -> list[dict[str, Any]]:
    result = state_service.read_state(
        namespace=namespace,
        query={
            "table": "address_entry",
            "filters": {"default_address_book_plugin__address_id": address_id},
            "order_by": "sort_order",
        },
    )
    if isinstance(result, dict):  # type: ignore[reportUnnecessaryIsInstance]
        data = result.get("data", {})
        if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
            rows = data.get("records", [])
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def get_entry_by_id(
    state_service: StateServiceProtocol,
    namespace: str,
    entry_id: str,
) -> dict[str, Any] | None:
    result = state_service.read_state(
        namespace=namespace,
        query={"table": "address_entry", "filters": {"id": entry_id}},
    )
    if isinstance(result, dict):  # type: ignore[reportUnnecessaryIsInstance]
        data = result.get("data", {})
        if isinstance(data, dict):  # type: ignore[reportUnnecessaryIsInstance]
            rows = data.get("records", [])
            if isinstance(rows, list) and rows:
                row = rows[0]
                if isinstance(row, dict):
                    return row
    return None


def extract_rows_from_result(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    data = result.get("data", {})
    if not isinstance(data, dict):
        return []
    result_rows = data.get("records", [])
    if not isinstance(result_rows, list):
        return []
    return [r for r in result_rows if isinstance(r, dict)]


def fetch_address_rows(
    state_service: StateServiceProtocol,
    namespace: str,
    address_type: str | None = None,
) -> list[dict[str, Any]]:
    db_query: dict[str, Any] = {"table": "address"}
    if address_type:
        db_query["where"] = {"address_type": address_type}
    result = state_service.read_state(namespace=namespace, query=db_query)
    return extract_rows_from_result(result)


def filter_rows(
    rows: list[dict[str, Any]],
    query: str | None,
    tag: str | None,
) -> list[dict[str, Any]]:
    if tag:
        rows = [r for r in rows if tag in r.get("tags", [])]
    if query:
        query_lower = query.lower()
        rows = [
            r for r in rows
            if query_lower in str(r.get("name", "")).lower()
            or query_lower in str(r.get("description", "")).lower()
        ]
    return rows


def count_tags_in_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    tag_counts: dict[str, int] = {}
    for r in rows:
        tags_value = r.get("tags", [])
        if isinstance(tags_value, list):
            for tag in tags_value:
                if isinstance(tag, str):
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return tag_counts
