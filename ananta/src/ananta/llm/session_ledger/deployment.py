"""Deployment domain mixin for the session-ledger repository.

W5.O cycle 9 §3.8: 7 M5 shipper bootstrap + pairing methods. Migrated off raw
``transactional()`` SQL onto the state-interface primitives (SQL-lockdown #0,
Slice "deployment"): INSERT → ``_write`` (write_state), point reads → ``_query``
(query_state), the pairing-status transitions → ``_update`` (update_state, whose
predicated ``filters`` ARE the compare-and-set on ``pairing_status``).
"""

from __future__ import annotations

from ananta.llm.session_ledger.base import SessionLedgerRepositoryBase
from ananta.llm.session_ledger.schema import (
    ID_PREFIX_DEPLOYMENT,
    NAMESPACE,
    TABLE_DEPLOYMENT,
)
from ananta.llm.session_ledger.shared import _new_id
from ananta.llm.session_ledger.types import PairingStatus

RELOAD_SAFE = True


class SessionLedgerDeploymentMixin(SessionLedgerRepositoryBase):
    """Deployment domain mixin (M5 shipper bootstrap + pairing)."""

    __slots__ = ()

    def insert_pending_deployment(
        self,
        *,
        machine_id: str,
        initiating_client_id: str,
        authorized_source_kinds: list[str],
    ) -> str:
        """Create a pending shipper deployment row (spec §13 step 1).

        ``authorized_source_kinds`` is passed as a Python list; the write layer
        serializes it to JSONB (no caller ``json.dumps`` / ``::jsonb`` cast).
        """
        deployment_id = _new_id(ID_PREFIX_DEPLOYMENT)
        now = self._clock()
        self._write(
            TABLE_DEPLOYMENT,
            {
                "id": deployment_id,
                "namespace": NAMESPACE,
                "machine_id": machine_id,
                "pairing_status": PairingStatus.PENDING.value,
                "initiating_client_id": initiating_client_id,
                "authorized_source_kinds": list(authorized_source_kinds),
                "created_at": now,
                "updated_at": now,
            },
        )
        return deployment_id

    def get_deployment(self, deployment_id: str) -> dict[str, object] | None:
        """Read a deployment row by id; return None if absent / deleted."""
        rows = self._query(TABLE_DEPLOYMENT, {"id": deployment_id, "is_deleted": 0})
        return rows[0] if rows else None

    def get_deployment_by_oauth_client_id(
        self, oauth_client_id: str
    ) -> dict[str, object] | None:
        """Lookup for the bridge policy resolver (spec §14.4)."""
        rows = self._query(
            TABLE_DEPLOYMENT,
            {
                "oauth_client_id": oauth_client_id,
                "pairing_status": PairingStatus.PAIRED.value,
                "is_deleted": 0,
            },
        )
        return rows[0] if rows else None

    def set_deployment_pairing_token(
        self,
        *,
        deployment_id: str,
        pairing_token_hash: str,
        pairing_token_salt: str,
        user_code: str,
    ) -> None:
        """Populate pairing_token + user_code on a pending deployment."""
        now = self._clock()
        self._update(
            TABLE_DEPLOYMENT,
            {"id": deployment_id, "is_deleted": 0},
            {
                "pairing_token_hash": pairing_token_hash,
                "pairing_token_salt": pairing_token_salt,
                "user_code": user_code,
                "pairing_initiated_at": now,
                "updated_at": now,
            },
        )

    def transition_deployment_to_approved(
        self, *, deployment_id: str
    ) -> None:
        """Pending → approved (spec §13.3). Clears user_code in same write.

        The ``pairing_status = PENDING`` filter IS the compare-and-set: a row in
        any other state matches 0 rows and the transition is a silent no-op
        (matching the pre-migration ``WHERE … AND pairing_status = %s``).
        """
        now = self._clock()
        self._update(
            TABLE_DEPLOYMENT,
            {
                "id": deployment_id,
                "pairing_status": PairingStatus.PENDING.value,
                "is_deleted": 0,
            },
            {
                "pairing_status": PairingStatus.APPROVED.value,
                "approved_at": now,
                "user_code": None,
                "updated_at": now,
            },
        )

    def transition_deployment_to_paired(
        self,
        *,
        deployment_id: str,
        oauth_client_id: str,
    ) -> None:
        """Approved → paired + bind OAuth client (spec §13.5 step 6).

        ``pairing_status = APPROVED`` filter is the CAS guard (see
        :meth:`transition_deployment_to_approved`).
        """
        now = self._clock()
        self._update(
            TABLE_DEPLOYMENT,
            {
                "id": deployment_id,
                "pairing_status": PairingStatus.APPROVED.value,
                "is_deleted": 0,
            },
            {
                "pairing_status": PairingStatus.PAIRED.value,
                "oauth_client_id": oauth_client_id,
                "paired_at": now,
                "pairing_token_hash": None,
                "pairing_token_salt": None,
                "updated_at": now,
            },
        )

    def transition_deployment_to_revoked(
        self, *, deployment_id: str
    ) -> None:
        """Paired → revoked (spec §13.2 shipper_self_revoke path)."""
        now = self._clock()
        self._update(
            TABLE_DEPLOYMENT,
            {"id": deployment_id, "is_deleted": 0},
            {
                "pairing_status": PairingStatus.REVOKED.value,
                "revoked_at": now,
                "updated_at": now,
            },
        )


__all__ = ["RELOAD_SAFE", "SessionLedgerDeploymentMixin"]
