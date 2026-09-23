"""Thinking Service — wrapper over bound thinking plugin.

Follows the InferenceService pattern (lazy first-use):
- Thin wrapper that delegates to bound plugin
- Plugin validated on first method call via _ensure_ready()
- NOT eager init — thinking plugin depends on context_management_service
  which may be initialized later in startup sequence

The wrapper validates the plugin satisfies ``ThinkingProvider``
(``@runtime_checkable`` Protocol). The concrete ABC
``ThinkingServiceInterface`` was deleted in Slice 11C.
All delegation methods return values from an Any-typed plugin
(the wrapper also delegates plan lifecycle methods not on
``ThinkingProvider``) — suppress no-any-return for this file.
"""
# mypy: disable-error-code="no-any-return"

import logging
from typing import Any

from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.thinking_provider_interface import ThinkingProvider

logger = logging.getLogger(__name__)


class ThinkingService:
    """Service wrapper for thinking plugin providers.

    Initialization is LAZY (InferenceService pattern):
    - Plugin name stored in __init__
    - Plugin resolved and validated on first use via _ensure_ready()
    - Avoids startup ordering issues with context_management_service
    """

    def __init__(
        self,
        plugin_manager: PluginManager,
        thinking_plugin_name: str,
    ) -> None:
        if not thinking_plugin_name:
            raise FrameworkError(
                "thinking_plugin_name is required. "
                "Ensure THINKING_SERVICE is bound in config/service_bindings.json."
            )

        self._thinking_plugin_name = thinking_plugin_name
        self._thinking_plugin: Any = None
        self._plugin_manager = plugin_manager

        logger.debug("ThinkingService created with plugin: %s", self._thinking_plugin_name)

    def is_ready(self) -> bool:
        """Check if the thinking service is ready for use."""
        if self._thinking_plugin is None:
            try:
                self._validate_thinking_plugin()
            except FrameworkError:
                return False
        if self._thinking_plugin is None:
            return False
        return self._thinking_plugin.is_ready()

    def get_readiness_error(self) -> str | None:
        """Get error message if not ready, None if ready."""
        if self._thinking_plugin is None:
            try:
                self._validate_thinking_plugin()
            except FrameworkError as e:
                return str(e)
        if self._thinking_plugin is None:
            return f"Thinking plugin '{self._thinking_plugin_name}' not found"
        return self._thinking_plugin.readiness_error

    def _validate_thinking_plugin(self) -> Any:
        """Validate that thinking plugin exists and satisfies ThinkingProvider.

        Uses ``@runtime_checkable`` Protocol isinstance check.

        Returns:
            The validated thinking plugin instance.

        Raises:
            FrameworkError: If plugin not found or doesn't satisfy ThinkingProvider.
        """
        if self._thinking_plugin is None:
            plugin = self._plugin_manager.get_plugin(self._thinking_plugin_name)

            if not isinstance(plugin, ThinkingProvider):
                raise FrameworkError(
                    f"Thinking plugin '{self._thinking_plugin_name}' does not "
                    f"satisfy ThinkingProvider protocol. Plugin type: {type(plugin)}"
                )

            self._thinking_plugin = plugin
            logger.debug("ThinkingService plugin validated (ThinkingProvider)")

        return self._thinking_plugin

    def _ensure_ready(self) -> Any:
        """Ensure thinking plugin exists, satisfies contract, and is ready.

        Returns:
            The thinking plugin instance.

        Raises:
            FrameworkError: If plugin not found, missing methods, or not ready.
        """
        plugin = self._validate_thinking_plugin()

        if not plugin.is_ready():
            error = plugin.readiness_error or "Unknown readiness error"
            raise FrameworkError(
                f"Thinking plugin '{self._thinking_plugin_name}' not ready: {error}"
            )

        return plugin

    # ==========================================================================
    # TASK LIFECYCLE
    # ==========================================================================

    def create_task(
        self,
        title: str,
        prompt: str,
        task_type: str = "plan",
    ) -> dict[str, Any]:
        return self._ensure_ready().create_task(title=title, prompt=prompt, task_type=task_type)

    def continue_task(self, task_id: str, prompt: str) -> dict[str, Any]:
        return self._ensure_ready().continue_task(task_id=task_id, prompt=prompt)

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._ensure_ready().get_task(task_id=task_id)

    def list_tasks(
        self,
        task_type: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().list_tasks(task_type=task_type, status=status)

    def archive_task(self, task_id: str) -> dict[str, Any]:
        return self._ensure_ready().archive_task(task_id=task_id)

    # ==========================================================================
    # PLAN LIFECYCLE
    # ==========================================================================

    @staticmethod
    def _resolve_acting_session(
        session_id: str,
        state: dict[str, Any] | None,
    ) -> str:
        """Resolve the acting session for a plan-focus operation (JOS-02).

        The server-built ``state`` (injected by ActionProcessor, always
        overwritten server-side) is authoritative on the verb path; the
        explicit kwarg is the Python-caller path. No session is fail-fast.
        """
        if state is not None:
            injected = str(state.get("session_id") or "")
            if injected:
                return injected
        if not session_id:
            raise FrameworkError(
                message=(
                    "plan operation requires an acting session; pass "
                    "session_id (Python callers) or invoke via an action "
                    "that carries one (verb callers)"
                ),
                error_code="thinking_service.session_required",
            )
        return session_id

    def advance_current_plan_step(
        self, *, session_id: str,
    ) -> dict[str, Any] | None:
        return self._ensure_ready().advance_current_plan_step(
            session_id=session_id,
        )

    def upsert_plan(
        self,
        content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_acting_session(session_id, state)
        return self._ensure_ready().upsert_plan(
            content=content, session_id=resolved,
        )

    def create_extended_plan(
        self,
        goal: str,
        topic: str | None = None,
        context: str | None = None,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().create_extended_plan(
            goal=goal,
            topic=topic,
            context=context,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def update_plan(
        self,
        task_id: str,
        status_update: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().update_plan(
            task_id=task_id,
            status_update=status_update,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def list_plans(
        self,
        status: str | None = None,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().list_plans(
            status=status,
            session_id=self._resolve_acting_session(session_id, state),
        )

    # ==========================================================================
    # PLAYBOOK LIFECYCLE
    # ==========================================================================

    def create_playbook(
        self,
        goal: str,
        constraints: str | None = None,
        investigation_context: str | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().create_playbook(
            goal=goal,
            constraints=constraints,
            investigation_context=investigation_context,
        )

    def get_playbook(self, playbook_id: str) -> dict[str, Any]:
        return self._ensure_ready().get_playbook(playbook_id=playbook_id)

    def get_playbook_section(
        self,
        playbook_id: str,
        section_id: str,
    ) -> dict[str, Any]:
        return self._ensure_ready().get_playbook_section(
            playbook_id=playbook_id,
            section_id=section_id,
        )

    def list_playbooks(self, status: str | None = None) -> dict[str, Any]:
        return self._ensure_ready().list_playbooks(status=status)

    def patch_playbook(
        self,
        playbook_id: str,
        patch_description: str,
    ) -> dict[str, Any]:
        return self._ensure_ready().patch_playbook(
            playbook_id=playbook_id,
            patch_description=patch_description,
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._ensure_ready().get_plan(plan_id=plan_id)

    # ==========================================================================
    # RESOLVED INTAKE STATE / WORK MANIFEST / COMPOSITION SKETCH / WORK BREAKDOWN STRUCTURE LIFECYCLE
    # ==========================================================================

    def create_resolved_intake_state(
        self,
        intake_id: str,
        content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().create_resolved_intake_state(
            intake_id=intake_id,
            content=content,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def create_work_manifest(
        self,
        content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().create_work_manifest(
            content=content,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def patch_work_manifest(
        self,
        manifest_id: str,
        content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().patch_work_manifest(
            manifest_id=manifest_id,
            content=content,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def create_authored_artifact(
        self,
        artifact_type: str,
        content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().create_authored_artifact(
            artifact_type=artifact_type,
            content=content,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def create_movement_design(
        self,
        manifest_id: str,
        movement_type: str,
        packet_content: str,
        ledger_content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().create_movement_design(
            manifest_id=manifest_id,
            movement_type=movement_type,
            packet_content=packet_content,
            ledger_content=ledger_content,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def patch_authored_artifact(
        self,
        artifact_type: str,
        artifact_id: str,
        content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().patch_authored_artifact(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            content=content,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def patch_work_breakdown_structure(
        self,
        wbs_id: str,
        content: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().patch_work_breakdown_structure(
            wbs_id=wbs_id,
            content=content,
            session_id=self._resolve_acting_session(session_id, state),
        )

    # ==========================================================================
    # AUTHORED-BY-VALUE REGISTRATION (Phase 3 Seam A)
    # ==========================================================================

    def validate_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        phase_number: int,
        manifest_id: str | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().validate_authored_work_breakdown_structure(
            content=content,
            wbs_id=wbs_id,
            phase_number=phase_number,
            manifest_id=manifest_id,
        )

    def register_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().register_authored_work_breakdown_structure(
            content=content,
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            phase_number=phase_number,
            phase_name=phase_name,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def validate_authored_joseki(
        self,
        content: str,
        joseki_key: str | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().validate_authored_joseki(
            content=content,
            joseki_key=joseki_key,
        )

    def register_authored_joseki(
        self,
        content: str,
    ) -> dict[str, Any]:
        return self._ensure_ready().register_authored_joseki(
            content=content,
        )

    # ==========================================================================
    # AUTHORED-JOSEKI LIFECYCLE (Phase 6 §4.3)
    # ==========================================================================

    def transition_authored_joseki(
        self,
        joseki_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().transition_authored_joseki(
            joseki_key=joseki_key,
            target_state=target_state,
            superseded_by=superseded_by,
        )

    def record_authored_joseki_run(
        self,
        joseki_key: str,
        wbs_id: str | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().record_authored_joseki_run(
            joseki_key=joseki_key,
            wbs_id=wbs_id,
        )

    def get_authored_joseki(
        self,
        joseki_key: str,
    ) -> dict[str, Any]:
        return self._ensure_ready().get_authored_joseki(
            joseki_key=joseki_key,
        )

    def reconcile_authored_joseki_row(
        self,
        joseki_key: str,
    ) -> dict[str, Any]:
        return self._ensure_ready().reconcile_authored_joseki_row(
            joseki_key=joseki_key,
        )

    # ==========================================================================
    # PLAN-TEMPLATE CURATION LIFECYCLE (SUB-01, POR §4.5 GOAL)
    # ==========================================================================

    def transition_plan_template(
        self,
        template_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().transition_plan_template(
            template_key=template_key,
            target_state=target_state,
            superseded_by=superseded_by,
        )

    def get_plan_template(
        self,
        template_key: str,
    ) -> dict[str, Any]:
        return self._ensure_ready().get_plan_template(
            template_key=template_key,
        )

    # ==========================================================================
    # PULL-BASED STEP EXECUTION (Phase 4 Seam C)
    # ==========================================================================

    def start_wbs_execution(
        self,
        wbs_id: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().start_wbs_execution(
            wbs_id=wbs_id,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def get_next_wbs_step(
        self,
        wbs_id: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().get_next_wbs_step(
            wbs_id=wbs_id,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def record_wbs_step_observation(
        self,
        wbs_id: str,
        step_number: int,
        process_key: str,
        result: dict[str, Any],
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().record_wbs_step_observation(
            wbs_id=wbs_id,
            step_number=step_number,
            process_key=process_key,
            result=result,
            state_summary=state_summary,
            output_artifacts=output_artifacts,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def advance_wbs_execution(
        self,
        wbs_id: str,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().advance_wbs_execution(
            wbs_id=wbs_id,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def generate_section_stem_wbs(
        self,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
        style_family: str,
        artifact_prefix: str,
        pipeline_spec_id: str | None = None,
        pipeline_spec: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().generate_section_stem_wbs(
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            phase_number=phase_number,
            phase_name=phase_name,
            style_family=style_family,
            artifact_prefix=artifact_prefix,
            pipeline_spec_id=pipeline_spec_id,
            pipeline_spec=pipeline_spec,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def record_work_breakdown_structure_step_state(
        self,
        wbs_id: str,
        step_number: int,
        status: str,
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().record_work_breakdown_structure_step_state(
            wbs_id=wbs_id,
            step_number=step_number,
            status=status,
            state_summary=state_summary,
            output_artifacts=output_artifacts,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def record_work_manifest_phase_state(
        self,
        manifest_id: str,
        phase_number: int,
        status: str,
        outcome_summary: str,
        approved_artifacts: list[str] | None = None,
        next_phase_instruction: str | None = None,
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().record_work_manifest_phase_state(
            manifest_id=manifest_id,
            phase_number=phase_number,
            status=status,
            outcome_summary=outcome_summary,
            approved_artifacts=approved_artifacts,
            next_phase_instruction=next_phase_instruction,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def graft_work_breakdown_structure_segment(
        self,
        wbs_id: str,
        anchor_step_number: int,
        segment: str = "",
        *,
        session_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._ensure_ready().graft_work_breakdown_structure_segment(
            wbs_id=wbs_id,
            anchor_step_number=anchor_step_number,
            segment=segment,
            session_id=self._resolve_acting_session(session_id, state),
        )

    def process_planning_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        return self._ensure_ready().process_planning_results(params=params, state=state)

    def resume_thinking_completion(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        return self._ensure_ready().resume_thinking_completion(
            params=params, state=state,
        )

    # ------------------------------------------------------------------
    # Joseki run driver (Track A, spec 2026-07-05 v3.1) — standard
    # delegation: the bound plugin constructs the engine via the shared
    # factory (joseki_run_wiring.build_joseki_run_engine) using its own
    # orchestrator reference, preserving wrapper↔plugin parity.
    # ------------------------------------------------------------------

    def run_joseki(
        self,
        joseki_key: str,
        bindings: dict[str, Any],
        label: str = "",
    ) -> dict[str, Any]:
        return self._ensure_ready().run_joseki(
            joseki_key=joseki_key, bindings=bindings, label=label,
        )

    def complete_joseki_run(self, wbs_id: str) -> dict[str, Any]:
        return self._ensure_ready().complete_joseki_run(wbs_id=wbs_id)

    def get_joseki_run(self, run_id: str) -> dict[str, Any]:
        return self._ensure_ready().get_joseki_run(run_id=run_id)

    def list_joseki_runs(
        self,
        status: str | None = None,
        joseki_key: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._ensure_ready().list_joseki_runs(
            status=status, joseki_key=joseki_key, limit=limit,
        )

    def reconcile_joseki_runs(self) -> dict[str, Any]:
        return self._ensure_ready().reconcile_joseki_runs()


__all__ = ["ThinkingService"]
