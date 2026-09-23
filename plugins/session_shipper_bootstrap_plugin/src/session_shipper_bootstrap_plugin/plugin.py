"""Shipper bootstrap plugin — registration shell.

The shipper plugin owns no ``@platform_process`` verbs of its own. The
service-interface surface (``generate_ingest_setup``, ``approve_pairing``,
``shipper_self_revoke``) lives on :class:`SessionLedgerService` (M5
additions per spec §17.5 because ``@service_interface_process`` requires
the declaration to live on ``services/.../interfaces/public.py``).

The HTTP pairing routes live in :mod:`.pairing_routes` and are mounted
on the bridge's FastAPI app via the bridge plugin's one-time §13.6
exception — see ``agent_messaging_plugin._build_fastapi_app``.
"""

from __future__ import annotations

import logging

from ananta.core.plugins.plugin_base import PluginBase

logger = logging.getLogger(__name__)


class SessionShipperBootstrapPlugin(PluginBase):
    """Container for the shipper-bootstrap surface.

    Empty by design: this plugin's only purpose is to be discoverable +
    importable by the bridge plugin's ``_build_fastapi_app`` which
    imports :func:`.pairing_routes.register_session_ledger_pairing_routes`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = "session_shipper_bootstrap_plugin"

    def prepare_for_readiness(self) -> None:
        # No services to start; no dependencies to probe. The pairing
        # routes are mounted by the bridge plugin, not by us.
        self.set_ready()


__all__ = ["SessionShipperBootstrapPlugin"]
