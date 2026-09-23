"""Context Service Public API.

AI-discoverable context-assembly operations with @service_interface_process
decorators. All methods here are indexed for process discovery and callable via
``process_call`` as ``service_interface::context_service::*``.

Phase 2 of the coding-agent substrate plan: a retrieval/provenance-first briefing
API. ``assemble_agent_context`` hands a frontier agent the grounding a Qwen prompt
would have gotten — process catalog, plan state, guidance, support articles, the
answer contract — as STRUCTURED DATA with provenance, so the agent can request
grounding without asking the solet to think for it. It NEVER calls an inference
provider; no local model is required.
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
from ananta.core.services.service_interface_decorator import service_interface_process


class ContextServiceAPI(ABC):
    """Public context-assembly operations — AI-discoverable via the process registry.

    ``assemble_agent_context`` — assemble a read-only, retrieval/provenance-first
    briefing (named bundles + provenance + answer contract) for an agent, with no
    inference and no local model.
    """

    @service_interface_process(
        name="assemble_agent_context",
        work_count_impact=0,  # Non-terminal: the agent uses the briefing to act next.
        provider="context_service",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "session_id": ParameterMetadata(
                description="Session identifier the briefing is assembled for.",
                required=True,
                type=ParameterType.STRING,
            ),
            "flow_id": ParameterMetadata(
                description="Flow identifier the briefing is assembled for.",
                required=True,
                type=ParameterType.STRING,
            ),
            "context_id": ParameterMetadata(
                description=(
                    "Optional platform-managed context stream id. When omitted, "
                    "the platform resolves the context per the active mode."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "budget": ParameterMetadata(
                description=(
                    "Optional cap on the number of context blocks included. A "
                    "briefing service, not a prompt shrinker: when omitted, EVERY "
                    "block is included (nothing is silently trimmed). When supplied, "
                    "at most this many blocks are retained and the manifest records "
                    "how many were dropped."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
            "requested_bundles": ParameterMetadata(
                description=(
                    "Optional list of bundle names to include (process_catalog, "
                    "plan_state, guidance, frame, conversation, answer_contract, "
                    "other). When omitted, every bundle is returned."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Structured agent-context briefing: named bundles of context blocks "
                "with provenance, the answer contract, available process contracts, a "
                "flat provenance list, and an assembly manifest."
            ),
            type=ParameterType.OBJECT,
            properties={
                "profile": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The assembly profile name ('agent_context').",
                    required=True,
                ),
                "bundles": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Named bundles keyed by name (process_catalog, plan_state, "
                        "guidance, frame, conversation, answer_contract, other); each "
                        "value is a list of {content, source_kind, subtype, "
                        "reasoning_slot, provenance{kind, ref}} items."
                    ),
                    required=True,
                ),
                "answer_contract": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "The decode/answer-spec schema as DATA (a decision spec for "
                        "the agent, not a token-level cage); null when no schema was "
                        "produced."
                    ),
                    required=False,
                ),
                "available_contracts": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Process-key strings surfaced in the briefing (from process_catalog provenance).",
                    required=True,
                ),
                "provenance": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Flat provenance rows: {block_id, source_kind, kind, ref} — one per included block.",
                    required=True,
                ),
                "manifest": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Assembly manifest: {block_count, bundle_counts, budget, "
                        "budget_applied, dropped, has_answer_contract}."
                    ),
                    required=True,
                ),
            },
            usage_patterns=[
                "Get grounding (process catalog, plan state, guidance, answer contract) before acting.",
                "Read provenance to trace each context block back to its source.",
                "Use available_contracts + answer_contract to construct valid next actions.",
                "Assemble grounding on a deployment with no local reasoner.",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(),
    )
    @abstractmethod
    def assemble_agent_context(
        self,
        *,
        session_id: str,
        flow_id: str,
        context_id: str | None = None,
        budget: int | None = None,
        requested_bundles: list[str] | None = None,
    ) -> dict[str, Any]:
        """Assemble a retrieval/provenance-first agent-context briefing.

        Runs the ``agent_context`` assembly profile (the bundle-producing stages,
        no serialization) and groups the resulting context blocks into named
        bundles with provenance plus the answer contract. Never calls an inference
        provider.
        """
        ...
