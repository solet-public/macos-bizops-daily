"""Undertaker service interface — single verb to tear a solet down.

The lifecycle matrix's teardown surface. Implemented by environment-specific
plugins; only ``aws_undertaker_plugin`` exists today. Per operator constraint
(``workbench/2026-06-02_aws_undertaker_plugin_design.md`` §2.2) there are no
macOS/Linux/Windows variants — local-machine teardown is operator-manual.

Per the D1 architectural mandate
(``ananta_platform/08_service_architecture/SERVICE_ARCHITECTURE.md``), the
``@service_interface_process`` registration surface lives at
``ananta/src/ananta/services/undertaker_service/interfaces/public.py``
with a matching JSON at
``ananta/knowledge_base/processes/undertaker_service/teardown_solet.json``.

Per D17' closure (operator decision 2026-06-02 PM per
``[[biological-organic-system-framing]]``) the interface is single-verb:
recovery within the configured snapshot / secret / KMS windows is
operator-side AWS-console work, not a platform verb. Plugins do NOT
expose a ``cancel_teardown`` companion.

Per D6' / Architect's matrix design §17 (``α`` closure), the verb is
**sync** — backgrounding is the caller's concern via
``scheduling_service::execute_in_seconds``. The platform has no async
dispatch path for ``@service_interface_process``-decorated methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.interfaces.lifecycle_result_types import TeardownResult


class UndertakerServiceInterface(ABC):
    """Tear down a solet: live → decommissioned with recovery windows."""

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def teardown_solet(
        self,
        *,
        name: str,
        snapshot_data: bool = True,
        snapshot_retention_days: int = 7,
        secret_recovery_days: int = 7,
        dry_run: bool = False,
    ) -> TeardownResult:
        """Tear the named solet down.

        Contract:

        - The verb is committed-once-started. Per D17' there is no
          platform ``cancel_teardown``. Recovery within the configured
          windows is operator-side AWS-console work using the
          identifiers returned in :class:`TeardownResult` (snapshot id,
          KMS key id, secret ARNs).

        - Implementations MUST be idempotent under mixed-state cleanup
          (per ``[[verify-against-code-before-asserting]]``: every
          step probes presence via the AWS Describe/List/Get verb and
          skips the corresponding Delete when already gone, rather
          than blind-deleting and catching not-found errors).

        - ``dry_run=True`` MUST plan + report without issuing any
          ``Delete*`` / ``Schedule*Deletion`` / ``Create*Snapshot`` /
          ``Tag*`` / ``Remove*`` / ``Modify*`` / ``Update*`` calls.
          Per Step 1 v2 §14, the same call with ``dry_run=False``
          attempts the steps surfaced in the dry-run output — no
          divergence allowed.

        Args:
            name: Target solet name. Validated against the
                ``[a-z][a-z0-9_-]{1,62}`` regex midwife uses at
                provisioning time. The implementation discovers
                resources by tag (``HomunculusName == name``); an
                unvalidated name could match unintended resources.
            snapshot_data: When ``True`` (default), take an RDS final
                snapshot before deleting the DB instance. Operator
                safety net.
            snapshot_retention_days: How long the RDS snapshot stays
                alive (integer 1–365). AWS has no native auto-delete
                for manual snapshots; implementations schedule a
                follow-on ``rds:DeleteDBSnapshot`` action via the
                platform scheduling service.
            secret_recovery_days: Passed straight to
                ``secretsmanager:DeleteSecret(RecoveryWindowInDays=N)``
                for each solet secret. AWS allows 7–30; default 7
                is the minimum window.
            dry_run: Plan-only mode (see contract above).

        Returns:
            :class:`TeardownResult` carrying the terminal status, the
            full step audit, snapshot identifiers, scheduled-deletion
            timestamps for secrets and KMS key, and the path of the
            on-disk Phase D1 teardown record.
        """
