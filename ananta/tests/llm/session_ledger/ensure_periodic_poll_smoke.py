#!/usr/bin/env python3
"""M5.C deferral #4 smoke — SessionLedgerService.ensure_periodic_poll_schedule.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/ensure_periodic_poll_smoke.py

Contract (mirrors ensure_global_heartbeat in shape):

1. With ``scheduling_service`` unbound, the verb raises ``RuntimeError``
   (fail-fast — the profile bound no scheduler, but the starting_action
   still tried to run the verb).
2. With ``scheduling_service`` bound, the verb:
   a. Calls ``clear_scheduled_actions_by_tag(tag='ledger:periodic_poll')``.
   b. Then ``create_cron_schedule`` with a cron derived from cadence_minutes,
      an action invoking
      ``service_interface::session_ledger_service::trigger_poll``, and the
      same tag.
   c. Returns ``status='created'`` on first run (no clear hit),
      ``status='normalized'`` when clear returned a non-zero cleared_count.
3. ``cadence_minutes`` validation rejects 0 and 60+ with ``ValueError``.
4. The action shape is exactly the trigger_poll envelope (no extras —
   we don't want to invent surface).
5. The cron expression is the ``"*/N * * * *"`` form for cadence N.
6. The starting_action's ``process_key`` matches the verb so a profile
   boot trivially wires this up.

The smoke stubs SessionLedgerService directly (no startup_sequence,
no real schedule plugin) — the verb under test is pure dispatch into
the scheduling_service handle. The bound trigger_poll method itself
isn't invoked here; the cron-firing path is exercised by an integration
smoke once a real scheduling plugin is in the loop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: E402

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


# ─── Fixture scaffolding ────────────────────────────────────────────────────


class _RecordingSchedulingService:
    """Captures every clear + create call so we can assert the dispatch shape."""

    def __init__(self, *, cleared_count: int = 0) -> None:
        self._cleared_count = cleared_count
        self.clear_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def clear_scheduled_actions_by_tag(self, tag: str) -> dict[str, Any]:
        self.clear_calls.append({"tag": tag})
        return {"data": {"cleared_count": self._cleared_count, "tag": tag}}

    def create_cron_schedule(
        self,
        *,
        cron_expression: str,
        actions: list[dict[str, Any]] | None = None,
        label: str | None = None,
        tags: list[str] | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append({
            "cron_expression": cron_expression,
            "actions": actions,
            "label": label,
            "tags": tags,
            "state": state,
        })
        return {"data": {"schedule_id": "sched-fixture-001"}}


def _make_service(
    *,
    scheduling_service: Any | None = None,
    state_service: StubStateService | None = None,
) -> SessionLedgerService:
    """Construct a minimal SessionLedgerService stand-in.

    SessionLedgerService.__init__ would normally wire state/blob/registry
    via SessionLedgerRepository (which calls state_service.execute_sql),
    SessionLedgerBlobAdapter (blob_storage_service), etc. The ensure_*
    paths exercise scheduling_service + state_service.upsert_state (for
    the template_flow record pre-seed); the rest of the construction is
    bypassed by allocating slot fields directly via __new__.
    """
    instance = SessionLedgerService.__new__(SessionLedgerService)
    instance._registry = None  # type: ignore[assignment]
    instance._repository = None  # type: ignore[assignment]
    instance._secret_gate = None  # type: ignore[assignment]
    instance._blob_adapter = None  # type: ignore[assignment]
    instance._importer = None  # type: ignore[assignment]
    instance._summary_writer = None  # type: ignore[assignment]
    instance._operator_equivalent_check = None
    instance._scheduling_service = scheduling_service
    instance._state_service = state_service or StubStateService()  # type: ignore[assignment]
    return instance


# ─── Cases ──────────────────────────────────────────────────────────────────


def test_raises_when_scheduling_unbound() -> None:
    """Contract #1: no scheduling_service → RuntimeError, not silent no-op."""
    service = _make_service(scheduling_service=None)
    raised: RuntimeError | None = None
    try:
        service.ensure_periodic_poll_schedule()
    except RuntimeError as exc:
        raised = exc
    _check(
        raised is not None,
        "ensure_periodic_poll_schedule raises RuntimeError when scheduling_service is None",
    )


