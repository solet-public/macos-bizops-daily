"""Thinking Service Public API.

AI-discoverable plan lifecycle and thinking operations.
All methods are indexed for process discovery via @service_interface_process decorators.

Plan tools orchestrate across thinking service (reasoning), memory service
(storage/focus), and state service (metadata).
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.domain.types import ActionResult
from ananta.core.services.service_interface_decorator import service_interface_process


class ThinkingServiceAPI(ABC):
    """Public thinking operations — AI-discoverable via process registry."""

    # ==========================================================================
    # PLAN LIFECYCLE
    # ==========================================================================

    @service_interface_process(
        name="upsert_plan",
        is_discoverable=False,
        provider="thinking_service",
        is_long_running=False,
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "content": ParameterMetadata(
                description="Plan content to store (replaces current plan text). The platform derives the current step from the existing focused plan and advances markers automatically.",
                required=True,
                type=ParameterType.STRING,
            ),
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Plan write result",
            type=ParameterType.OBJECT,
            properties={
                "plan_id": ParameterMetadata(type=ParameterType.STRING, description="Plan task ID"),
                "memory_id": ParameterMetadata(type=ParameterType.STRING, description="Memory ID"),
                "focused": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether plan was pinned to focus buffer",
                ),
            },
        ),
        # No result_processor_customizations — terminal by design.
        # upsert_plan is bookkeeping; its completion never triggers a process_results vertex.
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def upsert_plan(self, content: str, *, session_id: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="create_extended_plan",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=True,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "goal": ParameterMetadata(
                description="The goal to decompose into a plan",
                required=True,
                type=ParameterType.STRING,
            ),
            "topic": ParameterMetadata(
                description="Domain topic for knowledge base search. Falls back to goal if not provided.",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "context": ParameterMetadata(
                description="Prior observation from the calling vertex (e.g., knowledge base search results) to ground the planner in available processes.",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Created plan with task ID, memory ID, and content",
            type=ParameterType.OBJECT,
            properties={
                "plan_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Task ID (thk prefix)"
                ),
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Memory ID from remember()"
                ),
                "focused": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether plan was pinned to focus buffer",
                ),
                "step_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of steps in the plan"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING, description="Full plan text"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def create_extended_plan(
        self, goal: str, topic: str | None = None, context: str | None = None
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="update_plan",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=True,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "task_id": ParameterMetadata(
                description="Plan task ID (thk prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "status_update": ParameterMetadata(
                description="Progress report or instruction for the thinking model",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Updated plan content",
            type=ParameterType.OBJECT,
            properties={
                "plan_id": ParameterMetadata(type=ParameterType.STRING, description="Task ID"),
                "memory_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Memory ID (may change)"
                ),
                "refocused": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether plan was re-focused"
                ),
                "step_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of steps"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING, description="Updated plan text"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def update_plan(self, task_id: str, status_update: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="list_plans",
        is_discoverable=True,
        provider="thinking_service",
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "status": ParameterMetadata(
                description="Filter by status: active, paused, completed, abandoned",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of plans with metadata",
            type=ParameterType.OBJECT,
            properties={
                "plans": ParameterMetadata(
                    type=ParameterType.LIST, description="List of plan objects"
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total plans matching filter"
                ),
                "focused_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of plans in focus buffer"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def list_plans(self, status: str | None = None) -> dict[str, Any]: ...

    # ==========================================================================
    # PLAYBOOK LIFECYCLE
    # ==========================================================================

    @service_interface_process(
        name="create_playbook",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=True,
        parameters={
            "goal": ParameterMetadata(
                description="The project goal to create a playbook for",
                required=True,
                type=ParameterType.STRING,
            ),
            "constraints": ParameterMetadata(
                description="Locked constraints from investigation (approved direction, hard exclusions, style)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "investigation_context": ParameterMetadata(
                description="Findings from the investigation phase (recall summaries, search results, user approvals)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Playbook creation result with IDs for tracking",
            type=ParameterType.OBJECT,
            properties={
                "playbook_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Playbook ID (pbk- prefix)"
                ),
                "planning_context_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Planning context ID for the inference loop",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Playbook status (active)"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def create_playbook(
        self,
        goal: str,
        constraints: str | None = None,
        investigation_context: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_playbook",
        is_discoverable=True,
        provider="thinking_service",
        parameters={
            "playbook_id": ParameterMetadata(
                description="Playbook ID (pbk- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Full playbook content",
            type=ParameterType.OBJECT,
            properties={
                "playbook_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Playbook ID"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING, description="Full playbook markdown"
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Playbook lifecycle status"
                ),
                "title": ParameterMetadata(type=ParameterType.STRING, description="Playbook title"),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_playbook(self, playbook_id: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_playbook_section",
        is_discoverable=False,
        provider="thinking_service",
        parameters={
            "playbook_id": ParameterMetadata(
                description="Playbook ID (pbk- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "section_id": ParameterMetadata(
                description="Section ID matching <!-- section: <id> --> marker in the playbook",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Single playbook section content",
            type=ParameterType.OBJECT,
            properties={
                "playbook_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Playbook ID"
                ),
                "section_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Section ID"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING, description="Section markdown content"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_playbook_section(self, playbook_id: str, section_id: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="list_playbooks",
        is_discoverable=True,
        provider="thinking_service",
        parameters={
            "status": ParameterMetadata(
                description="Filter by status: active, paused, completed, abandoned",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of playbooks with metadata",
            type=ParameterType.OBJECT,
            properties={
                "playbooks": ParameterMetadata(
                    type=ParameterType.LIST, description="List of playbook objects"
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total playbooks matching filter"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def list_playbooks(self, status: str | None = None) -> dict[str, Any]: ...

    @service_interface_process(
        name="patch_playbook",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=True,
        parameters={
            "playbook_id": ParameterMetadata(
                description="Playbook ID (pbk- prefix) to patch",
                required=True,
                type=ParameterType.STRING,
            ),
            "patch_description": ParameterMetadata(
                description="Description of the patch (e.g., phase transition request, revision context)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Patch request stored; planning VERTEX will resume",
            type=ParameterType.OBJECT,
            properties={
                "playbook_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Playbook ID"
                ),
                "planning_context_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Planning context ID for the inference loop",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Playbook status (active)"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def patch_playbook(
        self,
        playbook_id: str,
        patch_description: str,
    ) -> dict[str, Any]: ...

    # ==========================================================================
    # RESOLVED INTAKE STATE / WORK MANIFEST / COMPOSITION SKETCH / WORK BREAKDOWN STRUCTURE LIFECYCLE
    # ==========================================================================

    @service_interface_process(
        name="create_resolved_intake_state",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "intake_id": ParameterMetadata(
                description="Resolved Intake State ID (intk- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description=(
                    "Full authored Resolved Intake State markdown document, "
                    "authored by the calling agent and passed by value"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Resolved Intake State creation result with full document",
            type=ParameterType.OBJECT,
            properties={
                "intake_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Intake State ID"
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Full created Resolved Intake State content",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def create_resolved_intake_state(
        self,
        intake_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="create_work_manifest",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "content": ParameterMetadata(
                description=(
                    "Full authored Work Manifest markdown document, authored "
                    "by the calling agent and passed by value"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Work Manifest creation result with full document",
            type=ParameterType.OBJECT,
            properties={
                "manifest_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Manifest ID (derived by the platform from the focused brief)",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Full created Work Manifest content",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def create_work_manifest(
        self,
        content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="patch_work_manifest",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "manifest_id": ParameterMetadata(
                description="Work Manifest ID (wmf- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Full Work Manifest markdown content (phases only, no work items or steps)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Work Manifest write result",
            type=ParameterType.OBJECT,
            properties={
                "manifest_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Manifest ID"
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def patch_work_manifest(
        self,
        manifest_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="create_authored_artifact",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "artifact_type": ParameterMetadata(
                description="Artifact type key selecting guidance and storage config",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description=(
                    "Full authored artifact document, authored by the "
                    "calling agent and passed by value (markdown; raw "
                    "JSON payload for pipeline_spec)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Authored artifact creation result",
            type=ParameterType.OBJECT,
            properties={
                "artifact_type": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Artifact type key",
                ),
                "artifact_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Artifact ID (derived by the platform)",
                ),
                "parent_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Parent artifact ID (derived by the platform; empty for brief)",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Full created artifact content",
                ),
                "knowledge_base_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Path within the knowledge base",
                ),
                "source_memory_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Focused memory ID for the stored artifact",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def create_authored_artifact(
        self,
        artifact_type: str,
        content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="create_movement_design",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "manifest_id": ParameterMetadata(
                description="Parent Work Manifest ID (wmf- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "movement_type": ParameterMetadata(
                description="Movement type (toccata, allemande, sarabande, gigue, etc.)",
                required=True,
                type=ParameterType.STRING,
            ),
            "packet_content": ParameterMetadata(
                description=(
                    "Full authored Movement Design Packet markdown document, "
                    "authored by the calling agent and passed by value"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "ledger_content": ParameterMetadata(
                description=(
                    "Full authored Phrase Design Ledger markdown document, "
                    "authored by the calling agent and passed by value"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Movement design creation result with packet and ledger",
            type=ParameterType.OBJECT,
            properties={
                "packet_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Movement Design Packet ID (mdp- prefix)",
                ),
                "ledger_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Phrase Design Ledger ID (pdl- prefix)",
                ),
                "manifest_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Parent manifest ID",
                ),
                "movement_type": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Movement type",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Operation status",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def create_movement_design(
        self,
        manifest_id: str,
        movement_type: str,
        packet_content: str,
        ledger_content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="patch_authored_artifact",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "artifact_type": ParameterMetadata(
                description="Artifact type key selecting storage config",
                required=True,
                type=ParameterType.STRING,
            ),
            "artifact_id": ParameterMetadata(
                description="Identifier of the artifact to update",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Full replacement markdown content",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Authored artifact write result",
            type=ParameterType.OBJECT,
            properties={
                "artifact_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Artifact ID",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def patch_authored_artifact(
        self,
        artifact_type: str,
        artifact_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="validate_authored_work_breakdown_structure",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "content": ParameterMetadata(
                description=(
                    "Full Work Breakdown Structure markdown document authored "
                    "by the calling agent (passed by value)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "phase_number": ParameterMetadata(
                description="Phase number the WBS content must stay within",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "manifest_id": ParameterMetadata(
                description="Parent Work Manifest ID (wmf- prefix)",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Authored WBS validation report (dry run, nothing stored)",
            type=ParameterType.OBJECT,
            properties={
                "valid": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when no hard-fail error was found",
                ),
                "errors": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Hard-fail validation errors (block registration)",
                ),
                "warnings": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Soft coherence findings (do not block registration)",
                ),
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="WBS ID echoed back",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def validate_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        phase_number: int,
        manifest_id: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="register_authored_work_breakdown_structure",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "content": ParameterMetadata(
                description=(
                    "Full Work Breakdown Structure markdown document authored "
                    "by the calling agent (passed by value)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "manifest_id": ParameterMetadata(
                description="Parent Work Manifest ID (wmf- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "phase_number": ParameterMetadata(
                description="Phase number within the manifest",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "phase_name": ParameterMetadata(
                description="Phase name",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Authored WBS registration result",
            type=ParameterType.OBJECT,
            properties={
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="WBS ID",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Operation status ('registered')",
                ),
                "knowledge_base_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Path within the thinking_plans knowledge base",
                ),
                "source_memory_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Focused memory ID for the stored document",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def register_authored_work_breakdown_structure(
        self,
        content: str,
        wbs_id: str,
        manifest_id: str,
        phase_number: int,
        phase_name: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="validate_authored_joseki",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "content": ParameterMetadata(
                description=(
                    "Full joseki knowledge-base card markdown authored by "
                    "the calling agent (passed by value)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "joseki_key": ParameterMetadata(
                description=(
                    "Expected JOSEKI_KEY; when provided, the card's own "
                    "JOSEKI_KEY line must match it"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Authored joseki card validation report (dry run, nothing stored)",
            type=ParameterType.OBJECT,
            properties={
                "valid": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when no hard-fail error was found",
                ),
                "errors": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Hard-fail validation errors (block registration)",
                ),
                "warnings": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Soft findings (do not block registration)",
                ),
                "joseki_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="JOSEKI_KEY parsed from the card (empty when absent)",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def validate_authored_joseki(
        self,
        content: str,
        joseki_key: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="register_authored_joseki",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "content": ParameterMetadata(
                description=(
                    "Full joseki knowledge-base card markdown authored by "
                    "the calling agent (passed by value); the card's "
                    "JOSEKI_KEY line is its identity"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Authored joseki card registration result",
            type=ParameterType.OBJECT,
            properties={
                "joseki_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="JOSEKI_KEY parsed from the card",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Operation status ('registered')",
                ),
                "state": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Initial lifecycle state ('draft')",
                ),
                "knowledge_base_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Card path within the authored_joseki knowledge base",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def register_authored_joseki(
        self,
        content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="transition_authored_joseki",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "joseki_key": ParameterMetadata(
                description="Stable JOSEKI_KEY of the registered card to advance",
                required=True,
                type=ParameterType.STRING,
            ),
            "target_state": ParameterMetadata(
                description=(
                    "Target lifecycle state: 'candidate' (validation-gated), "
                    "'superseded' (requires superseded_by), or 'archived' "
                    "(retire). 'proven' is earned via a recorded run, not set "
                    "here"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "superseded_by": ParameterMetadata(
                description=(
                    "JOSEKI_KEY of the replacing card; required only when "
                    "target_state is 'superseded'"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Lifecycle transition result for an authored joseki",
            type=ParameterType.OBJECT,
            properties={
                "joseki_key": ParameterMetadata(
                    type=ParameterType.STRING, description="The transitioned card",
                ),
                "previous_state": ParameterMetadata(
                    type=ParameterType.STRING, description="State before the transition",
                ),
                "state": ParameterMetadata(
                    type=ParameterType.STRING, description="State after the transition",
                ),
                "superseded_by": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Replacement key when superseded; null otherwise",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'transitioned' or 'unchanged' (idempotent no-op)",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def transition_authored_joseki(
        self,
        joseki_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="record_authored_joseki_run",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "joseki_key": ParameterMetadata(
                description="Stable JOSEKI_KEY of the card whose run to record",
                required=True,
                type=ParameterType.STRING,
            ),
            "wbs_id": ParameterMetadata(
                description=(
                    "Optional WBS id of the run instance; echoed in the result "
                    "for the caller's correlation (v1 records the run_count / "
                    "last_run_at aggregate, not a per-run log)"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Run-evidence result for an authored joseki",
            type=ParameterType.OBJECT,
            properties={
                "joseki_key": ParameterMetadata(
                    type=ParameterType.STRING, description="The card",
                ),
                "state": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="State after recording (candidate advances to proven)",
                ),
                "run_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Successful-run counter after this record",
                ),
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Echoed run instance id",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="'run_recorded'",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def record_authored_joseki_run(
        self,
        joseki_key: str,
        wbs_id: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_authored_joseki",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "joseki_key": ParameterMetadata(
                description="Stable JOSEKI_KEY of the card to read",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Authored-joseki lifecycle row (state + run evidence)",
            type=ParameterType.OBJECT,
            properties={
                "found": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="False when no lifecycle row exists for the key",
                ),
                "joseki_key": ParameterMetadata(
                    type=ParameterType.STRING, description="The card",
                ),
                "state": ParameterMetadata(
                    type=ParameterType.STRING, description="Current lifecycle state",
                ),
                "provenance": ParameterMetadata(
                    type=ParameterType.STRING, description="Authoring provenance",
                ),
                "knowledge_base_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Card path in the authored_joseki knowledge base",
                ),
                "superseded_by": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Replacement key when superseded; null otherwise",
                ),
                "run_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Recorded successful runs",
                ),
                "last_run_at": ParameterMetadata(
                    type=ParameterType.STRING, description="Last run timestamp (ISO)",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_authored_joseki(
        self,
        joseki_key: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="reconcile_authored_joseki_row",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "joseki_key": ParameterMetadata(
                description="Stable JOSEKI_KEY of the card whose row to reconcile",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Row-maintenance result (knowledge_base_path normalisation)",
            type=ParameterType.OBJECT,
            properties={
                "joseki_key": ParameterMetadata(
                    type=ParameterType.STRING, description="The card",
                ),
                "previous_knowledge_base_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The stale path before reconciliation, when changed",
                ),
                "knowledge_base_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Canonical card path after reconciliation",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'reconciled' or 'unchanged'",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def reconcile_authored_joseki_row(
        self,
        joseki_key: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="transition_plan_template",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "template_key": ParameterMetadata(
                description="Stable template key of the registered plan-template card to advance",
                required=True,
                type=ParameterType.STRING,
            ),
            "target_state": ParameterMetadata(
                description=(
                    "Target curation state: 'active' (endorse a draft as the "
                    "canonical skeleton to fork), 'superseded' (requires "
                    "superseded_by), or 'archived' (retire). 'draft' is the "
                    "authoring origin, not a manual target"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "superseded_by": ParameterMetadata(
                description=(
                    "Template key of the replacing card; required only when "
                    "target_state is 'superseded'"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Curation-lifecycle transition result for a plan template",
            type=ParameterType.OBJECT,
            properties={
                "template_key": ParameterMetadata(
                    type=ParameterType.STRING, description="The transitioned template",
                ),
                "previous_state": ParameterMetadata(
                    type=ParameterType.STRING, description="State before the transition",
                ),
                "state": ParameterMetadata(
                    type=ParameterType.STRING, description="State after the transition",
                ),
                "superseded_by": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Replacement key when superseded; null otherwise",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'transitioned' or 'unchanged' (idempotent no-op)",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def transition_plan_template(
        self,
        template_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_plan_template",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "template_key": ParameterMetadata(
                description="Stable template key of the plan-template card to read",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Plan-template curation view (front-matter state + discovery axes)",
            type=ParameterType.OBJECT,
            properties={
                "found": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="False when no card exists for the key",
                ),
                "template_key": ParameterMetadata(
                    type=ParameterType.STRING, description="The template",
                ),
                "title": ParameterMetadata(
                    type=ParameterType.STRING, description="Human-facing card title",
                ),
                "state": ParameterMetadata(
                    type=ParameterType.STRING, description="Current curation state",
                ),
                "goal": ParameterMetadata(
                    type=ParameterType.STRING, description="Discovery axis: what it achieves",
                ),
                "domain": ParameterMetadata(
                    type=ParameterType.STRING, description="Discovery axis: the problem domain",
                ),
                "outcome": ParameterMetadata(
                    type=ParameterType.STRING, description="Discovery axis: the kind of result",
                ),
                "superseded_by": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Replacement key when superseded; null otherwise",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_plan_template(
        self,
        template_key: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="start_wbs_execution",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Pull-mode execution session state (start or resume)",
            type=ParameterType.OBJECT,
            properties={
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING, description="WBS ID",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="WBS tracking status after the call ('in_progress')",
                ),
                "resumed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when prior executed steps exist (resume path)",
                ),
                "executed_step_numbers": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Step numbers already durably executed",
                ),
                "next": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Envelope for the next unexecuted step (see get_next_wbs_step)",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def start_wbs_execution(
        self,
        wbs_id: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_next_wbs_step",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Pull-mode envelope for the next unexecuted WBS step",
            type=ParameterType.OBJECT,
            properties={
                "kind": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'execute' | 'await_user' | 'complete'",
                ),
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING, description="WBS ID",
                ),
                "step_number": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Next step number (null when kind='complete')",
                ),
                "title": ParameterMetadata(
                    type=ParameterType.STRING, description="Step title",
                ),
                "process_keys": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Process keys the step declares",
                ),
                "sub_steps": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Sub-steps with resolved arguments (bound literals + "
                        "resolved Composed references) and any unresolved "
                        "Composed targets"
                    ),
                ),
                "support_articles": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="SUPPORT_ARTICLES filenames for the step",
                ),
                "result_processor_kind": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The step's RESULT_PROCESSOR_KIND (or null)",
                ),
                "auto_safe": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when the author marked AUTO_SAFE: true",
                ),
                "expected_result_contract": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="What a valid observation must contain",
                ),
                "completion_criteria": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="How this step is completed and recorded",
                ),
                "remaining_step_numbers": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="All unexecuted step numbers, in order",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_next_wbs_step(
        self,
        wbs_id: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="record_wbs_step_observation",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "step_number": ParameterMetadata(
                description="The executed step's number",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "process_key": ParameterMetadata(
                description="The process key the driver executed for the step",
                required=True,
                type=ParameterType.STRING,
            ),
            "result": ParameterMetadata(
                description=(
                    "The executed tool's result envelope (must carry a "
                    "success status: completed/succeeded/success)"
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
            "state_summary": ParameterMetadata(
                description="Optional short summary for the durable step record",
                required=False,
                type=ParameterType.STRING,
            ),
            "output_artifacts": ParameterMetadata(
                description="Optional list of produced artifact names",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Observation outcome — accepted=false means NOTHING advanced"
            ),
            type=ParameterType.OBJECT,
            properties={
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING, description="WBS ID",
                ),
                "step_number": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Observed step",
                ),
                "accepted": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when validated and durably recorded",
                ),
                "errors": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Validation errors (empty when accepted)",
                ),
                "next": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Envelope for the next unexecuted step",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def record_wbs_step_observation(
        self,
        wbs_id: str,
        step_number: int,
        process_key: str,
        result: dict[str, Any],
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="advance_wbs_execution",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Q15 advance evaluation: auto_safe steps come back as a "
                "validated ready-to-submit action definition; everything "
                "else returns control to the agent"
            ),
            type=ParameterType.OBJECT,
            properties={
                "mode": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'auto_safe' | 'agent_review' | 'complete'",
                ),
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING, description="WBS ID",
                ),
                "step_number": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Evaluated next step (absent when complete)",
                ),
                "reasons": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Why the step needs agent review (empty for auto_safe)",
                ),
                "action_definition": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Validated closed-world {process_key, arguments} — "
                        "present only for mode='auto_safe'; the driver may "
                        "submit it mechanically without review"
                    ),
                ),
                "envelope": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="The evaluated step's full envelope",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def advance_wbs_execution(
        self,
        wbs_id: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="patch_work_breakdown_structure",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Full Work Breakdown Structure markdown content",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Work Breakdown Structure update result",
            type=ParameterType.OBJECT,
            properties={
                "wbs_id": ParameterMetadata(type=ParameterType.STRING, description="WBS ID"),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def patch_work_breakdown_structure(
        self,
        wbs_id: str,
        content: str,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="generate_section_stem_wbs",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "manifest_id": ParameterMetadata(
                description="Parent Work Manifest ID (wmf- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "phase_number": ParameterMetadata(
                description="Phase number within the manifest",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "phase_name": ParameterMetadata(
                description="Phase name",
                required=True,
                type=ParameterType.STRING,
            ),
            "style_family": ParameterMetadata(
                description=(
                    "Style family identifier (e.g., 'neuro_ambient', "
                    "'early_baroque'). Selects the pipeline schema "
                    "from the matching knowledge base."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "artifact_prefix": ParameterMetadata(
                description=(
                    "Artifact filename prefix committed in the Work "
                    "Manifest. Output filenames are derived from this."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "pipeline_spec_id": ParameterMetadata(
                description=(
                    "PipelineSpec artifact ID (psp- prefix). The "
                    "platform reads the artifact from the knowledge "
                    "base, extracts the embedded JSON, and uses it to "
                    "generate the WBS."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "pipeline_spec": ParameterMetadata(
                description=(
                    "PipelineSpec as an inline JSON object — used by "
                    "direct callers (tests, integration harnesses). "
                    "Production flows pass pipeline_spec_id instead."
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Deterministic WBS generation result",
            type=ParameterType.OBJECT,
            properties={
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING, description="WBS ID",
                ),
                "manifest_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Parent manifest ID",
                ),
                "phase_number": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Phase number",
                ),
                "phase_name": ParameterMetadata(
                    type=ParameterType.STRING, description="Phase name",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Operation status",
                ),
                "content": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Full generated WBS content",
                ),
                "knowledge_base_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Path within the knowledge base",
                ),
                "source_memory_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Focused memory ID for the stored WBS",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
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
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="record_work_breakdown_structure_step_state",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (wbs- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "step_number": ParameterMetadata(
                description="WBS step number to update",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "status": ParameterMetadata(
                description="Step status (e.g. completed, in_progress)",
                required=True,
                type=ParameterType.STRING,
            ),
            "state_summary": ParameterMetadata(
                description="Summary of what happened in this step",
                required=False,
                type=ParameterType.STRING,
            ),
            "output_artifacts": ParameterMetadata(
                description="List of produced artifact filenames",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Step state recording result",
            type=ParameterType.OBJECT,
            properties={
                "wbs_id": ParameterMetadata(type=ParameterType.STRING, description="WBS ID"),
                "step_number": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Step number"
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def record_work_breakdown_structure_step_state(
        self,
        wbs_id: str,
        step_number: int,
        status: str,
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="record_work_manifest_phase_state",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "manifest_id": ParameterMetadata(
                description="Work Manifest ID (wmf- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
            "phase_number": ParameterMetadata(
                description="Phase number to update",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "status": ParameterMetadata(
                description="Phase status (e.g. completed, approved)",
                required=True,
                type=ParameterType.STRING,
            ),
            "outcome_summary": ParameterMetadata(
                description="Summary of the phase outcome",
                required=True,
                type=ParameterType.STRING,
            ),
            "approved_artifacts": ParameterMetadata(
                description="List of approved artifact filenames",
                required=False,
                type=ParameterType.LIST,
            ),
            "next_phase_instruction": ParameterMetadata(
                description="Instruction for next phase planning",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Phase state recording result",
            type=ParameterType.OBJECT,
            properties={
                "manifest_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Manifest ID"
                ),
                "phase_number": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Phase number"
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def record_work_manifest_phase_state(
        self,
        manifest_id: str,
        phase_number: int,
        status: str,
        outcome_summary: str,
        approved_artifacts: list[str] | None = None,
        next_phase_instruction: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="graft_work_breakdown_structure_segment",
        is_discoverable=True,
        provider="thinking_service",
        is_long_running=False,
        parameters={
            "state": ParameterMetadata(
                description=(
                    "Current application state (automatically injected at "
                    "execution time; carries the acting session — JOS-02)"
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
            "wbs_id": ParameterMetadata(
                description="Work Breakdown Structure ID (for tracking)",
                required=True,
                type=ParameterType.STRING,
            ),
            "anchor_step_number": ParameterMetadata(
                description="Active plan step number after which to graft",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "segment": ParameterMetadata(
                description="The execution steps to graft (1-based numbering, platform renumbers). Platform-injected from the focused WBS — model does not need to provide this.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Plan graft result",
            type=ParameterType.OBJECT,
            properties={
                "wbs_id": ParameterMetadata(type=ParameterType.STRING, description="WBS ID"),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, description="Operation status"
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def graft_work_breakdown_structure_segment(
        self,
        wbs_id: str,
        anchor_step_number: int,
        segment: str = "",
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_plan",
        is_discoverable=True,
        provider="thinking_service",
        parameters={
            "plan_id": ParameterMetadata(
                description="Plan ID (pln- prefix)",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Full plan content from knowledge base",
            type=ParameterType.OBJECT,
            properties={
                "plan_id": ParameterMetadata(type=ParameterType.STRING, description="Plan ID"),
                "content": ParameterMetadata(
                    type=ParameterType.STRING, description="Full plan markdown"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_plan(self, plan_id: str) -> dict[str, Any]: ...

    # ==========================================================================
    # PLANNING INFERENCE VERTEX
    # ==========================================================================

    @service_interface_process(
        name="process_planning_results",
        provider="thinking_service",
        processor_policy_category=ProcessorPolicyCategory.VERTEX,
        is_inference_capable=True,
        work_count_impact=0,
        parameters={
            "params": ParameterMetadata(
                description="Parameters dict containing observation (action result data)",
                required=False,
                type=ParameterType.OBJECT,
            ),
            "state": ParameterMetadata(
                description="Current application state (automatically injected at execution time)",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Planning inference loop output",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Planning output (actions or artifacts)",
                    required=False,
                ),
            },
        ),
        action_definition_template={
            "name": "process_planning_result",
            "description": "Continue the planning inference loop with observation data",
            "process": {
                "provider_type": "service_interface",
                "provider": "thinking_service",
                "function_name": "process_planning_results",
            },
            "arguments": {
                "prompt": {
                    "observation": {
                        "action_result": {
                            "action_status": "completed",
                            "data": "<<RESULT>>",
                            "_completed_arguments": "<<ACTION_ARGUMENTS>>",
                        },
                    },
                    "user": {
                        "instructions": [],
                    },
                },
            },
        },
    )
    @abstractmethod
    def process_planning_results(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    @service_interface_process(
        name="resume_thinking_completion",
        provider="thinking_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        work_count_impact=0,
        parameters={
            "request_id": ParameterMetadata(
                description=(
                    "The served completion request id (icr-...) whose durable "
                    "row carries the completion text and planning correlation."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "Planning loop re-entry output (INF-02 resume continuation): "
                "the same actions-or-artifacts shape process_planning_results "
                "produces"
            ),
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or failed",
                    required=False,
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Planning output (actions or artifacts)",
                    required=False,
                ),
            },
        ),
        # Deploy-fix 2026-07-05 (historical): both customization blocks were
        # added when the post-KB-merge boot check required them; that FATAL
        # was relaxed 2026-07-15 (customizations now optional on EDGE).
        # Sensitivities mirror the ACTUAL scalar return; no blob keys.
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def resume_thinking_completion(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult: ...

    # ------------------------------------------------------------------
    # Joseki run driver (Track A, spec 2026-07-05 v3.1) — the four verbs
    # that make a registered joseki card EXECUTE platform-side. run_joseki
    # and complete_joseki_run are EDGE_SINK (terminal; never in any
    # EdgeProcessDefinition dict). EDGE_SINK carries no RESULT block, but
    # complete_joseki_run carries the ERROR block: it is submitted as a
    # deterministic continuation, and §16 requires registered error
    # customizations for the factory to auto-inject (see its decorator
    # note). The two reads are EDGE with BOTH customization blocks
    # in-decorator per the deploy-fix convention above.
    # ------------------------------------------------------------------

    @service_interface_process(
        name="run_joseki",
        provider="thinking_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "joseki_key": ParameterMetadata(
                description=(
                    "JOSEKI_KEY of a registered candidate/proven card to "
                    "execute (drafts and retired cards are rejected)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "bindings": ParameterMetadata(
                description=(
                    "Concrete values for the card's <<BIND:name>> slots; the "
                    "card must be closed-world under these bindings or the "
                    "call fails typed (joseki_not_mechanizable)"
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
            "label": ParameterMetadata(
                description="Optional human label for the run",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Run handle; the run executes asynchronously — the envelope's "
                "returned action (Pattern 6a) submits step 1 in the run's own "
                "session and the coordinator chain drives the rest"
            ),
            type=ParameterType.OBJECT,
            properties={
                "run_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Run row id (jrun-…)",
                ),
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Instantiated joseki-scoped WBS id",
                ),
                "session_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The run's dedicated platform session",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Always 'running' on success",
                ),
                "actions": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Single-element Pattern-6a list: step 1's action "
                        "definition, platform-submitted after this verb returns"
                    ),
                ),
            },
        ),
    )
    @abstractmethod
    def run_joseki(
        self,
        joseki_key: str,
        bindings: dict[str, Any],
        label: str = "",
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="complete_joseki_run",
        is_discoverable=False,
        provider="thinking_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "wbs_id": ParameterMetadata(
                description=(
                    "The run WBS id; the terminal step the instantiator "
                    "appends binds this — not for human invocation"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Terminal outcome; run evidence records only on the winning "
                "status CAS (a lost race is a benign no-op)"
            ),
            type=ParameterType.OBJECT,
            properties={
                "run_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Run row id",
                ),
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING, description="The run WBS id",
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Run status after this call",
                ),
                "outcome": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'completed' or 'noop_lost_cas'",
                ),
                "joseki_state": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Card lifecycle state after evidence recording; None "
                        "on the noop path"
                    ),
                    required=False,
                ),
                "run_count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Card run-evidence count; None on the noop path",
                    required=False,
                ),
            },
        ),
        # §16 (action_factory._require_error_processor_for_deterministic):
        # this verb is the run WBS's TERMINAL step — the ONLY EDGE_SINK the
        # platform submits as a deterministic continuation — so the factory
        # must find registered error customizations to auto-inject, or it
        # REJECTS the hop at submission (live-proven: the first clean chain
        # died exactly here, 2026-07-05). The error path routes to inference
        # like every deterministic card step (spec v3.2 posture; JOS-01 is
        # the suppression knob).
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def complete_joseki_run(self, wbs_id: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="get_joseki_run",
        provider="thinking_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "run_id": ParameterMetadata(
                description="Run row id (jrun-…) to read",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Run row projection; found=False when absent",
            type=ParameterType.OBJECT,
            properties={
                "found": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="False when no run row exists for the id",
                ),
                "run_id": ParameterMetadata(
                    type=ParameterType.STRING, description="Run row id",
                ),
                "joseki_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Card being run",
                    required=False,
                ),
                "wbs_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Run WBS id",
                    required=False,
                ),
                "session_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Run session",
                    required=False,
                ),
                "flow_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Run flow",
                    required=False,
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="running/awaiting_user/completed/failed",
                    required=False,
                ),
                "current_step": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Last submitted step number (None pre-submit)",
                    required=False,
                ),
                "failure_detail": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Typed failure detail ('' unless failed)",
                    required=False,
                ),
                "label": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Run label",
                    required=False,
                ),
                "attempts": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Reconciler re-drive count",
                    required=False,
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def get_joseki_run(self, run_id: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="list_joseki_runs",
        provider="thinking_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "status": ParameterMetadata(
                description=(
                    "Optional status filter: running, awaiting_user, "
                    "completed, or failed"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "joseki_key": ParameterMetadata(
                description="Optional card filter",
                required=False,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description="Maximum rows returned (default 50)",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Bounded run listing",
            type=ParameterType.OBJECT,
            properties={
                "runs": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Run row projections (same shape as get_joseki_run)"
                    ),
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Rows returned",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def list_joseki_runs(
        self,
        status: str | None = None,
        joseki_key: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="reconcile_joseki_runs",
        is_discoverable=False,
        provider="thinking_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        return_value_schema=ReturnValueSchema(
            description=(
                "Sweep outcome over every running joseki run — the ordered "
                "reconciler duties (violation surfacing, runtime-failure "
                "surfacing, stall detection); the canonical EDGE_SINK "
                "cron-sibling shape"
            ),
            type=ParameterType.OBJECT,
            properties={
                "swept": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Running runs examined",
                ),
                "results": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Per-run duty outcomes",
                ),
            },
        ),
    )
    @abstractmethod
    def reconcile_joseki_runs(self) -> dict[str, Any]: ...
