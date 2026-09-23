"""Issue verb implementations — pure functions over a built ``jira.JIRA`` client.

Each function takes an already-built client + a ``params`` dict, and returns a
plain result dict. Business-data limits, 2026-08-03 operator revision
(design doc §0.1/§5.4): jira EXITED the data-export requirement (operator veto — "no PII
in Jira, just company internal accounts") and its "paging must be hidden"
ruling retired the disclosure-only shape jql_search briefly had. jql_search
is now LIMITS-ONLY, g_suite-class: results return INLINE, never to a file, no
containment gate — but it pages INTERNALLY across Atlassian's 100/call
ceiling up to the effective row limit and returns ONE complete result, with
the full acknowledge_default_limit_override/row_limit mechanism (§5) since
internal looping means there IS something for an override to raise (how many
hidden vendor calls happen), unlike the disposition that applied before this
ruling.

Invalid parameters raise ``ValueError`` (mapped to ``jira.invalid_params``);
``jira.JIRAError`` from the client propagates to the plugin's classifier.

Row shapes are FIXED and TRIMMED: ``jql_search`` returns a small, stable row
per issue; ``get_issue`` returns the fuller single-issue view (inline-capable —
operator-confirmed single-record reads are not the mass-exposure risk this
migration targets). Nested Jira objects (status, assignee, reporter) are
flattened None-safely — ``assignee`` is nullable on unassigned issues, so a
bare ``fields['assignee']['displayName']`` would crash.
"""

from __future__ import annotations

from typing import Any

from .constants import (
    DEFAULT_ROW_LIMIT,
    JQL_PAGE_SIZE,
    MAX_INTERNAL_CALLS,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    ROW_LIMIT_CAP,
)

# The render set jql_search always fetches + renders. A caller `fields` list ADDS
# to it (never narrows below it): the render set is unioned in so the returned row
# shape is always fully populated — a caller who passes only unrelated fields never
# gets hollow rows (empty summary/status/assignee/updated). See _fetch_fields.
_DEFAULT_JQL_FIELDS: tuple[str, ...] = ("summary", "status", "assignee", "updated")


