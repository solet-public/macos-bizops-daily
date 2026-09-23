"""Comment + transition verb implementations — pure functions over a JIRA client.

Same shape as issue_actions: take an already-built ``jira.JIRA`` client + a
``params`` dict, return plain result dicts, ``raise ValueError`` on bad params.
Nested Jira objects (comment author, transition target status) are flattened
None-safely.

Business-data limits, 2026-08-03 operator revision (design doc §0.1/§5.4):
jira EXITED the data-export requirement (operator veto — "no PII in Jira, just company
internal accounts") and its "paging must be hidden" ruling retired the
disclosure-only shape ``list_comments`` briefly had. ``list_comments`` is now
LIMITS-ONLY, g_suite-class: results return INLINE, never to a file, no
containment gate — but it pages INTERNALLY by offset (``start_at``, no
continuation token exists on this endpoint) across Atlassian's 100/call
ceiling up to the effective row limit and returns ONE complete result, with
the full acknowledge_default_limit_override/row_limit mechanism (§5).
"""

from __future__ import annotations

from typing import Any

from .constants import (
    COMMENTS_PAGE_SIZE,
    DEFAULT_ROW_LIMIT,
    MAX_INTERNAL_CALLS,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    ROW_LIMIT_CAP,
)


def add_comment(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Add a plain-text comment to an issue; return the new comment id."""
    key = _require_str(params, "key")
    body = _require_str(params, "body")
    comment = client.add_comment(key, body)
    return {"comment_id": _as_str(getattr(comment, "id", None))}


def list_comments(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List an issue's comments; return them inline, one complete result.

    The effective row limit defaults to DEFAULT_ROW_LIMIT (500); an
    acknowledged override may raise it up to ROW_LIMIT_CAP (5,000). Within
    that limit, this pages internally by offset across Atlassian's 100/call
    ceiling (COMMENTS_PAGE_SIZE) — the caller never sees ``start_at``. An
    optional ``max`` lets a caller request FEWER than the effective limit in
    this one call; it only narrows, never widens (widening requires the
    override).
    """
    key = _require_str(params, "key")
    ceiling = _resolve_effective_limit(params, verb="list_comments")
    target = _clamp_within_ceiling(params.get("max"), ceiling)
    rows, truncated = _paginate_comments(client, key, target)
    return {"comments": rows, "row_count": len(rows), "truncated": truncated}


def _paginate_comments(client: Any, key: str, target: int) -> tuple[list[dict[str, Any]], bool]:
    """Page internally by offset until ``target`` rows are collected or a
    short page signals end-of-data (this endpoint has no continuation token,
    unlike jql_search's ``nextPageToken`` — a page shorter than requested is
    the vendor's own end-of-data signal, same discriminator as zuora's
    ``_paginate_account_list``).

    Returns (rows, truncated). ``truncated`` is False ONLY on an explicit
    short-page signal (genuinely no more comments exist) — reaching
    ``target`` via full pages, or tripping the circuit breaker before
    reaching it, are both "we did not confirm this is everything" and
    report True.
    """
    rows: list[dict[str, Any]] = []
    start_at = 0
    calls = 0
    while len(rows) < target and calls < MAX_INTERNAL_CALLS:
        page_size = min(COMMENTS_PAGE_SIZE, target - len(rows))
        comments = client.comments(key, start_at=start_at, max_results=page_size)
        page_rows = [_comment_row(c) for c in _as_list(comments)]
        rows.extend(page_rows)
        start_at += len(page_rows)
        calls += 1
        if len(page_rows) < page_size:
            # A short page is the vendor's own end-of-data signal.
            return rows[:target], False
    return rows[:target], True


def _resolve_effective_limit(params: dict[str, Any], *, verb: str) -> int:
    """Resolve the effective row limit from the §5 override pair.

    Absent (or ``acknowledge_default_limit_override`` not exactly ``True``)
    with no ``row_limit``: returns DEFAULT_ROW_LIMIT. Both must be given
    together — the override flag alone, or ``row_limit`` alone, fails loud
    rather than silently honoring half. A ``row_limit`` above ROW_LIMIT_CAP
    is refused, never silently clamped back down.
    """
    override = params.get(PARAM_ACKNOWLEDGE_OVERRIDE)
    row_limit = params.get(PARAM_ROW_LIMIT)
    override_present = override is True
    row_limit_present = row_limit is not None
    if override_present != row_limit_present:
        raise ValueError(
            f"{verb}: '{PARAM_ACKNOWLEDGE_OVERRIDE}' and '{PARAM_ROW_LIMIT}' must be "
            f"given together — got {PARAM_ACKNOWLEDGE_OVERRIDE}={override!r}, "
            f"{PARAM_ROW_LIMIT}={row_limit!r}"
        )
    if not override_present:
        return DEFAULT_ROW_LIMIT
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise ValueError(f"{verb}: '{PARAM_ROW_LIMIT}' must be a positive integer")
    if row_limit > ROW_LIMIT_CAP:
        raise ValueError(
            f"{verb}: '{PARAM_ROW_LIMIT}'={row_limit} exceeds the hard cap of "
            f"{ROW_LIMIT_CAP}; refusing rather than silently clamping"
        )
    return row_limit


def _clamp_within_ceiling(value: Any, ceiling: int) -> int:
    """A caller-requested per-call target, narrowed to at most the ceiling.

    Never widens the ceiling — that is the override's job, resolved before
    this is called. An absent/invalid value falls back to the ceiling itself.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return ceiling
    return min(ceiling, value)


def list_transitions(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List the transitions available from the issue's current status."""
    key = _require_str(params, "key")
    transitions = client.transitions(key)
    rows = [_transition_row(t) for t in _as_list(transitions) if isinstance(t, dict)]
    return {"transitions": rows}


def transition_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Move an issue through a transition (explicit id), optionally with a comment."""
    key = _require_str(params, "key")
    transition_id = _require_str(params, "transition_id")
    comment = _as_str(params.get("comment")) or None
    client.transition_issue(key, transition_id, comment=comment)
    raw = _resource_raw(client.issue(key))
    new_status = _status_name((raw.get("fields") or {}).get("status"))
    return {"ok": True, "new_status": new_status}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _comment_row(comment: Any) -> dict[str, Any]:
    raw = _resource_raw(comment)
    return {
        "id": _as_str(raw.get("id")),
        "author": _display_name(raw.get("author")),
        "body": _as_str(raw.get("body")),
        "created": _as_str(raw.get("created")),
    }


def _transition_row(transition: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _as_str(transition.get("id")),
        "name": _as_str(transition.get("name")),
        "to_status": _status_name(transition.get("to")),
    }


def _status_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def _display_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("displayName")
        return name if isinstance(name, str) else None
    return None


# ---------------------------------------------------------------------------
# Param coercion + resource access
# ---------------------------------------------------------------------------


def _resource_raw(resource: Any) -> dict[str, Any]:
    raw = getattr(resource, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
