"""Local Self-Deployment Service Public API."""

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
from ananta.interfaces.lifecycle_result_types import AutostartResult, RestartResult

PROVIDER = "local_self_deployment_service"

_PRIOR_PID_PARAM = ParameterMetadata(
    description="OS pid of the previously active the solet process.",
    required=True,
    type=ParameterType.INTEGER,
)
_PRIOR_INSTANCE_ID_PARAM = ParameterMetadata(
    description="Router-side instance id for the previously active color.",
    required=True,
    type=ParameterType.STRING,
)
_PRIOR_COLOR_PARAM = ParameterMetadata(
    description="Prior color token, normally 'blue' or 'green'.",
    required=True,
    type=ParameterType.STRING,
)
_ROLLBACK_REASON_PARAM = ParameterMetadata(
    description="Operator-supplied rollback justification recorded in the response.",
    required=True,
    type=ParameterType.STRING,
)
_ROLLBACK_RELEASE_REASON_PARAM = ParameterMetadata(
    description="Operator-supplied audit string recorded in the rollback envelope.",
    required=True,
    type=ParameterType.STRING,
)
_ROLLBACK_RELEASE_EXPECTED_ETAG_PARAM = ParameterMetadata(
    description="Manifest ETag CAS lock asserted by the operator (see "
    "lifecycle_interfaces_design §13.2).",
    required=True,
    type=ParameterType.STRING,
)
_ROLLBACK_RELEASE_EXPECTED_CURRENT_RELEASE_PARAM = ParameterMetadata(
    description="Concurrency CAS: the rel-<id> the caller observed as the live "
    "'current' release. A mismatch against ReleaseManager.current_release "
    "returns FAILED(stale_current_release) before any spawn.",
    required=True,
    type=ParameterType.STRING,
)
_AUTOSTART_DRY_RUN_PARAM = ParameterMetadata(
    description="Plan and report without writing LaunchAgent files or invoking launchctl.",
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)
_RECONCILIATION_ID_PARAM = ParameterMetadata(
    description="Reconciliation operation identity for provenance correlation.",
    required=False,
    type=ParameterType.STRING,
    default="",
)
_CUTOVER_ID_PARAM = ParameterMetadata(
    description="Approved reconciliation identity authorizing this cutover.",
    required=True,
    type=ParameterType.STRING,
)
_CUTOVER_EXPECT_PARAMS: dict[str, ParameterMetadata] = {
    name: ParameterMetadata(description=description, required=True, type=ParameterType.STRING)
    for name, description in (
        ("expected_source_surface_sha256", "Approved reconciliation-surface digest."),
        ("expected_release_surface_sha256", "Approved complete release-surface digest."),
        ("expected_manifest_etag", "Manifest ETag observed at approval time."),
        ("expected_current_release_id", "Release the caller observed as current."),
        ("expected_active_instance_id", "Router-active instance observed at approval time."),
        ("expected_active_start_token", "Start token of that exact active instance."),
    )
}
_CUTOVER_DRY_RUN_PARAM = ParameterMetadata(
    description="If true, run every compare-and-swap leg and plan without touching router state.",
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)
_VERIFICATION_MODULES_PARAM = ParameterMetadata(
    description="Closed list of already-loaded module names to attest without importing.",
    required=True,
    type=ParameterType.LIST,
)



def _complete_swap_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="complete_swap outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Terminal status."),
            "prior_instance_id": ParameterMetadata(type=ParameterType.STRING, description="Prior router instance."),
            "prior_color": ParameterMetadata(type=ParameterType.STRING, description="Prior color token."),
            "steps_completed": ParameterMetadata(type=ParameterType.LIST, description="Completed cleanup steps."),
        },
    )


def _swap_status_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="swap_status outcome.",
        properties={
            "router_status": ParameterMetadata(type=ParameterType.OBJECT, description="Router status snapshot."),
            "swap_in_progress": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether this plugin is mid-swap."),
            "self_color": ParameterMetadata(type=ParameterType.STRING, description="This process color."),
            "self_instance_id": ParameterMetadata(type=ParameterType.STRING, description="This router instance id."),
        },
    )