def test_creates_schedule_on_first_run() -> None:
    """Contract #2c: clear returns 0 → outcome='created'."""
    scheduler = _RecordingSchedulingService(cleared_count=0)
    service = _make_service(scheduling_service=scheduler)
    result = service.ensure_periodic_poll_schedule()

    _check(result["outcome"] == "created", f"outcome='created' (got {result['outcome']!r})")
    _check(result["tag"] == "ledger:periodic_poll", f"tag default (got {result['tag']!r})")
    _check(result["cadence_minutes"] == 5, f"cadence default = 5 (got {result['cadence_minutes']})")
    _check(result["cleared_count"] == 0, "cleared_count == 0 on first run")
    _check(
        result["schedule_id"] == "sched-fixture-001",
        f"schedule_id surfaced from create envelope (got {result['schedule_id']!r})",
    )

    # Clear was called with the right tag
    _check(
        scheduler.clear_calls == [{"tag": "ledger:periodic_poll"}],
        f"clear called exactly once with default tag (got {scheduler.clear_calls})",
    )

    # Create was called with the right action shape + cron + tag
    _check(
        len(scheduler.create_calls) == 1,
        f"create called exactly once (got {len(scheduler.create_calls)})",
    )
    if scheduler.create_calls:
        call = scheduler.create_calls[0]
        _check(
            call["cron_expression"] == "*/5 * * * *",
            f"cron is */5 form for default 5-minute cadence (got {call['cron_expression']!r})",
        )
        _check(
            call["tags"] == ["ledger:periodic_poll"],
            f"create tags carry only ledger:periodic_poll (got {call['tags']})",
        )
        expected_action = {
            "process_key": "service_interface::session_ledger_service::trigger_poll",
            "arguments": {},
        }
        _check(
            call["actions"] == [expected_action],
            f"create action invokes trigger_poll with empty args, no result_processor_kind "
            f"(EDGE_SINK terminal/headless per P1-A 2026-06-16; got {call['actions']})",
        )


def test_normalizes_when_clear_returns_nonzero() -> None:
    """Contract #2c: clear returned >0 → outcome='normalized'."""
    scheduler = _RecordingSchedulingService(cleared_count=3)
    service = _make_service(scheduling_service=scheduler)
    result = service.ensure_periodic_poll_schedule()
    _check(result["outcome"] == "normalized", f"outcome='normalized' after clear>0 (got {result['outcome']!r})")
    _check(result["cleared_count"] == 3, f"cleared_count surfaced (got {result['cleared_count']})")


def test_custom_cadence_and_tag() -> None:
    """Custom cadence (3) and tag are honored; cron is */3."""
    scheduler = _RecordingSchedulingService(cleared_count=0)
    service = _make_service(scheduling_service=scheduler)
    result = service.ensure_periodic_poll_schedule(
        cadence_minutes=3, tag="ledger:periodic_poll_custom",
    )
    _check(
        scheduler.create_calls and scheduler.create_calls[0]["cron_expression"] == "*/3 * * * *",
        "custom 3-minute cadence renders as */3 cron",
    )
    _check(
        result["tag"] == "ledger:periodic_poll_custom",
        f"custom tag surfaced (got {result['tag']!r})",
    )


def test_cadence_validation_rejects_zero() -> None:
    """Contract #3: cadence_minutes=0 → ValueError."""
    scheduler = _RecordingSchedulingService()
    service = _make_service(scheduling_service=scheduler)
    raised: ValueError | None = None
    try:
        service.ensure_periodic_poll_schedule(cadence_minutes=0)
    except ValueError as exc:
        raised = exc
    _check(raised is not None, "cadence_minutes=0 raises ValueError")
    _check(scheduler.create_calls == [], "no create issued when validation fails")


def test_cadence_validation_rejects_too_high() -> None:
    """Contract #3: cadence_minutes>=60 → ValueError (cron */N requires N<60)."""
    scheduler = _RecordingSchedulingService()
    service = _make_service(scheduling_service=scheduler)
    raised: ValueError | None = None
    try:
        service.ensure_periodic_poll_schedule(cadence_minutes=60)
    except ValueError as exc:
        raised = exc
    _check(raised is not None, "cadence_minutes=60 raises ValueError")


def test_starting_actions_process_key_matches() -> None:
    """Contract #6: the starting_actions entry in local.yaml + cloud.yaml
    references this exact process_key."""
    import yaml  # noqa: PLC0415
    process_key = "service_interface::session_ledger_service::ensure_periodic_poll_schedule"
    # ORIGIN-ONLY LEG. `initialization/` is absent from the seed manifest's `copy:`
    # allowlist entirely, so these profiles exist only in this checkout: the
    # assertion is about the ORIGIN's boot wiring, not about a born solet, which
    # gets its profile from the midwife's own templates. Every other assertion in
    # this smoke is hermetic and ships. Measured on the r21 born-clone verdict
    # (sealed 254700866ab0…): this smoke was 1 of 14 BLOCKING, raising
    # FileNotFoundError here after its earlier checks had all printed PASS.
    profiles_root = REPO_ROOT / "initialization" / "profiles"
    if not profiles_root.is_dir():
        print("  SKIP  starting_actions profile wiring: initialization/profiles is origin-only (not shipped)")
        return
    for profile_name in ("local.yaml", "cloud.yaml"):
        text = (profiles_root / profile_name).read_text()
        raw = yaml.safe_load(text)
        keys = {entry["process_key"] for entry in raw.get("starting_actions", [])}
        _check(
            process_key in keys,
            f"{profile_name} starting_actions includes {process_key}",
        )


