#!/usr/bin/env python3
"""2026-07-26 smoke — audit_retrieval_corpus_cron (B-M6 fast-return fix +
EDGE_SINK cron-target sibling for the previously-dead memory-tag KB-audit
trigger).

Background: the "KB retrieval drift audit" cron dispatched
``service_interface::memory_service::get_memories_by_tag`` (the memory-tag
heartbeat shape) with ``result_processor_kind=None`` by construction —
confirmed against the live schedule row. That shape terminates at
``action_queue_poller``'s EDGE_SINK_SKIP branch: the fetched playbook memory
is written to the completed action's own result row and read by no one. The
fix is the canonical EDGE_SINK terminal-verb shape (mirroring session_ledger
``trigger_poll`` / actr_memory ``*_cron``): the cron now targets
``audit_retrieval_corpus_cron`` directly, a deterministic scheduled job that
does not depend on a model noticing a memory.

Separately, ``audit_retrieval_corpus`` itself runs ~14s/article serially and
would park the action-queue poll loop for several minutes if invoked inline
by a cron (KB `21_scheduling_service/02_action_queue_fast_return_contract.md`
— B-M6). ``audit_retrieval_corpus_cron`` submits the walk to a single-slot
background executor (``BoundedSummaryExecutor``, the same primitive
session-ledger's heartbeat drainers use) and returns a started/already-running
receipt immediately.

This smoke asserts:
  (1) ``audit_retrieval_corpus_cron`` is declared on ``KnowledgeSearchInterface``
      as an @abstractmethod (consumer-facing ABC).
  (2) Both concrete ``KnowledgeSearchInterface`` implementers (``KnowledgeService``,
      ``DefaultKnowledgePlugin``) have zero remaining abstract methods.
  (3) The @service_interface_process declaration on ``KnowledgeSearchAPI``
      declares ``processor_policy_category=EDGE_SINK`` and
      ``is_discoverable=False`` — the canonical cron-target contract.
  (4) Calling ``audit_retrieval_corpus_cron`` returns in well under the
      underlying audit's runtime (fast-return proof) with a "started" receipt.
  (5) A second call while the background pass is still running returns
      "already_running" (singleton-drainer proof) rather than double-running.
  (6) After the background pass drains, a further call returns "started"
      again (the slot is correctly released).

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/audit_retrieval_corpus_cron_smoke.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.interfaces.knowledge_service_interface_search import (  # noqa: E402
    KnowledgeSearchInterface,
)
from ananta.services.knowledge_service import KnowledgeService  # noqa: E402
from ananta.services.knowledge_service.interfaces.search import (  # noqa: E402
    KnowledgeSearchAPI,
)
from default_knowledge_plugin.plugin import DefaultKnowledgePlugin  # noqa: E402

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


def _read_service_interface_metadata(api_class: type, method_name: str) -> dict[str, Any]:
    """Extract the @service_interface_process metadata attached to a method."""
    method = getattr(api_class, method_name, None)
    if method is None:
        return {}
    for attr in (
        "_service_interface_metadata",
        "_action_metadata",
        "_process_metadata",
        "_platform_process_metadata",
    ):
        meta = getattr(method, attr, None)
        if meta is not None:
            return meta if isinstance(meta, dict) else meta.__dict__
    return {}


def test_declared_as_abstractmethod_on_interface() -> None:
    abstracts = KnowledgeSearchInterface.__abstractmethods__
    _check(
        "audit_retrieval_corpus_cron" in abstracts,
        "KnowledgeSearchInterface declares audit_retrieval_corpus_cron as @abstractmethod",
    )


def test_implementers_are_concrete() -> None:
    for cls in (KnowledgeService, DefaultKnowledgePlugin):
        _check(
            not cls.__abstractmethods__,
            (
                f"{cls.__name__} implements all KnowledgeSearchInterface abstracts "
                f"(unimplemented: {sorted(cls.__abstractmethods__)})"
            ),
        )


def test_declared_edge_sink_not_discoverable() -> None:
    meta = _read_service_interface_metadata(KnowledgeSearchAPI, "audit_retrieval_corpus_cron")
    _check(bool(meta), "audit_retrieval_corpus_cron @service_interface_process metadata probe non-empty")
    if not meta:
        return
    _check(
        meta.get("processor_policy_category") == ProcessorPolicyCategory.EDGE_SINK,
        f"processor_policy_category is EDGE_SINK (got {meta.get('processor_policy_category')!r})",
    )
    _check(
        meta.get("is_discoverable") is False,
        f"is_discoverable is False (got {meta.get('is_discoverable')!r})",
    )


def test_fast_return_and_singleton_drain() -> None:
    plugin = DefaultKnowledgePlugin()

    release = threading.Event()
    calls: list[int] = []
    lock = threading.Lock()

    def fake_audit() -> dict[str, object]:
        with lock:
            calls.append(1)
        release.wait(timeout=5)
        return {"status": "success", "data": {}}

    plugin.audit_retrieval_corpus = fake_audit  # type: ignore[method-assign]

    started_at = time.monotonic()
    first = plugin.audit_retrieval_corpus_cron()
    elapsed = time.monotonic() - started_at
    _check(elapsed < 0.5, f"first call returns fast, well under the audit's runtime ({elapsed:.3f}s)")
    _check(
        first.get("data", {}).get("audit") == "started",
        f"first call returns 'started' (got {first!r})",
    )

    # Give the background thread a moment to actually enter fake_audit.
    for _ in range(50):
        with lock:
            if calls:
                break
        time.sleep(0.01)

    second = plugin.audit_retrieval_corpus_cron()
    _check(
        second.get("data", {}).get("audit") == "already_running",
        f"overlapping call while the pass is in flight returns 'already_running' (got {second!r})",
    )

    release.set()
    for _ in range(200):
        with lock:
            if len(calls) >= 1:
                break
        time.sleep(0.01)
    time.sleep(0.05)  # let the executor release its slot after work() returns

    def fake_audit_2() -> dict[str, object]:
        with lock:
            calls.append(2)
        return {"status": "success", "data": {}}

    plugin.audit_retrieval_corpus = fake_audit_2  # type: ignore[method-assign]
    time.sleep(0.1)
    third = plugin.audit_retrieval_corpus_cron()
    _check(
        third.get("data", {}).get("audit") == "started",
        f"a further call after the slot drains returns 'started' again (got {third!r})",
    )
    time.sleep(0.1)
    with lock:
        final_calls = list(calls)
    _check(
        final_calls == [1, 2],
        f"the background work ran exactly twice, never doubled up (got {final_calls})",
    )


def main() -> int:
    print("=== audit_retrieval_corpus_cron_smoke (B-M6 fast-return + EDGE_SINK cron sibling) ===")
    test_declared_as_abstractmethod_on_interface()
    test_implementers_are_concrete()
    test_declared_edge_sink_not_discoverable()
    test_fast_return_and_singleton_drain()

    total = _passed + len(_failed)
    print(f"\n{_passed}/{total} passed")
    if _failed:
        print("FAILURES:")
        for f in _failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
