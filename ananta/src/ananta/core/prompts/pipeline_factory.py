"""PromptPipelineFactory — constructs configured pipeline instances from profiles.

Stages like ContextStage require injected services/config. The factory
holds the dependency bundle and maps ``stage_keys`` to configured instances.
This replaces the current pattern where the inference plugin constructs
stages with direct service references.

The inference plugin creates the factory once (with its service references)
and uses it to produce pipelines for any profile. Other callers (thinking
plugin via Unit 13A) call ``assemble_prompt()`` on the service interface,
which uses the same factory internally.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ananta.core.prompts.decode.step_schema import (
    PlanAdvancer,
    ProcessArgLookup,
)
from ananta.core.prompts.pipeline import PromptPipeline
from ananta.core.prompts.profiles import PromptAssemblyProfile
from ananta.core.prompts.stages import (
    APIStage,
    ArtifactPromptStage,
    CatalogStage,
    DecodeContractStage,
    GuidanceStage,
    PlanStateStage,
    TemplateStage,
)

logger = logging.getLogger(__name__)


class FormatStageLike(Protocol):
    """Protocol for FormatStage (avoids importing the concrete class)."""

    @property
    def name(self) -> str: ...
    def execute(self, ctx: Any) -> Any: ...


class ContextStageLike(Protocol):
    """Protocol for ContextStage (avoids importing concrete service types)."""

    @property
    def name(self) -> str: ...
    def execute(self, ctx: Any) -> Any: ...


@dataclass
class PipelineDependencies:
    """Dependency bundle for constructing pipeline stages.

    All fields are optional — not every profile needs every dependency.
    The factory checks profile flags before accessing dependencies.
    """

    # FormatStage — pre-constructed (holds prompts dir, template engine)
    format_stage: FormatStageLike | None = None

    # ContextStage — pre-constructed (holds services, config)
    context_stage: ContextStageLike | None = None

    # CatalogStage — platform service reference (no plugin callback)
    catalog_data_source: Any | None = None  # CatalogDataSource protocol

    # GuidanceStage — platform service references (no plugin callback for step guidance)
    guidance_article_reader: Any | None = None  # GuidanceArticleReader protocol
    guidance_process_lookup: Any | None = None  # ProcessDataLookup protocol
    # Transitional: vertex enrichment (trailer, action shapes) is a
    # separate concern from step guidance.  Stays as a plugin callback
    # until F4 (APIStage/ContextStage splitting) extracts it.
    vertex_enricher: Callable[[Any], None] | None = None

    # DecodeContractStage — platform service references (no plugin callback)
    process_arg_lookup: ProcessArgLookup | None = None
    plan_advancer: PlanAdvancer | None = None

    # Additional stage constructors for extensibility
    extra_stages: dict[str, Any] = field(default_factory=dict)


class PromptPipelineFactory:
    """Constructs configured pipeline instances from profiles.

    The factory holds the dependency bundle and maps stage_keys to
    configured instances.  Profile flags control which stages are
    included — stages that don't apply to a profile are skipped.
    """

    def __init__(self, deps: PipelineDependencies) -> None:
        self._deps = deps

    def create_pipeline(
        self, profile: PromptAssemblyProfile,
    ) -> PromptPipeline:
        """Create a configured pipeline for the given profile.

        Resolves stage keys to instances, respecting profile flags.
        """
        stages: list[Any] = []

        for key in profile.stage_keys:
            stage = self._resolve_stage(key, profile)
            if stage is not None:
                stages.append(stage)

        logger.info(
            "PIPELINE_FACTORY: Created %d-stage pipeline for profile '%s': %s",
            len(stages), profile.name,
            ", ".join(s.name for s in stages),
        )
        return PromptPipeline(stages)

    def _resolve_stage(
        self,
        key: str,
        profile: PromptAssemblyProfile,
    ) -> Any | None:
        """Resolve a stage key to a configured instance.

        Returns None when a stage should be skipped (profile flag is off
        or dependency is missing).
        """
        resolver = self._stage_resolvers.get(key)
        if resolver is not None:
            return resolver(self, profile)

        extra = self._deps.extra_stages.get(key)
        if extra is not None:
            return extra

        logger.warning("PIPELINE_FACTORY: Unknown stage key '%s' — skipped", key)
        return None

    # ── Individual stage resolvers ──────────────────────────────────

    @staticmethod
    def _resolve_template(_self: PromptPipelineFactory, _profile: PromptAssemblyProfile) -> Any:
        return TemplateStage()

    @staticmethod
    def _resolve_format(factory: PromptPipelineFactory, _profile: PromptAssemblyProfile) -> Any | None:
        return factory._deps.format_stage

    @staticmethod
    def _resolve_context(factory: PromptPipelineFactory, _profile: PromptAssemblyProfile) -> Any | None:
        return factory._deps.context_stage

    @staticmethod
    def _resolve_plan_state(_self: PromptPipelineFactory, _profile: PromptAssemblyProfile) -> Any:
        return PlanStateStage()

    @staticmethod
    def _resolve_catalog(factory: PromptPipelineFactory, profile: PromptAssemblyProfile) -> Any | None:
        if not profile.include_process_catalog:
            return None
        if factory._deps.catalog_data_source is None:
            raise RuntimeError(
                "CatalogStage requires catalog_data_source in "
                "PipelineDependencies — no fallback"
            )
        return CatalogStage(
            catalog_source=factory._deps.catalog_data_source,
        )

    @staticmethod
    def _resolve_guidance(factory: PromptPipelineFactory, profile: PromptAssemblyProfile) -> Any | None:
        if not profile.include_step_guidance:
            return None
        if factory._deps.guidance_article_reader is None:
            raise RuntimeError(
                "GuidanceStage requires guidance_article_reader in "
                "PipelineDependencies — no fallback"
            )
        if factory._deps.guidance_process_lookup is None:
            raise RuntimeError(
                "GuidanceStage requires guidance_process_lookup in "
                "PipelineDependencies — no fallback"
            )
        return GuidanceStage(
            article_reader=factory._deps.guidance_article_reader,
            process_lookup=factory._deps.guidance_process_lookup,
            vertex_enricher=factory._deps.vertex_enricher,
        )

    @staticmethod
    def _resolve_decode_contract(factory: PromptPipelineFactory, profile: PromptAssemblyProfile) -> Any | None:
        if not profile.include_decode_contract:
            return None
        if factory._deps.process_arg_lookup is None:
            raise RuntimeError(
                "DecodeContractStage requires process_arg_lookup in "
                "PipelineDependencies — no fallback"
            )
        return DecodeContractStage(
            process_arg_lookup=factory._deps.process_arg_lookup,
            plan_advancer=factory._deps.plan_advancer,
        )

    @staticmethod
    def _resolve_artifact_prompt(_self: PromptPipelineFactory, _profile: PromptAssemblyProfile) -> Any:
        return ArtifactPromptStage()

    @staticmethod
    def _resolve_api(_self: PromptPipelineFactory, _profile: PromptAssemblyProfile) -> Any:
        return APIStage()

    _stage_resolvers: dict[str, Callable[[PromptPipelineFactory, PromptAssemblyProfile], Any | None]] = {
        "template": _resolve_template,
        "format": _resolve_format,
        "context": _resolve_context,
        "plan_state": _resolve_plan_state,
        "catalog": _resolve_catalog,
        "guidance": _resolve_guidance,
        "decode_contract": _resolve_decode_contract,
        "artifact_prompt": _resolve_artifact_prompt,
        "api": _resolve_api,
    }
