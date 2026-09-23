"""Codex Cloud task-detail parser + normalizer (vendor='codex').

Companion to :mod:`ananta.llm.session_ledger.vendor.codex` (which handles the
LOCAL Codex CLI rollout JSONL shape at ``~/.codex/sessions/...``). This module
parses the CLOUD task-detail response shape returned by the
``GET https://chatgpt.com/backend-api/wham/tasks/{task_id}`` endpoint that the
open-source ``openai/codex`` Rust CLI calls during ``codex cloud status``.

Phase A discovery and the unified-url-walker design v3 §7.4 (the
Turn→RawSessionEvent transform table) are dev-checkout workbench design
records, not part of the shipped tree.

================================================================
Wire shape (operator-side walker SHIPS this; one envelope per cloud task)
================================================================

::

    {
        "external_session_id": "<task_id>",          # TaskSummary.id
        "task_title": "<str>",                       # TaskSummary.title (optional)
        "task_event_at": "<ISO8601 UTC>",            # TaskSummary.updated_at
        "environment_id": "<str | None>",            # TaskSummary.environment_id
        "environment_label": "<str | None>",         # TaskSummary.environment_label
        "task_status": "<str>",                      # TaskSummary.status, lowercase
        "task_details": {                            # CodeTaskDetailsResponse
            "current_user_turn":        <Turn | None>,
            "current_assistant_turn":   <Turn | None>,
            "current_diff_task_turn":   <Turn | None>,
        },
        "sibling_attempts": [                        # OPTIONAL — best-of-N
            {"turn_id": "<str>", "attempts": [<TurnAttempt>, ...]},
            ...
        ],
    }

The walker pulls TaskSummary from ``/wham/tasks/list`` and Turn-bearing fields
from ``/wham/tasks/{task_id}``. ``external_session_id`` is the task id; the
``(vendor='codex', external_session_id)`` pair drives the W5.B canonical-pointer
cross-source dedupe — same task id arriving from ``codex_local`` (Codex CLI
rollout JSONL) or ``codex_state`` (Codex Desktop's cloud-sync mirror of
``~/.codex/state_5.sqlite`` per openai/codex#27243) collapses against the
``codex_cloud`` canonical row.

================================================================
Turn → RawSessionEvent transform (per design v3 §7.4)
================================================================

For each Turn (``current_user_turn`` / ``current_assistant_turn`` /
``current_diff_task_turn``), in declaration order:

* ``input_items[type='message']`` → ``RawSessionEvent(role=USER, type=MESSAGE)``
  (regardless of which Turn — user/assistant input items model the prompt-side
  surface; assistant turns carry the same user prompts forward).
* ``output_items[type='message']`` → ``RawSessionEvent(role=ASSISTANT,
  type=MESSAGE)``.
* ``output_items[type='output_diff']`` → ``RawSessionEvent(type=TOOL_RESULT)``
  with subtype lift ``content_json={'subtype': 'output_diff', 'diff': ...}``.
* ``output_items[type='pr']`` → ``RawSessionEvent(type=TOOL_RESULT)`` with
  subtype lift ``content_json={'subtype': 'pr', 'diff': ...}``.
* ``output_items[type='reasoning']`` → ``RawSessionEvent(role=ASSISTANT,
  type=MESSAGE)`` with subtype lift ``content_json={'subtype': 'reasoning'}``.
* ``output_items[type='function_call']`` /
  ``output_items[type='function_call_output']`` → ``RawSessionEvent(role=TOOL,
  type=TOOL_CALL | TOOL_RESULT)`` per local-codex parser symmetry.
* ``worklog.messages[author.role='assistant']`` → ``RawSessionEvent(role=
  ASSISTANT, type=MESSAGE)`` with subtype lift ``content_json={'subtype':
  'worklog'}``.
* ``turn.error`` (when present, non-empty code/message) → ``RawSessionEvent(
  type=SYSTEM)`` with subtype lift ``content_json={'subtype': 'error', 'code':
  ..., 'message': ...}`` — parallels the existing claude_code away_summary
  subtype lift seam at
  ``knowledge_bases/ananta_platform/19_session_ledger/01_system_event_subtype_lift.md``.

Sibling attempts (when present in the envelope) yield identical
RawSessionEvents with ``attempt_placement`` stamped onto the payload so the
M6 auto-summarizer can route best-of-N attempts to the same logical
conversation without losing per-attempt provenance.

================================================================
Fast-fail rules (per the KB "Critical Development Guidelines v2")
================================================================

* Unrecognized envelope top-level field is silently ignored (forward-compat
  for new Codex backend fields).
* Unrecognized Turn / TurnItem field is silently ignored within the same
  forward-compat envelope.
* Unrecognized TurnItem ``type`` raises ``ValueError`` — the importer
  surfaces this at ``__import_batch.error_kind='value_error'`` and the
  operator decides whether to extend the recognized set.
* Empty/None payloads at known fields raise ``ValueError`` — never silently
  swallowed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from ananta.llm.session_ledger.types import (
    EventType,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
)

# ───────────────────────────────────────────────────────────────────────────
# Envelope keys
# ───────────────────────────────────────────────────────────────────────────

_KEY_EXTERNAL_SESSION_ID: Final = "external_session_id"
_KEY_TASK_EVENT_AT: Final = "task_event_at"
_KEY_TASK_DETAILS: Final = "task_details"
_KEY_SIBLING_ATTEMPTS: Final = "sibling_attempts"

# Internal payload-stash keys so :func:`normalize_raw` can dispatch without
# re-walking the original envelope. Always prefix with underscore so the
# downstream JSONB stripper does not surface them to the operator.
_INTERNAL_KIND: Final = "_codex_cloud_kind"
_INTERNAL_TURN_NAME: Final = "_codex_cloud_turn_name"
_INTERNAL_ATTEMPT_PLACEMENT: Final = "_codex_cloud_attempt_placement"

_KIND_USER_MESSAGE: Final = "user_message"
_KIND_ASSISTANT_MESSAGE: Final = "assistant_message"
_KIND_ASSISTANT_REASONING: Final = "assistant_reasoning"
_KIND_OUTPUT_DIFF: Final = "output_diff"
_KIND_PR: Final = "pr"
_KIND_FUNCTION_CALL: Final = "function_call"
_KIND_FUNCTION_CALL_OUTPUT: Final = "function_call_output"
_KIND_WORKLOG_ASSISTANT: Final = "worklog_assistant"
_KIND_TURN_ERROR: Final = "turn_error"

# Recognised Turn names appearing on CodeTaskDetailsResponse.
_TURN_NAMES: Final[tuple[str, ...]] = (
    "current_user_turn",
    "current_assistant_turn",
    "current_diff_task_turn",
)

# Recognised TurnItem.type values. Anything else → ValueError.
_RECOGNIZED_OUTPUT_ITEM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "message",
        "reasoning",
        "output_diff",
        "pr",
        "function_call",
        "function_call_output",
    }
)


# ───────────────────────────────────────────────────────────────────────────
# Public surface
# ───────────────────────────────────────────────────────────────────────────


def parse_chunk(chunk_text: str) -> Iterator[RawSessionEvent]:
    """Parse one Codex-Cloud task chunk.

    See module-level docstring for the wire shape. Yields one
    :class:`RawSessionEvent` per addressable event surfaced from the task
    detail (and optional sibling attempts).
    """
    envelope = _decode_envelope(chunk_text)
    external_session_id = _require_str(envelope, _KEY_EXTERNAL_SESSION_ID)
    event_at = _parse_iso(_require_str(envelope, _KEY_TASK_EVENT_AT))
    details = _require_dict(envelope, _KEY_TASK_DETAILS)
    yield from _walk_task_details(
        external_session_id=external_session_id,
        event_at=event_at,
        details=details,
    )
    sibling_attempts = envelope.get(_KEY_SIBLING_ATTEMPTS)
    if sibling_attempts is None:
        return
    if not isinstance(sibling_attempts, list):
        raise ValueError(
            "codex_cloud envelope 'sibling_attempts' must be a list when present"
        )
    yield from _walk_sibling_attempts(
        external_session_id=external_session_id,
        event_at=event_at,
        sibling_attempts=sibling_attempts,
    )


def normalize_raw(raw: RawSessionEvent) -> NormalizedSessionEvent:
    """Map a Codex Cloud RawSessionEvent to the canonical NormalizedSessionEvent.

    Dispatches on ``raw.payload[_INTERNAL_KIND]`` (stamped by
    :func:`parse_chunk`).
    """
    kind = raw.payload.get(_INTERNAL_KIND)
    if kind == _KIND_USER_MESSAGE:
        return _normalize_message(raw, MessageRole.USER, subtype=None)
    if kind == _KIND_ASSISTANT_MESSAGE:
        return _normalize_message(raw, MessageRole.ASSISTANT, subtype=None)
    if kind == _KIND_ASSISTANT_REASONING:
        return _normalize_message(raw, MessageRole.ASSISTANT, subtype="reasoning")
    if kind == _KIND_WORKLOG_ASSISTANT:
        return _normalize_message(raw, MessageRole.ASSISTANT, subtype="worklog")
    if kind == _KIND_OUTPUT_DIFF:
        return _normalize_diff_result(raw, subtype=_KIND_OUTPUT_DIFF)
    if kind == _KIND_PR:
        return _normalize_diff_result(raw, subtype=_KIND_PR)
    if kind == _KIND_FUNCTION_CALL:
        return _normalize_function_call(raw)
    if kind == _KIND_FUNCTION_CALL_OUTPUT:
        return _normalize_function_call_output(raw)
    if kind == _KIND_TURN_ERROR:
        return _normalize_turn_error(raw)
    raise ValueError(
        f"codex_cloud.normalize_raw: unrecognized "
        f"{_INTERNAL_KIND}={kind!r} in payload"
    )


# ───────────────────────────────────────────────────────────────────────────
# Envelope walk
# ───────────────────────────────────────────────────────────────────────────


def _walk_task_details(
    *,
    external_session_id: str,
    event_at: datetime,
    details: dict[str, Any],
) -> Iterator[RawSessionEvent]:
    for turn_name in _TURN_NAMES:
        turn = details.get(turn_name)
        if turn is None:
            continue
        if not isinstance(turn, dict):
            raise ValueError(
                f"codex_cloud task_details[{turn_name!r}] must be an object when present"
            )
        yield from _walk_turn(
            external_session_id=external_session_id,
            event_at=event_at,
            turn=turn,
            turn_name=turn_name,
            attempt_placement=None,
        )


def _walk_sibling_attempts(
    *,
    external_session_id: str,
    event_at: datetime,
    sibling_attempts: list[Any],
) -> Iterator[RawSessionEvent]:
    for entry in sibling_attempts:
        if not isinstance(entry, dict):
            raise ValueError(
                "codex_cloud sibling_attempts[] entries must be objects"
            )
        attempts = entry.get("attempts")
        if not isinstance(attempts, list):
            raise ValueError(
                "codex_cloud sibling_attempts[].attempts must be a list"
            )
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise ValueError(
                    "codex_cloud sibling_attempts[].attempts[] entries must be objects"
                )
            yield from _walk_attempt(
                external_session_id=external_session_id,
                event_at=event_at,
                attempt=attempt,
            )


def _walk_attempt(
    *,
    external_session_id: str,
    event_at: datetime,
    attempt: dict[str, Any],
) -> Iterator[RawSessionEvent]:
    turn_id = _require_attempt_turn_id(attempt)
    placement = _attempt_placement(attempt)
    yield from _yield_attempt_diff(
        external_session_id=external_session_id,
        event_at=event_at,
        attempt=attempt,
        turn_id=turn_id,
        placement=placement,
    )
    yield from _yield_attempt_messages(
        external_session_id=external_session_id,
        event_at=event_at,
        attempt=attempt,
        turn_id=turn_id,
        placement=placement,
    )


def _require_attempt_turn_id(attempt: dict[str, Any]) -> str:
    turn_id = attempt.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError(
            "codex_cloud sibling_attempts[].attempts[].turn_id must be a non-empty string"
        )
    return turn_id


def _attempt_placement(attempt: dict[str, Any]) -> int | None:
    raw = attempt.get("attempt_placement")
    if raw is None:
        return None
    if not isinstance(raw, int):
        raise ValueError(
            "codex_cloud sibling attempt 'attempt_placement' must be int when present"
        )
    return raw


def _yield_attempt_diff(
    *,
    external_session_id: str,
    event_at: datetime,
    attempt: dict[str, Any],
    turn_id: str,
    placement: int | None,
) -> Iterator[RawSessionEvent]:
    diff = attempt.get("diff")
    if not isinstance(diff, str) or not diff:
        return
    yield _build_raw(
        external_session_id=external_session_id,
        kind=_KIND_OUTPUT_DIFF,
        event_at=event_at,
        payload_extras={"diff": diff},
        turn_name=f"sibling::{turn_id}",
        vendor_event_id=f"{turn_id}::sibling::diff",
        vendor_parent_event_id=turn_id,
        attempt_placement=placement,
    )


def _yield_attempt_messages(
    *,
    external_session_id: str,
    event_at: datetime,
    attempt: dict[str, Any],
    turn_id: str,
    placement: int | None,
) -> Iterator[RawSessionEvent]:
    messages = attempt.get("messages")
    if messages is None:
        return
    if not isinstance(messages, list):
        raise ValueError(
            "codex_cloud sibling attempt 'messages' must be a list when present"
        )
    for index, message in enumerate(messages):
        if not isinstance(message, str):
            raise ValueError(
                "codex_cloud sibling attempt 'messages[]' entries must be strings"
            )
        if not message:
            continue
        yield _build_raw(
            external_session_id=external_session_id,
            kind=_KIND_ASSISTANT_MESSAGE,
            event_at=event_at,
            payload_extras={"text": message},
            turn_name=f"sibling::{turn_id}",
            vendor_event_id=f"{turn_id}::sibling::message::{index}",
            vendor_parent_event_id=turn_id,
            attempt_placement=placement,
        )


def _walk_turn(
    *,
    external_session_id: str,
    event_at: datetime,
    turn: dict[str, Any],
    turn_name: str,
    attempt_placement: int | None,
) -> Iterator[RawSessionEvent]:
    turn_id = _optional_str(turn.get("id"))
    parent_id_anchor = turn_id or turn_name
    yield from _walk_items(
        external_session_id=external_session_id,
        event_at=event_at,
        items=_extract_items(turn, "input_items"),
        turn_name=turn_name,
        turn_id=turn_id,
        role_default=MessageRole.USER,
        is_input=True,
        parent_id_anchor=parent_id_anchor,
        attempt_placement=attempt_placement,
    )
    yield from _walk_items(
        external_session_id=external_session_id,
        event_at=event_at,
        items=_extract_items(turn, "output_items"),
        turn_name=turn_name,
        turn_id=turn_id,
        role_default=MessageRole.ASSISTANT,
        is_input=False,
        parent_id_anchor=parent_id_anchor,
        attempt_placement=attempt_placement,
    )
    worklog = turn.get("worklog")
    if isinstance(worklog, dict):
        yield from _walk_worklog(
            external_session_id=external_session_id,
            event_at=event_at,
            worklog=worklog,
            turn_name=turn_name,
            turn_id=turn_id,
            parent_id_anchor=parent_id_anchor,
            attempt_placement=attempt_placement,
        )
    error = turn.get("error")
    if isinstance(error, dict):
        summary = _summarize_error(error)
        if summary is not None:
            yield _build_raw(
                external_session_id=external_session_id,
                kind=_KIND_TURN_ERROR,
                event_at=event_at,
                payload_extras={
                    "code": summary["code"],
                    "message": summary["message"],
                },
                turn_name=turn_name,
                vendor_event_id=f"{parent_id_anchor}::error",
                vendor_parent_event_id=turn_id,
                attempt_placement=attempt_placement,
            )


@dataclass(frozen=True, slots=True)
class _ItemCtx:
    """Per-item context threaded through the kind-specific item handlers.

    Bundles the loop-invariant args of :func:`_walk_items` so each per-type
    helper has a one-arg signature and keeps cyclomatic complexity of the
    dispatch loop in the A/B band.
    """

    external_session_id: str
    event_at: datetime
    turn_name: str
    turn_id: str | None
    role_default: MessageRole
    is_input: bool
    parent_id_anchor: str
    attempt_placement: int | None


def _walk_items(
    *,
    external_session_id: str,
    event_at: datetime,
    items: list[dict[str, Any]],
    turn_name: str,
    turn_id: str | None,
    role_default: MessageRole,
    is_input: bool,
    parent_id_anchor: str,
    attempt_placement: int | None,
) -> Iterator[RawSessionEvent]:
    ctx = _ItemCtx(
        external_session_id=external_session_id,
        event_at=event_at,
        turn_name=turn_name,
        turn_id=turn_id,
        role_default=role_default,
        is_input=is_input,
        parent_id_anchor=parent_id_anchor,
        attempt_placement=attempt_placement,
    )
    direction = "input" if is_input else "output"
    for index, item in enumerate(items):
        item_type = _require_item_type(item, ctx.turn_name, direction, index)
        handler = _ITEM_HANDLERS[item_type]
        yield from handler(item, ctx, direction, index)


def _require_item_type(
    item: dict[str, Any], turn_name: str, direction: str, index: int
) -> str:
    item_type = item.get("type")
    if not isinstance(item_type, str):
        raise ValueError(
            f"codex_cloud {turn_name} {direction}_items[{index}] lacks string 'type'"
        )
    if item_type not in _RECOGNIZED_OUTPUT_ITEM_TYPES:
        raise ValueError(
            f"codex_cloud {turn_name} {direction}_items[{index}] "
            f"unrecognized type {item_type!r}"
        )
    return item_type


def _handle_message_item(
    item: dict[str, Any], ctx: _ItemCtx, direction: str, index: int
) -> Iterator[RawSessionEvent]:
    text = _join_content_fragments(item.get("content"))
    if not text:
        return
    kind = (
        _KIND_USER_MESSAGE
        if (ctx.is_input and ctx.role_default is MessageRole.USER)
        else _KIND_ASSISTANT_MESSAGE
    )
    yield _build_raw(
        external_session_id=ctx.external_session_id,
        kind=kind,
        event_at=ctx.event_at,
        payload_extras={"text": text},
        turn_name=ctx.turn_name,
        vendor_event_id=f"{ctx.parent_id_anchor}::{direction}::{index}::message",
        vendor_parent_event_id=ctx.turn_id,
        attempt_placement=ctx.attempt_placement,
    )


def _handle_reasoning_item(
    item: dict[str, Any], ctx: _ItemCtx, direction: str, index: int
) -> Iterator[RawSessionEvent]:
    text = _join_content_fragments(item.get("content"))
    if not text:
        return
    yield _build_raw(
        external_session_id=ctx.external_session_id,
        kind=_KIND_ASSISTANT_REASONING,
        event_at=ctx.event_at,
        payload_extras={"text": text},
        turn_name=ctx.turn_name,
        vendor_event_id=f"{ctx.parent_id_anchor}::{direction}::{index}::reasoning",
        vendor_parent_event_id=ctx.turn_id,
        attempt_placement=ctx.attempt_placement,
    )


def _handle_output_diff_item(
    item: dict[str, Any], ctx: _ItemCtx, direction: str, index: int
) -> Iterator[RawSessionEvent]:
    diff = item.get("diff")
    if not isinstance(diff, str) or not diff:
        return
    yield _build_raw(
        external_session_id=ctx.external_session_id,
        kind=_KIND_OUTPUT_DIFF,
        event_at=ctx.event_at,
        payload_extras={"diff": diff},
        turn_name=ctx.turn_name,
        vendor_event_id=f"{ctx.parent_id_anchor}::{direction}::{index}::output_diff",
        vendor_parent_event_id=ctx.turn_id,
        attempt_placement=ctx.attempt_placement,
    )


def _handle_pr_item(
    item: dict[str, Any], ctx: _ItemCtx, direction: str, index: int
) -> Iterator[RawSessionEvent]:
    payload = item.get("output_diff")
    if not isinstance(payload, dict):
        return
    diff = payload.get("diff")
    if not isinstance(diff, str) or not diff:
        return
    yield _build_raw(
        external_session_id=ctx.external_session_id,
        kind=_KIND_PR,
        event_at=ctx.event_at,
        payload_extras={"diff": diff},
        turn_name=ctx.turn_name,
        vendor_event_id=f"{ctx.parent_id_anchor}::{direction}::{index}::pr",
        vendor_parent_event_id=ctx.turn_id,
        attempt_placement=ctx.attempt_placement,
    )


def _handle_function_call_item(
    item: dict[str, Any], ctx: _ItemCtx, direction: str, index: int
) -> Iterator[RawSessionEvent]:
    yield _build_raw(
        external_session_id=ctx.external_session_id,
        kind=_KIND_FUNCTION_CALL,
        event_at=ctx.event_at,
        payload_extras={
            "name": _optional_str(item.get("name")) or "",
            "arguments_text": _optional_str(item.get("arguments")) or "",
            "call_id": _optional_str(item.get("call_id")) or "",
        },
        turn_name=ctx.turn_name,
        vendor_event_id=f"{ctx.parent_id_anchor}::{direction}::{index}::function_call",
        vendor_parent_event_id=ctx.turn_id,
        attempt_placement=ctx.attempt_placement,
    )


def _handle_function_call_output_item(
    item: dict[str, Any], ctx: _ItemCtx, direction: str, index: int
) -> Iterator[RawSessionEvent]:
    yield _build_raw(
        external_session_id=ctx.external_session_id,
        kind=_KIND_FUNCTION_CALL_OUTPUT,
        event_at=ctx.event_at,
        payload_extras={
            "output_text": _optional_str(item.get("output")) or "",
            "call_id": _optional_str(item.get("call_id")) or "",
        },
        turn_name=ctx.turn_name,
        vendor_event_id=(
            f"{ctx.parent_id_anchor}::{direction}::{index}::function_call_output"
        ),
        vendor_parent_event_id=ctx.turn_id,
        attempt_placement=ctx.attempt_placement,
    )


_ItemHandler = Callable[
    [dict[str, Any], _ItemCtx, str, int], Iterator[RawSessionEvent],
]

_ITEM_HANDLERS: Final[dict[str, _ItemHandler]] = {
    "message": _handle_message_item,
    "reasoning": _handle_reasoning_item,
    "output_diff": _handle_output_diff_item,
    "pr": _handle_pr_item,
    "function_call": _handle_function_call_item,
    "function_call_output": _handle_function_call_output_item,
}


def _walk_worklog(
    *,
    external_session_id: str,
    event_at: datetime,
    worklog: dict[str, Any],
    turn_name: str,
    turn_id: str | None,
    parent_id_anchor: str,
    attempt_placement: int | None,
) -> Iterator[RawSessionEvent]:
    messages = worklog.get("messages")
    if messages is None:
        return
    if not isinstance(messages, list):
        raise ValueError(
            f"codex_cloud {turn_name} worklog.messages must be a list when present"
        )
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(
                f"codex_cloud {turn_name} worklog.messages[{index}] must be an object"
            )
        author = message.get("author")
        role = ""
        if isinstance(author, dict):
            role_raw = author.get("role")
            if isinstance(role_raw, str):
                role = role_raw.lower()
        if role != "assistant":
            continue
        content = message.get("content")
        text = _join_worklog_content(content)
        if not text:
            continue
        yield _build_raw(
            external_session_id=external_session_id,
            kind=_KIND_WORKLOG_ASSISTANT,
            event_at=event_at,
            payload_extras={"text": text},
            turn_name=turn_name,
            vendor_event_id=(
                f"{parent_id_anchor}::worklog::assistant::{index}"
            ),
            vendor_parent_event_id=turn_id,
            attempt_placement=attempt_placement,
        )


# ───────────────────────────────────────────────────────────────────────────
# RawSessionEvent → NormalizedSessionEvent
# ───────────────────────────────────────────────────────────────────────────


def _normalize_message(
    raw: RawSessionEvent,
    role: MessageRole,
    subtype: str | None,
) -> NormalizedSessionEvent:
    text = _payload_str(raw, "text")
    content_json: dict[str, Any] | None = None
    if subtype is not None:
        content_json = {"subtype": subtype}
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.MESSAGE,
        role=role,
        content_text=text,
        content_json=content_json,
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_diff_result(
    raw: RawSessionEvent, subtype: str
) -> NormalizedSessionEvent:
    diff = _payload_str(raw, "diff")
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_RESULT,
        role=MessageRole.TOOL,
        content_text=diff,
        content_json={"subtype": subtype},
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_function_call(raw: RawSessionEvent) -> NormalizedSessionEvent:
    name = _payload_str(raw, "name")
    arguments_text = _payload_str(raw, "arguments_text")
    call_id = _payload_str(raw, "call_id")
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_CALL,
        role=MessageRole.ASSISTANT,
        content_text=arguments_text,
        content_json={
            "subtype": "function_call",
            "name": name,
            "call_id": call_id,
        },
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_function_call_output(
    raw: RawSessionEvent,
) -> NormalizedSessionEvent:
    output_text = _payload_str(raw, "output_text")
    call_id = _payload_str(raw, "call_id")
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_RESULT,
        role=MessageRole.TOOL,
        content_text=output_text,
        content_json={"subtype": "function_call_output", "call_id": call_id},
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_turn_error(raw: RawSessionEvent) -> NormalizedSessionEvent:
    code = _payload_str(raw, "code")
    message = _payload_str(raw, "message")
    text = f"{code}: {message}" if (code and message) else (code or message)
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.SYSTEM,
        role=MessageRole.SYSTEM,
        content_text=text,
        content_json={"subtype": "error", "code": code, "message": message},
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _build_raw(
    *,
    external_session_id: str,
    kind: str,
    event_at: datetime,
    payload_extras: dict[str, Any],
    turn_name: str,
    vendor_event_id: str,
    vendor_parent_event_id: str | None,
    attempt_placement: int | None,
) -> RawSessionEvent:
    payload: dict[str, Any] = dict(payload_extras)
    payload[_INTERNAL_KIND] = kind
    payload[_INTERNAL_TURN_NAME] = turn_name
    if attempt_placement is not None:
        payload[_INTERNAL_ATTEMPT_PLACEMENT] = attempt_placement
    return RawSessionEvent(
        external_session_id=external_session_id,
        payload=payload,
        event_at=event_at,
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=vendor_parent_event_id,
    )


def _decode_envelope(chunk_text: str) -> dict[str, Any]:
    try:
        envelope = json.loads(chunk_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"codex_cloud chunk is not valid JSON: {exc}"
        ) from exc
    if not isinstance(envelope, dict):
        raise ValueError("codex_cloud chunk must be a JSON object")
    return envelope


def _require_str(envelope: dict[str, Any], key: str) -> str:
    value = envelope.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"codex_cloud envelope missing non-empty string {key!r}"
        )
    return value


def _require_dict(envelope: dict[str, Any], key: str) -> dict[str, Any]:
    value = envelope.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"codex_cloud envelope {key!r} must be an object")
    return value


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _payload_str(raw: RawSessionEvent, key: str) -> str:
    value = raw.payload.get(key)
    if not isinstance(value, str):
        return ""
    return value


def _extract_items(turn: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw_items = turn.get(key)
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise ValueError(f"codex_cloud turn {key!r} must be a list when present")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(
                f"codex_cloud turn {key}[{index}] must be an object"
            )
        items.append(item)
    return items


def _join_content_fragments(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for fragment in content:
        text = _fragment_to_text(fragment)
        if text:
            pieces.append(text)
    return "\n\n".join(pieces)


def _fragment_to_text(fragment: Any) -> str:
    if isinstance(fragment, str):
        return fragment
    if not isinstance(fragment, dict):
        return ""
    text = fragment.get("text")
    if not isinstance(text, str):
        return ""
    content_type = fragment.get("content_type")
    if isinstance(content_type, str) and content_type.lower() not in {
        "text",
        "input_text",
        "output_text",
    }:
        return ""
    return text


def _join_worklog_content(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    pieces: list[str] = []
    for fragment in parts:
        text = _fragment_to_text(fragment)
        if text:
            pieces.append(text)
    return "\n\n".join(pieces)


def _summarize_error(error: dict[str, Any]) -> dict[str, str] | None:
    code_raw = error.get("code")
    message_raw = error.get("message")
    code = code_raw if isinstance(code_raw, str) else ""
    message = message_raw if isinstance(message_raw, str) else ""
    if not code and not message:
        return None
    return {"code": code, "message": message}


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"codex_cloud envelope event_at not ISO-8601: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = [
    "normalize_raw",
    "parse_chunk",
]
