"""Unified artifact article registry.

Single source of truth for artifact-type article filenames and authored
artifact configurations.  Replaces the three separate article-key dicts
that previously lived in ``artifact_messages.py`` and the
``ARTIFACT_TYPE_CONFIGS`` dict from ``artifact_authoring.py``.

Every artifact type entry declares all three article roles (system frame,
guidance, reinforcement) as required fields.  Empty string means the role
is intentionally absent for that type.  The frozen dataclass constructor
prevents gaps at creation time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from ananta.error_handling import FrameworkError

if TYPE_CHECKING:
    from default_thinking_plugin.artifact_authoring import ArticleLoader

logger = logging.getLogger(__name__)


# ── Types ──────────────────────────────────────────────────────────────


class ArticleRole(Enum):
    """The three roles an artifact article can play in prompt assembly."""

    SYSTEM_FRAME = "system_frame"
    GUIDANCE = "guidance"
    REINFORCEMENT = "reinforcement"


@dataclass(frozen=True, slots=True)
class ArtifactArticles:
    """Article filenames for a single artifact type.

    All three fields are required.  Use empty string to indicate that a
    role is intentionally not provided (e.g., RIS guidance comes via
    support_articles, not from this registry).
    """

    system_frame: str
    guidance: str
    reinforcement: str

    def get(self, role: ArticleRole) -> str:
        """Look up filename by role enum."""
        if role is ArticleRole.SYSTEM_FRAME:
            return self.system_frame
        if role is ArticleRole.GUIDANCE:
            return self.guidance
        return self.reinforcement


@dataclass(frozen=True, slots=True)
class AuthoredArtifactConfig:
    """Configuration for types routed through ``create_authored_artifact``.

    Platform-only artifact types (resolved_intake_state, work_manifest,
    work_breakdown_structure) have ``ArtifactArticles`` entries but do
    NOT need this config — they have dedicated creation methods.

    Context loader policy controls deterministic dependency loading:
    - ``none``: no required parent dependency.
    - ``manifest``: load ``manifests/{parent_id}.md``.
    - ``pipeline_spec_authoring``: load manifest + active Complete Brief Form.
    """

    kb_path_template: str
    db_table: str
    focus_label: str
    id_placeholder: str
    parent_placeholder: str
    context_loader: str  # "manifest" | "none" | "pipeline_spec_authoring"
    defocus_existing_label: bool = False
    blocked_focus_labels: tuple[str, ...] = ()


# ── Article registry ───────────────────────────────────────────────────
#
# Every artifact type that needs prompt articles.  Platform types and
# domain types all live here.  Keyed by full readable name — never
# abbreviations.

ARTIFACT_ARTICLES: Final[dict[str, ArtifactArticles]] = {
    # ── Platform artifact types (dedicated creation methods) ──
    "resolved_intake_state": ArtifactArticles(
        system_frame="artifact_system_frame_resolved_intake_state.md",
        guidance="",  # RIS guidance loaded via support_articles
        reinforcement="artifact_reinforcement_resolved_intake_state.md",
    ),
    "work_manifest": ArtifactArticles(
        system_frame="artifact_system_frame_work_manifest.md",
        guidance="artifact_guidance_work_manifest.md",
        reinforcement="artifact_reinforcement_work_manifest.md",
    ),
    "work_breakdown_structure": ArtifactArticles(
        system_frame="artifact_system_frame_work_breakdown_structure.md",
        guidance="artifact_guidance_work_breakdown_structure.md",
        reinforcement="artifact_reinforcement_work_breakdown_structure.md",
    ),
    # ── Sub-artifact types (dedicated methods, WBS system frame) ──
    "movement_design": ArtifactArticles(
        system_frame="artifact_system_frame_work_breakdown_structure.md",
        guidance="artifact_guidance_movement_design.md",
        reinforcement="artifact_reinforcement_movement_design.md",
    ),
    "phrase_design_ledger": ArtifactArticles(
        system_frame="artifact_system_frame_work_breakdown_structure.md",
        guidance="artifact_guidance_phrase_design_ledger.md",
        reinforcement="artifact_reinforcement_phrase_design_ledger.md",
    ),
    # ── Authored artifact types (generic create_authored_artifact path) ──
    "brief": ArtifactArticles(
        system_frame="artifact_system_frame_complete_brief.md",
        guidance="artifact_guidance_complete_brief.md",
        reinforcement="artifact_reinforcement_complete_brief.md",
    ),
    "pipeline_spec": ArtifactArticles(
        system_frame="artifact_system_frame_pipeline_spec.md",
        guidance="artifact_guidance_pipeline_spec.md",
        reinforcement="artifact_reinforcement_pipeline_spec.md",
    ),
    "composition_design": ArtifactArticles(
        system_frame="artifact_system_frame_composition_design.md",
        guidance="artifact_guidance_composition_design.md",
        reinforcement="artifact_reinforcement_composition_design.md",
    ),
}


# ── Authored artifact configs ──────────────────────────────────────────
#
# Only for types routed through the generic ``create_authored_artifact``
# path.  Each type's articles are looked up from ``ARTIFACT_ARTICLES``
# using the same key.

AUTHORED_ARTIFACT_CONFIGS: Final[dict[str, AuthoredArtifactConfig]] = {
    "brief": AuthoredArtifactConfig(
        kb_path_template="briefs/{artifact_id}.md",
        db_table="thinking_brief",
        focus_label="complete_brief",
        id_placeholder="<<BRIEF_ID>>",
        parent_placeholder="<<MANIFEST_ID>>",
        context_loader="none",
    ),
    "pipeline_spec": AuthoredArtifactConfig(
        kb_path_template="pipeline_specs/{artifact_id}.md",
        db_table="thinking_pipeline_spec",
        focus_label="pipeline_spec",
        id_placeholder="<<SPEC_ID>>",
        parent_placeholder="<<MANIFEST_ID>>",
        context_loader="pipeline_spec_from_design",
        defocus_existing_label=True,
        blocked_focus_labels=(
            "complete_brief",
            "work_manifest",
            "composition_design",
        ),
    ),
    "composition_design": AuthoredArtifactConfig(
        kb_path_template="composition_designs/{artifact_id}.md",
        db_table="thinking_composition_design",
        focus_label="composition_design",
        id_placeholder="<<DESIGN_ID>>",
        parent_placeholder="<<MANIFEST_ID>>",
        context_loader="pipeline_spec_authoring",
        defocus_existing_label=True,
        blocked_focus_labels=("complete_brief", "work_manifest"),
    ),
}


# ── Load function ──────────────────────────────────────────────────────


def load_article(
    artifact_type: str,
    role: ArticleRole,
    article_loader: ArticleLoader,
    *,
    fallback: str = "",
) -> str:
    """Load an artifact article by type and role.

    Returns empty string for intentionally-blank entries (e.g., RIS
    guidance).  Uses *fallback* when the article file cannot be found
    (allows constructor-provided system prompts to serve as default).
    """
    articles = ARTIFACT_ARTICLES.get(artifact_type)
    if articles is None:
        raise FrameworkError(
            message=f"Unknown artifact type: {artifact_type!r}",
            error_code="ARTIFACT_UNKNOWN_TYPE",
        )
    filename = articles.get(role)
    if not filename:
        return ""  # intentionally blank for this type
    content = article_loader.load_article(filename)
    if content is None:
        if fallback:
            return fallback
        logger.warning(
            "Article file %s not found for %s.%s",
            filename, artifact_type, role.value,
        )
        return ""
    return content


def get_authored_config(artifact_type: str) -> AuthoredArtifactConfig:
    """Look up the authored-artifact config for a type.

    Raises FrameworkError for unknown types.
    """
    config = AUTHORED_ARTIFACT_CONFIGS.get(artifact_type)
    if config is None:
        raise FrameworkError(
            message=f"Unknown authored artifact type: {artifact_type!r}",
            error_code="ARTIFACT_UNKNOWN_TYPE",
        )
    return config
