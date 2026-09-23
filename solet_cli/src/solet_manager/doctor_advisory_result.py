"""Shared constructors for report-only doctor advisories.

``doctor_vintage_census.py`` grew an equivalent set of these constructors as
module-private functions (``_verified`` / ``_warn`` / ``_unknown`` / ``_result``).
A second advisory module cannot reach those without a private cross-module
import, which this repository has explicitly scoured for, so the shape is
published here once instead of being forked silently.

Unifying the census onto these public constructors is a deliberate follow-on,
not an oversight: that edit touches a file a sibling doctor-coverage lane is
concurrently extending, so it is left to whichever lane lands second rather
than raced here.

The emitted mapping is byte-compatible with the census's own ``_result``:
same keys, same ordering-independent content, ``blocking`` always ``False``.
An advisory reports skew; it never refuses an otherwise valid doctor result.
"""

from __future__ import annotations

from .models import JsonValue

ADVISORY_VERIFIED = "verified"
ADVISORY_WARN = "warn"
ADVISORY_UNKNOWN = "unknown"


def advisory_verified(
    check_id: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    """An advisory whose observed state matches what the manager expected."""

    return _advisory(check_id, ADVISORY_VERIFIED, summary, expected, observed, source, None, None)


def advisory_warn(
    check_id: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
    reason_code: str,
    repair: str,
) -> dict[str, JsonValue]:
    """An advisory that names a specific measured divergence and its repair."""

    return _advisory(
        check_id, ADVISORY_WARN, summary, expected, observed, source, reason_code, repair
    )


def advisory_unknown(
    check_id: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
    reason_code: str,
    detail: str,
) -> dict[str, JsonValue]:
    """An advisory that could not read the state it grades.

    Distinct from ``verified`` on purpose: an unreadable source is not a
    passing source, and collapsing the two is the false-green shape this
    campaign exists to remove.
    """

    return _advisory(
        check_id, ADVISORY_UNKNOWN, summary, expected, observed, source, reason_code, detail
    )


def _advisory(
    check_id: str,
    status: str,
    summary: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
    reason_code: str | None,
    repair: str | None,
) -> dict[str, JsonValue]:
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
