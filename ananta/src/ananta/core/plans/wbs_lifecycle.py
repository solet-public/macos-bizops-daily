"""WBS lifecycle operations — deterministic state mutations for WBS execution.

Extracted from the thinking plugin to enable platform-owned WBS lifecycle
management.  Each function takes narrow protocol dependencies rather than
the full plugin surface.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from ananta.core.domain.types import ActionResult
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)


# ── Dependency protocols ─────────────────────────────────────────────


class WbsStateService(Protocol):
    """Read/write state records for WBS tracking."""

    def read_state(
        self,
        namespace: str,
        query: dict[str, object],
    ) -> dict[str, Any]: ...

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
    ) -> Any: ...


class WbsKnowledgeStore(Protocol):
    """Read/write artifacts in the knowledge base."""

    def read(self, path: str) -> str: ...

    def write(self, path: str, content: str) -> None: ...


class WbsFocusManager(Protocol):
    """Manage focused memory documents."""

    def upsert(
        self,
        content: str,
        *,
        doc_tag: str,
        label: str,
    ) -> str: ...

    def get_focused(self) -> list[dict[str, Any]]: ...


class WbsMemoryService(Protocol):
    """Focused memory operations for artifact eviction."""

    def get_focused(self) -> dict[str, Any]: ...

    def forget(self, memory_id: str) -> Any: ...

    def unfocus(self, memory_id: str) -> Any: ...


class WorkProductStateService(Protocol):
    """State service for work-product register operations.

    Matches the ``StateServiceProtocol`` expected by
    ``WorkProductStoreAdapter``.
    """

    def read_state(
        self,
        namespace: str,
        query: dict[str, object],
    ) -> ActionResult: ...

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> ActionResult: ...


# ── Error codes (mirror plugin constants for platform use) ───────────

_ERROR_PARAMETER = "default_thinking_plugin.parameter_error"
_ERROR_WBS_NOT_FOUND = "default_thinking_plugin.wbs_not_found"


# ── Public functions ─────────────────────────────────────────────────


def record_step_state(
    wbs_id: str,
    step_number: int,
    status: str,
    state_summary: str | None,
    output_artifacts: list[str] | None,
    state_service: WbsStateService,
    knowledge_store: WbsKnowledgeStore,
    focus_manager: WbsFocusManager,
    namespace: str,
) -> dict[str, Any]:
    """Record step-level execution state in a WBS.

    Appends a state annotation to the WBS content in focused memory.

    Reads the FOCUSED memory version (which accumulates annotations
    across steps) rather than the knowledge base version (which
    retains the original clean WBS for embedding safety).  Reading
    from the KB would overwrite all prior annotations on each call,
    leaving only the latest one visible to the graft projector.
    """
    if not wbs_id:
        raise FrameworkError(
            message="wbs_id is required",
            error_code=_ERROR_PARAMETER,
        )

    # Read from knowledge base — the durable store for accumulated
    # annotations.  Focused memory upserts are lossy (forget/remember/
    # focus race condition), so the KB is the reliable source.
    record = _get_wbs_record(wbs_id, state_service, namespace)
    kb_path = str(record.get("knowledge_base_path", f"wbs/{wbs_id}.md"))
    current = knowledge_store.read(kb_path)
    source = "knowledge_base"

    # Count existing annotations before appending
    existing_annotations = current.count("<!-- Step ")
    annotation = _build_step_annotation(step_number, status, state_summary, output_artifacts)
    updated = current + annotation
    logger.info(
        "WBS_RECORD_STEP: wbs=%s step=%d source=%s read_len=%d "
        "existing_annotations=%d updated_len=%d",
        wbs_id, step_number, source, len(current),
        existing_annotations, len(updated),
    )

    # Write to knowledge base — this is the durable, reliable store.
    # The graft projector reads step-completion annotations to determine
    # which work items are complete.  HTML comment annotations do not
    # affect the markdown structure, only add ~35 chars each.
    record = _get_wbs_record(wbs_id, state_service, namespace)
    kb_path = str(record.get("knowledge_base_path", f"wbs/{wbs_id}.md"))
    knowledge_store.write(kb_path, updated)

    # Also update focused memory so the context stage sees annotations.
    # This upsert may be lossy (forget/remember/focus race), but the
    # KB write above is the authoritative source for the graft projector.
    focus_manager.upsert(
        updated,
        doc_tag=f"work_breakdown_structure:{wbs_id}",
        label="work_breakdown_structure",
    )

    logger.info("WBS %s step %d state: %s", wbs_id, step_number, status)
    return {
        "wbs_id": wbs_id,
        "step_number": step_number,
        "status": "recorded",
    }


def record_phase_state(
    manifest_id: str,
    phase_number: int,
    status: str,
    outcome_summary: str,
    approved_artifacts: list[str] | None,
    next_phase_instruction: str | None,
    knowledge_store: WbsKnowledgeStore,
    focus_manager: WbsFocusManager,
) -> dict[str, Any]:
    """Record phase-level outcome in a Work Manifest.

    Appends a phase state annotation without rewriting the full document.
    """
    if not manifest_id:
        raise FrameworkError(
            message="manifest_id is required",
            error_code=_ERROR_PARAMETER,
        )

    kb_path = f"manifests/{manifest_id}.md"
    current = knowledge_store.read(kb_path)

    annotation = _build_phase_annotation(
        phase_number,
        status,
        outcome_summary,
        approved_artifacts,
        next_phase_instruction,
    )
    updated = current + annotation
    knowledge_store.write(kb_path, updated)

    focus_manager.upsert(
        updated,
        doc_tag=f"work_manifest:{manifest_id}",
        label="work_manifest",
    )

    logger.info("Manifest %s phase %d: %s", manifest_id, phase_number, status)
    return {
        "manifest_id": manifest_id,
        "phase_number": phase_number,
        "status": "recorded",
    }


_PHASE_SUFFIX_RE = re.compile(r"-phase(\d+)$")


def initialize_work_product_register(
    wbs_id: str,
    state_service: WorkProductStateService,
) -> None:
    """Create or advance a work-product register for a WBS run.

    If a register already exists (from a prior phase using the same WBS
    ID), it is preserved and the phase stem offset is advanced so new
    stems don't collide with prior-phase products.

    For Phase 2+ WBS IDs (matching ``*-phaseN``), products from all
    prior phases are carried forward into the new register so that
    cross-phase input resolution succeeds (e.g. Phase 3 can reference
    Phase 2 section stems).
    """
    from ananta.core.plans.work_product_store import WorkProductStoreAdapter
    from ananta.core.plans.work_products import WorkProductRegister

    store = WorkProductStoreAdapter(state_service)
    existing = store.load_register(wbs_id)
    if existing:
        logger.info(
            "WORK_PRODUCTS: Register preserved for %s (semantic naming)",
            wbs_id,
        )
        return

    register = WorkProductRegister()

    # Carry forward products from prior phases so cross-phase input
    # resolution works (e.g. Phase 3 assembly can find Phase 2 stems).
    phase_match = _PHASE_SUFFIX_RE.search(wbs_id)
    if phase_match:
        phase_num = int(phase_match.group(1))
        base = wbs_id[: phase_match.start()]
        carried = 0
        for prior in range(1, phase_num):
            prior_id = f"{base}-phase{prior}"
            try:
                prior_data = store.load_register(prior_id)
            except RuntimeError:
                continue
            if not prior_data:
                continue
            prior_register = WorkProductRegister.deserialize(prior_data)
            for product in prior_register.all_products():
                register.record(product)
                carried += 1
        if carried:
            logger.info(
                "WORK_PRODUCTS: Carried %d product(s) from phases 1-%d into %s",
                carried,
                phase_num - 1,
                wbs_id,
            )

    store.save_register(wbs_id, register.serialize())
    logger.info("WORK_PRODUCTS: Initialized register for %s", wbs_id)


# Tag prefixes for planning artifacts that should be unfocused at
# the scoping-to-execution transition.
DEFAULT_PLANNING_ARTIFACT_TAG_PREFIXES: tuple[str, ...] = (
    "resolved_intake_state:",
    "work_manifest:",
    "pipeline_spec:",
    "authored_artifact:",
)


def unfocus_planning_artifacts(
    memory_service: WbsMemoryService,
    artifact_tag_prefixes: tuple[str, ...] = DEFAULT_PLANNING_ARTIFACT_TAG_PREFIXES,
) -> int:
    """Unfocus scoping artifacts at the execution transition.

    The WBS graft marks the transition from scoping to execution.
    Planning artifacts (manifest, pipeline spec, intake state) were consumed
    during WBS creation — carrying them in focus bloats execution
    prompts and causes runaway token generation.

    Returns the number of evicted artifacts.
    """
    focused_items: list[dict[str, Any]] = memory_service.get_focused()["memories"]
    evicted = 0
    for item in focused_items:
        tags: list[str] = item.get("tags", [])
        if "plan" in tags:
            continue  # Preserve the active plan
        if not _matches_artifact_prefix(tags, artifact_tag_prefixes):
            continue
        mid: str = item.get("memory_id", "")
        if mid:
            _safe_unfocus(mid, memory_service)
            memory_service.forget(mid)
            evicted += 1
            logger.info(
                "GRAFT_EVICT: Unfocused planning artifact %s (tags=%s)",
                mid,
                [t for t in tags if t != "plan"],
            )
    if evicted:
        logger.info(
            "GRAFT_EVICT: Unfocused %d planning artifact(s) for execution",
            evicted,
        )
    return evicted


_WBS_ID_LINE_RE = re.compile(r"^WBS ID:\s*(\S+)", re.MULTILINE)


def read_wbs_content_for_graft(
    model_wbs_id: str,
    memory_service: WbsMemoryService,
) -> tuple[str, str]:
    """Read WBS content for grafting.

    Returns:
        Tuple of (wbs_content, actual_wbs_id).

    Searches focused memory for WBS documents (tagged
    ``work_breakdown_structure``).  Prefers an exact ``WBS ID:`` match
    against *model_wbs_id*.  If no exact match exists but exactly one
    focused WBS is present, returns that document — the model often
    hallucinates a slightly different ID than the platform stored at
    creation time, and the focused WBS is authoritative.
    """
    candidates: list[tuple[str, str]] = []
    for item in memory_service.get_focused()["memories"]:
        tags: list[str] = item.get("tags", [])
        if "work_breakdown_structure" not in tags:
            continue
        content = item.get("content", "")
        if not isinstance(content, str):
            continue
        wbs_id_match = _WBS_ID_LINE_RE.search(content)
        if not wbs_id_match:
            continue
        actual_id = wbs_id_match.group(1)
        if actual_id == model_wbs_id:
            return content, model_wbs_id
        candidates.append((content, actual_id))

    if len(candidates) == 1:
        actual_content, actual_id = candidates[0]
        logger.warning(
            "WBS ID mismatch: model requested %r, focused memory has %r "
            "— using authoritative focused WBS",
            model_wbs_id,
            actual_id,
        )
        return actual_content, actual_id

    if candidates:
        ids = [c[1] for c in candidates]
        raise FrameworkError(
            message=(
                f"WBS {model_wbs_id} not found in focused memory and "
                f"multiple WBS documents are focused ({ids}) — cannot "
                f"resolve unambiguously"
            ),
            error_code=_ERROR_PARAMETER,
        )

    raise FrameworkError(
        message=f"No WBS document found in focused memory (model requested {model_wbs_id})",
        error_code=_ERROR_PARAMETER,
    )


# ── Internal helpers ─────────────────────────────────────────────────


def _get_wbs_record(
    wbs_id: str,
    state_service: WbsStateService,
    namespace: str,
) -> dict[str, Any]:
    """Look up a WBS tracking record by ID."""
    result = state_service.read_state(
        namespace=namespace,
        query={
            "table": "thinking_wbs",
            "filters": {"id": wbs_id, "is_deleted": 0},
        },
    )
    data = result.get("data")
    records: list[dict[str, Any]] = data.get("records", []) if isinstance(data, dict) else []
    if not records:
        raise FrameworkError(
            message=f"Work Breakdown Structure not found: {wbs_id}",
            error_code=_ERROR_WBS_NOT_FOUND,
        )
    return records[0]


def _build_step_annotation(
    step_number: int,
    status: str,
    state_summary: str | None,
    output_artifacts: list[str] | None,
) -> str:
    """Build an HTML comment annotation for a WBS step."""
    parts = [f"status={status}"]
    if state_summary:
        parts.append(f"summary={state_summary}")
    if output_artifacts:
        parts.append(f"artifacts={','.join(output_artifacts)}")
    return f"\n<!-- Step {step_number}: {'; '.join(parts)} -->\n"


def _build_phase_annotation(
    phase_number: int,
    status: str,
    outcome_summary: str,
    approved_artifacts: list[str] | None,
    next_phase_instruction: str | None,
) -> str:
    """Build an HTML comment annotation for a manifest phase."""
    parts = [f"status={status}", f"outcome={outcome_summary}"]
    if approved_artifacts:
        parts.append(f"artifacts={','.join(approved_artifacts)}")
    if next_phase_instruction:
        parts.append(f"next={next_phase_instruction}")
    return f"\n<!-- Phase {phase_number}: {'; '.join(parts)} -->\n"


def _safe_unfocus(memory_id: str, memory_service: WbsMemoryService) -> None:
    """Unfocus a memory item, logging on failure."""
    try:
        memory_service.unfocus(memory_id)
    except Exception:
        logger.warning("Failed to unfocus %s", memory_id)


def _matches_artifact_prefix(
    tags: list[str],
    prefixes: tuple[str, ...],
) -> bool:
    """Check if any tag starts with one of the artifact prefixes."""
    return any(tag.startswith(prefix) for tag in tags for prefix in prefixes)
