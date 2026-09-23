import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, cast, runtime_checkable

from ananta.config.schema_factory import get_standardized_schema
from ananta.core.domain.constants import (
    DEFAULT_SEARCH_LIMIT,
    KEY_ACTION_STATUS,
    KEY_COUNT,
    KEY_DATA,
    KEY_ERROR,
    KEY_NAMESPACES,
    KEY_NAMESPACES_SEARCHED,
    KEY_QUERY,
    KEY_RESULT,
    KEY_RESULTS,
    STATUS_COMPLETED,
    STATUS_ERROR,
)
from ananta.core.domain.enums import ErrorSeverity, InputType
from ananta.core.domain.types import ActionResult
from ananta.error_handling import FrameworkError
from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.interfaces.vector_service_interface import VectorServiceInterface
from ananta.services.discovery_service.confidence_engine import (
    ConfidenceThresholds,
    DiscoveryConfidence,
    DiscoveryConfidenceEngine,
    ProcessScore,
)
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema

logger = logging.getLogger(__name__)

# ============================================================================
# DISCOVERY SERVICE CONSTANTS (no magic strings)
# ============================================================================

# Metadata keys for discovery results
KEY_PROCESS_KEY = "process_key"
KEY_DESCRIPTION = "description"
KEY_REQUIRED_PARAMETERS = "required_parameters"
KEY_REQUIRED_PARAMETER_COUNT = "required_parameter_count"
KEY_INVOCATION_SCHEMA = "invocation_schema"

# Garbage cutoff threshold: return no_matches for low-confidence results below this score
# Prevents garbage results from polluting prompts (e.g., "go ahead!" matching random processes)
# See: knowledge_base/2026-01-13_bad_example_prevention_implementation.md
GARBAGE_CUTOFF_THRESHOLD = 0.55
KEY_PARAMETERS = "parameters"

# Parameter metadata keys
KEY_REQUIRED = "required"
KEY_TYPE = "type"
KEY_NAME = "name"
KEY_DEFAULT = "default"

# Processes defined in system prompt - always available, never need discovery
# These are excluded from discovery results to avoid redundant descriptions
# Note: IO plugin post_message is injected dynamically and excluded via
# the include_in_system_prompt flag, not this set.
SYSTEM_PROMPT_PROCESSES: frozenset[str] = frozenset({
    "service_interface::discovery_service::query_process_registry",
    "service_interface::discovery_service::get_process_schema",
})


# Static schema definition for discovery_processes namespace
# This is registered during startup (step 7) before DiscoveryService is instantiated (step 10)
def get_discovery_schema_definitions(embedding_dimensions: int) -> list[SchemaDefinition]:
    """Get schema definitions for discovery service vector storage.

    This function is called during schema initialization (startup step 7) to create
    the discovery_processes namespace and its __embeddings table. The table structure
    matches what the pgvector plugin expects when storing discovery process vectors.

    Returns:
        List containing schema definition for discovery_processes namespace
    """
    embeddings_table = TableSchema(
        table_name="embeddings",  # Will become discovery_processes__embeddings
        id_prefix="dpe",  # discovery_processes_embeddings
        columns={
            "embedding": ColumnDefinition(
                type=ColumnType.VECTOR,
                type_params={"dimension": embedding_dimensions},
                not_null=True,
                description="Vector embedding for semantic process search",
            ),
            "dimension": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description="Vector dimension for validation",
            ),
            "metadata": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description="Process metadata (JSON) for filtering and display",
            ),
            "distance_metric": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description="Distance metric used for similarity search",
            ),
            # NOTE: external_id is NOT defined here - uses platform standard which has unique=True
            # The process_key is stored in external_id via _store_vector()
        },
        description="Vector embeddings for semantic discovery of processes",
    )

    return [
        SchemaDefinition(
            namespace="discovery_processes",
            tables={"embeddings": embeddings_table},
            description="Discovery service vector storage for process semantic search",
        )
    ]


@runtime_checkable
class ToDictProtocol(Protocol):
    """Protocol for objects with a to_dict method."""

    def to_dict(self) -> dict[str, object]: ...


class MatchType(Enum):
    TEXT_WITH_USAGE_BOOST = "text_with_usage_boost"
    UTILITY_BASED = "utility_based"


@dataclass
class ProcessMatch:
    process_key: str
    score: float
    match_type: str
    matched_fields: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    processes: list[ProcessMatch]
    query: str
    match_type: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, object]:
        def parse_process_key(key: str) -> dict[str, str]:
            """Parse process_key into provider_type, provider, function_name."""
            parts = key.split("::")
            if len(parts) == 3:
                return {
                    "provider_type": parts[0],
                    "provider": parts[1],
                    "function_name": parts[2],
                }
            return {"provider_type": "", "provider": "", "function_name": ""}

        # Build process list with full invocation schema for direct execution
        # - process_key: Unique identifier
        # - provider_type/provider/function_name: For constructing the action
        # - description: For understanding what the process does
        # - invocation_schema: Full JSON schema for constructing valid arguments
        processes = []
        for p in self.processes:
            process_entry: dict[str, object] = {
                "process_key": p.process_key,
                **parse_process_key(p.process_key),
                "description": p.metadata.get("description", ""),
            }
            # Include full invocation_schema for direct execution
            invocation_schema = p.metadata.get("invocation_schema", {})
            if invocation_schema:
                process_entry["invocation_schema"] = invocation_schema
            # Include is_long_running flag
            if p.metadata.get("is_long_running"):
                process_entry["is_long_running"] = True
            processes.append(process_entry)

        return {
            "success": True,
            "process_keys": [p.process_key for p in self.processes],
            "process_count": len(self.processes),
            "processes": processes,
            "query": self.query,
            "match_type": self.match_type,
            "timestamp": self.timestamp,
            "action_status": "completed",
        }


@dataclass
class UsageStats:
    total_executions: int
    last_used: str


@dataclass
class UsagePatterns:
    follows_sequences: list[dict[str, object]]
    followed_by_sequences: list[dict[str, object]]


@dataclass
class ServiceHealth:
    is_healthy: bool
    index_last_built: str
    total_processes: int
    total_usage_records: int
    errors: list[str] = field(default_factory=list)


class DiscoveryServiceError(FrameworkError):
    pass


class DiscoveryIndexCorruptedError(DiscoveryServiceError):
    def __init__(self, message: str = "Search index corrupted, rebuild required"):
        super().__init__(
            message=message, error_code="discovery.index.corrupted", severity=ErrorSeverity.CRITICAL
        )


class DiscoveryServiceUnavailableError(DiscoveryServiceError):
    def __init__(self, message: str = "Discovery service unavailable"):
        super().__init__(
            message=message,
            error_code="discovery.service.unavailable",
            severity=ErrorSeverity.CRITICAL,
        )


