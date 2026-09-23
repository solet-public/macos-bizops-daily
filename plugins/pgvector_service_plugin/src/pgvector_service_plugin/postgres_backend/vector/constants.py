"""Constants for pgvector-backed plugins.

PLUGIN_NAMESPACE is intentionally absent — each plugin defines its own.
"""

from enum import StrEnum
from typing import Final

from ananta.core.domain.constants import (
    KEY_ACTION_STATUS as KEY_ACTION_STATUS,
)
from ananta.core.domain.constants import (
    KEY_COUNT as KEY_COUNT,
)
from ananta.core.domain.constants import (
    KEY_DATA as KEY_DATA,
)
from ananta.core.domain.constants import (
    KEY_ERROR as KEY_ERROR,
)
from ananta.core.domain.constants import (
    KEY_NAMESPACES as KEY_NAMESPACES,
)
from ananta.core.domain.constants import (
    KEY_NAMESPACES_SEARCHED as KEY_NAMESPACES_SEARCHED,
)
from ananta.core.domain.constants import (
    KEY_QUERY as KEY_QUERY,
)
from ananta.core.domain.constants import (
    KEY_RESULT as KEY_RESULT,
)
from ananta.core.domain.constants import (
    KEY_RESULTS as KEY_RESULTS,
)
from ananta.core.domain.constants import (
    STATUS_COMPLETED as STATUS_COMPLETED,
)
from ananta.core.domain.constants import (
    STATUS_ERROR as STATUS_ERROR,
)
from ananta.core.domain.constants import (
    TABLE_SUFFIX as TABLE_SUFFIX,
)
from ananta.core.domain.enums import DistanceMetric as DistanceMetric
from ananta.core.domain.enums import InputType as InputType


class DistanceOperator(StrEnum):
    """PostgreSQL pgvector distance operators."""

    COSINE = "<=>"
    EUCLIDEAN = "<->"
    DOT_PRODUCT = "<#>"


TABLE_EMBEDDINGS: Final[str] = "embeddings"

COLUMN_ID: Final[str] = "id"
COLUMN_NAMESPACE: Final[str] = "namespace"
COLUMN_CREATED_AT: Final[str] = "created_at"
COLUMN_UPDATED_AT: Final[str] = "updated_at"
COLUMN_CREATED_BY: Final[str] = "created_by"
COLUMN_UPDATED_BY: Final[str] = "updated_by"
COLUMN_NAME: Final[str] = "name"
COLUMN_IS_DELETED: Final[str] = "is_deleted"
COLUMN_EXTERNAL_ID: Final[str] = "external_id"

COLUMN_EMBEDDING: Final[str] = "embedding"
COLUMN_DIMENSION: Final[str] = "dimension"
COLUMN_METADATA: Final[str] = "metadata"
COLUMN_DISTANCE_METRIC: Final[str] = "distance_metric"
COLUMN_DISTANCE: Final[str] = "distance"

KEY_RECORDS: Final[str] = "records"
KEY_ROWS: Final[str] = "rows"
KEY_GENERATED_IDS: Final[str] = "generated_ids"
KEY_DELETED: Final[str] = "deleted"
KEY_UPDATED: Final[str] = "updated"
KEY_MISSING: Final[str] = "missing"

KEY_TABLE: Final[str] = "table"
KEY_FILTERS: Final[str] = "filters"
KEY_LIMIT: Final[str] = "limit"
KEY_SOFT_DELETE: Final[str] = "soft_delete"
KEY_UPDATES: Final[str] = "updates"

KEY_INSERTED_IDS: Final[str] = "inserted_ids"
KEY_DELETED_COUNT: Final[str] = "deleted_count"
KEY_VECTOR: Final[str] = "vector"
KEY_EMBEDDINGS: Final[str] = "embeddings"
KEY_VECTOR_COUNT: Final[str] = "vector_count"
KEY_DIMENSIONS: Final[str] = "dimensions"
KEY_OLDEST_CREATED: Final[str] = "oldest_created"
KEY_NEWEST_CREATED: Final[str] = "newest_created"

KEY_DIMENSION: Final[str] = "dimension"
KEY_DISTANCE_METRIC: Final[str] = "distance_metric"
KEY_METADATA: Final[str] = "metadata"

SQL_EMPTY_STRING: Final[str] = ""
SQL_UNION_ALL: Final[str] = " UNION ALL "
SQL_AND_METADATA_FILTER: Final[str] = " AND metadata::jsonb @> %s::jsonb"
SQL_ORDER_BY_DISTANCE_LIMIT: Final[str] = " ORDER BY distance LIMIT %s"
SQL_WILDCARD_PREFIX: Final[str] = "%"
SQL_WILDCARD_SUFFIX: Final[str] = "%"

DEFAULT_DISTANCE_METRIC: Final[DistanceMetric] = DistanceMetric.COSINE
DEFAULT_INPUT_TYPE: Final[InputType] = InputType.TEXT

DELETED_FLAG_ACTIVE: Final[int] = 0
DELETED_FLAG_DELETED: Final[int] = 1

INDEX_FIRST_EMBEDDING: Final[int] = 0

DISTANCE_METRIC_TO_OPERATOR: Final[dict[DistanceMetric, DistanceOperator]] = {
    DistanceMetric.COSINE: DistanceOperator.COSINE,
    DistanceMetric.EUCLIDEAN: DistanceOperator.EUCLIDEAN,
    DistanceMetric.DOT_PRODUCT: DistanceOperator.DOT_PRODUCT,
}
