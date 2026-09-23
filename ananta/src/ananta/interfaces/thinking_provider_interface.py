"""ThinkingProvider — narrow provider contract for thinking model invocation.

Model-specific reasoning operations. A replacement thinking plugin
must implement ONLY these methods. All deterministic CRUD and
lifecycle operations live in platform services
(``PlanLifecycleServiceInterface``, ``WbsLifecycleServiceInterface``).

Signatures match the ``DefaultThinkingPlugin`` implementation and
the ``ThinkingServiceAPI`` process registry definitions.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ananta.core.domain.types import ActionResult


@runtime_checkable
class ThinkingProvider(Protocol):
    """Provider contract for thinking model invocation.

    Methods that invoke the thinking model to generate content.
    Deterministic operations (plan CRUD, WBS state, playbooks)
    are on ``PlanLifecycleServiceInterface`` and
    ``WbsLifecycleServiceInterface``.
    """

    def is_ready(self) -> bool: ...
    def get_readiness_error(self) -> str | None: ...

    # Task management (model invocation)
    def create_task(
        self,
        title: str,
        prompt: str,
        task_type: str = "plan",
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]: ...

    def continue_task(self, task_id: str, prompt: str) -> dict[str, Any]: ...

    # Plan authoring (model invocation)
    def create_extended_plan(
        self,
        goal: str,
        topic: str | None = None,
        context: str | None = None,
    ) -> dict[str, Any]: ...

    def update_plan(
        self,
        task_id: str,
        status_update: str,
    ) -> dict[str, Any]: ...

    # Artifact authoring (authored-by-value; model paths retired per DEP-01)
    def create_resolved_intake_state(
        self,
        intake_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    def create_work_manifest(
        self,
        content: str,
    ) -> dict[str, Any]: ...

    def patch_work_manifest(
        self,
        manifest_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    def create_authored_artifact(
        self,
        artifact_type: str,
        content: str,
    ) -> dict[str, Any]: ...

    def create_movement_design(
        self,
        manifest_id: str,
        movement_type: str,
        packet_content: str,
        ledger_content: str,
    ) -> dict[str, Any]: ...

    def patch_authored_artifact(
        self,
        artifact_type: str,
        artifact_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    # WBS revision (deterministic; push-generation retired per DEP-01)
    def patch_work_breakdown_structure(
        self,
        wbs_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    # Authored-by-value registration (deterministic validate/store — Seam A)
    def validate_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        phase_number: int,
        manifest_id: str | None = None,
    ) -> dict[str, Any]: ...

    def register_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
    ) -> dict[str, Any]: ...

    def validate_authored_joseki(
        self,
        content: str,
        joseki_key: str | None = None,
    ) -> dict[str, Any]: ...

    def register_authored_joseki(
        self,
        content: str,
    ) -> dict[str, Any]: ...

    # Authored-joseki lifecycle (Phase 6 §4.3)
    def transition_authored_joseki(
        self,
        joseki_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]: ...

    def record_authored_joseki_run(
        self,
        joseki_key: str,
        wbs_id: str | None = None,
    ) -> dict[str, Any]: ...

    def get_authored_joseki(
        self,
        joseki_key: str,
    ) -> dict[str, Any]: ...

    def reconcile_authored_joseki_row(
        self,
        joseki_key: str,
    ) -> dict[str, Any]: ...

    # Plan-template curation lifecycle (SUB-01, POR §4.5 GOAL)
    def transition_plan_template(
        self,
        template_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]: ...

    def get_plan_template(
        self,
        template_key: str,
    ) -> dict[str, Any]: ...

    # Pull-based step execution (deterministic pull/validate/advance — Seam C)
    def start_wbs_execution(
        self,
        wbs_id: str,
    ) -> dict[str, Any]: ...

    def get_next_wbs_step(
        self,
        wbs_id: str,
    ) -> dict[str, Any]: ...

    def record_wbs_step_observation(
        self,
        wbs_id: str,
        step_number: int,
        process_key: str,
        result: dict[str, Any],
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def advance_wbs_execution(
        self,
        wbs_id: str,
    ) -> dict[str, Any]: ...

    # Planning results processing (model invocation)
    def process_planning_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    # Planning loop re-entry with a served INF-02 completion
    def resume_thinking_completion(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...


# Set outside the class body so it is NOT included in __protocol_attrs__
# (Protocol isinstance checks all names defined in the body). Accessible
# via getattr(ThinkingProvider, "INTERFACE_VERSION") for service binding
# version validation.
ThinkingProvider.INTERFACE_VERSION = "1.0.0"  # type: ignore[attr-defined]
