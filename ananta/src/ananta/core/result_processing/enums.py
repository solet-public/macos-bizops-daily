"""Result-processor and error-processor kind enums.

These enums classify how the platform handles a completed action's
result or error.  They are action-level — not process-registry-level —
so the same process can route differently depending on how it was
invoked (plan step vs direct MCP ``process_call``).

ResultProcessorKind selects how a *successful*, contract-valid tool
result is processed:

- ``INFERENCE``: route the result through ``process_results`` for
  VERTEX inference (search interpretation, prose composition,
  planning-extension, branch selection, model-authored work).
- ``DETERMINISTIC_CONTINUATION``: no result-processing inference.
  The platform validates the Joseki/WBS result contract, advances
  the plan, constructs the next planned action in code, and submits
  it.  Valid only on executable Joseki/WBS plan steps; rejected on
  any other invocation surface.
- ``BRIDGE_DELIVERY``: no result-processing inference.  The platform
  validates a minimal bridge-delivery contract and submits an
  EDGE_SINK ``deliver_result`` action against the originating bridge
  plugin so the raw structured payload is delivered to the caller.
  Platform-set on direct MCP ``process_call`` invocations only;
  rejected if it appears in WBS/Joseki authored plan text.

ErrorProcessorKind selects how a *failed* action (execution failure
or successful tool result that violates the result contract) is
handled:

- ``INFERENCE``: route through ``process_error`` inference so the
  model can interpret and recover.  Default for plan-derived actions
  and the historical default for every direct invocation.
- ``BRIDGE_DELIVERY``: no error-processing inference.  The platform
  validates the same bridge-delivery contract and submits an
  EDGE_SINK ``deliver_error`` action against the originator so the
  structured failure payload is delivered to the caller.
  Platform-set on direct MCP ``process_call`` invocations only.

Both kinds are stored on ``core__action_events`` and consulted by the
result-processing coordinator (success path) and error dispatcher
(failure path).  Plan-derived actions must never persist
``BRIDGE_DELIVERY`` for either kind — bridge delivery is platform-set
on direct invocations only.
"""

from __future__ import annotations

from enum import StrEnum


class ResultProcessorKind(StrEnum):
    """How a successful tool result should be processed."""

    INFERENCE = "inference"
    DETERMINISTIC_CONTINUATION = "deterministic_continuation"
    BRIDGE_DELIVERY = "bridge_delivery"


class ErrorProcessorKind(StrEnum):
    """How a failed action (or a result-contract violation) is handled."""

    INFERENCE = "inference"
    BRIDGE_DELIVERY = "bridge_delivery"
