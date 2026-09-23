"""Cloud Self-Deployment Service Public API.

AI-discoverable cloud-self-deployment verbs with ``@service_interface_process``
decorators. Methods are registered as
``service_interface::cloud_self_deployment_service::*`` and callable from
any operator-approved MCP client (except ``complete_deploy``, which is
internal-only and only dispatched by the platform action queue).

Four verbs cooperate via AWS state + the durable action queue:

- ``deploy_self`` runs steps 1–7 of the blue-green mechanic on the
  current container, then enqueues a ``complete_deploy`` action targeted
  at the new color. Returns immediately after enqueue with
  ``status='cutover_complete'``.
- ``complete_deploy`` runs steps 8–10 on the new color (observation
  window, teardown of old, post-cutover schema actions). Picked up by
  the new color's ``action_queue_poller``; not invoked by operators
  directly.
- ``deploy_status`` reads the live AWS state (ALB listener rule + ECS
  service descriptors) and reports the current version + any in-flight
  deploy.
- ``deploy_rollback`` swaps the listener rule back during the observation
  window and cancels the enqueued ``complete_deploy`` row.

Provider key renamed from ``self_deployment_service`` to
``cloud_self_deployment_service`` per the Phase 1 extension-interface
split (workbench/2026-06-02_aws_self_deployment_plugin_design.md §3.3
Option C). The 1-verb lifecycle base
(``service_interface::self_deployment_service::restart_with_manifest``)
lives at ``ananta/src/ananta/services/self_deployment_service/interfaces/public.py``;
cloud-only verbs stay separate so macOS-bound solets never see them
in ``process_search``.

See the self-deployment-plugin design record plus its addendum §A
(both dev-checkout workbench records — not part of the shipped tree).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process

PROVIDER = "cloud_self_deployment_service"

_IMAGE_TAG_PARAM = ParameterMetadata(
    description=(
        "ECR image tag to deploy, e.g. 'v4'. The plugin verifies the image "
        "exists in the solet repo before any AWS state changes."
    ),
    required=True,
    type=ParameterType.STRING,
)

_TIMEOUT_PARAM = ParameterMetadata(
    description=(
        "Maximum seconds to wait for the new target group to report healthy "
        "after the sibling ECS service boots. Default 300s covers a cold-cache "
        "ECS-on-EC2 boot."
    ),
    required=False,
    type=ParameterType.INTEGER,
    default=300,
)

_OBSERVATION_PARAM = ParameterMetadata(
    description=(
        "Seconds the new color holds before tearing down the old version. "
        "``deploy_rollback`` is callable during this window. Default 60s."
    ),
    required=False,
    type=ParameterType.INTEGER,
    default=60,
)

_DRY_RUN_PARAM = ParameterMetadata(
    description=(
        "If true, the plugin prints the planned AWS calls without executing "
        "any of them. Useful for auditing the deploy plan before committing."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)

_DETAIL_PARAM = ParameterMetadata(
    description=(
        "If true, the response includes resolved AWS resource ARNs (service, "
        "target group, listener rule) in addition to the version labels."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)

_REASON_PARAM = ParameterMetadata(
    description=(
        "Operator-supplied free-text justification for the rollback. Recorded "
        "in the response for audit. Example: 'canary-window-anomaly'."
    ),
    required=True,
    type=ParameterType.STRING,
)

_FINISHER_ACTION_ID_PARAM = ParameterMetadata(
    description=(
        "The action_id of the durable row ``deploy_self`` enqueued to trigger "
        "this finisher. Used to detect cancellation written by ``deploy_rollback``."
    ),
    required=True,
    type=ParameterType.STRING,
)

_PRIOR_VERSION_PARAM = ParameterMetadata(
    description=(
        "Version label of the now-old color (e.g. 'v3') whose ECS service and "
        "target group this finisher tears down."
    ),
    required=True,
    type=ParameterType.STRING,
)


def _deploy_self_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Outcome of a deploy_self invocation (steps 1–7).",
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "One of 'cutover_complete' (steps 1–7 done, finisher enqueued), "
                    "'rolled_back', 'failed', 'dry_run'."
                ),
            ),
            "from_version": ParameterMetadata(
                type=ParameterType.STRING,
                description="Resource-version label that was live before this deploy.",
            ),
            "to_version": ParameterMetadata(
                type=ParameterType.STRING,
                description="Resource-version label this deploy cut over to.",
            ),
            "image_tag": ParameterMetadata(
                type=ParameterType.STRING,
                description="Echo of the image tag the new task definition references.",
            ),
            "service_arn": ParameterMetadata(
                type=ParameterType.STRING,
                description="ARN of the newly-created ECS service.",
            ),
            "target_group_arn": ParameterMetadata(
                type=ParameterType.STRING,
                description="ARN of the newly-created target group.",
            ),
            "cutover_at": ParameterMetadata(
                type=ParameterType.STRING,
                description="ISO-8601 timestamp of the ALB ModifyRule call.",
            ),
            "duration_seconds": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Wall-clock seconds spent across steps 1–7.",
            ),
            "steps_completed": ParameterMetadata(
                type=ParameterType.LIST,
                description="Ordered list of step labels that ran to completion.",
            ),
            "finisher_action_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Action queue row id of the enqueued complete_deploy.",
            ),
            "error": ParameterMetadata(
                type=ParameterType.STRING,
                description="Human-readable failure message; absent on success.",
            ),
        },
    )


def _complete_deploy_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Outcome of a complete_deploy invocation (steps 8–10).",
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description="'completed', 'cancelled' (rollback fired), or 'failed'.",
            ),
            "prior_version": ParameterMetadata(
                type=ParameterType.STRING,
                description="Version label of the torn-down old color.",
            ),
            "steps_completed": ParameterMetadata(
                type=ParameterType.LIST,
                description="Ordered list of step labels that ran (observation, teardown, post_cutover_ddl).",
            ),
            "schema_actions_enqueued": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Count of destructive schema actions submitted to the platform queue.",
            ),
            "error": ParameterMetadata(
                type=ParameterType.STRING,
                description="Human-readable failure message; absent on success.",
            ),
        },
    )


def _deploy_status_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Live deploy state read from AWS.",
        properties={
            "current_version": ParameterMetadata(
                type=ParameterType.STRING,
                description="Version label of the service the listener rule forwards to.",
            ),
            "current_service_arn": ParameterMetadata(
                type=ParameterType.STRING,
                description="ECS service ARN of the live version (present when detail=true).",
            ),
            "current_tg_arn": ParameterMetadata(
                type=ParameterType.STRING,
                description="ALB target group ARN of the live version (present when detail=true).",
            ),
            "in_progress_deploy": ParameterMetadata(
                type=ParameterType.OBJECT,
                description="Sibling-deploy snapshot when one is mid-flight; absent otherwise.",
            ),
        },
    )


def _deploy_rollback_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Outcome of a deploy_rollback invocation.",
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description="'rolled_back' on success, 'rollback_not_applicable' outside the observation window.",
            ),
            "rolled_back_from": ParameterMetadata(
                type=ParameterType.STRING,
                description="Version label the listener rule was pointing at before rollback.",
            ),
            "rolled_back_to": ParameterMetadata(
                type=ParameterType.STRING,
                description="Version label the listener rule was swapped back to.",
            ),
            "finisher_cancelled": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description="True when the enqueued complete_deploy row was successfully cancelled.",
            ),
            "reason": ParameterMetadata(
                type=ParameterType.STRING,
                description="Echo of the operator-supplied reason argument.",
            ),
        },
    )


class CloudSelfDeploymentServicePublicAPI(ABC):
    """AI-discoverable cloud-self-deployment verbs.

    Access via: ``service_interface::cloud_self_deployment_service::{verb}``.
    """

    @service_interface_process(
        name="deploy_self",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "image_tag": _IMAGE_TAG_PARAM,
            "timeout_seconds": _TIMEOUT_PARAM,
            "observation_window_seconds": _OBSERVATION_PARAM,
            "dry_run": _DRY_RUN_PARAM,
        },
        return_value_schema=_deploy_self_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="self_deployment_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def deploy_self(
        self,
        image_tag: str,
        timeout_seconds: int = 300,
        observation_window_seconds: int = 60,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run steps 1–7 of the blue-green swap; enqueue ``complete_deploy``; return.

        Verifies the image exists in ECR, births a sibling task definition +
        target group + ECS service, waits for health, atomically swaps the
        ALB listener rule, drains the old TG, then enqueues a durable
        ``complete_deploy`` action for the new color to pick up. Steps 8–10
        run there, off the now-deprecated v(N) executor.
        """

    @service_interface_process(
        name="complete_deploy",
        provider=PROVIDER,
        is_discoverable=False,
        parameters={
            "finisher_action_id": _FINISHER_ACTION_ID_PARAM,
            "prior_version": _PRIOR_VERSION_PARAM,
            "observation_window_seconds": _OBSERVATION_PARAM,
        },
        return_value_schema=_complete_deploy_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="self_deployment_complete",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def complete_deploy(
        self,
        finisher_action_id: str,
        prior_version: str,
        observation_window_seconds: int = 60,
    ) -> dict[str, Any]:
        """Run steps 8–10 on the new color (observation, teardown, post-cutover DDL).

        Internal-only; picked up by the new color's ``action_queue_poller``
        after ``deploy_self`` enqueued this row. Watches the action row's
        status for cancellation written by ``deploy_rollback``; if cancelled
        during the observation window, returns ``status='cancelled'`` without
        tearing down the old color.
        """

    @service_interface_process(
        name="deploy_status",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"detail": _DETAIL_PARAM},
        return_value_schema=_deploy_status_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="self_deployment_status",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def deploy_status(self, detail: bool = False) -> dict[str, Any]:
        """Read AWS state and report the live version + any in-flight deploy.

        Reads the ALB listener rule (forward target group) to identify the live
        version. When a sibling target group exists, includes the in-progress
        snapshot. Always callable; reads from AWS as source of truth so the
        new color can answer for a deploy started by the old color.
        """

    @service_interface_process(
        name="deploy_rollback",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"reason": _REASON_PARAM},
        return_value_schema=_deploy_rollback_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="self_deployment_rollback",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def deploy_rollback(self, reason: str) -> dict[str, Any]:
        """Swap the listener rule back during the observation window and cancel the finisher.

        Verifies the old target group still exists, atomically modifies the
        listener rule back to it, and updates the durable ``complete_deploy``
        row to ``status='cancelled'`` so the new color's poller skips
        teardown. Returns ``status='rollback_not_applicable'`` if called
        before cutover or after the old service is gone.
        """
