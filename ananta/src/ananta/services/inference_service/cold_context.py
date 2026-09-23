"""Cold-context assembly for the ``sys:autonomic`` vertex forward (INF-01 §D.4/B.2).

The vertex path short-circuits ABOVE ``_execute_transaction``, so a forward
normally carries only the flow's RAW params/state — safe when originator ==
holder (tagged flows), COLD for the non-originating ``sys:autonomic`` holder.
This module runs the platform-owned PromptPipeline STANDALONE
(``assemble_prompt`` + the service's own pipeline factory — no plugin
involvement) so the forward can carry the organism's assembled view
(history/memories/system) the holder cannot otherwise reconstruct.

Strictly best-effort by contract: ANY fault degrades LOUD to ``None`` — the
forward still happens with the raw flow refs (``flow_id``/``session_id``),
which the holder can pull context with; a cold forward must never lose the
turn. Assembly imports stay function-local to keep this module a safe leaf
for the package ``__init__`` to import at module load.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# The state key the assembled context rides under in the vertex forward. The
# forwarder (``SessionInferenceProvider._emit_bridge_event``) serializes
# ``state`` wholesale, so enriching it reaches the holder with no wire change.
AUTONOMIC_ASSEMBLED_CONTEXT_KEY = "autonomic_assembled_context"


def assemble_cold_context(
    ensure_factory: Callable[[], Any],
    *,
    params: dict[str, Any],
    state: dict[str, Any],
    orchestrator: object,
) -> dict[str, Any] | None:
    """Best-effort standalone assembly for the autonomic forward.

    ``ensure_factory`` resolves (building if needed) the service's
    ``PromptPipelineFactory``; ``context_id`` stays ``None`` in v1 (contract-
    valid per ``PromptAssemblyRequest`` — assembly fidelity against the
    plugin-resolved context is a live-path verification item).

    ``orchestrator`` resolves ``io_namespace`` from the flow's
    ``source_namespace`` (INF-05: the standalone pipeline run needs this
    resolved the same way ``inference_transaction.execute`` resolves it for
    the default path — decode-contract's ``<origin_io>``/pseudo-key
    resolution raises when it is ``None`` on any WBS step that references it,
    which this module's own broad except previously swallowed silently).
    """
    try:
        factory = ensure_factory()
        from ananta.core.prompts.profiles import INFERENCE_PROFILE
        from ananta.services.inference_service.assembly import assemble_prompt
        from ananta.services.inference_service.assembly_types import (
            PromptAssemblyRequest,
        )
        from ananta.services.inference_service.inference_transaction import (
            _resolve_io_process_key,
        )
        from ananta.services.inference_service.transaction import (
            _resolve_io_namespace,
        )

        flow_id = state.get("flow_id")
        session_id = state.get("session_id")
        action_name = state.get("action_name")
        io_process_key = _resolve_io_process_key(orchestrator, state)
        io_namespace = _resolve_io_namespace({**state, "io_process_key": io_process_key})
        result = assemble_prompt(
            PromptAssemblyRequest(
                profile_name="inference",
                flow_id=flow_id if isinstance(flow_id, str) else "",
                action_name=action_name if isinstance(action_name, str) else "",
                session_id=session_id if isinstance(session_id, str) else "",
                raw_action_params=params,
                io_namespace=io_namespace,
            ),
            INFERENCE_PROFILE,
            factory,
        )
    except Exception:  # noqa: BLE001 — cold-context is additive; the forward itself is the guarantee
        logger.warning(
            "sys:autonomic cold-context assembly failed — forwarding raw "
            "flow refs only (the holder pulls context itself)",
            exc_info=True,
        )
        return None
    return {
        "messages": [dict(message) for message in result.messages],
        "output_schema": result.output_schema,
        "profile_name": result.profile_name,
    }


__all__ = [
    "AUTONOMIC_ASSEMBLED_CONTEXT_KEY",
    "assemble_cold_context",
]
