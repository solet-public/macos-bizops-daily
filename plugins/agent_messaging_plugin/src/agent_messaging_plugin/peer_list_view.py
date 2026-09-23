"""Single source for the wire shape of a ``peer_list`` snapshot.

Three surfaces render the SAME ``PeerRegistry.list_agent_ids()`` snapshot for
a caller:

- the localhost bridge route ``GET .../peer/list`` (``http_routes``);
- the Streamable-HTTP MCP tool dispatch (``mcp_streamable.dispatch``);
- the ``plugin::agent_messaging_plugin::peer_list`` platform process
  (``plugin.peer_list``) — the no-MCP enumeration path (WS-1a's peer_inbox
  pattern, generalized to the peer-enumeration asymmetry it left open:
  a no-MCP session could read its own mail but could not see who else was
  live).

Before this module the shape was copied per surface (byte-identical dict
comprehensions in ``http_routes.py`` and ``mcp_streamable/dispatch.py``). A
copied shape is a shape that drifts; see ``peer_inbox_view``'s docstring for
the class of bug that motivates the dedup.

Field-set decision, made explicitly rather than inherited silently: a
``BridgeBinding`` row carries eight fields (``bridge_id``, ``agent_id``,
``agent_instance_id``, ``session_label``, ``parent_pid``, ``created_at``,
``updated_at``, ``agent_session_id``). Both pre-existing MCP surfaces emit
only SIX of them, grouped under a top-level ``agent_id`` key (so
``agent_id`` itself is not repeated per-row): ``agent_instance_id``,
``session_label``, ``parent_pid``, ``registered_at`` (deprecated alias for
``created_at``, kept one release), ``created_at``, ``updated_at``.
``bridge_id`` and ``agent_session_id`` are NEVER emitted by either existing
surface — they are routing/identity fields one peer could use to address or
impersonate another's transport, and localhost is the trust boundary for
"can enumerate peers at all," not for "can route to a specific bridge." This
module preserves that six-field subset for the new no-MCP process rather
than widening it: the CLI path gets parity with the existing MCP surfaces,
not a wider exposure than either has ever had.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import BridgeBinding


def serialize_peer_list(snapshot: dict[str, list[BridgeBinding]]) -> dict[str, Any]:
    """Render a ``PeerRegistry.list_agent_ids()`` snapshot to JSON-safe primitives.

    ``agent_ids`` is the sorted key list; ``instances`` groups the six-field
    subset (see module docstring) per ``agent_id``, in the snapshot's
    per-agent-id row order (unfiltered, global — no per-caller scoping).
    """
    instances: dict[str, list[dict[str, object]]] = {
        agent_id: [
            {
                "agent_instance_id": b.agent_instance_id,
                "session_label": b.session_label,
                "parent_pid": b.parent_pid,
                "registered_at": b.created_at,
                "created_at": b.created_at,
                "updated_at": b.updated_at,
            }
            for b in bindings
        ]
        for agent_id, bindings in snapshot.items()
    }
    return {
        "agent_ids": sorted(snapshot.keys()),
        "instances": instances,
    }
