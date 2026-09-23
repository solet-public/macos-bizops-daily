"""Constants for default_knowledge_plugin."""

from __future__ import annotations

from enum import StrEnum

PLUGIN_NAME = "default_knowledge_plugin"
MANIFEST_FILE_NAME = "manifest.yaml"

TABLE_KNOWLEDGE_INSTALL = "knowledge_install"


class Scope(StrEnum):
    WORKSPACE = "workspace"
    PLUGIN = "plugin"
    ALL = "all"


class WritePosture(StrEnum):
    """Per-KB write policy enforced by the file-ops verbs.

    ``FULL`` (default) preserves today's behavior — create, edit (with or
    without a content-hash precondition), and delete all permitted.
    ``CREATE_AND_CAS_EDIT`` (workbench's setting) permits create, permits
    edit only when a *current* content-hash precondition is supplied (W12),
    and rejects delete in favor of ``archive_file``. ``CREATE_ONLY`` permits
    only create. ``READ_ONLY`` rejects all three write verbs. ``archive_file``
    is a lifecycle verb, not a content write — it is allowed wherever
    ``archive_subdir`` is configured, independent of posture.
    """

    FULL = "full"
    CREATE_AND_CAS_EDIT = "create_and_cas_edit"
    CREATE_ONLY = "create_only"
    READ_ONLY = "read_only"


# The §4 metadata-block convention has ONE definition in code (see the risk
# note in workbench/2026-07-16_workbench_kb_option_b_implementation_plan.md §6:
# the chunker's tolerant matcher and the create-path validator must not drift).
# REQUIRED keys gate ``create_file`` when a KB sets ``require_metadata_block``;
# RECOGNIZED keys (required + the machine-stamped lifecycle keys) bound the
# tolerant run the title_block chunker extracts.
METADATA_BLOCK_REQUIRED_KEYS: tuple[str, ...] = (
    "Date",
    "Author",
    "Status",
    "Embedding Description",
    "Summary",
)
METADATA_BLOCK_RECOGNIZED_KEYS: tuple[str, ...] = (
    *METADATA_BLOCK_REQUIRED_KEYS,
    "Superseded_by",
    "Archived",
)

# Prior-version snapshot sidecar written before every W12 CAS edit. Excluded
# from indexing at the code level (``collect_files``), gitignored, and rejected
# as a write target by the W4 protected-path guard.
DOC_HISTORY_DIRNAME = ".doc_history"


TAG_DOMAIN_OFFICIAL = "knowledge:official"
TAG_PROMOTED = "knowledge:promoted"

TAG_PREFIX_KB_ID = "knowledge:kb_id:"
TAG_PREFIX_VERSION = "knowledge:version:"
TAG_PREFIX_SCOPE = "knowledge:scope:"
TAG_PREFIX_SCOPE_KEY = "knowledge:scope_key:"
TAG_PREFIX_PLUGIN = "knowledge:plugin:"
TAG_PREFIX_PROCESS_KEY = "knowledge:process_key:"
TAG_PREFIX_MANIFEST_TAG = "knowledge:tag:"
TAG_PREFIX_DOC = "knowledge:doc:"
# Knowledge layer tag prefix. Each chunk carries exactly one
# ``knowledge:layer:<n>`` tag for chunks derived from articles with an
# ``Article Layer:`` annotation, or ``knowledge:layer:unlabeled`` for
# chunks from articles without an annotation. The runtime search API
# uses this tag to enforce per-stage retrieval policy. See the layer
# registry article in ananta_platform/14_knowledge_retrieval/
# knowledge_layer_registry.md for the canonical taxonomy.
TAG_PREFIX_LAYER = "knowledge:layer:"
TAG_LAYER_UNLABELED = "knowledge:layer:unlabeled"

DEFAULT_CHUNK_MAX_CHARS = 2000
DEFAULT_CHUNK_OVERLAP_CHARS = 200

# Knowledge bases excluded from DEFAULT semantic (Tier 3) + diversity search.
# Internal data stores / discovery indexes whose chunks are semantically
# adjacent to domain queries but are NOT reference material — they pollute
# unscoped results. The exclusion is a pollution heuristic for unscoped
# queries, NOT access control: an explicit ``name=<kb>`` scope BYPASSES it
# (``honor_exclusions=(name is None)`` in ``search()``), and process-key
# (Tier 1) and tag (Tier 2) searches always include these KBs. Architect Q1
# ruling 2026-07-16 (RATIFY-WITH-CONDITIONS); ``workbench`` joins the set as a
# name/summary discovery index reachable only by explicit ``name="workbench"``.
SEARCH_EXCLUDED_KB_NAMES: frozenset[str] = frozenset(
    {"thinking_plans", "thinking_playbooks", "workbench"}
)

DEFAULT_INCLUDE_PATTERNS = ["*.md", "*.txt", "*.rst"]
DEFAULT_EXCLUDE_PATTERNS = ["**/.git/**", "**/.archive/**", "**/scripts/**"]


def normalize_text_for_tag(value: str) -> str:
    """Normalize text to a safe tag payload."""
    return value.replace(" ", "_").replace("/", "_")


def kb_id_tag(kb_id: str) -> str:
    return f"{TAG_PREFIX_KB_ID}{kb_id}"


def kb_version_tag(version: str) -> str:
    return f"{TAG_PREFIX_VERSION}{version}"


def kb_scope_tag(scope: Scope) -> str:
    return f"{TAG_PREFIX_SCOPE}{scope.value}"


def kb_scope_key_tag(scope: Scope, plugin_name: str | None) -> str:
    plugin_part = plugin_name or "workspace"
    return f"{TAG_PREFIX_SCOPE_KEY}{scope.value}:{plugin_part}"


def kb_plugin_tag(plugin_name: str) -> str:
    return f"{TAG_PREFIX_PLUGIN}{plugin_name}"


def process_key_tag(process_key: str) -> str:
    return f"{TAG_PREFIX_PROCESS_KEY}{normalize_text_for_tag(process_key)}"


def manifest_tag(tag: str) -> str:
    return f"{TAG_PREFIX_MANIFEST_TAG}{normalize_text_for_tag(tag)}"


def document_tag(relative_path: str) -> str:
    normalized = normalize_text_for_tag(relative_path)
    return f"{TAG_PREFIX_DOC}{normalized}"


def knowledge_layer_tag(layer: int) -> str:
    """Return the chunk tag for a given knowledge layer integer.

    Validation lives outside this helper so callers can produce better
    error messages. The platform supports any positive integer layer;
    new layers are added by editing the layer registry article and the
    Python taxonomy file in ``plugins/default_knowledge_plugin/tools/knowledge_layers/_taxonomy.py``.
    """
    return f"{TAG_PREFIX_LAYER}{layer}"
