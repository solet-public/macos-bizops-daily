"""Shared result constructors for the report-only doctor advisory families.

``doctor_vintage_census`` and ``doctor_residue_census`` answer different
questions -- one compares manager and target vintage, the other names
host-side residue that no writer corrects -- but they must speak the same
result shape, because a reader of ``doctor``'s ``advisories`` list cannot tell
which module produced any given entry.

The tri-state is the point.  ``verified`` and ``warn`` are claims; ``unknown``
is the explicit refusal to make one when the evidence could not be read.
Collapsing ``unknown`` into either of the others is the unknown-reads-as-false
defect that several of the register rows these advisories detect are made of,
so the constructor for it is a peer of the other two rather than an
afterthought.
"""

from __future__ import annotations

from .models import JsonValue


def verified(
    check_id: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    """The advisory looked, and the state it names is correct."""

    return result(check_id, "verified", summary, expected, observed, source, None, None)


def warn(
    check_id: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
    reason_code: str,
    repair: str,
) -> dict[str, JsonValue]:
    """The advisory looked, and the state it names is wrong."""

    return result(check_id, "warn", summary, expected, observed, source, reason_code, repair)


def unknown(
    check_id: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
    reason_code: str,
    detail: str,
) -> dict[str, JsonValue]:
    """The advisory could not read its evidence and declines to claim either way."""

    return result(check_id, "unknown", summary, expected, observed, source, reason_code, detail)


def result(
    check_id: str,
    status: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
    reason_code: str | None,
    repair: str | None,
) -> dict[str, JsonValue]:
    """Build one advisory entry.

    ``blocking`` is always ``False``: an advisory reports, it never refuses an
    otherwise valid doctor result.  Anything that must refuse belongs in the
    target's pinned completion contract instead.
    """

    return {
        "check_id": check_id,
        "status": status,
        "summary": summary,
        "expected": expected,
        "observed": observed,
        "source": source,
        "reason_code": reason_code,
        "repair": repair,
        "blocking": False,
    }
