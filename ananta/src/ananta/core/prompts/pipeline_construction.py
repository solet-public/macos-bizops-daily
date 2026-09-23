"""Platform-owned PipelineDependencies construction (shared core home).

Builds the ``PipelineDependencies`` bundle from orchestrator-resolved services.
Provider-independent: every adapter wraps a platform service resolved from the
orchestrator (memory, discovery, context-management, plan-lifecycle) — no
inference plugin, no inference-service coupling. It lives here in ``core/prompts``
so BOTH the inference service (for ``assemble_prompt``) and the context service
(for ``assemble_agent_context``, Phase 2) share one construction path without the
context service importing the inference package.

Relocated from ``services/inference_service/pipeline_construction.py`` 2026-07-02
(Phase 2 decoupling; operator-approved) — the code is unchanged, only the home.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ananta.error_handling import FrameworkError

if TYPE_CHECKING:
    from ananta.core.prompts.pipeline_factory import PipelineDependencies
    from ananta.core.services.prompt_context_builder import MemoryServiceProtocol
    from ananta.services.context_management.config import ContextManagementConfig
    from ananta.services.context_management.service import ContextManagementService

logger = logging.getLogger(__name__)

# Knowledge base paths for guidance articles
_GUIDANCE_KB_NAME = "prospection_and_goal_directed_planning"
_GUIDANCE_EXAMPLES_SUBDIR = "examples"


def build_pipeline_dependencies(
    app_home: str,
    context_config: ContextManagementConfig,
    orchestrator: object,
    state_service: object | None,
) -> PipelineDependencies:
    """Construct PipelineDependencies from platform services.

    All adapters wrap platform services resolved from the orchestrator.
    No plugin methods or callbacks.
    """
    from ananta.core.orchestration.service_bindings import ServiceName
    from ananta.core.prompts.pipeline_factory import PipelineDependencies
    from ananta.core.prompts.plan_drivers.guidance_drivers import (
        DiscoveryProcessDataLookup,
    )
    from ananta.core.prompts.stages import ContextStage, FormatStage
    from ananta.core.prompts.vertex_enrichment import enrich_vertex
    from ananta.core.services.prompt_context_builder import PromptContextBuilder

    get_svc = getattr(orchestrator, "get_service", None)
    if not callable(get_svc):
        raise FrameworkError("orchestrator missing get_service")

    # Resolve required services
    memory_svc_raw = get_svc(ServiceName.MEMORY_SERVICE)
    if memory_svc_raw is None:
        raise FrameworkError("Memory service required for pipeline")
    memory_svc = cast("MemoryServiceProtocol", memory_svc_raw)
    discovery_svc = get_svc("discovery_service")
    if discovery_svc is None:
        raise FrameworkError("Discovery service required for pipeline")

    # Context management (optional — delegated mode has no context service)
    ctx_mgmt = _resolve_context_management(orchestrator)

    # File paths
    prompts_dir = Path(app_home) / "config" / "prompts"
    if not prompts_dir.exists():
        raise FrameworkError(f"prompts_dir not found: {prompts_dir}")
    guidance_dir = (
        Path(app_home).parent / "knowledge_bases"
        / _GUIDANCE_KB_NAME / _GUIDANCE_EXAMPLES_SUBDIR
    )

    # FormatStage
    format_stage = FormatStage(prompts_dir)

    # ContextStage
    content_storage = ctx_mgmt.content_storage if ctx_mgmt else None
    playbook_reader = _build_playbook_reader(orchestrator)
    context_stage = ContextStage(
        PromptContextBuilder(memory_svc),
        context_management_service=ctx_mgmt,
        content_storage=content_storage,
        context_config=context_config,
        memory_service=memory_svc,
        playbook_section_reader=playbook_reader,
    )

    # Catalog adapter
    catalog = _CatalogAdapter(discovery_svc)

    # Guidance article reader
    article_reader = _ArticleReader(guidance_dir)

    # Guidance process lookup (platform adapter)
    guidance_lookup = DiscoveryProcessDataLookup(
        discovery_service=discovery_svc,
        state_service=state_service,
    )

    # Process arg lookup
    arg_lookup = _ProcessArgAdapter(discovery_svc)

    # Plan advancer
    plan_svc = get_svc(ServiceName.PLAN_LIFECYCLE_SERVICE)
    plan_advancer = _PlanAdvancerAdapter(plan_svc)

    return PipelineDependencies(
        format_stage=format_stage,
        context_stage=context_stage,
        catalog_data_source=catalog,
        guidance_article_reader=article_reader,
        guidance_process_lookup=guidance_lookup,
        vertex_enricher=enrich_vertex,
        process_arg_lookup=arg_lookup,
        plan_advancer=plan_advancer,
    )


def _resolve_context_management(
    orchestrator: object,
) -> ContextManagementService | None:
    """Resolve context management service with type narrowing."""
    from ananta.services.context_management.service import ContextManagementService

    get_svc = getattr(orchestrator, "get_service", None)
    if not callable(get_svc):
        return None
    svc = get_svc("context_management_service")
    return svc if isinstance(svc, ContextManagementService) else None


def _build_playbook_reader(
    orchestrator: object,
) -> Any:
    """Build playbook section reader from plan lifecycle service."""
    from ananta.core.orchestration.service_bindings import ServiceName

    get_svc = getattr(orchestrator, "get_service", None)
    if not callable(get_svc):
        return None
    svc = get_svc(ServiceName.PLAN_LIFECYCLE_SERVICE)
    if not svc:
        return None
    get_section = getattr(svc, "get_playbook_section", None)
    if not callable(get_section):
        return None

    def reader(playbook_id: str, section_id: str) -> str:
        result = get_section(playbook_id, section_id)
        if not isinstance(result, dict):
            msg = f"get_playbook_section returned non-dict: {type(result)}"
            raise ValueError(msg)
        content = result.get("content", "")
        if not isinstance(content, str) or not content:
            msg = f"Playbook section empty: {playbook_id}/{section_id}"
            raise ValueError(msg)
        return content

    return reader


class _CatalogAdapter:
    """CatalogDataSource using discovery service directly."""

    def __init__(self, discovery: Any) -> None:
        self._discovery = discovery

    def get_system_prompt_processes(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = self._discovery.get_system_prompt_processes()
        return result

    def get_all_io_processes(self) -> list[dict[str, object]]:
        all_procs: dict[str, dict[str, object]] = self._discovery.get_all_processes()
        io: list[dict[str, object]] = []
        for key, data in all_procs.items():
            parts = key.split("::")
            if len(parts) >= 3 and parts[0] == "plugin" and parts[2] == "post_message":
                io.append({
                    "process_key": key,
                    "description": data.get("description", ""),
                    "invocation_schema": data.get("invocation_schema", {}),
                })
        io.sort(key=lambda p: str(p.get("process_key", "")))
        return io

    def get_process_by_key(self, process_key: str) -> dict[str, object] | None:
        result = self._discovery.get_process_by_key(process_key)
        return result if isinstance(result, dict) else None


class _ArticleReader:
    """GuidanceArticleReader using filesystem directly."""

    def __init__(self, articles_dir: Path) -> None:
        self._dir = articles_dir

    def read_article(self, article_name: str) -> str | None:
        path = self._dir / article_name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8").rstrip("\n")


class _ProcessArgAdapter:
    """ProcessArgLookup using discovery service directly."""

    def __init__(self, discovery: Any) -> None:
        self._discovery = discovery

    def get_arg_properties(
        self, process_key: str,
    ) -> dict[str, dict[str, object]]:
        from ananta.core.prompts.decode.action_schema import (
            extract_invocation_arg_properties,
        )

        data = self._discovery.get_process_by_key(process_key)
        if not isinstance(data, dict):
            return {}
        # Return the full property dict — the output_schema build site
        # caps the merged total, and runtime callers (bound-arg type
        # checks, validation) need every key to avoid the silent-coerce
        # hole on properties past the per-process cap.
        return extract_invocation_arg_properties(data)

    def get_required_properties(self, process_key: str) -> set[str]:
        args = self._navigate_args(process_key)
        if args is None:
            return set()
        req = args.get("required")
        return {str(r) for r in req} if isinstance(req, list) else set()

    def get_declared_properties(self, process_key: str) -> set[str]:
        args = self._navigate_args(process_key)
        if args is None:
            return set()
        props = args.get("properties")
        return set(props.keys()) if isinstance(props, dict) else set()

    def _navigate_args(self, process_key: str) -> dict[str, object] | None:
        data = self._discovery.get_process_by_key(process_key)
        if not isinstance(data, dict):
            return None
        schema = data.get("invocation_schema")
        if not isinstance(schema, dict):
            return None
        outer = schema.get("properties")
        if not isinstance(outer, dict):
            return None
        args = outer.get("arguments")
        return args if isinstance(args, dict) else None


class _PlanAdvancerAdapter:
    """PlanAdvancer using plan lifecycle service (session-scoped, JOS-02)."""

    def __init__(self, plan_svc: Any) -> None:
        self._svc = plan_svc

    def advance_current_plan_step(
        self, *, session_id: str,
    ) -> dict[str, Any] | None:
        if self._svc is None:
            raise RuntimeError("Plan lifecycle service unavailable")
        result: dict[str, Any] | None = self._svc.advance_current_plan_step(
            session_id=session_id,
        )
        return result
