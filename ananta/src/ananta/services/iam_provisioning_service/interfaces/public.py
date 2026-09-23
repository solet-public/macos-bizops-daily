"""IAM Provisioning Service Public API.

AI-discoverable ``provision_lifecycle_roles`` + ``audit_lifecycle_roles``
verbs with the ``@service_interface_process`` decorator. Registers as
``service_interface::iam_provisioning_service::*`` and routes through the
platform's standard service-interface dispatch path
(``service_bindings.get_plugin_name("iam_provisioning_service")`` →
resolved plugin → method call).

The actual ABC contract lives at
``ananta.interfaces.iam_provisioning_service_interface
.IamProvisioningServiceInterface``; this wrapper exists so the
process-registry scanner picks the verbs up — the scanner walks
``*/interfaces/public.py`` files looking for
``@service_interface_process`` decorators, and bound ``ServiceProvider``
plugins are skipped from the ``plugin::`` namespace, so without this
wrapper the verbs would be documented in the KB but not actually
callable via ``process_call``.

Per the D1 architectural mandate
(``ananta_platform/08_service_architecture/SERVICE_ARCHITECTURE.md``)
this is the canonical public-API surface; matching JSONs sit at
``ananta/knowledge_base/processes/iam_provisioning_service/<verb>.json``.
The registry refuses to build if a decorated method is missing its JSON.

Per D30' (operator decision 2026-06-02 PM) v1 verb subset = two verbs
only; ``rotate_role_policies`` is folded into
``provision_lifecycle_roles`` per the idempotency-as-rotation insight
(design §6.2); ``decommission_role`` is rare operator-action acceptable
via AWS console. ``[[no-phantom-abstractions]]``.

Per D-LANDMINE-ASYNC (α) closure, both verbs are SYNC ``def``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process
from ananta.interfaces.lifecycle_result_types import AdminResult

PROVIDER = "iam_provisioning_service"


_BIRTHER_NAME_PARAM = ParameterMetadata(
    description=(
        "The birther solet whose midwife + undertaker IAM roles are "
        "being provisioned or audited. Validated against "
        "``[a-z][a-z0-9_-]{1,62}``. Used to compose role names "
        "``<birther_name>-midwife`` and ``<birther_name>-undertaker``."
    ),
    required=True,
    type=ParameterType.STRING,
)
_ROLE_NAMES_REQUIRED_PARAM = ParameterMetadata(
    description=(
        "Subset of ``[\"midwife\", \"undertaker\"]``. Empty list rejects "
        "with ``status=rejected`` and ``message=\"no_roles_specified\"``. "
        "Allows callers to provision just midwife OR just undertaker if "
        "the birther is single-purpose."
    ),
    required=True,
    type=ParameterType.LIST,
)
_POLICY_TEMPLATE_VERSION_PARAM = ParameterMetadata(
    description=(
        "Opaque semver string (e.g. ``\"v1.0.0\"``) the caller pins. The "
        "plugin's policy rendering code carries a constant of \"blessed\" "
        "versions; mismatches reject with "
        "``message=\"policy_template_version_unsupported\"``. Forces "
        "operator to update the plugin before claiming a new version."
    ),
    required=True,
    type=ParameterType.STRING,
)
_DRY_RUN_PARAM = ParameterMetadata(
    description=(
        "When true, the implementation plans + reports without issuing "
        "any state-changing IAM calls (no ``CreateRole``, "
        "``PutRolePolicy``, ``UpdateAssumeRolePolicy``, ``DeleteRole``, "
        "``TagRole``, ``UntagRole``). Returns the planned step list so "
        "an operator can audit before committing. Re-running with "
        "``dry_run=false`` attempts the same steps — no divergence "
        "allowed (Step 1 v2 §14)."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)


def _admin_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description=(
            "Admin verb outcome from the bound IAM provisioning plugin "
            "(see :class:`~ananta.interfaces.lifecycle_result_types.AdminResult`)."
        ),
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "AdminStatus value: ``success`` / ``partial`` / "
                    "``failed`` / ``rejected`` / ``dry_run``."
                ),
            ),
            "birther_name": ParameterMetadata(
                type=ParameterType.STRING,
                description="Echo of the operator-supplied birther name.",
            ),
            "idempotency_key": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "16-hex sha256 derived from (birther_name, sorted "
                    "(role_names_required), policy_template_version). "
                    "Tags applied to provisioned roles carry this so a "
                    "resume can identify its own prior work. Empty for "
                    "``audit_lifecycle_roles``."
                ),
            ),
            "dry_run": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description="Echo of the dry_run flag.",
            ),
            "verb": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Identifier of the producing verb "
                    "(``provision_lifecycle_roles`` or "
                    "``audit_lifecycle_roles``)."
                ),
            ),
            "steps": ParameterMetadata(
                type=ParameterType.LIST,
                description=(
                    "Per-role audit records. Each entry has at minimum "
                    "``role`` (``midwife`` or ``undertaker``), "
                    "``status``, and a ``role_arn`` when the role "
                    "exists. Per-step output detail is implementation-"
                    "defined."
                ),
            ),
            "roles_provisioned": ParameterMetadata(
                type=ParameterType.LIST,
                description=(
                    "Role ARNs the verb created or updated this attempt. "
                    "Empty when no state changed (audit, dry_run, "
                    "fully-current re-runs)."
                ),
            ),
            "policy_template_version": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Echo of the policy template version the verb planned "
                    "against. Empty for ``audit_lifecycle_roles`` (each "
                    "role can carry a different version; per-step output "
                    "records the per-role version)."
                ),
            ),
            "message": ParameterMetadata(
                type=ParameterType.STRING,
                description="Human-readable detail.",
            ),
        },
    )


class IamProvisioningServicePublicAPI(ABC):
    """AI-discoverable IAM provisioning surface.

    Access via:

    - ``service_interface::iam_provisioning_service::provision_lifecycle_roles``
    - ``service_interface::iam_provisioning_service::audit_lifecycle_roles``

    The scanner discovers this class because it lives at the canonical
    ``*/interfaces/public.py`` path. The ``@service_interface_process``
    decorators register both verbs with the process registry; at
    dispatch time the action processor resolves the bound plugin via
    ``orchestrator.get_service('iam_provisioning_service')`` and calls
    the corresponding method on it directly (this ABC is never
    instantiated).
    """

    @service_interface_process(
        name="provision_lifecycle_roles",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "birther_name": _BIRTHER_NAME_PARAM,
            "role_names_required": _ROLE_NAMES_REQUIRED_PARAM,
            "policy_template_version": _POLICY_TEMPLATE_VERSION_PARAM,
            "dry_run": _DRY_RUN_PARAM,
        },
        return_value_schema=_admin_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="admin_provision_lifecycle_roles_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def provision_lifecycle_roles(
        self,
        *,
        birther_name: str,
        role_names_required: list[str],
        policy_template_version: str,
        dry_run: bool = False,
    ) -> AdminResult:
        """Create or update ``<birther>-midwife`` + ``<birther>-undertaker`` IAM roles.

        Cloud profile binds this to ``aws_account_admin_plugin``. No
        local profile binding exists (local-machine has no IAM).

        Idempotent + rotation-aware per the design §6.2
        idempotency-as-rotation insight. ``dry_run=True`` returns the
        planned step list without issuing any state-changing IAM calls.

        Per the herky-jerky load semantics (design §5), the verb is
        callable only during operator-initiated transient
        ``apply_manifest`` cycles that ADD the bound plugin.
        """

    @service_interface_process(
        name="audit_lifecycle_roles",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "birther_name": _BIRTHER_NAME_PARAM,
        },
        return_value_schema=_admin_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=False,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="admin_audit_lifecycle_roles_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def audit_lifecycle_roles(
        self,
        *,
        birther_name: str,
    ) -> AdminResult:
        """Read-only inventory of the birther's lifecycle role quartet members.

        Reports trust + inline policy hashes, attached managed policies,
        tag sets. Detects drift from expected shape; detects orphaned
        roles. No IAM mutations of any kind. The same per-step drift-
        detection fields ``provision_lifecycle_roles(dry_run=True)``
        surfaces — operators have a clean "audit then provision" workflow
        without duplicate state machines.
        """