def _runtime_attestation_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Router-served immutable release and live-module attestation.",
        properties={
            "schema_version": ParameterMetadata(type=ParameterType.INTEGER, description="Schema version."),
            "status": ParameterMetadata(type=ParameterType.STRING, description="Attestation status."),
            "solet_name": ParameterMetadata(type=ParameterType.STRING, description="Serving solet name."),
            "served_by_self": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether router serves this process."),
            "self_instance_id": ParameterMetadata(type=ParameterType.STRING, description="This router instance."),
            "self_color": ParameterMetadata(type=ParameterType.STRING, description="This router color."),
            "self_pid": ParameterMetadata(type=ParameterType.INTEGER, description="This process PID."),
            "self_start_token": ParameterMetadata(type=ParameterType.STRING, description="This process start token."),
            "router_active_instance_id": ParameterMetadata(type=ParameterType.STRING, description="Router active instance."),
            "router_active_color": ParameterMetadata(type=ParameterType.STRING, description="Router active color."),
            "release_id": ParameterMetadata(type=ParameterType.STRING, description="Serving immutable release."),
            "current_release_id": ParameterMetadata(type=ParameterType.STRING, description="Current release pointer."),
            "manifest_etag": ParameterMetadata(type=ParameterType.STRING, description="Release manifest ETag."),
            "reconciliation_id": ParameterMetadata(type=ParameterType.STRING, description="Reconciliation identity."),
            "source_surface_sha256": ParameterMetadata(type=ParameterType.STRING, description="Reconciliation surface digest."),
            "release_surface_sha256": ParameterMetadata(type=ParameterType.STRING, description="Complete release surface digest."),
            "release_version_sha256": ParameterMetadata(type=ParameterType.STRING, description="VERSION file digest."),
            "modules": ParameterMetadata(type=ParameterType.LIST, description="Observed module provenance."),
            "sample_basis": ParameterMetadata(type=ParameterType.STRING, description="Module sampling basis."),
        },
    )


def _cutover_release_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Reconciliation-authorized cutover outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Terminal or queued cutover status."),
            "reason_code": ParameterMetadata(type=ParameterType.STRING, description="Machine-readable outcome cause."),
            "reconciliation_id": ParameterMetadata(type=ParameterType.STRING, description="Authorizing reconciliation."),
            "prior_release_id": ParameterMetadata(type=ParameterType.STRING, description="Release serving before the swap."),
            "prior_instance_id": ParameterMetadata(type=ParameterType.STRING, description="Exact prior router instance."),
            "prior_color": ParameterMetadata(type=ParameterType.STRING, description="Prior router colour."),
            "provenance": ParameterMetadata(type=ParameterType.OBJECT, description="Provenance stamped into the candidate VERSION."),
            "expected": ParameterMetadata(type=ParameterType.STRING, description="Asserted value on a stale-approval refusal."),
            "observed": ParameterMetadata(type=ParameterType.STRING, description="Measured value on a stale-approval refusal."),
            "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether this was a planning run."),
        },
    )


def _swap_rollback_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="swap_rollback outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Terminal status."),
            "rolled_back_to": ParameterMetadata(type=ParameterType.STRING, description="Color restored as active."),
            "reason": ParameterMetadata(type=ParameterType.STRING, description="Rollback reason."),
        },
    )


def _rollback_release_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="rollback_release outcome (durable code rollback).",
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description="Terminal RestartStatus: queued / failed / "
                "needs_intervention.",
            ),
            "restart_action_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="Pollable complete_swap action id when queued.",
            ),
            "message": ParameterMetadata(type=ParameterType.STRING, description="Detail."),
            "reason": ParameterMetadata(type=ParameterType.STRING, description="Echoed reason."),
            "expected_etag": ParameterMetadata(
                type=ParameterType.STRING, description="Echoed ETag CAS lock.",
            ),
            "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN, description="Echo of dry_run."),
            "reason_code": ParameterMetadata(
                type=ParameterType.STRING,
                description="Machine-readable cause (e.g. no_previous, "
                "rollback_target_unbootable).",
            ),
        },
    )


def _autostart_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Autostart-verb outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Terminal autostart status."),
            "verb": ParameterMetadata(type=ParameterType.STRING, description="Autostart verb name."),
            "solet_name": ParameterMetadata(type=ParameterType.STRING, description="Target solet."),
            "label": ParameterMetadata(type=ParameterType.STRING, description="LaunchAgent label."),
            "plist_path": ParameterMetadata(type=ParameterType.STRING, description="LaunchAgent plist path."),
            "prior_state": ParameterMetadata(type=ParameterType.STRING, description="State before the verb."),
            "last_run_at": ParameterMetadata(type=ParameterType.STRING, description="Last observed launch time."),
            "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN, description="Echo of dry_run."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="Human-readable detail."),
        },
    )