def test_trigger_poll_cron_action_is_terminal_no_result_processor_kind() -> None:
    """Inverted from the 2026-06-06 RESULT_CONTRACT_VIOLATION regression guard.

    P1-A 2026-06-16: ``trigger_poll`` is now ``EDGE_SINK`` (terminal),
    not ``EDGE``. The cron action MUST NOT carry ``result_processor_kind``
    so ``action_queue_poller._dispatch_*`` short-circuits via the
    EDGE_SINK_SKIP branch
    (``result_processor_kind is None and result_processor is None`` →
    terminal action, no dispatch). The pre-P1-A
    ``RESULT_CONTRACT_VIOLATION (result_processor_kind_missing)`` does
    NOT fire here because that check runs inside the success-path
    dispatch which EDGE_SINK actions skip entirely. Reintroducing
    ``result_processor_kind`` would re-trigger the
    ``Empty source_namespace in flow trigger_data`` bug 78x/10min.
    """
    scheduler = _RecordingSchedulingService(cleared_count=0)
    service = _make_service(scheduling_service=scheduler)
    service.ensure_periodic_poll_schedule()

    _check(len(scheduler.create_calls) == 1, "exactly one create call recorded")
    if not scheduler.create_calls:
        return
    actions = scheduler.create_calls[0]["actions"]
    _check(isinstance(actions, list) and len(actions) == 1, "exactly one action in the cron payload")
    if not (isinstance(actions, list) and actions):
        return
    action = actions[0]
    _check(
        "result_processor_kind" not in action,
        "cron action does NOT declare result_processor_kind (EDGE_SINK terminal/headless per P1-A 2026-06-16)",
    )
    _check(
        action.get("process_key")
        == "service_interface::session_ledger_service::trigger_poll",
        "poll cron targets trigger_poll",
    )
    _check(
        action.get("arguments") == {},
        "poll cron action carries empty arguments dict",
    )


def test_summarize_cron_action_is_terminal_no_result_processor_kind() -> None:
    """Sibling terminal-shape regression guard for the auto-summarize cron.

    Same EDGE_SINK contract as ``trigger_poll`` — see the docstring on
    ``test_trigger_poll_cron_action_is_terminal_no_result_processor_kind``
    for the full rationale. Reintroducing ``result_processor_kind`` on
    either cron would re-trigger the bug class P1-A closed.
    """
    scheduler = _RecordingSchedulingService(cleared_count=0)
    service = _make_service(scheduling_service=scheduler)
    service.ensure_periodic_summarize_schedule()

    _check(len(scheduler.create_calls) == 1, "exactly one create call for summarize cron")
    if not scheduler.create_calls:
        return
    actions = scheduler.create_calls[0]["actions"]
    _check(isinstance(actions, list) and len(actions) == 1, "exactly one summarize action")
    if not (isinstance(actions, list) and actions):
        return
    action = actions[0]
    _check(
        action.get("process_key")
        == "service_interface::session_ledger_service::summarize_quiescent_sessions",
        "summarize cron targets summarize_quiescent_sessions",
    )
    _check(
        "result_processor_kind" not in action,
        "summarize cron action does NOT declare result_processor_kind (EDGE_SINK terminal/headless)",
    )
    _check(
        action.get("arguments") == {},
        "summarize cron action carries empty arguments dict",
    )


def test_kb_stub_present() -> None:
    """The service-process KB JSON stub exists at the expected path."""
    stub_path = (
        REPO_ROOT
        / "ananta"
        / "knowledge_base"
        / "processes"
        / "session_ledger_service"
        / "ensure_periodic_poll_schedule.json"
    )
    _check(stub_path.is_file(), f"KB stub exists at {stub_path.relative_to(REPO_ROOT)}")
    if stub_path.is_file():
        import json  # noqa: PLC0415
        body = json.loads(stub_path.read_text())
        _check(
            body.get("process_key")
            == "service_interface::session_ledger_service::ensure_periodic_poll_schedule",
            "KB stub process_key matches",
        )


def main() -> int:
    print("=== ensure_periodic_poll_smoke (M5.C deferral #4) ===")
    test_raises_when_scheduling_unbound()
    test_creates_schedule_on_first_run()
    test_normalizes_when_clear_returns_nonzero()
    test_custom_cadence_and_tag()
    test_cadence_validation_rejects_zero()
    test_cadence_validation_rejects_too_high()
    test_starting_actions_process_key_matches()
    test_trigger_poll_cron_action_is_terminal_no_result_processor_kind()
    test_summarize_cron_action_is_terminal_no_result_processor_kind()
    test_kb_stub_present()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
