"""Column definition normalization helpers for PostgreSQL schema creation."""

import logging
from typing import Final

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition

logger = logging.getLogger(__name__)

STRING_TYPE_MAPPING: Final[dict[str, str]] = {
    "text": "TEXT",
    "string": "TEXT",
    "integer": "INTEGER",
    "int": "INTEGER",
    "real": "DOUBLE PRECISION",
    "float": "DOUBLE PRECISION",
    "double": "DOUBLE PRECISION",
    "blob": "BYTEA",
    "binary": "BYTEA",
    "datetime": "TIMESTAMP",
    "timestamp": "TIMESTAMP",
    "date": "TIMESTAMP",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
}


def map_column_type(column_type: ColumnType | None) -> str | None:
    """Map ColumnType enum to PostgreSQL type string."""
    if column_type is None:
        return None
    return {
        "TEXT": "TEXT",
        "INTEGER": "INTEGER",
        "REAL": "DOUBLE PRECISION",
        "BLOB": "BYTEA",
        "DATETIME": "TIMESTAMP",
        "BOOLEAN": "BOOLEAN",
    }.get(column_type.name)


def strip_column_name_prefix(column_name: str, fragment: str) -> str:
    lower_column = column_name.lower()
    if fragment.lower().startswith(f"{lower_column} "):
        return fragment[len(column_name):].strip()
    return fragment


def skip_multiword_type(tokens: list[str], type_token: str) -> int:
    if len(tokens) > 1 and type_token.upper() == "DOUBLE" and tokens[1].upper() == "PRECISION":
        return 2
    return 1


def should_skip_token(tokens: list[str], idx: int) -> int:
    """Return number of tokens to skip (0 = keep this token)."""
    token = tokens[idx]
    upper_token = token.upper()
    if upper_token == "DEFAULT" and idx + 1 < len(tokens) and "__CONTRACT:" in tokens[idx + 1]:
        return 2
    if "__CONTRACT:" in token or upper_token == "AUTOINCREMENT":
        return 1
    return 0


def normalize_tokens(tokens: list[str]) -> str:
    type_token = tokens[0]
    mapped_type = STRING_TYPE_MAPPING.get(type_token.lower(), type_token.upper())
    normalized: list[str] = [mapped_type]
    idx = skip_multiword_type(tokens, type_token)
    while idx < len(tokens):
        skip_count = should_skip_token(tokens, idx)
        if skip_count:
            idx += skip_count
            continue
        normalized.append(tokens[idx])
        idx += 1
    return " ".join(normalized).strip()


def normalize_sql_fragment(column_name: str, fragment: str) -> str:
    """Normalize a SQLite-style column definition for PostgreSQL compatibility."""
    raw_fragment = strip_column_name_prefix(column_name, fragment.strip())
    if not raw_fragment:
        return ""
    tokens = raw_fragment.split()
    if not tokens:
        return ""
    return normalize_tokens(tokens)


def format_default_clause(default_value: object) -> str | None:
    if default_value is None:
        return None
    if isinstance(default_value, str):
        trimmed = default_value.strip()
        if "__CONTRACT:" in trimmed:
            return None
        upper_trimmed = trimmed.upper()
        if upper_trimmed.startswith(
            ("CURRENT_TIMESTAMP", "NOW()", "(NOW()", "CURRENT_DATE", "CURRENT_TIME")
        ):
            return f"DEFAULT {trimmed}"
        if trimmed.startswith(("'", '"')) and trimmed.endswith(("'", '"')):
            return f"DEFAULT {trimmed}"
        return f"DEFAULT '{trimmed}'"
    return f"DEFAULT {default_value}"


def resolve_column_type(column_name: str, column_def: dict[str, object]) -> str:
    type_value = column_def.get("type")
    column_type: ColumnType | None = None
    if isinstance(type_value, ColumnType):
        column_type = type_value
    elif isinstance(type_value, str):
        try:
            column_type = ColumnType[type_value.upper()]
        except KeyError:
            column_type = None
    if column_type:
        return map_column_type(column_type) or "TEXT"
    if isinstance(type_value, str):
        return STRING_TYPE_MAPPING.get(type_value.lower(), type_value.upper())
    logger.error("Invalid type for column %s: %s", column_name, type_value)
    return "TEXT"


def add_constraint_fragments(column_def: dict[str, object], fragments: list[str]) -> None:
    primary_key = bool(column_def.get("primary_key") or column_def.get("is_primary_key"))
    if primary_key:
        fragments.append("PRIMARY KEY")
    nullable_flag = column_def.get("nullable")
    not_null = bool(column_def.get("not_null")) or (nullable_flag is False)
    if not primary_key and not_null:
        fragments.append("NOT NULL")
    if column_def.get("unique"):
        fragments.append("UNIQUE")


def add_default_and_check_fragments(
    column_def: dict[str, object], fragments: list[str]
) -> None:
    default_clause = format_default_clause(column_def.get("default"))
    if default_clause:
        fragments.append(default_clause)
    check_clause = column_def.get("check")
    if isinstance(check_clause, str) and check_clause.strip():
        clause = check_clause.strip()
        if not clause.upper().startswith("CHECK"):
            clause = f"CHECK ({clause})"
        fragments.append(clause)


def convert_dict_definition(column_name: str, column_def: dict[str, object]) -> str:
    mapped_type = resolve_column_type(column_name, column_def)
    fragments: list[str] = [mapped_type]
    add_constraint_fragments(column_def, fragments)
    add_default_and_check_fragments(column_def, fragments)
    return " ".join(fragments)


def normalize_column_definition(
    column_name: str, column_def: object
) -> str | ColumnDefinition | None:
    """Normalize assorted column definition formats into PostgreSQL-ready SQL."""
    if column_def is None:
        return None
    if isinstance(column_def, ColumnDefinition):
        return column_def
    if isinstance(column_def, ColumnType):
        mapped = map_column_type(column_def)
        if not mapped:
            logger.error("Unsupported ColumnType for %s: %s", column_name, column_def)
            return None
        return mapped
    if isinstance(column_def, dict):
        fragment = convert_dict_definition(column_name, column_def)
        return normalize_sql_fragment(column_name, fragment)
    if isinstance(column_def, str):
        return normalize_sql_fragment(column_name, column_def)
    logger.error("Unknown column definition format for %s: %s", column_name, type(column_def))
    return None


def normalize_columns(
    columns_raw: dict[str, object],
) -> dict[str, ColumnType | str | ColumnDefinition]:
    normalized: dict[str, ColumnType | str | ColumnDefinition] = {}
    for col_name, col_def in columns_raw.items():
        result = normalize_column_definition(col_name, col_def)
        if result:
            normalized[col_name] = result
        else:
            logger.error(
                "Skipping column %s due to unsupported definition: %s", col_name, col_def
            )
    return normalized
