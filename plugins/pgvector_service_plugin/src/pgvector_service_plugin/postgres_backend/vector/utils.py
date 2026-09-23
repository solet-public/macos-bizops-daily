"""Utility functions for pgvector-backed plugins."""

from collections.abc import Sequence

from .constants import (
    DISTANCE_METRIC_TO_OPERATOR,
    DistanceMetric,
)


def format_vector_literal(vector: Sequence[float]) -> str:
    """Render a float vector as a pgvector literal ``[v1, v2, ...]``."""
    formatted_values = ", ".join(f"{value:.12f}" for value in vector)
    return f"[{formatted_values}]"


def quote_identifier(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded double-quotes."""
    return '"' + name.replace('"', '""') + '"'


def qualified_table(schema: str, table: str) -> str:
    """Compose a schema-qualified, double-quoted ``"schema"."table"`` name."""
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def get_distance_operator(metric: DistanceMetric) -> str:
    """Get pgvector operator string for a distance metric enum.

    Raises:
        TypeError: If metric is not a DistanceMetric enum
        ValueError: If metric has no operator mapping
    """
    if not isinstance(metric, DistanceMetric):  # type: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"metric must be DistanceMetric enum, got {type(metric).__name__}")

    operator = DISTANCE_METRIC_TO_OPERATOR.get(metric)
    if operator is None:
        raise ValueError(f"No operator mapping for metric: {metric.value}")

    return operator.value


def validate_distance_metric(metric: DistanceMetric) -> bool:
    """Return True if metric is a supported DistanceMetric enum value."""
    return isinstance(metric, DistanceMetric) and metric in DISTANCE_METRIC_TO_OPERATOR  # type: ignore[reportUnnecessaryIsInstance]
