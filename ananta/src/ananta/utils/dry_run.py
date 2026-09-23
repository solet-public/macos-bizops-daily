"""Fail-safe normalization for public destructive-process ``dry_run`` flags."""

from __future__ import annotations


def coerce_dry_run(raw: object) -> bool:
    """Return a safe ``dry_run`` value from a public process parameter.

    Omission and ``None`` are report-only. JSON booleans retain their value;
    bridge strings are accepted only for their spelled boolean values. Any
    other value fails before a destructive engine can apply a transition.
    """
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError("dry_run must be a boolean or the string 'true' or 'false'")