class DiscoveryService:
    """Discovery service for process registry search and semantic matching."""

    def __init__(
        self,
        app_home: str,
        state_service: StateServiceProtocol,
        plugin_manager: object | None = None,
        process_registry: dict[str, object] | None = None,
        embedding_service: EmbeddingServiceInterface | object | None = None,
        vector_service: VectorServiceInterface | object | None = None,
        confidence_thresholds: ConfidenceThresholds | None = None,
        min_similarity_threshold: float | None = None,
    ):
        self.app_home = app_home
        self.state_service = state_service
        self.plugin_manager = plugin_manager
        # Store as object, cast in _get_*_service methods
        self.embedding_service: EmbeddingServiceInterface | object | None = embedding_service
        self.vector_service: VectorServiceInterface | object | None = vector_service

        # Minimum similarity threshold for vector search results.
        # Results below this threshold are considered "no match" and not returned.
        # Cosine similarity: 1.0 = identical, 0.0 = orthogonal, -1.0 = opposite
        self._min_similarity_threshold = min_similarity_threshold

        self.namespace = "core"
        self.vector_namespace = "discovery_processes"

        # Process storage for deterministic internal lookups
        self.processes: dict[str, dict[str, object]] = {}

        # Confidence engine for intelligent result filtering
        self.confidence_engine = DiscoveryConfidenceEngine(confidence_thresholds)

        # Ensure required tables exist
        self._ensure_usage_stats_table()

        if process_registry:
            self._load_processes(process_registry)

    def _ensure_usage_stats_table(self) -> None:
        try:
            logger.debug(
                "🔧 USAGE_TABLE_001: Creating usage_stats table with proper schema constraints"
            )

            # Use standardized schema from SchemaFactory which includes proper system fields
            usage_stats_schema = get_standardized_schema("usage_stats")

            # Convert TableSchema objects to dictionary format expected by state service
            schema: dict[str, object] = {"tables": {}}
            for table_name, table_schema in usage_stats_schema.tables.items():
                column_definitions: dict[str, str] = {}
                for col_name, col_def in table_schema.columns.items():
                    # Use the ColumnDefinition.to_sql() method to generate complete SQL with constraints
                    full_sql_definition = col_def.to_sql(col_name)

                    # Extract just the type and constraints part (without column name)
                    parts = full_sql_definition.split(" ", 1)
                    if len(parts) > 1:
                        column_sql = parts[1]  # Everything after column name
                    else:
                        column_sql = col_def.type.value  # Fallback to just type

                    column_definitions[col_name] = column_sql

                # Build table definition with id_prefix and columns
                table_def: dict[str, object] = {
                    "id_prefix": table_schema.id_prefix,
                    "columns": column_definitions,
                }
                schema["tables"] = {table_name: table_def}

            # Create schema with proper unique constraint on process_key
            result = self.state_service.create_schema(
                namespace=self.namespace,
                schema=schema,
            )

            if result.get("action_status") == "completed":
                logger.debug(
                    "🔧 USAGE_TABLE_002: usage_stats table created successfully with unique constraints"
                )
            else:
                logger.debug(f"🔧 USAGE_TABLE_003: Table creation result: {result}")

        except Exception:
            # Table might already exist - this is fine, fail gracefully
            pass

    # ===== VECTOR SERVICE ACCESS =====

    def _get_embedding_service(self) -> EmbeddingServiceInterface | object | None:
        """Get embedding service instance.

        Returns the embedding_service passed in constructor.
        No fallback - embedding_service must be configured via constructor.
        """
        return self.embedding_service

    def _get_vector_service(self) -> VectorServiceInterface | object | None:
        """Get vector service instance.

        Returns the vector_service passed in constructor.
        No fallback - vector_service must be configured via constructor.
        """
        return self.vector_service

    def _generate_process_embedding_text(
        self, process_key: str, process_data: dict[str, object]
    ) -> str:
        """Generate searchable text from process metadata for embedding.

        Uses 'clustering:' prefix for nomic-embed-text-v1.5 instruction tuning.
        NOTE: This prefix is model-specific - remove when changing embedding models.

        Two-description architecture:
        - embedding_description (200-400 chars): Keyword-dense for semantic matching
        - description (350-800 chars): Technical explanation for LLM usage

        We use embedding_description for search if available, falling back to description
        during migration. NO FALLBACK after migration completes.
        """
        parts: list[str] = []
        parts.append(process_key)

        if name := process_data.get("name"):
            parts.append(str(name))

        # Prefer embedding_description for search (keyword-dense, optimized for semantic matching)
        # Fall back to description only during migration period
        if embedding_desc := process_data.get("embedding_description"):
            parts.append(str(embedding_desc))
        elif description := process_data.get("description"):
            # Fallback for migration - remove after all processes have embedding_description
            parts.append(str(description))

        text = " ".join(parts)
        return f"clustering: {text}"

    # ===== INTERNAL DETERMINISTIC INTERFACE =====
    # These methods provide deterministic process storage/lookup for framework internal use
    # Used by: orchestrator, action_manager, and other framework components

    def clear_process_vectors(self) -> None:
        """Clear all process embeddings from vector store.

        Should be called BEFORE loading processes to ensure clean slate.
        Process embeddings are transient - they should be rebuilt from
        the current process registry on each startup.

        This is a public method so ProcessRegistryManager can call it
        before the process loading loop.
        """
        self._clear_process_vectors()

    def store_process(self, process_key: str, process_data: dict[str, object]) -> None:
        """Store process in local dict and vector store for semantic search.

        Non-discoverable processes are stored in local dict but NOT in vector store.
        This allows internal processes to be invoked directly but not appear in
        semantic discovery results.

        Processes with include_in_system_prompt=True are also excluded from vector
        storage since they're always available in the system prompt and would
        otherwise crowd out niche tools in discovery results.
        """
        self.processes[process_key] = process_data.copy()

        # Only store vectors for discoverable processes
        is_discoverable = process_data.get("is_discoverable", True)
        if not is_discoverable:
            logger.debug(f"Skipping vector storage for non-discoverable process: {process_key}")
            return

        # Skip vector storage for system prompt processes (always available, no need for discovery)
        include_in_system_prompt = process_data.get("include_in_system_prompt", False)
        if include_in_system_prompt:
            logger.debug(f"Skipping vector storage for system prompt process: {process_key}")
            return

        self._store_process_vector(process_key, process_data)

    def _store_process_vector(self, process_key: str, process_data: dict[str, object]) -> None:
        """Generate and store vector embedding for semantic search."""
        try:
            embedding_service = self._get_embedding_service()
            vector_service = self._get_vector_service()

            if embedding_service is None or vector_service is None:
                return

            # Cast to interface types - duck typing ensures compatibility
            typed_embedding_service = cast(EmbeddingServiceInterface, embedding_service)
            typed_vector_service = cast(VectorServiceInterface, vector_service)

            embeddings, dimension = self._generate_embedding(
                typed_embedding_service, process_key, process_data
            )
            if embeddings is None:
                return

            self._store_vector(
                typed_vector_service, process_key, process_data, embeddings, dimension
            )

        except Exception as e:
            logger.error(f"Error storing vector for {process_key}: {e}")

    def _generate_embedding(
        self,
        embedding_service: EmbeddingServiceInterface,
        process_key: str,
        process_data: dict[str, object],
    ) -> tuple[list[object] | None, int]:
        """Generate embedding for process."""
        text = self._generate_process_embedding_text(process_key, process_data)
        result = embedding_service.generate_embeddings(
            inputs=[text], input_type=InputType.TEXT.value
        )

        if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            logger.error(f"Failed to generate embedding for {process_key}: {result.get(KEY_ERROR)}")
            return None, 0

        extracted = self._extract_embedding_data(result, process_key)
        return extracted

    def _extract_embedding_data(
        self, result: ActionResult, process_key: str
    ) -> tuple[list[object] | None, int]:
        """Extract embeddings and dimension from result."""
        data_obj = result.get(KEY_DATA)
        if not isinstance(data_obj, dict):
            logger.error(f"Invalid data structure for {process_key}")
            return None, 0

        result_obj = data_obj.get(KEY_RESULT)
        if not isinstance(result_obj, dict):
            logger.error(f"Invalid result structure for {process_key}")
            return None, 0

        embeddings_obj = result_obj.get("embeddings")
        dimension_obj = result_obj.get("dimension")
        if not isinstance(embeddings_obj, list) or not isinstance(dimension_obj, int):
            logger.error(f"Invalid embedding result structure for {process_key}")
            return None, 0

        return embeddings_obj, dimension_obj

    def _store_vector(
        self,
        vector_service: VectorServiceInterface,
        process_key: str,
        process_data: dict[str, object],
        embeddings: list[object],
        dimension: int,
    ) -> None:
        """Store vector in vector service."""
        vector_result = vector_service.store_vectors(
            namespace=self.vector_namespace,
            vectors=[
                {
                    "external_id": process_key,
                    "vector": embeddings[0],
                    "dimension": dimension,
                    "metadata": {
                        "process_key": process_key,
                        "name": process_data.get("name", ""),
                    },
                }
            ],
        )

        if vector_result.get(KEY_ACTION_STATUS) == STATUS_COMPLETED:
            pass
        else:
            logger.error(
                f"Failed to store vector for {process_key}: {vector_result.get(KEY_ERROR)}"
            )

    def _is_valid_process_key_format(self, process_key: str) -> bool:
        """Validate that process_key follows expected format.

        Process keys must be 'provider_type::provider::function_name'.
        This validation prevents the model from using query text as process_key.

        Args:
            process_key: The key to validate

        Returns:
            True if format is valid, False otherwise
        """
        if not process_key:
            return False

        # Must have exactly 2 '::' separators (3 parts)
        parts = process_key.split("::")
        if len(parts) != 3:
            return False

        provider_type, provider, function_name = parts

        # All parts must be non-empty
        if not provider_type or not provider or not function_name:
            return False

        # provider_type must be 'service_interface' or 'plugin'
        if provider_type not in ("service_interface", "plugin"):
            return False

        return True

    def get_process_by_key(self, process_key: str) -> dict[str, object] | None:
        return self.processes.get(process_key)

    def get_process_by_name(self, name: str) -> dict[str, object] | None:
        for process_data in self.processes.values():
            if process_data.get("name") == name:
                return process_data
        return None

    def _serialize_process_metadata(self, process_data: dict[str, object]) -> dict[str, object]:
        """Recursively serialize process metadata to ensure JSON compatibility.

        Converts dataclass objects (ReturnValueSchema, ParameterMetadata, etc.)
        to dicts by calling their to_dict() methods.
        """
        serialized: dict[str, object] = {}

        for key, value in process_data.items():
            if isinstance(value, ToDictProtocol):
                # Object has to_dict method - use it
                serialized[key] = value.to_dict()
            elif isinstance(value, dict):
                # Recursively serialize nested dicts
                serialized[key] = self._serialize_process_metadata(value)
            elif isinstance(value, list):
                # Serialize list items
                serialized[key] = [
                    item.to_dict()
                    if isinstance(item, ToDictProtocol)
                    else self._serialize_process_metadata(item)
                    if isinstance(item, dict)
                    else item
                    for item in value
                ]
            else:
                # Primitive value - keep as is
                serialized[key] = value

        return serialized

    def get_all_processes(self) -> dict[str, dict[str, object]]:
        return self.processes.copy()

    def get_system_prompt_processes(self) -> list[dict[str, object]]:
        """Get all processes that should be included in the system prompt.

        Queries the in-memory process registry for processes where
        include_in_system_prompt=True.

        Returns:
            List of process dicts with process_key, description, and invocation_schema
            for injection into the system prompt "Built-in Processes" block.
        """
        result: list[dict[str, object]] = []

        for process_key, process_data in self.processes.items():
            if not process_data.get("include_in_system_prompt", False):
                continue

            result.append({
                "process_key": process_key,
                "description": process_data.get("description", ""),
                "invocation_schema": process_data.get("invocation_schema", {}),
            })

        logger.debug(f"Found {len(result)} system prompt processes")
        return result

    def get_process_count(self) -> int:
        return len(self.processes)

    def get_process_schemas(self, max_processes: int = 200) -> str:
        """
        Get structured process metadata including parameter schemas.

        Returns JSON array of process metadata with exact parameter_schema for each process.
        This enables LLMs to use correct parameter names without guessing.

        Args:
            max_processes: Maximum number of processes to include (default: 200)

        Returns:
            JSON string with array of {process_key, description, parameter_schema, return_value_schema}
        """
        import json

        if not self.processes:
            return "[]"

        # No tag-based filtering - use all processes
        filtered_processes = self.processes

        if not filtered_processes:
            return "[]"

        # Get usage stats and sort by usage
        process_usage = []
        for process_key in filtered_processes.keys():
            stats = self._get_usage_stats(process_key)
            process_usage.append((process_key, stats.total_executions))

        process_usage.sort(key=lambda x: x[1], reverse=True)
        top_processes = process_usage[:max_processes]

        # Build structured metadata array
        schemas = []
        for process_key, _usage in top_processes:
            process_data = filtered_processes[process_key]
            schema_entry = {
                "process_key": process_key,
                "description": process_data.get("description", ""),
                "invocation_schema": process_data.get("invocation_schema", {}),
            }
            schemas.append(schema_entry)

        return json.dumps(schemas)

    # ===== EXTERNAL NON-DETERMINISTIC INTERFACE =====
    # These methods provide intelligent search and are NON-DETERMINISTIC
    # Used by: user-facing actions via inference providers ONLY

    def query_process_registry(
        self,
        query: str,
        exclude_tags: list[str] | None = None,
        include_tags: list[str] | None = None,
        max_results: int = 10,
        state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Query process registry with semantic search.

        IMPORTANT: This method must NOT run model inference.
        It is called *by* the model inside a ReAct loop. Any disambiguation or
        conversation-context resolution should be done in the model; discovery here
        is a deterministic semantic search over the process registry.

        Args:
            query: Natural language description of what user wants to accomplish
            exclude_tags: DEPRECATED - raises ValueError if provided
            include_tags: DEPRECATED - raises ValueError if provided
            max_results: Maximum number of results to return
            state: Optional runtime state (unused). This may be injected by the
                ActionProcessor for session correlation, but discovery does not
                perform additional inference or require conversation context.

        Note: exclude_tags and include_tags parameters are removed - fail if provided.
        Discovery is now based purely on semantic embeddings (no fallback).
        """
        self._validate_query_params(exclude_tags, include_tags)

        # Handle empty query gracefully — return empty result instead of erroring.
        # Model sometimes calls query_process_registry with empty query from process_results.
        if not query.strip():
            logger.info("🔍 DISCOVERY: empty query — returning direct_response")
            return {
                "action_status": "completed",
                "success": True,
                "match_type": "direct_response",
                "directive": (
                    "No process execution needed. "
                    "Respond directly to the user via post_message."
                ),
                "original_input": query,
            }

        logger.debug(f"🔍 DISCOVERY_INPUT: query='{query[:100]}...'")

        # NOTE: discovery is a semantic search tool; do not perform additional inference here.
        # Even if this service is constructed with an inference_service, query_process_registry
        # must not call it. Nested inference breaks the ReAct loop by hiding tool results behind
        # additional routing decisions.
        search_query = query
        logger.debug(f"🔍 DISCOVERY_SEARCH: query='{search_query[:100]}...'")

        logger.debug(
            f"🔍 DISCOVERY_001: query_process_registry called with query='{search_query}', max_results={max_results}"
        )

        # Vector semantic search - no fallback, fail fast on errors
        embedding_service, vector_service = self._validate_discovery_services()
        query_embedding = self._generate_query_embedding(embedding_service, search_query)
        results_list = self._search_vectors(vector_service, query_embedding, max_results)

        return self._process_vector_results(results_list, search_query, max_results)

    def execute_embeddings_search(
        self,
        query: str,
        original_input: str,
        max_results: int = 10,
        state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Execute vector embeddings search for process discovery.

        Args:
            query: Abstract operation query (e.g., 'generate audio')
            original_input: User's original input for context
            max_results: Maximum number of results to return
            state: Application state (injected by action processor, unused)

        Returns:
            Discovery results with matched processes
        """
        if not query.strip():
            raise ValueError("Embeddings search query cannot be empty")

        logger.info(f"🔍 EMBEDDINGS_SEARCH: query='{query[:100]}...'")

        # Vector semantic search - no fallback, fail fast on errors
        embedding_service, vector_service = self._validate_discovery_services()
        query_embedding = self._generate_query_embedding(embedding_service, query)
        results_list = self._search_vectors(vector_service, query_embedding, max_results)

        result = self._process_vector_results(results_list, query, max_results)

        # Add original_input to result for downstream use
        result["original_input"] = original_input

        logger.info(
            f"🔍 EMBEDDINGS_RESULT: {result.get('process_count', 0)} processes found"
        )
        return result

    def _validate_query_params(
        self, exclude_tags: list[str] | None, include_tags: list[str] | None
    ) -> None:
        """Validate query parameters. Raises ValueError for invalid inputs."""
        if exclude_tags or include_tags:
            raise ValueError("Tag-based filtering is removed. Use semantic query only.")
        # Empty query is handled gracefully in query_process_registry (returns empty result).
        # Model sometimes sends empty queries; raising here triggers error→retry loops.

    def _process_vector_results(
        self,
        results_list: list[dict[str, object]],
        enhanced_query: str,
        max_results: int,
    ) -> dict[str, object]:
        """Process vector search results and return discovery result."""
        if not results_list:
            logger.debug("🔍 DISCOVERY_005: No vector matches")
            logger.info("🔍 DISCOVERY_NO_RESULTS: vector search returned empty list")
            return self._build_no_matches_result(enhanced_query)

        text_matches = self._convert_to_process_matches(results_list)
        logger.debug(f"🔍 DISCOVERY_006: Found {len(text_matches)} semantic matches")
        # Log converted matches with scores
        for i, match in enumerate(text_matches[:10]):
            logger.debug(
                f"🔍 DISCOVERY_CONVERTED[{i}]: key='{match.process_key}', score={match.score:.4f}"
            )

        filtered_matches = self._apply_threshold_filter(text_matches)
        filtered_matches = self._exclude_system_prompt_processes(filtered_matches)

        if not filtered_matches:
            logger.debug("🔍 DISCOVERY_005b: Semantic matches below threshold or all excluded")
            logger.info("🔍 DISCOVERY_ALL_FILTERED: no matches after threshold/exclusion filtering")
            return self._build_no_matches_result(enhanced_query)

        return self._apply_confidence_and_build_result(filtered_matches, enhanced_query, max_results)

    def _exclude_system_prompt_processes(
        self, matches: list[ProcessMatch]
    ) -> list[ProcessMatch]:
        """Exclude system prompt processes from discovery results.

        System prompt processes (post_message, query_process_registry, get_process_schema)
        are always available in the system prompt, so we exclude them from discovery
        results to avoid redundant descriptions.
        """
        before_count = len(matches)
        filtered = [m for m in matches if m.process_key not in SYSTEM_PROMPT_PROCESSES]

        if (excluded_count := before_count - len(filtered)) > 0:
            logger.debug(f"Excluded {excluded_count} system prompt processes from discovery")

        return filtered

    def _build_no_matches_result(self, query: str) -> dict[str, object]:
        """Build result dict for no matches case."""
        logger.debug("🔍 DISCOVERY_007: No matches from vector search")
        return DiscoveryResult(processes=[], query=query, match_type="no_matches").to_dict()

    def _validate_discovery_services(
        self,
    ) -> tuple[EmbeddingServiceInterface, VectorServiceInterface]:
        """Validate and return embedding and vector services."""
        embedding_service = self._get_embedding_service()
        vector_service = self._get_vector_service()

        if not embedding_service or not vector_service:
            raise DiscoveryServiceUnavailableError(
                "Vector or embedding service not available for discovery"
            )
        # Cast to interface types - duck typing ensures compatibility
        return cast(EmbeddingServiceInterface, embedding_service), cast(
            VectorServiceInterface, vector_service
        )

    def _generate_query_embedding(
        self, embedding_service: EmbeddingServiceInterface, query: str
    ) -> list[float]:
        """Generate embedding for query string.

        Uses 'search_query:' prefix for nomic-embed-text-v1.5 instruction tuning.
        NOTE: This prefix is model-specific - remove when changing embedding models.
        """
        logger.debug("🔍 DISCOVERY_002: Generating query embedding")
        prefixed_query = f"search_query: {query}"
        logger.debug(f"🔍 DISCOVERY_EMBED_INPUT: prefixed_query='{prefixed_query[:100]}...'")
        embedding_result = embedding_service.generate_embeddings(
            inputs=[prefixed_query], input_type=InputType.TEXT.value
        )

        if embedding_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            error_msg = embedding_result.get(KEY_RESULT, "Unknown error")
            raise DiscoveryServiceError(f"Failed to generate query embedding: {error_msg}")

        data_obj = embedding_result.get(KEY_DATA)
        if not isinstance(data_obj, dict):
            raise DiscoveryServiceError("Invalid embedding result: missing data")
        result_obj = data_obj.get(KEY_RESULT)
        if not isinstance(result_obj, dict):
            raise DiscoveryServiceError("Invalid embedding result: missing result")
        embeddings_obj = result_obj.get("embeddings")
        if not isinstance(embeddings_obj, list) or len(embeddings_obj) == 0:
            raise DiscoveryServiceError("Invalid embedding result: missing or empty embeddings")
        first_embedding = embeddings_obj[0]
        if not isinstance(first_embedding, list):
            raise DiscoveryServiceError("Invalid embedding result: embedding is not a list")
        # Log embedding fingerprint for debugging (first 5 values)
        embed_fingerprint = [round(v, 4) for v in first_embedding[:5]]
        logger.debug(
            f"🔍 DISCOVERY_EMBED_OUTPUT: dim={len(first_embedding)}, "
            f"fingerprint={embed_fingerprint}"
        )
        return first_embedding

    def _search_vectors(
        self, vector_service: VectorServiceInterface, query_embedding: list[float], max_results: int
    ) -> list[dict[str, object]]:
        """Search for similar vectors and return results list."""
        logger.debug("🔍 DISCOVERY_003: Searching for similar vectors")
        logger.debug(
            f"🔍 DISCOVERY_SEARCH_PARAMS: namespace='{self.vector_namespace}', "
            f"top_k={max_results * 2}, embed_dim={len(query_embedding)}"
        )
        search_result = vector_service.search_similar(
            namespace=self.vector_namespace,
            query_vector=query_embedding,
            top_k=max_results * 2,
        )

        if search_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            error_msg = search_result.get(KEY_RESULT, "Unknown error")
            raise DiscoveryServiceError(f"Vector search failed: {error_msg}")

        search_data_obj = search_result.get(KEY_DATA)
        if not isinstance(search_data_obj, dict):
            raise DiscoveryServiceError("Invalid search result: missing data")
        search_result_obj = search_data_obj.get(KEY_RESULT)
        if not isinstance(search_result_obj, dict):
            raise DiscoveryServiceError("Invalid search result: missing result")
        results_list_obj = search_result_obj.get(KEY_RESULTS)
        if not isinstance(results_list_obj, list):
            raise DiscoveryServiceError("Invalid search result: missing or invalid results")

        logger.debug(f"🔍 DISCOVERY_004: Found {len(results_list_obj)} vector matches")
        # Log raw vector search results with scores for debugging
        for i, result in enumerate(results_list_obj[:10]):
            external_id = result.get("external_id", "unknown")
            distance = result.get("distance", -1)
            similarity = 1 - distance if isinstance(distance, int | float) else "N/A"
            logger.debug(
                f"🔍 DISCOVERY_RAW_RESULT[{i}]: external_id='{external_id}', "
                f"distance={distance:.4f}, similarity={similarity:.4f}"
                if isinstance(similarity, float)
                else f"🔍 DISCOVERY_RAW_RESULT[{i}]: external_id='{external_id}', "
                f"distance={distance}, similarity={similarity}"
            )
        return results_list_obj

    def _convert_to_process_matches(
        self, results_list: list[dict[str, object]]
    ) -> list[ProcessMatch]:
        """Convert vector search results to ProcessMatch objects."""
        matches = []
        for result in results_list:
            metadata_obj = result.get("metadata", {})
            if not isinstance(metadata_obj, dict):
                continue
            process_key = metadata_obj.get("process_key")

            if not process_key or not isinstance(process_key, str):
                continue

            distance_obj = result.get("distance", 0.0)
            distance = float(distance_obj) if isinstance(distance_obj, int | float) else 0.0
            similarity_score = 1.0 - distance
            enriched_metadata = self._build_enriched_metadata(process_key)

            matches.append(
                ProcessMatch(
                    process_key=process_key,
                    score=similarity_score,
                    match_type="vector_semantic",
                    matched_fields=["embedding"],
                    metadata=enriched_metadata,
                )
            )
        return matches

    def _build_enriched_metadata(self, process_key: str) -> dict[str, object]:
        """Build metadata for discovery including full invocation schema.

        Returns fields needed for process selection AND invocation:
        - description: What the process does (for LLM to choose the right process)
        - invocation_schema: Full JSON schema for constructing valid action arguments
        - is_long_running: Whether to notify user before execution

        The invocation_schema includes all parameter definitions, types, and validation
        constraints - no separate get_process_schema call needed.
        """
        full_process_data = self.get_process_by_key(process_key)
        if not full_process_data:
            logger.error(f"Process {process_key} found in vector store but not in registry")
            return {}

        invocation_schema = full_process_data.get(KEY_INVOCATION_SCHEMA, {})

        return {
            KEY_DESCRIPTION: full_process_data.get(KEY_DESCRIPTION, ""),
            KEY_INVOCATION_SCHEMA: invocation_schema if isinstance(invocation_schema, dict) else {},
            "is_long_running": full_process_data.get("is_long_running", False),
        }

    def _apply_threshold_filter(self, matches: list[ProcessMatch]) -> list[ProcessMatch]:
        """Filter matches below similarity threshold."""
        # Threshold is guaranteed non-None by constructor validation when inference_service is provided
        assert self._min_similarity_threshold is not None
        below_threshold = [m for m in matches if m.score < self._min_similarity_threshold]
        filtered = [m for m in matches if m.score >= self._min_similarity_threshold]
        logger.debug(
            f"🔍 DISCOVERY_006b: After threshold filtering ({self._min_similarity_threshold}): "
            f"{len(filtered)} of {len(matches)} matches"
        )
        if below_threshold:
            logger.info(
                f"🔍 DISCOVERY_BELOW_THRESHOLD: dropped {len(below_threshold)} matches "
                f"(threshold={self._min_similarity_threshold}): "
                f"{[(m.process_key, f'{m.score:.4f}') for m in below_threshold[:5]]}"
            )
        return filtered

    def _apply_confidence_and_build_result(
        self, matches: list[ProcessMatch], query: str, max_results: int
    ) -> dict[str, object]:
        """Apply confidence engine and build final discovery result."""
        logger.debug(f"🔍 DISCOVERY_007: Applying confidence engine to {len(matches)} matches")

        usage_stats = self._gather_usage_stats(matches)
        process_scores = [ProcessScore(process_key=m.process_key, score=m.score) for m in matches]
        assessment = self.confidence_engine.assess(process_scores, usage_stats)

        logger.debug(
            f"🔍 DISCOVERY_008: Confidence assessment: {assessment.confidence.value} "
            f"(top={assessment.top_score:.3f}, gap={assessment.score_gap or 0:.3f}, "
            f"recommended={assessment.recommended_results}) - {assessment.reasoning}"
        )

        # Garbage cutoff: return no_matches for low-confidence results below threshold
        # This prevents garbage queries (like "go ahead!") from polluting prompts
        if (
            assessment.confidence == DiscoveryConfidence.LOW
            and assessment.top_score < GARBAGE_CUTOFF_THRESHOLD
        ):
            logger.debug(
                f"🔍 DISCOVERY_008c: Garbage cutoff triggered "
                f"(confidence={assessment.confidence.value}, top_score={assessment.top_score:.3f} < {GARBAGE_CUTOFF_THRESHOLD})"
            )
            return DiscoveryResult(processes=[], query=query, match_type="no_matches").to_dict()

        effective_limit = min(assessment.recommended_results, max_results)
        final_results = matches[:effective_limit]
        logger.debug(
            f"🔍 DISCOVERY_008b: After confidence filtering: {len(final_results)} results "
            f"(recommended={assessment.recommended_results}, max={max_results})"
        )

        process_keys = [p.process_key for p in final_results]
        logger.debug(f"🔍 DISCOVERY_009: Returning process keys: {process_keys}")

        match_type = f"confidence_{assessment.confidence.value}"
        if assessment.usage_influenced:
            match_type += "_usage_boosted"

        result = DiscoveryResult(processes=final_results, query=query, match_type=match_type)
        logger.debug(
            f"🔍 DISCOVERY_010: Created DiscoveryResult with {len(result.processes)} processes "
            f"(confidence={assessment.confidence.value})"
        )
        # Log final result summary
        logger.info(
            f"🔍 DISCOVERY_FINAL_RESULT: match_type='{match_type}', "
            f"process_count={len(final_results)}, "
            f"top_keys={process_keys[:5]}"
        )
        return result.to_dict()

    def record_usage(self, process_key: str) -> None:
        if not process_key:
            raise ValueError("Process key cannot be empty")

        try:
            result = self.state_service.read_state(
                namespace=self.namespace,
                query={"table": "usage_stats", "filters": {"process_key": process_key}},
            )

            # Type narrowing: safely extract records
            data_obj = result.get("data")
            if isinstance(data_obj, dict):
                result_obj = data_obj.get("result")
                if isinstance(result_obj, dict):
                    records_obj = result_obj.get("records")
                    if isinstance(records_obj, list) and len(records_obj) > 0:
                        current_stats_obj = records_obj[0]
                        if isinstance(current_stats_obj, dict):
                            total_obj = current_stats_obj.get("total_executions")
                            if isinstance(total_obj, int):
                                new_total = total_obj + 1

                                self.state_service.update_state(
                                    namespace=self.namespace,
                                    query={
                                        "table": "usage_stats",
                                        "filters": {"process_key": process_key},
                                    },
                                    updates={
                                        "total_executions": new_total,
                                        "last_used": datetime.now(UTC).isoformat(),
                                    },
                                )
                                self._update_plugin_popularity(process_key)
                                return

            # No existing record, create new one
            self.state_service.write_state(
                namespace=self.namespace,
                data={
                    "table": "usage_stats",
                    "records": [
                        {
                            "process_key": process_key,
                            "total_executions": 1,
                            "last_used": datetime.now(UTC).isoformat(),
                        }
                    ],
                },
            )

            self._update_plugin_popularity(process_key)
        except Exception as e:
            # Usage tracking should not block discovery - log and continue
            logger.error(f"Failed to record usage for {process_key}: {e}")

    def get_usage_patterns(self, process_key: str) -> UsagePatterns:
        # TODO: Implement usage pattern tracking to query historical usage data
        # Stub: Returns empty patterns for any process_key until tracking is implemented
        _ = process_key  # Acknowledge parameter is part of public API
        return UsagePatterns(follows_sequences=[], followed_by_sequences=[])

    def get_service_health(self) -> dict[str, Any]:
        # SELECT COUNT(*), written as one. This previously read the ENTIRE
        # usage_stats table with `filters: {}` and no limit purely to call len()
        # on the rows — shipping every row across the boundary to compute a
        # number the database can produce as a scalar. `count` runs the
        # aggregate inside the owner plugin and returns only that scalar.
        #
        # It was also reading a key that does not exist: it looked for
        # data["result"]["records"], but read_state's envelope is
        # {records, count, namespace, table} with no "result". So
        # total_usage_records was structurally 0 no matter what the table held —
        # a whole-table fetch whose result was then discarded. The bound and the
        # navigation are fixed together here because bounding a read whose
        # result is thrown away would leave the site just as broken.
        result = self.state_service.count(
            namespace=self.namespace, data={"table": "usage_stats", "filters": {}}
        )

        total_usage_records = 0
        # count reports its scalar at data.result.value (this envelope DOES have
        # a "result" key — read_state's does not; they are different shapes).
        data_obj = result.get("data")
        if isinstance(data_obj, dict):
            result_obj = data_obj.get("result")
            if isinstance(result_obj, dict):
                value_obj = result_obj.get("value")
                if isinstance(value_obj, int):
                    total_usage_records = value_obj

        # Check if vector services are available
        vector_service = self._get_vector_service()
        is_healthy = vector_service is not None

        health = ServiceHealth(
            is_healthy=is_healthy,
            index_last_built="vector-based",
            total_processes=len(self.processes),
            total_usage_records=total_usage_records,
        )
        return asdict(health)

    def get_process_schema(self, process_key: str) -> dict[str, object]:
        """Retrieve the full invocation schema for a process.

        This is Step 2 of the two-step discovery workflow:
        1. query_process_registry - find matching processes (lightweight metadata)
        2. get_process_schema - get full schema for selected process

        Args:
            process_key: The process key (format: 'provider_type::provider::function_name')

        Returns:
            ActionResult with process_key, description, and invocation_schema
        """
        # Validate process_key format to prevent query-as-key regression
        # Process keys must be 'provider_type::provider::function_name'
        # See: knowledge_base/2026-01-13_bad_example_prevention_implementation.md
        if not self._is_valid_process_key_format(process_key):
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_ERROR: (
                    f"Invalid process_key format: '{process_key}'. "
                    "Process keys must follow 'service_interface::provider::function_name' or "
                    "'plugin::provider::function_name' format. "
                    "Use query_process_registry to find valid process keys first."
                ),
            }

        full_process = self.get_process_by_key(process_key)
        if not full_process:
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_ERROR: f"Process not found: {process_key}",
            }

        return {
            KEY_ACTION_STATUS: STATUS_COMPLETED,
            KEY_DATA: {
                KEY_PROCESS_KEY: process_key,
                KEY_DESCRIPTION: full_process.get(KEY_DESCRIPTION, ""),
                KEY_INVOCATION_SCHEMA: full_process.get(KEY_INVOCATION_SCHEMA, {}),
                "is_long_running": full_process.get("is_long_running", False),
                # Phase 6 §4.6: surface the deprecation tombstone (replacement_key,
                # superseded_date, migration_note, active_retrieval) to a migrating
                # agent. The block is set on the entry by the Tier-2 overlay
                # (apply_deprecation); this projection previously dropped it, so an
                # agent inspecting a schema could not see the replacement. None when
                # the process is not deprecated.
                "deprecation": full_process.get("deprecation"),
            },
        }

    def rebuild_index(self, process_registry: dict[str, object] | None = None) -> None:
        """Rebuild vector embeddings for all processes."""
        if process_registry:
            # Load from provided registry format
            processes_data_obj = process_registry.get("processes", {})
            # Type narrowing: verify processes_data is a dict
            if isinstance(processes_data_obj, dict):
                self.processes.clear()
                for key, data_obj in processes_data_obj.items():
                    # Type narrowing: verify data is a dict
                    if isinstance(data_obj, dict):
                        self.store_process(key, data_obj)

        # Vector embeddings are generated automatically in store_process()
        logger.debug(f"Rebuilt discovery index with {len(self.processes)} processes")

    def _load_processes(self, process_registry: dict[str, object]) -> None:
        """Load processes from registry and generate vector embeddings.

        Clears existing process embeddings first to ensure changes to
        embedding_description are reflected in the vector index.
        """
        # Clear existing embeddings before rebuilding - ensures embedding_description
        # changes take effect on restart (vectors have unique constraint on external_id)
        self._clear_process_vectors()

        processes_data_obj = process_registry.get("processes", {})
        # Type narrowing: verify processes_data is a dict
        if isinstance(processes_data_obj, dict):
            for process_key, process_data_obj in processes_data_obj.items():
                # Type narrowing: verify process_data is a dict
                if isinstance(process_data_obj, dict):
                    self.store_process(process_key, process_data_obj)

        # Vector embeddings are generated automatically in store_process()
        logger.debug(f"Loaded {len(self.processes)} processes into discovery service")

    def _clear_process_vectors(self) -> None:
        """Hard-clear process embeddings via the vector_service abstraction.

        Symmetric with `_store_vector`, which writes through the same
        abstraction. The previous implementation went through
        `state_service.delete_records` with a `{is_deleted: 0}` filter and
        silently matched zero rows, because the platform's standardized rows
        carry NULL in that column for active records — the count was
        misleading but never raised.
        """
        if self.vector_service is None:
            return
        vector_service = cast(VectorServiceInterface, self.vector_service)
        result = vector_service.delete_all_in_namespace(namespace=self.vector_namespace)
        if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            raise RuntimeError(
                f"Failed to clear process embeddings: {result.get(KEY_ERROR)}"
            )
        data = result.get(KEY_DATA, {})
        deleted = data.get("deleted_count", 0) if isinstance(data, dict) else 0
        logger.info(f"Cleared {deleted} existing process embeddings for rebuild")

    def _gather_usage_stats(self, matches: list[ProcessMatch]) -> dict[str, int]:
        """Gather usage statistics for all matches.

        Returns a dict mapping process_key to total_executions.
        Used by the confidence engine for usage-based disambiguation.
        """
        usage_stats: dict[str, int] = {}
        for match in matches:
            stats = self._get_usage_stats(match.process_key)
            usage_stats[match.process_key] = stats.total_executions
        return usage_stats

    def _get_usage_stats(self, process_key: str) -> UsageStats:
        try:
            result = self.state_service.read_state(
                namespace=self.namespace,
                query={"table": "usage_stats", "filters": {"process_key": process_key}},
            )

            # Type narrowing: safely extract stats
            data_obj = result.get("data")
            if isinstance(data_obj, dict):
                result_obj = data_obj.get("result")
                if isinstance(result_obj, dict):
                    records_obj = result_obj.get("records")
                    if isinstance(records_obj, list) and len(records_obj) > 0:
                        stats_obj = records_obj[0]
                        if isinstance(stats_obj, dict):
                            total_obj = stats_obj.get("total_executions")
                            last_used_obj = stats_obj.get("last_used")
                            if isinstance(total_obj, int) and isinstance(last_used_obj, str):
                                return UsageStats(
                                    total_executions=total_obj, last_used=last_used_obj
                                )
        except Exception:
            pass

        return UsageStats(total_executions=0, last_used=datetime.now(UTC).isoformat())

    def record_process_usage(self, process_key: str) -> None:
        try:
            # Get current stats
            current_stats = self._get_usage_stats(process_key)

            # Create/update record with unique process_key constraint
            record = {
                "process_key": process_key,  # This ensures uniqueness via UNIQUE constraint
                "total_executions": current_stats.total_executions + 1,
                "last_used": datetime.now(UTC).isoformat(),
            }

            self.state_service.write_state(
                namespace=self.namespace, data={"table": "usage_stats", "record": record}
            )

        except Exception:
            # Non-critical feature - don't fail if usage tracking fails
            pass

    def _get_plugin_popularity(self, process_key: str) -> int:
        plugin_name = self._extract_plugin_name(process_key)
        if not plugin_name:
            return 0

        result = self.state_service.read_state(
            namespace=self.namespace,
            query={"table": "plugin_popularity", "filters": {"plugin_name": plugin_name}},
        )

        # Type narrowing: safely extract execution_count
        data_obj = result.get("data")
        if isinstance(data_obj, dict):
            result_obj = data_obj.get("result")
            if isinstance(result_obj, dict):
                records_obj = result_obj.get("records")
                if isinstance(records_obj, list) and len(records_obj) > 0:
                    first_record = records_obj[0]
                    if isinstance(first_record, dict):
                        count_obj = first_record.get("execution_count")
                        if isinstance(count_obj, int):
                            return count_obj

        return 0

    def _update_plugin_popularity(self, process_key: str) -> None:
        plugin_name = self._extract_plugin_name(process_key)
        if not plugin_name:
            return

        result = self.state_service.read_state(
            namespace=self.namespace,
            query={"table": "plugin_popularity", "filters": {"plugin_name": plugin_name}},
        )

        # Type narrowing: safely extract current count
        data_obj = result.get("data")
        if isinstance(data_obj, dict):
            result_obj = data_obj.get("result")
            if isinstance(result_obj, dict):
                records_obj = result_obj.get("records")
                if isinstance(records_obj, list) and len(records_obj) > 0:
                    first_record = records_obj[0]
                    if isinstance(first_record, dict):
                        count_obj = first_record.get("execution_count")
                        if isinstance(count_obj, int):
                            self.state_service.update_state(
                                namespace=self.namespace,
                                query={
                                    "table": "plugin_popularity",
                                    "filters": {"plugin_name": plugin_name},
                                },
                                updates={
                                    "execution_count": count_obj + 1,
                                    "last_updated": datetime.now(UTC).isoformat(),
                                },
                            )
                            return

        # No existing record, create new one
        self.state_service.write_state(
            namespace=self.namespace,
            data={
                "table": "plugin_popularity",
                "records": [
                    {
                        "plugin_name": plugin_name,
                        "execution_count": 1,
                        "last_updated": datetime.now(UTC).isoformat(),
                    }
                ],
            },
        )

    def _extract_plugin_name(self, process_key: str) -> str | None:
        parts = process_key.split("::")
        return parts[1] if len(parts) >= 2 else None

    # ===== MULTI-NAMESPACE SEARCH INTERFACE =====
    # Platform-wide vector search across multiple namespaces

    def search_platform(
        self, query: str, namespaces: list[str], limit: int = DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        """Search across multiple vector namespaces using natural language."""
        self._validate_platform_search_inputs(query, namespaces)

        embedding_service, vector_service = self._validate_discovery_services()

        try:
            query_embedding = self._generate_query_embedding(embedding_service, query)

            logger.debug(
                f"🔍 PLATFORM_SEARCH_002: Searching {len(namespaces)} namespaces with limit={limit}"
            )
            all_results, namespaces_searched = self._search_multiple_namespaces(
                vector_service, query_embedding, namespaces, limit
            )

            sorted_results = self._sort_and_limit_results(all_results, limit)
            logger.debug(f"🔍 PLATFORM_SEARCH_003: Found {len(sorted_results)} results")

            return {
                KEY_QUERY: query,
                KEY_NAMESPACES_SEARCHED: namespaces_searched,
                KEY_RESULTS: sorted_results,
                KEY_COUNT: len(sorted_results),
                KEY_ACTION_STATUS: STATUS_COMPLETED,
            }

        except (ValueError, TypeError):
            raise
        except DiscoveryServiceError:
            raise
        except Exception as e:
            logger.error(f"Platform search failed: {e}")
            raise DiscoveryServiceError(f"Search operation failed: {e}") from e

    def _validate_platform_search_inputs(self, query: str, namespaces: list[str]) -> None:
        """Validate inputs for platform search."""
        if not query.strip():
            raise ValueError("Search query cannot be empty")
        if not namespaces:
            raise ValueError("Namespaces list cannot be empty")

    def _search_multiple_namespaces(
        self,
        vector_service: VectorServiceInterface,
        query_embedding: list[float],
        namespaces: list[str],
        limit: int,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Search multiple namespaces and return merged results."""
        all_results: list[dict[str, object]] = []
        namespaces_searched: list[str] = []

        for ns in namespaces:
            search_result = vector_service.search_similar(
                namespace=ns,
                query_vector=query_embedding,
                top_k=limit,
            )

            if search_result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
                logger.error(f"Vector search failed for namespace {ns}")
                continue

            namespaces_searched.append(ns)
            ns_results = self._extract_namespace_results(search_result)
            all_results.extend(ns_results)

        return all_results, namespaces_searched

    def _extract_namespace_results(self, search_result: ActionResult) -> list[dict[str, object]]:
        """Extract results list from namespace search result."""
        ns_data = search_result.get(KEY_DATA)
        if not isinstance(ns_data, dict):
            return []
        ns_result = ns_data.get(KEY_RESULT)
        if not isinstance(ns_result, dict):
            return []
        ns_results = ns_result.get("results", [])
        if isinstance(ns_results, list):
            return ns_results
        return []

    def _sort_and_limit_results(
        self, results: list[dict[str, object]], limit: int
    ) -> list[dict[str, object]]:
        """Sort results by distance and limit count."""

        def get_distance(x: dict[str, object]) -> float:
            dist = x.get("distance")
            if isinstance(dist, int | float):
                return float(dist)
            return float("inf")

        results.sort(key=get_distance)
        return results[:limit]

    def list_vector_namespaces(self) -> dict[str, Any]:
        """List all available vector namespaces in the platform.

        Queries the database information_schema to find all tables matching
        the vector embeddings pattern ({namespace}__embeddings).

        Returns:
            Dictionary with namespace list:
                pass
            {
                "namespaces": list[str],
                "action_status": "completed"
            }

        Raises:
            DiscoveryServiceUnavailableError: If vector service unavailable
            DiscoveryServiceError: If listing fails
        """
        # Get vector service
        vector_service = self._get_vector_service()

        if not vector_service:
            raise DiscoveryServiceUnavailableError("Vector service not available")

        # Cast to interface type - duck typing ensures compatibility
        typed_vector_service = cast(VectorServiceInterface, vector_service)

        try:
            logger.debug("🔍 LIST_NAMESPACES_001: Requesting namespace list from vector service")

            # Use the vector service's list_namespaces method
            result = typed_vector_service.list_namespaces()

            if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
                error_msg = result.get(KEY_RESULT, "Unknown error")
                raise DiscoveryServiceError(f"Failed to list namespaces: {error_msg}")

            # Extract namespaces from result
            data_obj = result.get(KEY_DATA)
            if not isinstance(data_obj, dict):
                data_obj = {}
            result_data_obj = data_obj.get(KEY_RESULT)
            if not isinstance(result_data_obj, dict):
                result_data_obj = {}
            namespaces_obj = result_data_obj.get(KEY_NAMESPACES)
            if not isinstance(namespaces_obj, list):
                namespaces = []
            else:
                namespaces = namespaces_obj

            logger.debug(f"🔍 LIST_NAMESPACES_002: Found {len(namespaces)} namespaces")

            return {KEY_NAMESPACES: namespaces, KEY_ACTION_STATUS: STATUS_COMPLETED}

        except DiscoveryServiceError:
            # Re-raise discovery errors
            raise
        except Exception as e:
            logger.error(f"Failed to list namespaces: {e}")
            raise DiscoveryServiceError(f"Namespace listing failed: {e}") from e
