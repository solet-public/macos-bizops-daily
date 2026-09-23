#!/usr/bin/env python3
"""TRIPWIRE: ``ingest_raw_chunk`` MUST stay a synchronous service-interface EDGE process.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/ingest_raw_chunk_sync_dispatch_tripwire_smoke.py

WHY THIS EXISTS (Architect ruling 2026-06-21, SQL-lockdown Slice 5). The
``upsert_session`` UPDATE branch was migrated off the atomic SQL
``first=LEAST(first,%s)`` / ``last=GREATEST(last,%s)`` / 7×``COALESCE`` bound-merge
onto a Python read-compute-write (``_update_existing_session``). That read-then-
write is NON-COMMUTATIVE — two writers racing the same session's row would lose
an update (wrong first/last_event_at bounds AND wrong COALESCE-keep snapshot
columns: the 4 originator/recipient cols + summary_text). The Architect ruled
it SAFE in production NOT because of any lease on the push path, but because the
platform's action queue dispatches SERIALLY:

  * ``ActionQueuePoller`` runs ONE ``_poll_loop``;
  * ``_poll_once`` drains claimed actions in a serial
    ``for action: await self._process_action(action)`` loop;
  * ``_process_action`` awaits ``run_in_executor(execute_action)`` to COMPLETION
    before advancing;
  * the push entrypoint ``service_interface::session_ledger_service::ingest_raw_chunk``
    is a SYNCHRONOUS (``is_async=False``) ``@service_interface_process`` EDGE
    terminal — the action queue reads ``is_async`` from the blueprint
    (``_read_async_flag_from_registry``) and, being False, runs the whole
    ``dispatch_pushed`` → ``upsert_session`` persist to completion inside that one
    awaited call. So N concurrent shippers enqueue + drain ONE-AT-A-TIME; two
    push-path ``upsert_session`` writes to one session never overlap.

LOAD-BEARING: that safety holds ONLY while ``ingest_raw_chunk`` stays a
synchronous service-interface process. If it is ever made ``is_async`` /
self-completing (e.g. converted to an async plugin process, or the
``@service_interface_process`` decorator gains an ``is_async`` knob that gets
flipped, or the service-interface scanner stops hard-coding ``is_async=False``),
the serial fence breaks and the read-compute-write race returns SILENTLY. This
tripwire turns that emergent property into a GUARDED invariant: it fails loudly
the moment any of those structural facts changes.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import (  # noqa: E402
    InvocationSchemaGenerator,
)
from ananta.core.services.service_interface_decorator import (  # noqa: E402
    ServiceInterfaceActionMetadata,
    service_interface_process,
)
from ananta.services.session_ledger_service.interfaces.public import (  # noqa: E402
    SessionLedgerIngestAPI,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def main() -> int:
    print("ingest_raw_chunk synchronous-dispatch tripwire")
    print("==============================================")

    method = getattr(SessionLedgerIngestAPI, "ingest_raw_chunk", None)
    _check(method is not None, "SessionLedgerIngestAPI declares ingest_raw_chunk")
    if method is None:
        return 1

    # (1) It is a service-interface process — service-interface processes are
    # dispatched synchronously (the SI scanner hard-codes is_async=False; there
    # is no other dispatch path for them). Converting it to a plugin/async
    # process would drop this marker and trip the wire.
    raw_metadata = getattr(method, "_service_interface_metadata", None)
    _check(
        raw_metadata is not None,
        "ingest_raw_chunk carries @service_interface_process metadata "
        "(it is a service-interface process → synchronous dispatch)",
    )
    if raw_metadata is None:
        return 1
    metadata = cast(ServiceInterfaceActionMetadata, raw_metadata)

    # (2) It is an EDGE terminal (synchronous, self-completing-in-executor) —
    # not a GENERATE/inference (async) process.
    _check(
        getattr(metadata, "processor_policy_category", None) is ProcessorPolicyCategory.EDGE,
        "ingest_raw_chunk is an EDGE process (synchronous terminal, not async inference)",
    )

    # (3) The @service_interface_process decorator exposes NO is_async parameter,
    # so service-interface processes structurally cannot be individually marked
    # async. If this changes, the load-bearing assumption needs re-verification.
    decorator_params = set(inspect.signature(service_interface_process).parameters)
    _check(
        "is_async" not in decorator_params,
        "@service_interface_process has no is_async parameter "
        "(SI processes cannot be flagged async at the decorator)",
    )

    # (4) THE PRODUCTION READ PATH (Architect 2026-06-21 — assert the field the
    # poller ACTUALLY reads, not a correlated proxy). The action queue's
    # ``_read_async_flag_from_registry`` (action_queue_poller.py:817-821) reads
    # ``action_blueprint["metadata"]["is_async"]`` — and for an SI process the
    # ``action_blueprint`` is what ``generate_action_blueprint_from_metadata``
    # produces, whose ``metadata`` dict is ``{is_inference_capable,
    # estimated_duration, version}`` with NO ``is_async`` key. So the poller does
    # ``bool(None)`` → False: synchronous dispatch is guaranteed by the ABSENCE
    # of the key. (The top-level ``is_async`` the SI scanner hard-codes at
    # process-entry level is a DIFFERENT registry field the poller never reads —
    # asserting it would be a false guard.) Mirror the poller's exact read here.
    process_key = f"service_interface::{metadata.provider}::{metadata.name}"
    action_blueprint = InvocationSchemaGenerator().generate_action_blueprint_from_metadata(
        process_key, metadata
    )
    ab_meta = action_blueprint.get("metadata")
    poller_is_async = bool(ab_meta.get("is_async")) if isinstance(ab_meta, dict) else False
    _check(
        process_key == "service_interface::session_ledger_service::ingest_raw_chunk",
        f"process_key resolves canonically (got {process_key!r})",
    )
    _check(
        poller_is_async is False,
        "the action_blueprint the poller ACTUALLY reads yields is_async=False "
        "(bool(action_blueprint['metadata'].get('is_async')) — the exact "
        "_read_async_flag_from_registry computation)",
    )
    # (5) Belt-and-suspenders: the key is ABSENT, not present-and-falsy. If a
    # future change ADDS ``is_async`` to the action_blueprint metadata (even
    # ``=False``), trip so the fence assumption gets re-verified at that edit.
    _check(
        isinstance(ab_meta, dict) and "is_async" not in ab_meta,
        "is_async key is ABSENT from the action_blueprint metadata "
        "(the fence is absence-of-key; adding it — even =False — should prompt "
        "a re-verify of the synchronous-dispatch invariant)",
    )

    print("\n----------------------------------------------")
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
