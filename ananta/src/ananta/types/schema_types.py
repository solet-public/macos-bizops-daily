from dataclasses import dataclass, field
from typing import Literal

from ananta.types.column_sql_generator import ColumnSqlGenerator
from ananta.types.column_types import ColumnType as ColumnType
from ananta.types.schema_validator import SchemaValidator
from ananta.types.sql_function_detector import SqlFunctionDetector

# Per W5.P §3.2: PG-native referential actions exposed on the
# ColumnDefinition FK primitive. Subset of the SQL standard appropriate
# for our use cases (declarative orphan-impossibility for the actr_memory
# child rows; the SET DEFAULT path is omitted intentionally — no current
# schema uses it).
ReferentialAction = Literal[
    "NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT",
]


@dataclass
class ColumnDefinition:
    type: ColumnType
    primary_key: bool = False
    not_null: bool = False
    default: object | None = None
    unique: bool = False
    check: str | None = None
    description: str | None = None  # Human-readable column description for schema introspection
    type_params: dict[str, object] | None = (
        None  # Type-specific parameters (e.g., {"dimension": 384} for VECTOR)
    )
    # Data sensitivity annotation (0.0 = public, 1.0 = restricted). SCHEMA
    # metadata — persisted by the schema registry and round-tripped through
    # plugin schema serialization; NOT part of the removed exposure filter.
    # Default is 1.0 (restricted) - fail closed, fields must be explicitly marked public
    data_sensitivity: float = 1.0
    # Per W5.P §3.2: PG-native foreign-key declaration. ``foreign_key`` is
    # a (target_table, target_column) pair where target_table is the
    # ALREADY-namespace-prefixed table name (the ``namespace__table``
    # convention emitted by the Postgres-native DDL renderer). When set, the
    # column emits ``REFERENCES <target_table>(<target_col>) ON DELETE
    # <on_delete> ON UPDATE <on_update>`` in its DDL, and
    # ``adoption.plan_fk_reconciliation`` will ADD CONSTRAINT for it
    # against live schema state. Default ``None`` preserves existing
    # column-definition shape for the 100+ callers that don't declare
    # references.
    foreign_key: tuple[str, str] | None = None
    on_delete: ReferentialAction = "NO ACTION"
    on_update: ReferentialAction = "NO ACTION"

    # Class-level service instances for SQL generation
    _sql_function_detector = SqlFunctionDetector()
    _sql_generator = ColumnSqlGenerator(_sql_function_detector)

    def __post_init__(self) -> None:
        if not 0.0 <= self.data_sensitivity <= 1.0:
            raise ValueError(
                f"data_sensitivity must be in [0, 1], got {self.data_sensitivity}"
            )

    def to_sql(self, column_name: str) -> str:
        """
        Generate SQL column definition string.

        REFACTORED: Delegated to ColumnSqlGenerator service - reduced from C(11) to A(1) complexity
        """
        return self._sql_generator.generate_column_sql(
            column_name=column_name,
            column_type=self.type,
            primary_key=self.primary_key,
            not_null=self.not_null,
            default=self.default,
            unique=self.unique,
            check=self.check,
            type_params=self.type_params,
            foreign_key=self.foreign_key,
            on_delete=self.on_delete,
            on_update=self.on_update,
        )


@dataclass
class IndexDefinition:
    name: str
    columns: list[str]
    unique: bool = False
    where: str | None = None
    # Expression-/method-specific index kinds
    # (``gin`` / ``gist`` / ``brin`` / ``hash``) and
    # per-column operator classes (e.g. ``gin_trgm_ops`` for pg_trgm
    # similarity matching, ``vector_cosine_ops`` for pgvector cosine ANN).
    # Both kwargs default to None so every existing IndexDefinition usage
    # stays valid — only call sites that need a non-btree index opt in.
    using: str | None = None
    column_operator_classes: dict[str, str] | None = None
    # W5.E §5.2 G2: per-index ``WITH (k1=v1, k2=v2)`` reloptions for index
    # methods that take build-time tuning knobs (HNSW: ``m`` /
    # ``ef_construction``; BRIN: ``pages_per_range``; etc.). Default None
    # so every existing IndexDefinition usage stays valid — only call
    # sites that need build-time tuning opt in. The 5-surface fold
    # documented in the W5.E design memo §5.2 covers DDL emission (local
    # + RDS), schema_diff equivalence (local + RDS), adoption
    # introspection + shape-match, and plugin schema-service
    # serialization round-trip so a declared HNSW survives the
    # blue-green adoption + RDS-mirror path without silent swap to
    # btree.
    index_with_options: dict[str, object] | None = None


@dataclass
class TableSchema:
    table_name: str
    columns: dict[str, ColumnDefinition]
    indexes: list[IndexDefinition] = field(default_factory=list)
    check_constraints: list[str] = field(default_factory=list)
    with_history: bool = False  # Enable automatic history table generation
    description: str | None = None  # Human-readable table description for schema introspection
    id_prefix: str | None = None  # Prefix for auto-generated IDs (e.g., "vop" for voice_profile)
    # Table-level data sensitivity (0.0 = public, 1.0 = restricted). SCHEMA
    # metadata — the default for columns that don't specify their own value;
    # persisted/serialized with the schema, NOT part of the removed exposure filter.
    data_sensitivity: float = 1.0  # Default restrictive

    def __post_init__(self) -> None:
        if not 0.0 <= self.data_sensitivity <= 1.0:
            raise ValueError(
                f"data_sensitivity must be in [0, 1], got {self.data_sensitivity}"
            )


@dataclass
class SchemaDefinition:
    namespace: str
    tables: dict[str, TableSchema]
    version: str = "1.0.0"
    description: str | None = None

    # Class-level service instance for validation
    _validator = SchemaValidator()

    def get_table(self, table_name: str) -> TableSchema | None:
        return self.tables.get(table_name)

    def add_table(self, table_schema: TableSchema) -> None:
        self.tables[table_schema.table_name] = table_schema

    def remove_table(self, table_name: str) -> TableSchema | None:
        return self.tables.pop(table_name, None)

    def validate(self) -> list[str]:
        """
        Validate schema definition and return list of validation errors.

        REFACTORED: Delegated to SchemaValidator service - reduced from B(8) to A(1) complexity
        """
        return self._validator.validate_schema(
            namespace=self.namespace,
            tables=self.tables,
            require_primary_keys=False,  # Allow plugin schemas without primary keys
        )


@dataclass
class SchemaRegistry:
    schemas: dict[str, SchemaDefinition] = field(default_factory=dict)

    def register(self, schema: SchemaDefinition) -> None:
        self.schemas[schema.namespace] = schema

    def get(self, namespace: str) -> SchemaDefinition | None:
        return self.schemas.get(namespace)

    def unregister(self, namespace: str) -> SchemaDefinition | None:
        return self.schemas.pop(namespace, None)

    def list_namespaces(self) -> list[str]:
        return list(self.schemas.keys())
