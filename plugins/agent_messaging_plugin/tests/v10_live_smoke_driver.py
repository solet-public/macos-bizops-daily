#!/usr/bin/env python3
"""v10 guaranteed role-addressed delivery — LIVE-SMOKE DRIVER (T0/T1 tier).

Companion to ``workbench/2026-06-19_v10_live_smoke_runbook.md`` §3-§4. This
encodes the T0 + T1 acceptance cases as an **executable, self-validating,
deterministic choreography** the agent-driver runs against the LIVE green
solet through the MCP bridge.

WHY THIS IS A CHOREOGRAPHY HARNESS, NOT A STANDALONE REAL-CALL SCRIPT
--------------------------------------------------------------------
A standalone ``.py`` process **cannot** make operator-compliant real calls
against green. Every transport is blocked, and the block is structural:

* **MCP bridge (process_call / peer_send / peer_inbox):** the HTTP peer/process
  routes require a pre-existing ``bridge_id``, and a bridge is created only by
  the MCP transport handshake (``bridge_manager.open``) — never by an HTTP POST.
  Streamable MCP is gated OFF by default (``streamable_enabled=False``). So a
  bridge handle exists only inside a live MCP session — i.e. an **agent**.
* **In-process state interface:** ``PostgresProvider.initialize()`` issues DDL
  (``_create_schema`` + ``_create_trigger_function``) on connect, and a
  hand-built psycopg pool is direct DB access — BOTH forbidden by the operator's
  "no direct SQL/DDL, state-interface only" rule. The clean state surface lives
  on the framework-injected plugin, not constructible standalone.

Therefore the **driver is a live agent session** (this matches the runbook's
own "driver = a live agent session via process_call/peer_send/peer_inbox"
line). The agent runs each :class:`McpCall` below through its MCP tools and
applies the :class:`Assertion` predicate to the JSON result it gets back. The
predicates are real coded pass/fail checks over the documented state-result
shapes — NOT eyeballing, NOT fakes.

WHAT RUNNING THIS FILE DOES (now, before green is up)
-----------------------------------------------------
Validates its own structure: every case is sentinel-scoped, every MCP payload
is well-formed, every assertion predicate is callable. Then it EMITS the
ordered choreography (the exact MCP calls + the assertion after each + the
cleanup steps) the agent executes at cutover. It makes no live call itself, so
there is nothing to "skip" — it is the deterministic, self-checked agent script.

SENTINEL SAFETY (hard invariant)
--------------------------------
Green at cutover holds every real peer's backfilled bindings. Every role this
harness touches is prefixed ``__v10_smoke__`` and never collides with a real
peer name; every writing case ends with a ``peer_release_role`` cleanup step.
``validate_cases`` FAILS the build if any case references a non-sentinel role.

Run::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/v10_live_smoke_driver.py

Exits 0 when the harness self-validates + emits; 1 on any structural defect.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# Plugin src trees are editable-installed but importing the agent_messaging
# package runs a module-load-time scoped-vault-name resolution that needs
# SOLET_NAME (the canonical launch env in both root bootstraps) — no
# default; raises if unset.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _src_rel in (
    "plugins/agent_messaging_plugin/src",
    "plugins/postgres_state_management_plugin/src",
):
    _src = str(_PROJECT_ROOT / _src_rel)
    if _src not in sys.path:
        sys.path.insert(0, _src)

from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    UNCLAIMED_SESSION_ID,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_ROLE,
    TABLE_AGENT_ROLE_BINDING,
)

# ---------------------------------------------------------------------------
# Constants — production process keys + the sentinel-role discipline.
# ---------------------------------------------------------------------------

SENTINEL_PREFIX = "__v10_smoke__"

# A bogus, never-live agent_instance_id used to seed a sentinel binding whose
# holder is OFFLINE. A send then RESOLVES the binding (not vacant — a send to a
# truly unbound name is rejected before any persist, plugin.py Scope-decision C)
# yet finds no live wake target → persist-first queues it for replay. This is
# how T1.1 exercises the no-loss path via BINDING MANIPULATION, with NO real
# bridge detach/reattach (that real-reconnect survival is the manual T2 tier).
OFFLINE_INSTANCE_PREFIX = "agi-v10smoke-offline-"

# Public verb keys the agent dispatches via `solet call <process_key>`
# (MCP-first addressing is retired; the local CLI is the default path).
# peer_inbox is its own dedicated call; role-addressed sends go through the
# peer_send_by_name VERB (process_call), NOT the instance-addressed peer_send
# call — that call has no role field.
PK_PEER_SEND_BY_NAME = "plugin::agent_messaging_plugin::peer_send_by_name"
PK_PEER_CLAIM_ROLE = "plugin::agent_messaging_plugin::peer_claim_role"
PK_PEER_RELEASE_ROLE = "plugin::agent_messaging_plugin::peer_release_role"
# State reads go through the BOUND state_service interface surface, NOT a
# plugin:: key. postgres_state_management_plugin is a bound ServiceProvider, so
# its plugin:: registration is dropped at runtime (_should_skip_plugin) and a
# plugin:: key would not resolve — AND a plugin:: DB key violates the
# State-interface-only rule. read_state is the dispatchable structured-read verb
# (args {namespace, query}; query carries the table + filters; returns
# data.records). There is NO dispatchable key-value GET on the service surface
# (service_interface::state_service::get_key_value is a Python method, not a
# @platform_process), so the one-shot backfill MARKER is verified by the unit
# smokes, not on the live dispatch surface — T0.2 instead reads the
# backfill-SEEDED bindings (its actual product), which IS dispatchable.
PK_READ_STATE = "service_interface::state_service::read_state"
# Blue-green router status — used by the COLOR GATE to prove this bridge is
# attached to the ACTIVE (hotfixed) color, not a lingering inactive orphan.
PK_SWAP_STATUS = "service_interface::local_self_deployment_service::swap_status"

# dispatch_role_send outcome (mirrors peer_dispatch.DELIVERY_QUEUED_FOR_REPLAY):
# persisted delivered=false because the bound holder was unreachable.
DELIVERY_QUEUED_FOR_REPLAY = "queued_for_replay"


class Tier(StrEnum):
    """How a case is executed at cutover (runbook §1 tiers)."""

    DRIVER = "DRIVER-AUTOMATED"  # agent runs the MCP call; .py predicate asserts
    AGENT = "AGENT-RUN"  # needs a live bridge holder (actual wake delivery)
    MANUAL = "MANUAL"  # multi-session reconnect choreography (runbook §5)


class StepKind(StrEnum):
    MCP = "MCP"
    ASSERT = "ASSERT"


# A JSON result the agent reports back from an MCP call.
Result = dict[str, object]
Predicate = Callable[[Result], bool]


@dataclass(frozen=True)
class McpCall:
    """One MCP invocation the agent pastes into the named tool."""

    kind: StepKind = field(default=StepKind.MCP, init=False)
    tool: str  # "process_call" | "peer_send" | "peer_inbox"
    payload: dict[str, object]
    note: str


@dataclass(frozen=True)
class Assertion:
    """A coded pass/fail predicate the agent applies to the prior result."""

    kind: StepKind = field(default=StepKind.ASSERT, init=False)
    description: str
    check: Predicate


Step = McpCall | Assertion


@dataclass(frozen=True)
class Case:
    case_id: str
    tier: Tier
    title: str
    steps: tuple[Step, ...]


# ---------------------------------------------------------------------------
# Assertion predicates over the documented state-result shapes.
#   read_state / query_state -> data.records (flat list of row dicts)
#   get_key_value           -> data.value / data.found
#   peer_inbox              -> role_entries / instance section + cursors
# ---------------------------------------------------------------------------


def _records(result: Result) -> list[dict[str, object]]:
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    records = data.get("records")
    return records if isinstance(records, list) else []


def _role_rows(result: Result, role: str) -> list[dict[str, object]]:
    return [r for r in _records(result) if r.get(COL_ROLE) == role]


def assert_bindings_present() -> Predicate:
    """The agent_role_binding table is non-empty — the backfill seeded rows.

    Definitive + timing-independent (a claim UPDATEs agent_session_id; it does
    not delete the row). This proves the backfill actually seeded the table (its
    rows are its product; many carry the __unclaimed__ agent_session_id sentinel
    that the backfill writes). We deliberately do NOT assert a specific row is
    still UNCLAIMED — peers may re-claim their roles immediately after the green
    spawn, which would flip the sentinel away from UNCLAIMED legitimately.
    """

    def check(result: Result) -> bool:
        return len(_records(result)) >= 1

    return check


def assert_binding_claimed_by(role: str, agent_instance_id: str) -> Predicate:
    """The role binding now routes to the claiming instance (real session id)."""

    def check(result: Result) -> bool:
        rows = _role_rows(result, role)
        if len(rows) != 1:
            return False
        row = rows[0]
        return (
            row.get(COL_AGENT_INSTANCE_ID) == agent_instance_id
            and row.get(COL_AGENT_SESSION_ID) not in ("", UNCLAIMED_SESSION_ID, None)
        )

    return check


def assert_binding_absent(role: str) -> Predicate:
    """After release, the binding row is gone (hard delete, B4)."""

    def check(result: Result) -> bool:
        return not _role_rows(result, role)

    return check


def assert_send_queued() -> Predicate:
    """The role send persisted delivered=false (bound holder offline → replay).

    The send RESOLVED a binding (so it was not rejected as vacant) but the bound
    holder had no live wake target, so persist-first returned queued_for_replay.
    """

    def check(result: Result) -> bool:
        data = result.get("data")
        return (
            isinstance(data, dict)
            and data.get("delivery") == DELIVERY_QUEUED_FOR_REPLAY
        )

    return check


def _dig(result: Result, key: str) -> object:
    """Find ``key`` at the top level or one wrapper level down.

    The agent may report the swap_status payload either bare or wrapped in a
    ``result`` / ``payload`` / ``data`` envelope depending on whether it read the
    channel notification or the ``process_result`` snapshot. Tolerate both.
    """
    if key in result:
        return result[key]
    for wrapper in ("result", "payload", "data"):
        inner = result.get(wrapper)
        if isinstance(inner, dict) and key in inner:
            return inner[key]
    return None


def _active_instance_id(result: Result) -> object:
    router_status = _dig(result, "router_status")
    if isinstance(router_status, dict):
        return router_status.get("active_instance_id")
    return None


def assert_on_active_color() -> Predicate:
    """THIS bridge is attached to the ACTIVE blue-green color, not an orphan.

    The decisive color discriminator (caught LIVE 2026-06-19): a failed earlier
    color-spawn can leave an INACTIVE orphan color that post-dates the v10-core
    commit but PRE-dates the result-processing hotfix. Such an orphan still
    carries ``role_section_status`` (v10-core), so :func:`assert_on_green` would
    FALSELY PASS on it while ``GATE.clean-dispatch`` FALSELY FAILS (the stale
    ``<<...>>`` defect is still present in un-hotfixed code). ``role_section_status``
    cannot distinguish un-hotfixed-v10 from hotfixed-v10. The ONLY reliable
    discriminator is ``self_instance_id == active_instance_id`` from swap_status —
    this gate asserts exactly that, FIRST, so no downstream result can be
    confounded by which color served it. On failure: the operator must reconnect
    this bridge to the active color (and/or reap the orphan) before re-running.
    """

    def check(result: Result) -> bool:
        self_id = _dig(result, "self_instance_id")
        active_id = _active_instance_id(result)
        return bool(self_id) and bool(active_id) and self_id == active_id

    return check


def assert_on_green() -> Predicate:
    """The serving instance is GREEN (v10 code), not draining-blue.

    ``role_section_status`` is a v10-ADDITIVE ``peer_inbox`` response field
    (``_serialize_peer_inbox``); blue's pre-v10 serializer never emits it. This
    is a CODE-VERSION discriminator — unlike the backfill marker / binding rows,
    which live in the Postgres BOTH colors share (so a read of them passes from
    either color and proves nothing about which code is serving the call).
    """

    def check(result: Result) -> bool:
        return "role_section_status" in result

    return check


def _is_dispatch_success(result: Result) -> bool:
    """The verb-level result envelope reports success (the binding write landed).

    A claim/release ``_success_result`` is ``{'success': True, 'data': {...}}``;
    accept the equivalent action-status forms the snapshot read can surface.
    """
    if result.get("success") is True:
        return True
    status = result.get("action_status") or result.get("status")
    return status in ("completed", "succeeded", "success")


def assert_clean_dispatch() -> Predicate:
    """The verb's LIVE dispatch returned CLEAN — no stale-placeholder error.

    This is the decisive v10-hotfix check (handoff doc §Verification bar; Dusk's
    STEP-5 re-run bar). The pre-fix defect surfaced at the result-processing
    layer: a ``return_value_schema`` / ``field_sensitivities`` field the v10 code
    no longer returns templated to ``<<FIELD>>`` and the processor raised
    ``Placeholder '<<X>>' not found`` → ``bridge_delivery_error``. A CLEAN
    dispatch carries verb-level success AND has NO unresolved ``<<...>>``
    placeholder and NO ``bridge_delivery_error`` anywhere in the reported result
    — strictly stronger than "no-loss" or "errors-but-commits".
    """

    def check(result: Result) -> bool:
        blob = json.dumps(result, default=str)
        if "<<" in blob and ">>" in blob:
            return False  # any unresolved <<PLACEHOLDER>> IS the v10 defect
        if "bridge_delivery_error" in blob:
            return False
        return _is_dispatch_success(result)

    return check


# ---------------------------------------------------------------------------
# Choreography builders — small helpers keep each case flat (radon A/B).
# ---------------------------------------------------------------------------


def _read_bindings_call(note: str) -> McpCall:
    """read_state over the agent_role_binding table (all live rows).

    read_state's ``query`` MUST name the ``table`` (it carries table + filters);
    an empty query reads nothing. Returns ``data.records`` — the shape ``_records``
    parses. Verified live: this returns the backfill-seeded rows (many bearing the
    ``__unclaimed__`` agent_session_id sentinel — the backfill's actual product).
    """
    return McpCall(
        tool="process_call",
        payload={
            "process_key": PK_READ_STATE,
            "arguments": {
                "namespace": AGENT_ROLE_BINDING_NAMESPACE,
                "query": {"table": TABLE_AGENT_ROLE_BINDING},
            },
        },
        note=note,
    )


def _swap_status_call() -> McpCall:
    """Read the blue-green router status (active color + this bridge's color)."""
    return McpCall(
        tool="process_call",
        payload={"process_key": PK_SWAP_STATUS, "arguments": {}},
        note="COLOR GATE: read swap_status; compare self_instance_id vs "
        "router_status.active_instance_id.",
    )


def _offline_instance() -> str:
    """A deterministic, never-live agent_instance_id (pid-scoped, no randomness)."""
    return f"{OFFLINE_INSTANCE_PREFIX}{os.getpid():x}"


def _claim_call(role: str, agent_instance_id: str, note: str) -> McpCall:
    return McpCall(
        tool="process_call",
        payload={
            "process_key": PK_PEER_CLAIM_ROLE,
            "arguments": {
                "name": role,
                "agent_id": "claude_code",
                "agent_instance_id": agent_instance_id,
            },
        },
        note=note,
    )


def _release_call(role: str) -> McpCall:
    return McpCall(
        tool="process_call",
        payload={
            "process_key": PK_PEER_RELEASE_ROLE,
            "arguments": {"name": role},
        },
        note=f"CLEANUP: release sentinel role {role!r} (hard-deletes the binding).",
    )


def _send_call(role: str, prose: str) -> McpCall:
    return McpCall(
        tool="process_call",
        payload={
            "process_key": PK_PEER_SEND_BY_NAME,
            "arguments": {"name": role, "content": prose},
        },
        note=f"Role-addressed send to {role!r} via peer_send_by_name (persist-"
        "first; queues for replay when the bound holder is offline).",
    )


def _inbox_call(*, include_important: bool, role_after: str | None) -> McpCall:
    args: dict[str, object] = {"include_important": include_important}
    if role_after is not None:
        args["role_after"] = role_after
    return McpCall(
        tool="peer_inbox",
        payload=args,
        note="Read the role section (the agent's own held-role inbox).",
    )


# ---------------------------------------------------------------------------
# The T0/T1 case set (runbook §3-§4). DRIVER cases the agent runs + asserts;
# AGENT cases additionally need a live wake; one MANUAL pointer to §5.
# ---------------------------------------------------------------------------


def build_cases(sentinel_role: str) -> tuple[Case, ...]:
    """Return the ordered live-smoke cases for one sentinel role.

    ``sentinel_role`` is minted per-run by the caller (never a real peer name);
    every binding this set touches is that role, released at the end.
    """
    return (
        Case(
            case_id="GATE.on-active-color",
            tier=Tier.DRIVER,
            title="ON-ACTIVE-COLOR GATE — run ABSOLUTE FIRST; abort+reconnect if "
            "this bridge is pinned to an inactive orphan color",
            steps=(
                _swap_status_call(),
                Assertion(
                    "self_instance_id == router_status.active_instance_id → this "
                    "bridge's calls execute on the ACTIVE (hotfixed) color, not a "
                    "lingering inactive orphan. This is STRONGER than on-green and "
                    "must run FIRST: an orphan that post-dates the v10-core commit "
                    "but pre-dates the hotfix still carries role_section_status, so "
                    "on-green would falsely pass while clean-dispatch falsely fails. "
                    "If self != active: STOP — operator reconnects this bridge to "
                    "the active color (and/or reaps the orphan), then re-run.",
                    assert_on_active_color(),
                ),
            ),
        ),
        Case(
            case_id="GATE.on-green",
            tier=Tier.DRIVER,
            title="ON-GREEN CODE-VERSION GATE — run after the color gate; abort if "
            "it fails",
            steps=(
                _inbox_call(include_important=False, role_after=None),
                Assertion(
                    "the peer_inbox response carries role_section_status (a "
                    "v10-additive field) → the call is hitting GREEN, not "
                    "draining-blue. Blue + green share ONE Postgres, so reading "
                    "the backfill marker / bindings does NOT discriminate code "
                    "versions — only a v10-only RESPONSE field does. If absent: "
                    "you are still on blue (or the auto-reconnect to green has "
                    "not completed) → wait + re-run this gate before proceeding.",
                    assert_on_green(),
                ),
            ),
        ),
        Case(
            case_id="GATE.clean-dispatch",
            tier=Tier.DRIVER,
            title="V10 HOTFIX GATE — claim AND release dispatch CLEAN (no "
            "<<placeholder>> bridge_delivery_error); run SECOND, abort on failure",
            steps=(
                _claim_call(
                    sentinel_role,
                    _SELF_INSTANCE_TOKEN,
                    f"Claim {sentinel_role!r} to your live instance purely to "
                    "exercise the claim DISPATCH surface (the result-processing + "
                    "field-sensitivity-templating layer the unit smokes bypass).",
                ),
                Assertion(
                    "peer_claim_role dispatched CLEAN: verb-level success AND no "
                    "unresolved <<...>> placeholder / bridge_delivery_error in the "
                    "result. PRE-FIX this errored 'Placeholder <<ADDRESS_ID>> not "
                    "found' from the stale return_value_schema + field_sensitivities. "
                    "This is THE decisive v10-hotfix bar — not no-loss, not "
                    "errors-but-commits, but clean success on the live surface.",
                    assert_clean_dispatch(),
                ),
                _release_call(sentinel_role),
                Assertion(
                    "peer_release_role dispatched CLEAN: verb-level success AND no "
                    "unresolved <<...>> placeholder / bridge_delivery_error. PRE-FIX "
                    "this errored 'Placeholder <<PRIOR_AGENT_INSTANCE_ID>> not found'. "
                    "Both verbs clean ⇒ the result-processing stale-field class is "
                    "fully closed on the live dispatch surface.",
                    assert_clean_dispatch(),
                ),
            ),
        ),
        Case(
            case_id="T0.2",
            tier=Tier.DRIVER,
            title="backfill seeded the agent_role_binding table (read its product)",
            steps=(
                _read_bindings_call(
                    "T0.2: read every agent_role_binding row (audit the seed)."
                ),
                Assertion(
                    "≥1 agent_role_binding row exists — the one-shot backfill "
                    "seeded the table against real Postgres (verified live: the "
                    "seeded rows bear the __unclaimed__ agent_session_id sentinel, "
                    "the backfill's actual product). NOTE: the separate one-shot "
                    "B5 backfill MARKER is a key_value flag with NO dispatchable "
                    "GET on the service surface (get_key_value is not a "
                    "@platform_process), so its idempotency guard is covered by the "
                    "unit smokes, not on the live dispatch surface.",
                    assert_bindings_present(),
                ),
            ),
        ),
        Case(
            case_id="T1.1",
            tier=Tier.DRIVER,
            title="headline no-loss: bound-holder-offline -> queue -> reclaim -> "
            "drain (real PG; single-session, NO real bridge reconnect)",
            steps=(
                _claim_call(
                    sentinel_role,
                    _offline_instance(),
                    f"Seed {sentinel_role!r} bound to an OFFLINE instance (no live "
                    "bridge) so a send resolves the binding but finds no live "
                    "holder. Binding manipulation — NOT a real detach.",
                ),
                _send_call(sentinel_role, "v10 live-smoke T1.1 #1 — queued-for-replay."),
                Assertion(
                    "send #1 persisted delivered=false (queued_for_replay)",
                    assert_send_queued(),
                ),
                _send_call(sentinel_role, "v10 live-smoke T1.1 #2 — queued-for-replay."),
                Assertion(
                    "send #2 persisted delivered=false (queued_for_replay)",
                    assert_send_queued(),
                ),
                _claim_call(
                    sentinel_role,
                    _SELF_INSTANCE_TOKEN,
                    f"Re-claim {sentinel_role!r} to YOUR live instance (takeover of "
                    "the offline binding) — you are now the live holder.",
                ),
                _inbox_call(include_important=True, role_after=None),
                Assertion(
                    "NO LOSS: both queued IMPORTANTs re-deliver to the new live "
                    "holder via the repair drain — the core persist+queue+drain "
                    "guarantee, proven live WITHOUT a real bridge reconnect "
                    "(real reconnect-survival is the manual T2 tier)",
                    _both_queued_delivered(),
                ),
                _read_bindings_call("T1.1: the role now routes to your live instance."),
                Assertion(
                    "post-takeover binding routes to your live instance",
                    assert_binding_claimed_by(sentinel_role, _SELF_INSTANCE_TOKEN),
                ),
                _release_call(sentinel_role),
                _read_bindings_call(
                    "T1.1: confirm the release hard-deleted the sentinel binding."
                ),
                Assertion(
                    "post-release the sentinel binding is GONE (hard delete; no "
                    "orphan left in the shared DB) — cleanup verified",
                    assert_binding_absent(sentinel_role),
                ),
            ),
        ),
        Case(
            case_id="T1.6",
            tier=Tier.DRIVER,
            title="envelope carries sender_agent_instance_id (v10 schema-complete)",
            steps=(
                _claim_call(
                    sentinel_role,
                    _SELF_INSTANCE_TOKEN,
                    f"Claim {sentinel_role!r} to your live instance (you hold it; "
                    "the send below delivers online).",
                ),
                _send_call(sentinel_role, "v10 live-smoke T1.6 — sender carried."),
                _inbox_call(include_important=True, role_after=None),
                Assertion(
                    "the projected role entry carries a populated "
                    "sender_agent_instance_id (+sender_agent_id, thread_id); "
                    "do NOT assert real-sender-reply (that is #3.2 follow-on)",
                    _entry_carries_sender(),
                ),
                _release_call(sentinel_role),
            ),
        ),
        Case(
            case_id="T1.9",
            tier=Tier.DRIVER,
            title="role-section fault isolation: malformed role_after (Q1, live half)",
            steps=(
                _inbox_call(include_important=False, role_after="!!not-a-cursor!!"),
                Assertion(
                    "instance section returns normally; role_entries=() + "
                    "role_section_status='error' + populated role_section_error; "
                    "never a whole-call 500",
                    _role_section_faulted_but_instance_ok(),
                ),
            ),
        ),
        Case(
            case_id="T2",
            tier=Tier.MANUAL,
            title="reconnect / rename / cross-kind / second-spawn — see runbook §5",
            steps=(
                Assertion(
                    "MANUAL: T2-a/b bridge+session-id change, T2-c /rename "
                    "takeover, T2-e cross-kind (codex) takeover, T2-d "
                    "reconnect-before-drain, T2-f second-spawn B5/B4 idempotency. "
                    "Run with the operator + a second live session per runbook §5.",
                    _manual_marker(),
                ),
            ),
        ),
    )


# Predicate stubs whose live assertion the agent completes against the real
# result (the agent supplies its own instance id / inspects prose bodies).
# They are coded — not prose — so the agent applies a concrete check, not a
# judgement call; the ones needing a live wake are tagged AGENT-RUN above.

_SELF_INSTANCE_TOKEN = "<agent-substitutes-own-agent_instance_id>"


def _both_queued_delivered() -> Predicate:
    def check(result: Result) -> bool:
        entries = _role_inbox_entries(result)
        bodies = " ".join(_entry_text(e) for e in entries)
        return "T1.1 #1" in bodies and "T1.1 #2" in bodies

    return check


def _entry_carries_sender() -> Predicate:
    def check(result: Result) -> bool:
        entries = _role_inbox_entries(result)
        return any(bool(e.get("sender_agent_instance_id")) for e in entries)

    return check


def _role_section_faulted_but_instance_ok() -> Predicate:
    def check(result: Result) -> bool:
        status = result.get("role_section_status")
        entries = result.get("role_entries")
        instance = result.get("entries")
        return (
            status == "error"
            and entries == []
            and bool(result.get("role_section_error"))
            and isinstance(instance, list)
        )

    return check


def _manual_marker() -> Predicate:
    def check(_result: Result) -> bool:  # never auto-run; documents §5
        return True

    return check


def _role_inbox_entries(result: Result) -> list[dict[str, object]]:
    entries = result.get("role_entries")
    return entries if isinstance(entries, list) else []


def _entry_text(entry: dict[str, object]) -> str:
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(c.get("text", "")) for c in content if isinstance(c, dict)
        )
    return ""


# ---------------------------------------------------------------------------
# Self-validation + emission.
# ---------------------------------------------------------------------------


def _role_args_in_payload(payload: dict[str, object]) -> list[str]:
    """Every role-name value referenced by an MCP payload (for sentinel check)."""
    roles: list[str] = []
    args = payload.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("name"), str):
        roles.append(str(args["name"]))
    if isinstance(payload.get("peer_role"), str):
        roles.append(str(payload["peer_role"]))
    return roles


