"""
Column Types

Basic enumeration types for database schema definitions.
This module is intentionally minimal to avoid circular imports.
"""

from enum import StrEnum


class ColumnType(StrEnum):
    """Database column types — logical, backend-agnostic.

    Each value is the symbolic logical type. Providers translate to native types:
    - Postgres: TEXT→TEXT, INTEGER→INTEGER, REAL→REAL, BLOB→BYTEA,
                DATETIME→TIMESTAMP, BOOLEAN→BOOLEAN, VECTOR→vector(dimension),
                JSON→JSONB
    - SQLite: TEXT→TEXT, INTEGER→INTEGER, REAL→REAL, BLOB→BLOB,
              DATETIME→TIMESTAMP, BOOLEAN→INTEGER, VECTOR→unsupported (raises error),
              JSON→TEXT (stored as JSON string)

    Storage-type translation is the provider's responsibility, not the enum's.
    """

    TEXT = "TEXT"
    INTEGER = "INTEGER"
    REAL = "REAL"
    BLOB = "BLOB"
    DATETIME = "DATETIME"
    BOOLEAN = "BOOLEAN"
    VECTOR = "VECTOR"
    JSON = "JSON"


class IndexType(StrEnum):
    NORMAL = "NORMAL"
    UNIQUE = "UNIQUE"
    PRIMARY = "PRIMARY"
