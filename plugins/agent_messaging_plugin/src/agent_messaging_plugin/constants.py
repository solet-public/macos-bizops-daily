"""Stable identifiers for ``agent_messaging_plugin``."""

from typing import Final

PLUGIN_NAME: str = "agent_messaging_plugin"

# The sender identity the platform itself uses for role-handover notices.
# Lives here rather than in ``plugin`` so ``role_claim`` — shared by the
# ``peer_claim_role`` verb and the ``peer/claim_role`` bridge route — can reach
# them without importing ``plugin`` (which imports ``http_routes``, so the
# reverse direction would be an import cycle).
SYSTEM_AGENT_ID: Final[str] = "system"
SYSTEM_ROLE_HANDOVER_ID: Final[str] = "system:role-handover"
SYSTEM_ROLE_HANDOVER_LABEL: Final[str] = "System (role handover)"

# GAU-06 (G2), 2026-08-19. The sender identity for a rotation SELF-notice's
# durable half. Its OWN sentinel rather than SYSTEM_SCHEDULER_ID, for the
# reason ``SYSTEM_JOB_COMPLETION_ID`` already records in ``plugin``: peer
# threads are keyed on ``(sender_bridge_id, peer_instance)``, so a distinct
# sentinel gives these notices their own thread per recipient instead of
# interleaving a machine-generated context measurement with the scheduler's
# coordination traffic. That separation IS the read-side half of GAU-06's
# noise ruling -- a drain of coordination mail can exclude one thread, which
# is cheaper and more honest than teaching every reader to filter by content.
#
# It is also a provenance claim that is TRUE: a self-notice is not
# scheduler-originated work being relayed, it is the platform telling a
# session about itself.
SYSTEM_ROTATION_NOTICE_ID: Final[str] = "system:rotation-notice"
SYSTEM_ROTATION_NOTICE_LABEL: Final[str] = "System (rotation notice)"

# The synthetic identity ``PermissiveBearerVerifier`` stamps on every caller
# when ``streamable_no_auth=True`` — authenticated by NO bearer, so both the
# ``agent_id`` and ``agent_instance_id`` carry this sentinel. Lives here (the
# leaf constants module) rather than in ``mcp_streamable.auth`` so the auth,
# dispatch, and platform-surface layers can all import it without a package
# import cycle. Keyed off by the B1 Slice-A untrusted-principal gate.
TUNNEL_PASSTHROUGH_SENTINEL: Final[str] = "tunnel_passthrough"