class LocalSelfDeploymentServicePublicAPI(ABC):
    """AI-discoverable local self-deployment extension surface."""

    @service_interface_process(
        name="attest_runtime_code",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "reconciliation_id": _RECONCILIATION_ID_PARAM,
            "verification_modules": _VERIFICATION_MODULES_PARAM,
        },
        return_value_schema=_runtime_attestation_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="local_runtime_code_attestation",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def attest_runtime_code(
        self,
        *,
        reconciliation_id: str,
        verification_modules: tuple[str, ...],
    ) -> dict[str, Any]:
        """Attest the router-served immutable release and loaded modules."""

    @service_interface_process(
        name="cutover_release",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "reconciliation_id": _CUTOVER_ID_PARAM,
            **_CUTOVER_EXPECT_PARAMS,
            "dry_run": _CUTOVER_DRY_RUN_PARAM,
        },
        return_value_schema=_cutover_release_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="local_reconciliation_cutover",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def cutover_release(
        self,
        *,
        reconciliation_id: str,
        expected_source_surface_sha256: str,
        expected_release_surface_sha256: str,
        expected_manifest_etag: str,
        expected_current_release_id: str,
        expected_active_instance_id: str,
        expected_active_start_token: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Cut over to a candidate built from reconciliation-approved bytes."""

    @service_interface_process(
        name="complete_swap",
        provider=PROVIDER,
        is_discoverable=False,
        parameters={
            "prior_pid": _PRIOR_PID_PARAM,
            "prior_instance_id": _PRIOR_INSTANCE_ID_PARAM,
            "prior_color": _PRIOR_COLOR_PARAM,
        },
        return_value_schema=_complete_swap_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="local_blue_green_complete_swap_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def complete_swap(
        self,
        prior_pid: int,
        prior_instance_id: str,
        prior_color: str,
    ) -> dict[str, Any]:
        """Finish a local blue-green swap from the new active color."""

    @service_interface_process(
        name="swap_status",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={},
        return_value_schema=_swap_status_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="local_blue_green_swap_status",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def swap_status(self) -> dict[str, Any]:
        """Read router state plus plugin-local swap state."""

    @service_interface_process(
        name="swap_rollback",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"reason": _ROLLBACK_REASON_PARAM},
        return_value_schema=_swap_rollback_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="local_blue_green_swap_rollback_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def swap_rollback(self, reason: str) -> dict[str, Any]:
        """Rollback to the previous color during the drain window."""

    @service_interface_process(
        name="rollback_release",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "reason": _ROLLBACK_RELEASE_REASON_PARAM,
            "expected_etag": _ROLLBACK_RELEASE_EXPECTED_ETAG_PARAM,
            "expected_current_release": _ROLLBACK_RELEASE_EXPECTED_CURRENT_RELEASE_PARAM,
        },
        return_value_schema=_rollback_release_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="local_blue_green_rollback_release_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def rollback_release(
        self, *, reason: str, expected_etag: str, expected_current_release: str,
    ) -> RestartResult:
        """Durably roll back to the prior materialized release (escape hatch).

        DISTINCT from ``swap_rollback`` (the in-drain-window router re-point):
        this brings the prior release's code back up and flips the durable
        ``current``/``previous`` symlinks, so it works at any time — post-drain,
        post-reboot. ``expected_current_release`` is the concurrency CAS — the
        ``rel-<id>`` the caller observed as ``current``; a mismatch returns
        FAILED(stale_current_release) before any spawn. Returns the
        RestartResult partition (queued / failed / needs_intervention) — see
        ``RestartReasonCode`` for the cause codes.
        """

    @service_interface_process(
        name="install_autostart",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"dry_run": _AUTOSTART_DRY_RUN_PARAM},
        return_value_schema=_autostart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="macos_self_deployment_autostart_install_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def install_autostart(self, *, dry_run: bool = False) -> AutostartResult:
        """Install and load the per-solet LaunchAgent."""

    @service_interface_process(
        name="uninstall_autostart",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"dry_run": _AUTOSTART_DRY_RUN_PARAM},
        return_value_schema=_autostart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="macos_self_deployment_autostart_uninstall_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def uninstall_autostart(self, *, dry_run: bool = False) -> AutostartResult:
        """Unload and remove the per-solet LaunchAgent."""

    @service_interface_process(
        name="status_autostart",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={},
        return_value_schema=_autostart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="macos_self_deployment_autostart_status_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def status_autostart(self) -> AutostartResult:
        """Report whether the LaunchAgent is installed and loaded."""
