"""Self-Deployment Service Public API.

AI-discoverable ``restart_with_manifest`` verb with
``@service_interface_process`` decorator. Registers as
``service_interface::self_deployment_service::restart_with_manifest``
and routes through the platform's standard service-interface dispatch
(``service_bindings.get_plugin_name("self_deployment_service")`` →
resolved plugin → ``restart_with_manifest()`` method).

The actual ABC contract lives at
``ananta.interfaces.self_deployment_service_interface.SelfDeploymentServiceInterface``;
this wrapper exists solely so the process-registry scanner
(``service_interface_scanner._scan_service_interfaces_in_directory``)
picks the verb up — the scanner walks ``*/interfaces/public.py`` files
looking for ``@service_interface_process`` decorators, and bound
``ServiceProvider`` plugins are skipped from the ``plugin::`` namespace,
so without this wrapper the verb would be documented in the KB but not
actually callable via ``process_call``.

Per the D1 architectural mandate
(``knowledge_bases/ananta_platform_knowledge_base/08_service_architecture/SERVICE_ARCHITECTURE.md``)
this is the canonical public-API surface; a matching JSON sits at
``ananta/knowledge_base/processes/self_deployment_service/restart_with_manifest.json``.
The registry refuses to build if a decorated method is missing its JSON.

Per D6' (operator-confirmed 2026-06-02), the verb is **sync** —
backgrounding is the caller's concern via
``scheduling_service::execute_in_seconds``. No async cascade through
the platform's action dispatch.
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
from ananta.interfaces.lifecycle_result_types import RestartResult, StopSelfResult

PROVIDER = "self_deployment_service"


_NEW_MANIFEST_PARAM = ParameterMetadata(
    description=(
        "The manifest dict ``apply_manifest`` just wrote to disk. v1 scope "
        "(Architect §15.1): ``{plugins: list[str], profile_name?: str}``. "
        "Carried for audit + future routing; the on-disk file at "
        "``<APP_HOME>/config/manifest.yaml`` is the source of truth that "
        "the new boot actually reads."
    ),
    required=True,
    type=ParameterType.OBJECT,
)
_EXPECTED_ETAG_PARAM = ParameterMetadata(
    description=(
        "CAS lock from ``apply_manifest``'s pre-flight read of the on-disk "
        "manifest. Implementations re-read the manifest's stored ETag at "
        "dispatch time and refuse the restart when it differs from this "
        "value. Guards against a second ``apply_manifest`` racing the first "
        "between manifest-write and restart-dispatch."
    ),
    required=True,
    type=ParameterType.STRING,
)
_REASON_PARAM = ParameterMetadata(
    description=(
        "Operator-supplied audit string. Recorded in the restart message "
        "and surfaced in the response for audit correlation."
    ),
    required=True,
    type=ParameterType.STRING,
)
_DRY_RUN_PARAM = ParameterMetadata(
    description=(
        "When ``True``, the implementation plans + reports without "
        "mutating any cloud / local-process state. Cloud sibling emits "
        "the planned AWS calls; macOS sibling reports what the watchdog "
        "would do without forking."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)

_STOP_REASON_PARAM = ParameterMetadata(
    description=(
        "Operator-supplied audit string for the stop. Required — a stop "
        "without an audit message is undisciplined operator action. "
        "Recorded on the LaunchAgent log / CloudWatch + echoed in the "
        "response."
    ),
    required=True,
    type=ParameterType.STRING,
)
_STOP_DRY_RUN_PARAM = ParameterMetadata(
    description=(
        "When ``True``, plan + report without writing the drain "
        "sentinel, spawning the watchdog (macOS), or calling ECS "
        "UpdateService (cloud). Returns StopSelfStatus.DRY_RUN."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)


def _stop_self_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="stop_self outcome from the bound self-deployment plugin.",
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "StopSelfStatus value: 'success' (stop scheduled/confirmed), "
                    "'already_stopped' (idempotent re-invocation), 'failed', or "
                    "'dry_run'."
                ),
            ),
            "reason": ParameterMetadata(
                type=ParameterType.STRING,
                description="Echo of the operator-supplied reason.",
            ),
            "duration_seconds": ParameterMetadata(
                type=ParameterType.FLOAT,
                description=(
                    "Wall-clock seconds the verb spent. Sub-second on macOS "
                    "(watchdog runs out-of-process); minutes on cloud "
                    "(describe_services polling)."
                ),
            ),
            "stopped_at": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "ISO-8601 UTC of confirmed stop (cloud: runningCount=0; "
                    "macOS: watchdog spawn moment)."
                ),
            ),
            "backend_action_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Backend-specific audit id: macOS watchdog pid as a string "
                    "or cloud ECS service ARN."
                ),
            ),
            "dry_run": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description="Echo of the dry_run flag.",
            ),
            "message": ParameterMetadata(
                type=ParameterType.STRING,
                description="Human-readable status detail.",
            ),
        },
    )


def _restart_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Restart outcome from the bound self-deployment plugin.",
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "RestartStatus value: 'queued' (async cutover scheduled "
                    "— happy path), 'completed' (synchronous restart finished "
                    "— rare), 'failed' (could not be scheduled; system "
                    "unchanged + retryable), or 'needs_intervention' "
                    "(automated recovery exhausted — a human must act). When "
                    "the status is outside {queued, completed}, "
                    "``apply_manifest`` propagates the failure into its own "
                    "envelope per Codex review #2 Finding 1."
                ),
            ),
            "restart_action_id": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Pollable identifier for the cutover (cloud sibling "
                    "returns ``finisher_action_id`` consumable by "
                    "``cloud_self_deployment_service::deploy_status``). "
                    "Empty string for macOS sibling (audit-only token) and "
                    "on failed status."
                ),
            ),
            "message": ParameterMetadata(
                type=ParameterType.STRING,
                description="Human-readable status detail.",
            ),
            "reason": ParameterMetadata(
                type=ParameterType.STRING,
                description="Echo of the operator-supplied reason.",
            ),
            "expected_etag": ParameterMetadata(
                type=ParameterType.STRING,
                description="Echo of the CAS-lock etag the caller asserted.",
            ),
            "dry_run": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description="Echo of the dry_run flag.",
            ),
        },
    )


class SelfDeploymentServicePublicAPI(ABC):
    """AI-discoverable self-deployment surface.

    Access via:
    ``service_interface::self_deployment_service::restart_with_manifest``.

    The scanner discovers this class because it lives at the canonical
    ``*/interfaces/public.py`` path. The ``@service_interface_process``
    decorator registers the verb with the process registry; at dispatch
    time the action processor resolves the bound self-deployment plugin
    via ``orchestrator.get_service('self_deployment_service')`` and calls
    ``restart_with_manifest()`` on it directly (this ABC is never
    instantiated).
    """

    @service_interface_process(
        name="restart_with_manifest",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "new_manifest": _NEW_MANIFEST_PARAM,
            "expected_etag": _EXPECTED_ETAG_PARAM,
            "reason": _REASON_PARAM,
            "dry_run": _DRY_RUN_PARAM,
        },
        return_value_schema=_restart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="self_deployment_restart_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def restart_with_manifest(
        self,
        *,
        new_manifest: dict[str, Any],
        expected_etag: str,
        reason: str,
        dry_run: bool = False,
    ) -> RestartResult:
        """Restart this solet with the manifest just written to disk.

        Cloud profile binds this to ``aws_self_deployment_plugin`` (blue-green
        ECS swap with the new manifest fetched from S3 at the new color's
        boot). macOS profile binds it to ``macos_self_deployment_plugin``
        (router-mediated blue/green swap that spawns the next color via
        ``python -m ananta.cli``, polls the router until green registers,
        and swaps atomically).

        Per D6' (operator-confirmed 2026-06-02), the verb is **sync** —
        backgrounding is the caller's concern via
        ``scheduling_service::execute_in_seconds``.
        """

    @service_interface_process(
        name="stop_self",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "reason": _STOP_REASON_PARAM,
            "dry_run": _STOP_DRY_RUN_PARAM,
        },
        return_value_schema=_stop_self_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="self_deployment_stop_self_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def stop_self(
        self,
        *,
        reason: str,
        dry_run: bool = False,
    ) -> StopSelfResult:
        """Stop this solet without tearing down its infrastructure.

        Cloud profile binds this to ``aws_self_deployment_plugin`` (ECS
        ``UpdateService(DesiredCount=0)`` + polling). macOS profile
        binds it to ``macos_self_deployment_plugin`` (drain sentinel
        write + detached SIGTERM watchdog). Distinct from
        ``aws_undertaker_plugin::teardown_solet`` which DESTROYS
        infra; stop_self leaves all infra in place so the operator can
        re-set ``DesiredCount=1`` (cloud) or run ``./launch.py``
        (macOS) to bring the solet back.

        Per Slice 4.5 of the bridge-port-routing design.
        """