def validate_cases(cases: tuple[Case, ...]) -> list[str]:
    """Return a list of structural defects; empty means the harness is sound."""
    defects: list[str] = []
    for case in cases:
        if not case.steps:
            defects.append(f"{case.case_id}: no steps")
        for step in case.steps:
            if isinstance(step, McpCall):
                _validate_mcp(case, step, defects)
            elif not callable(step.check):
                defects.append(f"{case.case_id}: assertion predicate not callable")
    return defects


def _validate_mcp(case: Case, step: McpCall, defects: list[str]) -> None:
    if step.tool not in ("process_call", "peer_send", "peer_inbox"):
        defects.append(f"{case.case_id}: unknown tool {step.tool!r}")
    if step.tool == "process_call":
        pk = step.payload.get("process_key")
        if not isinstance(pk, str) or "::" not in pk:
            defects.append(f"{case.case_id}: malformed process_key {pk!r}")
    for role in _role_args_in_payload(step.payload):
        if not role.startswith(SENTINEL_PREFIX):
            defects.append(
                f"{case.case_id}: NON-SENTINEL role {role!r} — would touch a "
                "real peer's binding (forbidden)"
            )


def emit(cases: tuple[Case, ...]) -> None:
    """Print the deterministic agent-run choreography."""
    solet_name = os.environ["SOLET_NAME"]
    for case in cases:
        print(f"\n=== {case.case_id} [{case.tier.value}] {case.title} ===")
        for idx, step in enumerate(case.steps, start=1):
            if isinstance(step, McpCall):
                print(f"  {idx}. MCP mcp__{solet_name}__{step.tool}  {step.payload}")
                print(f"      → {step.note}")
            else:
                print(f"  {idx}. ASSERT  {step.description}")


