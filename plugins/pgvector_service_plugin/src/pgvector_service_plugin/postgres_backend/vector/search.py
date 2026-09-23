"""Similarity-search machinery for the pgvector provider.

Extracted from ``PGVectorProvider`` — which had grown to the coherence-bounded
god-class LOC limit — into pure module functions over
``(state_service, schema_name, query params)``. The provider's
``search_similar`` is now a thin delegator over :func:`run_similarity_search`.
Keeping these as free functions (rather than a mixin) makes the query
composition + row parsing directly unit-testable without constructing a
provider. Identical in both pgvector twins.

NOTE: :func:`run_similarity_search` issues a raw ``execute_sql`` — pre-existing
search-path debt relocated verbatim under this decompose, NOT migrated onto a
state-interface primitive.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from ananta.interfaces.state_service_protocol import StateServiceProtocol

from .constants import (
    COLUMN_CREATED_AT,
    COLUMN_DIMENSION,
    COLUMN_DISTANCE,
    COLUMN_DISTANCE_METRIC,
    COLUMN_EMBEDDING,
    COLUMN_EXTERNAL_ID,
    COLUMN_ID,
    COLUMN_IS_DELETED,
    COLUMN_METADATA,
    COLUMN_NAMESPACE,
    COLUMN_UPDATED_AT,
    DELETED_FLAG_ACTIVE,
    KEY_ACTION_STATUS,
    KEY_COUNT,
    KEY_DATA,
    KEY_ERROR,
    KEY_NAMESPACES_SEARCHED,
    KEY_RECORDS,
    KEY_RESULTS,
    STATUS_COMPLETED,
    TABLE_SUFFIX,
    DistanceMetric,
)
from .utils import format_vector_literal, get_distance_operator, qualified_table

logger = logging.getLogger(__name__)


def validate_search_params(
    namespaces: list[str],
    query_vector: list[float],
    top_k: int,
    distance_metric: DistanceMetric,
) -> None:
    if not namespaces:
        raise ValueError("namespaces list cannot be empty")

    if not isinstance(namespaces, list):  # type: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"namespaces must be a list, got {type(namespaces).__name__}")

    if not all(isinstance(ns, str) for ns in namespaces):  # type: ignore[reportUnnecessaryIsInstance]
        raise TypeError("All namespaces must be strings")

    if not isinstance(distance_metric, DistanceMetric):  # type: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"distance_metric must be DistanceMetric enum, "
            f"got {type(distance_metric).__name__}"
        )

    if not query_vector:
        raise ValueError("query_vector cannot be empty")

    if top_k <= 0:
        raise ValueError(f"top_k must be positive, got {top_k}")


def build_search_query(
    schema_name: str,
    namespaces: list[str],
    query_vector: list[float],
    top_k: int,
    filters: dict[str, Any] | None,
    distance_metric: DistanceMetric,
) -> tuple[str, list[Any]]:
    operator = get_distance_operator(distance_metric)
    vector_literal = format_vector_literal(query_vector)
    table_name = qualified_table(
        schema_name, f"{namespaces[0]}{TABLE_SUFFIX}"
    )

    query = (
        f"SELECT {COLUMN_ID}, {COLUMN_NAMESPACE}, {COLUMN_DIMENSION}, "
        f"{COLUMN_METADATA}, {COLUMN_DISTANCE_METRIC}, {COLUMN_CREATED_AT}, "
        f"{COLUMN_UPDATED_AT}, {COLUMN_EXTERNAL_ID}, "
        f"({COLUMN_EMBEDDING} {operator} %s::vector) AS {COLUMN_DISTANCE} "
        f"FROM {table_name} "
        f"WHERE ({COLUMN_IS_DELETED} IS NULL OR {COLUMN_IS_DELETED} = {DELETED_FLAG_ACTIVE})"
    )
    params: list[Any] = [vector_literal]

    if filters:
        query += f" AND {COLUMN_METADATA}::jsonb @> %s::jsonb"
        params.append(json.dumps(filters))

    query += f" ORDER BY {COLUMN_DISTANCE} LIMIT %s"
    params.append(top_k)

    return query, params


def parse_search_row(row: list[Any] | tuple[Any, ...]) -> dict[str, Any] | None:
    if len(row) < 9:
        logger.error("Skipping malformed row: %s", row)
        return None

    metadata_str = row[3]
    metadata = (
        json.loads(metadata_str) if metadata_str and isinstance(metadata_str, str) else {}
    )

    result: dict[str, Any] = {
        COLUMN_ID: row[0],
        COLUMN_NAMESPACE: row[1],
        COLUMN_DIMENSION: row[2],
        COLUMN_METADATA: metadata,
        COLUMN_DISTANCE_METRIC: row[4],
        COLUMN_CREATED_AT: row[5],
        COLUMN_UPDATED_AT: row[6],
        COLUMN_DISTANCE: float(row[8]) if row[8] is not None else 0.0,
    }

    if row[7]:
        result[COLUMN_EXTERNAL_ID] = row[7]

    return result


def run_similarity_search(
    state_service: StateServiceProtocol,
    schema_name: str,
    namespaces: list[str],
    query_vector: list[float],
    top_k: int,
    filters: dict[str, Any] | None,
    distance_metric: DistanceMetric,
) -> dict[str, Any]:
    """Search for similar vectors using pgvector distance operators."""
    validate_search_params(namespaces, query_vector, top_k, distance_metric)

    try:
        filters_summary = json.dumps(filters, sort_keys=True) if filters else "{}"
        logger.debug(
            "PGVECTOR_SEARCH namespaces=%s metric=%s top_k=%d filters=%s",
            namespaces, distance_metric.value, top_k, filters_summary,
        )

        query, params = build_search_query(
            schema_name, namespaces, query_vector, top_k, filters, distance_metric
        )
        sql_result = state_service.execute_sql(sql_query=query, sql_params=params)

        if sql_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            error = sql_result.get(KEY_ERROR, "Unknown error")
            raise RuntimeError(f"StateService execute_sql failed: {error}")

        data = cast(dict[str, Any], sql_result.get(KEY_DATA, {}))
        rows = cast(list[list[Any] | tuple[Any, ...]], data.get(KEY_RECORDS, []))

        results: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list | tuple):  # type: ignore[reportUnnecessaryIsInstance]
                continue
            parsed = parse_search_row(row)
            if parsed:
                results.append(parsed)

        return {
            KEY_RESULTS: results,
            KEY_COUNT: len(results),
            KEY_NAMESPACES_SEARCHED: namespaces,
        }

    except (ValueError, TypeError):
        raise
    except Exception as e:
        logger.error("Failed to search similar vectors: %s", e)
        raise RuntimeError(f"Similarity search failed: {e}") from e
