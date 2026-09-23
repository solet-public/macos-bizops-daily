"""Artifact authoring service — persists authored-by-value artifacts.

The qwen push-authoring path (nested thinking-model artifact creation)
was retired per DEP-01: the calling agent authors each document and
passes it by value; this service validates, stores, and focuses it.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ananta.error_handling import FrameworkError

from .artifact_helpers import (
    build_section_index,
    extract_manifest_title,
    normalize_authored_markdown,
    validate_artifact_structure,
    validate_section_sizes,
)
from .artifact_id_derivation import (
    ArtifactIdDerivationError,
    derive_authored_artifact_id,
    derive_brief_id,
)
from .artifact_registry import (
    AuthoredArtifactConfig,
    get_authored_config,
)
from .constants import ErrorCode

logger = logging.getLogger(__name__)

# ── Dependency protocols ─────────────────────────────────────────────


class KnowledgeWriter(Protocol):
    """Write and read artifacts in the knowledge base."""

    def write(self, path: str, content: str) -> None: ...

    def read(self, path: str) -> str: ...


class StateStore(Protocol):
    """Read/write state records and generate IDs."""

    def write_state(self, namespace: str, data: dict[str, object]) -> Any: ...

    def read_state(
        self,
        namespace: str,
        query: dict[str, object],
    ) -> dict[str, Any]: ...

    def generate_id(self, *, prefix: str) -> str: ...


class FocusManager(Protocol):
    """Manage focused memory documents."""

    def upsert(self, content: str, *, doc_tag: str, label: str) -> str: ...

    def defocus_by_label(self, label: str) -> None: ...

    def get_focused(self) -> list[dict[str, Any]]: ...


class ArticleLoader(Protocol):
    """Load knowledge base articles by filename."""

    def load_article(self, filename: str) -> str | None: ...


# Article registry, configs, and load functions are in artifact_registry.py


# All artifact system frames instruct the model to keep ## sections
# under 2500 chars (hard limit 3000).  Validation enforces this
# uniformly — no tables are exempt.
_SKIP_SECTION_VALIDATION_TABLES: tuple[str, ...] = ()


# ── Service class ────────────────────────────────────────────────────


class ArtifactAuthoringService:
    """Validates, stores, and focuses authored-by-value artifacts."""

    def __init__(
        self,
        knowledge_writer: KnowledgeWriter,
        state_store: StateStore,
        focus_manager: FocusManager,
        *,
        namespace: str = "default_thinking_plugin",
    ) -> None:
        self._knowledge_writer = knowledge_writer
        self._state_store = state_store
        self._focus_manager = focus_manager
        self._namespace = namespace
        self._logger = logging.getLogger(__name__)

    # ── Public artifact creation ─────────────────────────────────────

    def create_resolved_intake_state(
        self,
        intake_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Persist an authored-by-value Resolved Intake State artifact."""
        _require_non_empty(intake_id, "intake_id")
        _require_authored_content("create_resolved_intake_state", content)

        content = normalize_authored_markdown(content)
        validate_section_sizes(content, intake_id)

        kb_path = f"intake_states/{intake_id}.md"
        self._knowledge_writer.write(kb_path, content)
        self._state_store.write_state(
            namespace=self._namespace,
            data={
                "table": "thinking_intake_state",
                "record": {
                    "id": intake_id,
                    "status": "active",
                    "knowledge_base_path": kb_path,
                },
            },
        )
        self._focus_manager.upsert(
            content,
            doc_tag=f"resolved_intake_state:{intake_id}",
            label="resolved_intake_state",
        )
        self._logger.info(
            "Resolved Intake State %s created at %s",
            intake_id,
            kb_path,
        )
        return {"intake_id": intake_id, "status": "created", "content": content}

    def create_work_manifest(
        self,
        content: str,
    ) -> dict[str, Any]:
        """Persist an authored-by-value Work Manifest document.

        The ``manifest_id`` is derived deterministically from the focused
        Complete Brief Form's ``composition_number`` and ``genre`` fields.
        The brief is created at the prior plan step and stays focused
        through manifest creation. The caller never authors the id.

        Idempotent: returns the existing manifest when one already exists
        under the derived ``manifest_id`` rather than storing again.
        """
        _require_authored_content("create_work_manifest", content)

        brief_id = _load_focused_brief_id(self._focus_manager)
        if not brief_id:
            raise ArtifactIdDerivationError(
                "create_work_manifest: no focused Complete Brief Form — "
                "brief authoring must complete before manifest creation"
            )
        manifest_id = f"wmf-{brief_id.removeprefix('brf-')}"

        existing_result = self._state_store.read_state(
            namespace=self._namespace,
            query={
                "table": "thinking_manifest",
                "filters": {"id": manifest_id, "is_deleted": 0},
                "limit": 1,
            },
        )
        existing_records = existing_result.get("records", [])
        if existing_records:
            kb_path = existing_records[0].get("knowledge_base_path", f"manifests/{manifest_id}.md")
            existing_content = self._knowledge_writer.read(kb_path)
            if existing_content:
                self._logger.info(
                    "Work Manifest %s already exists — returning existing artifact", manifest_id
                )
                self._focus_manager.upsert(
                    existing_content,
                    doc_tag=f"work_manifest:{manifest_id}",
                    label="work_manifest",
                )
                return {
                    "manifest_id": manifest_id,
                    "status": "existing",
                    "content": existing_content,
                }

        content = normalize_authored_markdown(content)

        # The manifest consumes the Resolved Intake State — defocus it
        # so it no longer appears in subsequent prompts.
        self._focus_manager.defocus_by_label("resolved_intake_state")

        kb_path = f"manifests/{manifest_id}.md"
        title = extract_manifest_title(content)
        artifact_memory_id = self._store_artifact(
            content=content,
            kb_path=kb_path,
            db_table="thinking_manifest",
            db_record={
                "id": manifest_id,
                "status": "active",
                "title": title,
                "knowledge_base_path": kb_path,
            },
            focus_label="work_manifest",
            focus_tag=f"work_manifest:{manifest_id}",
            defocus_first=False,
        )

        self._logger.info("Work Manifest %s created at %s", manifest_id, kb_path)
        return {
            "manifest_id": manifest_id,
            "status": "created",
            "content": content,
            "source_memory_id": artifact_memory_id,
        }

    def patch_work_manifest(
        self,
        manifest_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Revise an existing Work Manifest document."""
        _require_non_empty(manifest_id, "manifest_id")
        _require_non_empty(content, "content")
        validate_section_sizes(content, manifest_id)

        kb_path = f"manifests/{manifest_id}.md"
        self._knowledge_writer.write(kb_path, content)

        title = extract_manifest_title(content)
        self._state_store.write_state(
            namespace=self._namespace,
            data={
                "table": "thinking_manifest",
                "record": {
                    "id": manifest_id,
                    "status": "active",
                    "title": title,
                    "knowledge_base_path": kb_path,
                },
            },
        )
        self._focus_manager.upsert(
            content,
            doc_tag=f"work_manifest:{manifest_id}",
            label="work_manifest",
        )
        self._logger.info("Work Manifest %s written to %s", manifest_id, kb_path)
        return {"manifest_id": manifest_id, "status": "updated"}

    def create_authored_artifact(
        self,
        artifact_type: str,
        content: str,
    ) -> dict[str, Any]:
        """Persist an authored-by-value artifact document.

        ``artifact_id`` and ``parent_id`` are derived deterministically:

        - For ``brief`` (created before any manifest exists), the id is
          derived from the authored document's ``composition_number`` and
          ``genre`` fields. The brief has no parent.
        - For all other types, the id and parent_id come from the focused
          Work Manifest in scope. The artifact_id is the manifest_id with
          its ``wmf-`` prefix swapped for the artifact-type prefix.

        The caller never authors identifiers.
        """
        _require_non_empty(artifact_type, "artifact_type")
        _require_authored_content("create_authored_artifact", content)

        config = get_authored_config(artifact_type)
        artifact_id, parent_id = self.derive_authored_ids(
            artifact_type, content,
        )

        content = normalize_authored_markdown(content)

        # P4: Structural validation of the authored document
        structural_errors = validate_artifact_structure(
            content,
            artifact_type,
            artifact_id,
        )
        if structural_errors:
            raise ValueError(f"Structural validation failed for {artifact_type} {artifact_id}: {'; '.join(structural_errors)}")

        kb_path = config.kb_path_template.format(artifact_id=artifact_id)
        db_record = _build_db_record(artifact_id, parent_id, kb_path)
        artifact_memory_id = self._store_artifact(
            content=content,
            kb_path=kb_path,
            db_table=config.db_table,
            db_record=db_record,
            focus_label=config.focus_label,
            focus_tag=f"{config.focus_label}:{artifact_id}",
            defocus_first=config.defocus_existing_label,
        )

        self._logger.info(
            "Authored artifact [%s] %s created for parent %s at %s",
            artifact_type,
            artifact_id,
            parent_id,
            kb_path,
        )
        return {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "parent_id": parent_id,
            "status": "created",
            "content": content,
            "knowledge_base_path": kb_path,
            "source_memory_id": artifact_memory_id,
        }

    def create_movement_design(
        self,
        manifest_id: str,
        movement_type: str,
        packet_content: str,
        ledger_content: str,
    ) -> dict[str, Any]:
        """Persist an authored Movement Design Packet and Phrase Design Ledger."""
        _require_non_empty(manifest_id, "manifest_id")
        _require_non_empty(movement_type, "movement_type")
        _require_authored_content(
            "create_movement_design", packet_content, param="packet_content",
        )
        _require_authored_content(
            "create_movement_design", ledger_content, param="ledger_content",
        )

        packet_result = self._author_movement_design_packet(
            manifest_id,
            movement_type,
            packet_content,
        )
        ledger_result = self._author_phrase_design_ledger(
            manifest_id,
            movement_type,
            packet_result["packet_id"],
            ledger_content,
        )
        return {
            "packet_id": packet_result["packet_id"],
            "ledger_id": ledger_result["ledger_id"],
            "manifest_id": manifest_id,
            "movement_type": movement_type,
            "status": "created",
        }

    def invoke_pipeline_spec_authoring(
        self,
        spec_id: str,
        manifest_id: str,
        content: str,
    ) -> str:
        """Accept an authored-by-value Pipeline Spec payload.

        Returns the raw authored ``content`` for JSON parsing — no
        markdown normalization, no structural markdown validation, no
        KB write. The caller is responsible for parsing JSON, persisting
        to blob storage, and recording state.
        """
        _require_non_empty(spec_id, "spec_id")
        _require_non_empty(manifest_id, "manifest_id")
        _require_authored_content("create_authored_artifact[pipeline_spec]", content)
        return content

    def patch_authored_artifact(
        self,
        artifact_type: str,
        artifact_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Revise an existing authored artifact document."""
        _require_non_empty(artifact_type, "artifact_type")
        _require_non_empty(artifact_id, "artifact_id")
        _require_non_empty(content, "content")

        config = get_authored_config(artifact_type)
        if config.db_table not in _SKIP_SECTION_VALIDATION_TABLES:
            validate_section_sizes(content, artifact_id)

        kb_path = self._resolve_existing_kb_path(config, artifact_id)

        self._knowledge_writer.write(kb_path, content)
        self._focus_manager.upsert(
            content,
            doc_tag=f"{config.focus_label}:{artifact_id}",
            label=config.focus_label,
        )

        self._logger.info(
            "Authored artifact [%s] %s updated at %s",
            artifact_type,
            artifact_id,
            kb_path,
        )
        return {"artifact_id": artifact_id, "status": "updated"}

    # ── Storage helpers ──────────────────────────────────────────────

    def _store_artifact(
        self,
        *,
        content: str,
        kb_path: str,
        db_table: str,
        db_record: dict[str, Any],
        focus_label: str,
        focus_tag: str,
        defocus_first: bool,
    ) -> str:
        """Store an authored artifact in knowledge base, state, and focus.

        Returns the memory_id of the focused artifact.
        """
        artifact_id = db_record.get("id", kb_path)
        # Always persist to KB — cross-step retrieval (e.g., WBS reads
        # sketch from KB fallback after focus churn) depends on the KB
        # path being populated.
        if db_table not in _SKIP_SECTION_VALIDATION_TABLES:
            validate_section_sizes(content, artifact_id)
        index = build_section_index(content, str(artifact_id), db_table)
        self._logger.info(
            "SECTION_INDEX: %s type=%s sections=%d max_chars=%d",
            artifact_id,
            db_table,
            len(index),
            max((s["char_count"] for s in index), default=0),
        )
        self._state_store.write_state(
            namespace=self._namespace,
            data={"table": db_table, "record": db_record},
        )
        if defocus_first:
            self._focus_manager.defocus_by_label(focus_label)
        memory_id = self._focus_manager.upsert(
            content,
            doc_tag=focus_tag,
            label=focus_label,
        )
        # KB write after focus — always persist so cross-step retrieval
        # and WBS parent loading have a durable path.
        self._knowledge_writer.write(kb_path, content)
        return memory_id

    # ── Context loading helpers ──────────────────────────────────────

    def derive_authored_ids(
        self,
        artifact_type: str,
        content: str,
    ) -> tuple[str, str]:
        """Derive ``(artifact_id, parent_id)`` for an authored artifact.

        Brief is special-cased because it is created before any manifest
        exists — its parent_id is empty and its artifact_id comes from
        the authored document's structured fields. All other types inherit
        the run identity from the focused brief by prefix swap.

        The brief is the trustworthy source of run identity: its id is
        derived deterministically from the original directive and the
        platform substitutes it into the brief's content. The focused
        manifest's content is NOT trusted for id parsing because the
        thinking model has been observed truncating the substituted
        identifier when copying it into the manifest body.

        Public so the plugin wrapper can derive IDs for the structured
        ``pipeline_spec`` path that bypasses ``create_authored_artifact``.
        """
        if artifact_type == "brief":
            return derive_brief_id(content), ""
        brief_id = _load_focused_brief_id(self._focus_manager)
        if not brief_id:
            raise ArtifactIdDerivationError(
                f"create_authored_artifact[{artifact_type}]: no focused "
                "Complete Brief Form — cannot derive run identifiers"
            )
        manifest_id = f"wmf-{brief_id.removeprefix('brf-')}"
        artifact_id = derive_authored_artifact_id(manifest_id, artifact_type)
        return artifact_id, manifest_id

    def _resolve_existing_kb_path(
        self,
        config: AuthoredArtifactConfig,
        artifact_id: str,
    ) -> str:
        """Look up the existing knowledge base path or derive one."""
        return _resolve_kb_path(
            config,
            artifact_id,
            self._state_store,
            self._namespace,
        )

    # ── Movement design sub-steps ────────────────────────────────────

    def _author_movement_design_packet(
        self,
        manifest_id: str,
        movement_type: str,
        content: str,
    ) -> dict[str, Any]:
        """Store the authored Movement Design Packet (artifact 1 of 2)."""
        packet_id: str = self._state_store.generate_id(prefix="mdp-")

        content = normalize_authored_markdown(content)

        kb_path = f"movement_design_packets/{packet_id}.md"
        self._store_artifact(
            content=content,
            kb_path=kb_path,
            db_table="thinking_movement_design_packet",
            db_record={
                "id": packet_id,
                "manifest_id": manifest_id,
                "movement_type": movement_type,
                "status": "active",
                "knowledge_base_path": kb_path,
            },
            focus_label="movement_design_packet",
            focus_tag=f"movement_design_packet:{packet_id}",
            defocus_first=True,
        )
        self._logger.info(
            "Movement Design Packet %s created for manifest %s movement %s",
            packet_id,
            manifest_id,
            movement_type,
        )
        return {"packet_id": packet_id, "content": content}

    def _author_phrase_design_ledger(
        self,
        manifest_id: str,
        movement_type: str,
        packet_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Store the authored Phrase Design Ledger (artifact 2 of 2)."""
        ledger_id: str = self._state_store.generate_id(prefix="pdl-")

        content = normalize_authored_markdown(content)

        kb_path = f"phrase_design_ledgers/{ledger_id}.md"
        self._store_artifact(
            content=content,
            kb_path=kb_path,
            db_table="thinking_phrase_design_ledger",
            db_record={
                "id": ledger_id,
                "manifest_id": manifest_id,
                "movement_type": movement_type,
                "packet_id": packet_id,
                "status": "active",
                "knowledge_base_path": kb_path,
            },
            focus_label="phrase_design_ledger",
            focus_tag=f"phrase_design_ledger:{ledger_id}",
            defocus_first=True,
        )
        self._logger.info(
            "Phrase Design Ledger %s created for packet %s",
            ledger_id,
            packet_id,
        )
        return {"ledger_id": ledger_id, "content": content}


# ── Module-level helpers ─────────────────────────────────────────────


# ── Context loading helpers (extracted from class for size budget) ────

def _load_focused_brief_id(focus_manager: FocusManager) -> str:
    """Return the focused Complete Brief's id from its focus tag.

    The brief is upserted to focus with the doc_tag ``complete_brief:{brief_id}``
    stored in the memory's ``tags`` list (alongside the ``complete_brief``
    label) — see ``DefaultThinkingPlugin._focus_document_with_doc_tag``.
    Reading the id from this tag is the source of truth — the brief's
    markdown body is authored by the thinking model, which has been
    observed producing briefs that nest the ``brief_id`` field in YAML
    structures (copied from the form-schema support article) instead of
    the simple ``- brief_id: brf-...`` line the guidance specifies.

    The platform already owns this identifier; deriving children from
    focus metadata removes any dependency on the brief's authored shape.
    """
    for item in focus_manager.get_focused():
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("complete_brief:"):
                return tag.split(":", 1)[1]
    return ""

def _resolve_kb_path(
    config: AuthoredArtifactConfig,
    artifact_id: str,
    ss: StateStore,
    namespace: str,
) -> str:
    """Look up existing KB path for an artifact, or derive one."""
    result = ss.read_state(
        namespace=namespace,
        query={"table": config.db_table, "filters": {"id": artifact_id, "is_deleted": 0}},
    )
    records = result.get("data", {}).get("records", [])
    if records:
        path = str(records[0].get("knowledge_base_path", ""))
        if path:
            return path
    return config.kb_path_template.format(artifact_id=artifact_id)


def _require_non_empty(value: str, name: str) -> None:
    """Raise FrameworkError if value is empty or falsy."""
    if not value:
        raise FrameworkError(
            message=f"{name} is required",
            error_code=ErrorCode.PARAMETER_ERROR,
        )


def _require_authored_content(
    verb: str,
    content: str,
    *,
    param: str = "content",
) -> None:
    """Fail loud when a converted verb is invoked without authored content."""
    if not content or not content.strip():
        raise FrameworkError(
            message=(
                f"{verb} requires authored-by-value `{param}` — the qwen "
                "thinking-model authoring path was retired (DEP-01); a "
                "frontier agent must author the artifact and pass it by value."
            ),
            error_code=ErrorCode.AUTHORED_CONTENT_REQUIRED,
        )


def _build_db_record(
    artifact_id: str,
    parent_id: str,
    kb_path: str,
) -> dict[str, Any]:
    """Build the state-service DB record for an authored artifact."""
    return {
        "id": artifact_id,
        "manifest_id": parent_id,
        "status": "active",
        "knowledge_base_path": kb_path,
    }