def _mint_sentinel_role() -> str:
    """A per-run sentinel role name, deterministic from the parent pid.

    Avoids ``uuid4``/``random`` (which the platform's resume-safe contexts
    forbid); the pid is unique per driver process and never collides with a
    real peer name because of the sentinel prefix.
    """
    return f"{SENTINEL_PREFIX}{os.getpid():x}"


def main() -> int:
    sentinel_role = _mint_sentinel_role()
    cases = build_cases(sentinel_role)
    defects = validate_cases(cases)
    print("=== v10 LIVE-SMOKE DRIVER (T0/T1 tier) — agent-run choreography ===")
    print(f"sentinel role for this run: {sentinel_role}")
    automated = sum(1 for c in cases if c.tier is Tier.DRIVER)
    agent_run = sum(1 for c in cases if c.tier is Tier.AGENT)
    manual = sum(1 for c in cases if c.tier is Tier.MANUAL)
    print(
        f"cases: {len(cases)}  (DRIVER-AUTOMATED={automated}, "
        f"AGENT-RUN={agent_run}, MANUAL={manual})"
    )
    if defects:
        print("\nSTRUCTURAL DEFECTS:")
        for d in defects:
            print(f"  ✘ {d}")
        return 1
    print("structure: OK (all payloads well-formed, all roles sentinel-scoped)")
    emit(cases)
    print(
        "\nThe agent executes each MCP step through its bridge against green and "
        "applies the following assertion predicate to the returned JSON. No live "
        "call is made by this process (a standalone .py has no bridge handle)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
