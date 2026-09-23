"""PGVector provider for vector storage and similarity search.

Pool creation is decoupled from this class via the ``pool_builder`` constructor
parameter.  The local plugin passes ``make_local_pool``; the RDS plugin passes
its own builder that wires in SSL and Secrets Manager credentials.  The provider
never branches on environment — it only calls ``pool_builder(config)``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Final, cast

import psycopg
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.state_service.read_bounds import MAX_READ_ROWS
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import PGVectorConfig
from .constants import (
    COLUMN_CREATED_AT,
    COLUMN_CREATED_BY,
    COLUMN_DIMENSION,
    COLUMN_DISTANCE_METRIC,
    COLUMN_EMBEDDING,
    COLUMN_EXTERNAL_ID,
    COLUMN_ID,
    COLUMN_IS_DELETED,
    COLUMN_METADATA,
    COLUMN_NAME,
    COLUMN_NAMESPACE,
    COLUMN_UPDATED_AT,
    COLUMN_UPDATED_BY,
    DEFAULT_DISTANCE_METRIC,
    DELETED_FLAG_ACTIVE,
    KEY_ACTION_STATUS,
    KEY_COUNT,
    KEY_DATA,
    KEY_DELETED,
    KEY_DELETED_COUNT,
    KEY_DIMENSIONS,
    KEY_ERROR,
    KEY_FILTERS,
    KEY_GENERATED_IDS,
    KEY_INSERTED_IDS,
    KEY_LIMIT,
    KEY_MISSING,
    KEY_NAMESPACES,
    KEY_NEWEST_CREATED,
    KEY_OLDEST_CREATED,
    KEY_RECORDS,
    KEY_RESULT,
    KEY_SOFT_DELETE,
    KEY_TABLE,
    KEY_UPDATED,
    KEY_VECTOR,
    KEY_VECTOR_COUNT,
    STATUS_COMPLETED,
    TABLE_EMBEDDINGS,
    TABLE_SUFFIX,
    DistanceMetric,
)
from .search import run_similarity_search
from .utils import qualified_table

logger = logging.getLogger(__name__)

# Membership reads are chunked to this many candidate ids per query. DERIVED
# from the state service's own row cap rather than hard-coded, so the two can
# never drift apart: a chunk read filters on at most this many external_ids and
# therefore can never return more rows than the cap allows.
_EXTERNAL_ID_CHUNK: Final[int] = MAX_READ_ROWS


def make_local_pool(config: PGVectorConfig) -> ConnectionPool:  # type: ignore[type-arg]
    """Build a plain local connection pool with pgvector type adapters registered.

    Pass this as the ``pool_builder`` argument when constructing
    :class:`PGVectorProvider` for a local (non-RDS) Postgres database.
    """
    from pgvector.psycopg import register_vector  # type: ignore[import-not-found]

    conninfo = (
        f"host={config.host} port={config.port} dbname={config.database} "
        f"user={config.user} password={config.password}"
    )
    return ConnectionPool(  # type: ignore[return-value]
        conninfo=conninfo,
        min_size=1,
        max_size=config.pool_size,
        timeout=30,
        configure=register_vector,
    )


class PGVectorProvider:
    """pgvector CRUD and similarity-search provider; pool creation injected via pool_builder."""

    def __init__(
        self,
        config: PGVectorConfig,
        plugin_namespace: str,
        *,
        pool_builder: Callable[[PGVectorConfig], ConnectionPool],  # type: ignore[type-arg]
        state_service: StateServiceProtocol,
    ) -> None:
        self.config = config
        self._plugin_namespace = plugin_namespace
        self._pool_builder = pool_builder
        self._state_service = state_service
        self._pool: ConnectionPool | None = None  # type: ignore[type-arg]
        self._initialized = False

    def initialize(self) -> None:
        """Initialize connection pool and register pgvector types.

        Raises:
            RuntimeError: If initialization fails.
        """
        if self._initialized:
            return

        try:
            self._pool = self._pool_builder(self.config)

            with self._pool.connection() as conn:
                conn.execute("SELECT 1")

            self._initialized = True
            logger.debug(
                "PGVectorProvider initialized: %s:%s/%s",
                self.config.host, self.config.port, self.config.database,
            )

        except Exception as e:
            error_msg = f"Failed to initialize PGVectorProvider: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    @contextmanager
    def _get_connection(self) -> Generator[psycopg.Connection[Any]]:
        if not self._pool:
            raise RuntimeError("Provider not initialized")

        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def store_vectors(self, namespace: str, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        """Store vectors with metadata using StateService."""
        if not vectors:
            raise ValueError("No vectors to store")

        try:
            new_dim = self._validate_batch_dimensions(vectors)
            self._check_dimension_compatibility(namespace, new_dim)
            records_to_insert = self._prepare_vector_records(vectors)
            return self._execute_vector_insert(namespace, records_to_insert)
        except Exception as e:
            logger.error("Failed to store vectors: %s", e)
            raise RuntimeError(f"Vector storage failed: {e}") from e

    def _validate_batch_dimensions(self, vectors: list[dict[str, Any]]) -> int:
        dimensions = {v[COLUMN_DIMENSION] for v in vectors}
        if len(dimensions) > 1:
            raise ValueError(f"Inconsistent dimensions in batch: {dimensions}")
        return int(list(dimensions)[0])

    def _check_dimension_compatibility(self, namespace: str, new_dim: int) -> None:
        existing_check_result = self._state_service.read_state(
            namespace=namespace, query={KEY_TABLE: TABLE_EMBEDDINGS, KEY_LIMIT: 1}
        )
        if existing_check_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            return
        data = cast(dict[str, Any], existing_check_result.get(KEY_DATA, {}))
        records = cast(list[dict[str, Any]], data.get(KEY_RECORDS, []))
        if records:
            existing_dim = records[0].get(COLUMN_DIMENSION)
            if existing_dim != new_dim:
                raise ValueError(
                    f"Dimension mismatch: namespace has {existing_dim}, trying to insert {new_dim}"
                )

    def _prepare_vector_records(self, vectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records_to_insert = []
        for vector_data in vectors:
            metadata_json = json.dumps(vector_data.get(COLUMN_METADATA, {}))
            distance_metric = vector_data.get(COLUMN_DISTANCE_METRIC, DEFAULT_DISTANCE_METRIC)
            record: dict[str, Any] = {
                COLUMN_EMBEDDING: vector_data[KEY_VECTOR],
                COLUMN_DIMENSION: vector_data[COLUMN_DIMENSION],
                COLUMN_METADATA: metadata_json,
                COLUMN_DISTANCE_METRIC: distance_metric,
            }
            if COLUMN_EXTERNAL_ID in vector_data:
                record[COLUMN_EXTERNAL_ID] = vector_data[COLUMN_EXTERNAL_ID]
            if COLUMN_ID in vector_data:
                record[COLUMN_ID] = vector_data[COLUMN_ID]
            records_to_insert.append(record)
        return records_to_insert

    def _execute_vector_insert(
        self, namespace: str, records: list[dict[str, Any]]
    ) -> dict[str, Any]:
        write_result = self._state_service.write_state(
            namespace=namespace,
            data={KEY_TABLE: TABLE_EMBEDDINGS, KEY_RECORDS: records},
        )
        if write_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            error = write_result.get("error", "Unknown error")
            raise RuntimeError(f"StateService write failed: {error}")
        data = cast(dict[str, Any], write_result.get(KEY_DATA, {}))
        result_data = cast(dict[str, Any], data.get(KEY_RESULT, {}))
        inserted_ids = cast(list[str], result_data.get(KEY_GENERATED_IDS, []))
        return {KEY_INSERTED_IDS: inserted_ids, KEY_COUNT: len(inserted_ids)}


    def get_vector(self, namespace: str, vector_id: str) -> dict[str, Any]:
        """Retrieve a vector by ID using StateService."""
        try:
            read_result = self._state_service.read_state(
                namespace=namespace,
                query={
                    KEY_TABLE: TABLE_EMBEDDINGS,
                    KEY_FILTERS: {
                        COLUMN_ID: vector_id,
                        COLUMN_IS_DELETED: DELETED_FLAG_ACTIVE,
                    },
                    KEY_LIMIT: 1,
                },
            )

            if read_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
                error = read_result.get("error", "Unknown error")
                raise RuntimeError(f"StateService read failed: {error}")

            data = cast(dict[str, Any], read_result.get(KEY_DATA, {}))
            records = cast(list[dict[str, Any]], data.get(KEY_RECORDS, []))
            if not records:
                raise ValueError(f"Vector not found: {vector_id} in namespace {namespace}")

            row = records[0]
            metadata = json.loads(row[COLUMN_METADATA]) if row.get(COLUMN_METADATA) else {}

            result: dict[str, Any] = {
                COLUMN_ID: row[COLUMN_ID],
                COLUMN_NAMESPACE: row[COLUMN_NAMESPACE],
                KEY_VECTOR: row[COLUMN_EMBEDDING],
                COLUMN_DIMENSION: row[COLUMN_DIMENSION],
                COLUMN_METADATA: metadata,
                COLUMN_DISTANCE_METRIC: row.get(COLUMN_DISTANCE_METRIC),
                COLUMN_CREATED_AT: row.get(COLUMN_CREATED_AT),
                COLUMN_UPDATED_AT: row.get(COLUMN_UPDATED_AT),
            }
            for key in [COLUMN_EXTERNAL_ID, COLUMN_CREATED_BY, COLUMN_UPDATED_BY, COLUMN_NAME]:
                if key in row:
                    result[key] = row[key]

            return result

        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to get vector: %s", e)
            raise RuntimeError(f"Vector retrieval failed: {e}") from e


    def delete_vectors(
        self,
        namespace: str,
        vector_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Delete vectors by IDs using StateService."""
        if vector_ids is None and filters is None:
            return {KEY_DELETED_COUNT: 0}

        if vector_ids is None:
            raise NotImplementedError("Deletion by filters not yet implemented")

        if not vector_ids:
            return {KEY_DELETED_COUNT: 0}

        try:
            deleted_count = 0
            for vector_id in vector_ids:
                delete_result = self._state_service.delete_records(
                    namespace=namespace,
                    query={
                        KEY_TABLE: TABLE_EMBEDDINGS,
                        KEY_FILTERS: {COLUMN_ID: vector_id},
                        KEY_SOFT_DELETE: True,
                    },
                )
                if delete_result.get(KEY_ACTION_STATUS) == STATUS_COMPLETED:
                    data = cast(dict[str, Any], delete_result.get(KEY_DATA, {}))
                    result_data = cast(dict[str, Any], data.get(KEY_RESULT, {}))
                    deleted_count += cast(int, result_data.get(KEY_DELETED, 0))

            return {KEY_DELETED_COUNT: deleted_count}

        except Exception as e:
            logger.error("Failed to delete vectors: %s", e)
            raise RuntimeError(f"Vector deletion failed: {e}") from e

    def delete_all_in_namespace(self, namespace: str) -> dict[str, Any]:
        """Hard-delete every row in {schema}.{namespace}{TABLE_SUFFIX}."""
        table_name = qualified_table(
            self.config.schema_name, f"{namespace}{TABLE_SUFFIX}"
        )
        sql_result = self._state_service.execute_sql(
            sql_query=f"DELETE FROM {table_name}",
            sql_params=None,
        )
        if sql_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            raise RuntimeError(
                f"delete_all_in_namespace({namespace}) failed: "
                f"{sql_result.get(KEY_ERROR)}"
            )
        data = cast(dict[str, Any], sql_result.get(KEY_DATA, {}))
        deleted = cast(int, data.get("rowcount", data.get(KEY_DELETED_COUNT, 0)))
        return {KEY_DELETED_COUNT: deleted}

    def delete_by_external_ids(
        self,
        namespace: str,
        external_ids: list[str],
    ) -> dict[str, Any]:
        """Delete vectors by external_id."""
        if not external_ids:
            return {KEY_DELETED_COUNT: 0}

        try:
            table_name = qualified_table(
                self.config.schema_name, f"{namespace}{TABLE_SUFFIX}"
            )
            placeholders = ", ".join(["%s"] * len(external_ids))
            query = (
                f"SELECT {COLUMN_ID} FROM {table_name} "
                f"WHERE {COLUMN_EXTERNAL_ID} IN ({placeholders}) "
                f"AND ({COLUMN_IS_DELETED} IS NULL OR {COLUMN_IS_DELETED} = {DELETED_FLAG_ACTIVE})"
            )

            sql_result = self._state_service.execute_sql(
                sql_query=query,
                sql_params=list(external_ids),
            )

            if sql_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
                raise RuntimeError(f"Failed to lookup vectors: {sql_result.get(KEY_ERROR)}")

            data = cast(dict[str, Any], sql_result.get(KEY_DATA, {}))
            records = cast(list[list[object]], data.get(KEY_RECORDS, []))

            if not records:
                return {KEY_DELETED_COUNT: 0}

            vector_ids = [str(row[0]) for row in records]
            return self.delete_vectors(namespace=namespace, vector_ids=vector_ids)

        except Exception as e:
            logger.error("Failed to delete by external_ids: %s", e)
            raise RuntimeError(f"Delete by external_ids failed: {e}") from e

    def find_missing_external_ids(
        self,
        namespace: str,
        candidate_external_ids: list[str],
    ) -> dict[str, Any]:
        """Return the subset of ``candidate_external_ids`` with no ACTIVE vector.

        A single ``external_id = ANY(candidates)`` read over the namespace's
        embeddings table (the ``is_deleted = 0`` active filter mirrors
        ``get_vector``), then a Python set-difference: every candidate whose
        ``external_id`` is absent from the present set is "missing". A
        soft-deleted row is excluded by the active filter, so it counts as
        missing — matching the orphan-reconcile intent (a soft-deleted vector
        is effectively gone).

        Uses the ``read_state`` primitive (no raw SQL). TRIPWIRE: the
        list-valued ``external_id`` filter relies on the Postgres ``= ANY``
        translation in the state provider's filter grammar; a future in-memory
        state backing (scalar ``==`` only) would silently break the membership
        set. ``is_deleted`` is a ``DEFAULT 0`` column the state write path never
        sets NULL, so the strict ``= 0`` match is faithful (the same invariant
        ``get_vector`` relies on). Not exposed as an AI-callable action.
        """
        if not candidate_external_ids:
            return {KEY_MISSING: []}

        # CHUNKED (D9, 2026-08-15): the membership read is bounded by the
        # CALLER's list length, and that caller is the orphan reconcile, which
        # passes every active memory id — 42,500 on this deployment. One read
        # of that shape exceeds the read_state row cap and is refused, which
        # killed a green boot at `reindex_orphaned_memories`. Chunking makes
        # each read bounded BY CONSTRUCTION (a chunk can match at most its own
        # length), so this needs no `unbounded` opt-in and never depends on how
        # large the caller's list happens to get.
        present: set[str] = set()
        for start in range(0, len(candidate_external_ids), _EXTERNAL_ID_CHUNK):
            chunk = candidate_external_ids[start : start + _EXTERNAL_ID_CHUNK]
            read_result = self._state_service.read_state(
                namespace=namespace,
                query={
                    KEY_TABLE: TABLE_EMBEDDINGS,
                    KEY_FILTERS: {
                        COLUMN_EXTERNAL_ID: chunk,
                        COLUMN_IS_DELETED: DELETED_FLAG_ACTIVE,
                    },
                    # Explicit and exact: the filter can match at most one row
                    # per candidate, so this states the true bound rather than
                    # relying on the provider's default.
                    KEY_LIMIT: len(chunk),
                },
            )
            if read_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
                error = read_result.get(KEY_ERROR, "Unknown error")
                raise RuntimeError(f"StateService read failed: {error}")

            data = cast(dict[str, Any], read_result.get(KEY_DATA, {}))
            records = cast(list[dict[str, Any]], data.get(KEY_RECORDS, []))
            present.update(str(row.get(COLUMN_EXTERNAL_ID)) for row in records)

        return {
            KEY_MISSING: [
                eid
                for eid in dict.fromkeys(candidate_external_ids)
                if str(eid) not in present
            ]
        }

    def update_metadata(
        self, namespace: str, vector_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """Update vector metadata using StateService."""
        try:
            metadata_json = json.dumps(metadata)
            update_result = self._state_service.update_state(
                namespace=namespace,
                query={
                    KEY_TABLE: TABLE_EMBEDDINGS,
                    KEY_FILTERS: {
                        COLUMN_ID: vector_id,
                        COLUMN_IS_DELETED: DELETED_FLAG_ACTIVE,
                    },
                },
                updates={COLUMN_METADATA: metadata_json},
            )

            if update_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
                error = update_result.get("error", "Unknown error")
                raise RuntimeError(f"StateService update failed: {error}")

            data = cast(dict[str, Any], update_result.get(KEY_DATA, {}))
            result_data = cast(dict[str, Any], data.get(KEY_RESULT, {}))
            updated_count = cast(int, result_data.get(KEY_UPDATED, 0))

            if updated_count == 0:
                raise ValueError(f"Vector not found: {vector_id} in namespace {namespace}")

            return {KEY_UPDATED: True}

        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to update metadata: %s", e)
            raise RuntimeError(f"Metadata update failed: {e}") from e


    def search_similar(
        self,
        namespaces: list[str],
        query_vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
        distance_metric: DistanceMetric = DEFAULT_DISTANCE_METRIC,
    ) -> dict[str, Any]:
        """Search for similar vectors (delegates to the extracted search module)."""
        return run_similarity_search(
            self._state_service,
            self.config.schema_name,
            namespaces,
            query_vector,
            top_k,
            filters,
            distance_metric,
        )


    def list_namespaces(self) -> dict[str, Any]:
        """Return the plugin namespace this provider serves."""
        return {KEY_NAMESPACES: [self._plugin_namespace]}

    def get_namespace_stats(self, namespace: str) -> dict[str, Any]:
        """Get statistics for a namespace via a direct pool connection."""
        try:
            with self._get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    sql.SQL("""
                        SELECT
                            COUNT(*) as vector_count,
                            ARRAY_AGG(DISTINCT dimension) as dimensions,
                            MIN(created_at) as oldest_created,
                            MAX(created_at) as newest_created
                        FROM {}
                        WHERE (is_deleted IS NULL OR is_deleted = %s)
                    """).format(
                        sql.Identifier(self.config.schema_name, f"{namespace}{TABLE_SUFFIX}")
                    ),
                    (DELETED_FLAG_ACTIVE,),
                )
                row = cur.fetchone()

                if not row or row[KEY_VECTOR_COUNT] == 0:
                    raise ValueError(f"Namespace is empty: {namespace}")

                return {
                    KEY_VECTOR_COUNT: row[KEY_VECTOR_COUNT],
                    KEY_DIMENSIONS: row[KEY_DIMENSIONS],
                    KEY_OLDEST_CREATED: (
                        row[KEY_OLDEST_CREATED].isoformat() if row[KEY_OLDEST_CREATED] else None
                    ),
                    KEY_NEWEST_CREATED: (
                        row[KEY_NEWEST_CREATED].isoformat() if row[KEY_NEWEST_CREATED] else None
                    ),
                }

        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to get namespace stats: %s", e)
            raise RuntimeError(f"Namespace stats retrieval failed: {e}") from e


    def close(self) -> None:
        """Close connection pool and release resources."""
        if self._pool:
            self._pool.close()
            self._pool = None
            self._initialized = False
            logger.debug("PGVectorProvider closed")