def jql_search(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Run a JQL search; return trimmed rows inline, one complete result.

    The effective row limit defaults to DEFAULT_ROW_LIMIT (500); an
    acknowledged override may raise it up to ROW_LIMIT_CAP (5,000). Within
    that limit, this pages internally across Atlassian's 100/call ceiling
    (JQL_PAGE_SIZE) — the caller never sees a continuation token. An optional
    ``max_results`` lets a caller request FEWER than the effective limit in
    this one call; it only narrows, never widens (widening requires the
    override). Beyond the hard cap, the route is narrowing the JQL, not
    resuming a token: ``truncated`` is the honest signal.
    """
    jql = _require_str(params, "jql")
    ceiling = _resolve_effective_limit(params, verb="jql_search")
    target = _clamp_within_ceiling(params.get("max_results"), ceiling)
    fields = _fetch_fields(params.get("fields"))
    rows, total, truncated = _paginate_jql(client, jql, fields, target)
    return {"issues": rows, "total": total, "row_count": len(rows), "truncated": truncated}


def _paginate_jql(
    client: Any, jql: str, fields: list[str], target: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Page internally across Atlassian's 100/call ceiling until ``target``
    rows are collected or the vendor genuinely runs out.

    Returns (rows, total, truncated). ``total`` is exact when not truncated
    (== len(rows), the vendor genuinely had no more); when truncated, it
    comes from a separate ``approximate_issue_count`` call, since
    ``/search/jql`` itself never returns a total. Atlassian removed the
    legacy ``/rest/api/*/search`` endpoint (HTTP 410 since 2026);
    ``enhanced_search_issues`` hits its replacement ``/search/jql``, which is
    Jira-Cloud-only (matches this connector's ratified Cloud-only scope).
    """
    issues: list[dict[str, Any]] = []
    next_page_token: str | None = None
    calls = 0
    while len(issues) < target and calls < MAX_INTERNAL_CALLS:
        page_size = min(JQL_PAGE_SIZE, target - len(issues))
        kwargs: dict[str, Any] = {"maxResults": page_size, "fields": fields, "json_result": True}
        if next_page_token:
            kwargs["nextPageToken"] = next_page_token
        response = client.enhanced_search_issues(jql, **kwargs)
        page_issues = response.get("issues") or []
        issues.extend(_issue_row(item) for item in page_issues)
        calls += 1
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break
    truncated = bool(next_page_token) or len(issues) > target
    rows = issues[:target]
    total = _as_int(client.approximate_issue_count(jql), default=len(rows)) if truncated else len(rows)
    return rows, total, truncated


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


def get_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one issue: summary, description, status, people, labels, attachment meta."""
    key = _require_str(params, "key")
    raw = _resource_raw(client.issue(key))
    fields = raw.get("fields") or {}
    return {
        "key": _as_str(raw.get("key")) or key,
        "summary": _as_str(fields.get("summary")),
        "description": _as_str(fields.get("description")),
        "status": _status_name(fields.get("status")),
        "assignee": _display_name(fields.get("assignee")),
        "reporter": _display_name(fields.get("reporter")),
        "labels": _as_str_list(fields.get("labels")),
        "attachments": _attachment_meta(fields.get("attachment")),
    }


def create_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create an issue in a project of a given type. Extra fields merge over the core three."""
    project = _require_str(params, "project")
    issue_type = _require_str(params, "issue_type")
    summary = _require_str(params, "summary")
    description = _as_str(params.get("description"))
    fields: dict[str, Any] = {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "summary": summary,
    }
    if description:
        fields["description"] = description
    extra = params.get("fields")
    if isinstance(extra, dict):
        fields.update(extra)
    created = client.create_issue(fields=fields)
    return {"key": _as_str(getattr(created, "key", None)), "id": _as_str(getattr(created, "id", None))}


def update_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Apply a non-empty ``fields`` object to an existing issue."""
    key = _require_str(params, "key")
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("'fields' is required and must be a non-empty object")
    client.issue(key).update(fields=fields)
    return {"ok": True}


def delete_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Delete an issue by key (explicit target — RATIFY-2 acceptable-loss class)."""
    key = _require_str(params, "key")
    client.issue(key).delete()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _issue_row(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields") or {}
    return {
        "key": _as_str(item.get("key")),
        "summary": _as_str(fields.get("summary")),
        "status": _status_name(fields.get("status")),
        "assignee": _display_name(fields.get("assignee")),
        "updated": _as_str(fields.get("updated")),
    }


def _attachment_meta(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for att in value:
        if not isinstance(att, dict):
            continue
        out.append(
            {
                "attachment_id": _as_str(att.get("id")),
                "filename": _as_str(att.get("filename")),
                "mime": _as_str(att.get("mimeType")),
                "size": _as_int(att.get("size"), default=0),
            }
        )
    return out


def _status_name(value: Any) -> str | None:
    """Flatten a Jira status object to its name; None if absent."""
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def _display_name(value: Any) -> str | None:
    """Flatten a Jira user object (assignee/reporter) to its displayName; None-safe.

    Assignee is nullable on unassigned issues, so this must tolerate ``None``.
    """
    if isinstance(value, dict):
        name = value.get("displayName")
        return name if isinstance(name, str) else None
    return None


# ---------------------------------------------------------------------------
# Param coercion + resource access
# ---------------------------------------------------------------------------


def _resource_raw(resource: Any) -> dict[str, Any]:
    """Return a pycontribs Resource's ``.raw`` dict (empty dict if unavailable)."""
    raw = getattr(resource, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _fetch_fields(requested: Any) -> list[str]:
    """Fields to fetch: the render set ALWAYS, plus any caller-requested extras.

    The render set is unioned in first so the rendered rows are never hollow, even
    when the caller passes only fields outside the render set. Order preserved:
    render set, then extras (deduplicated).
    """
    fields = list(_DEFAULT_JQL_FIELDS)
    for name in _as_str_list(requested):
        if name not in fields:
            fields.append(name)
    return fields
