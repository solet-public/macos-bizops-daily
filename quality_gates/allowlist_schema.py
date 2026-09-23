#!/usr/bin/env python3
"""Shared tracked-debt allowlist schema: owner, reason, and expires are mandatory.

Ten quality-gate loaders (``flow_probe_registry_gate.py``,
``bundle_license_gate.py``, ``embedding_description_bound_gate.py``,
``dependency_declaration_gate.py``, ``return_shape_gate.py``,
``radon_mi_check.py``, ``whole_tree_integration_gate.py``,
``sql_access_gate.py``, ``god_class_check.py``,
``wint2_vault_key_declaration_check.py``) each parsed their own
``<key>  # <comment>`` allowlist file independently (iss_23fa51b5), and none
of the fifteen text-based allowlists in this directory required an entry to
carry an owner, an expiry, or a reason (iss_9b0ad3d5) -- so a good-reason-once
exemption had no mechanism that ever forced it back into review, and at least
one entry (``MacosVaultPlugin`` / ``SecretsManagerVaultPlugin``, iss_c0db0880)
grew under a later unit instead of shrinking.

This module is the one shared parser every migrated loader now calls. A line
is either a comment/blank (ignored) or a tagged entry:

    <key>  # owner: <id> reason: <free text> expires: <YYYY-MM-DD>

Field order does not matter and ``reason`` may contain spaces; each label is
matched literally and its value runs until the next label or end of line.
``key`` stays a gate-specific literal (a path, a ``path::name`` pair, a
``check_id::scope::specifier`` triple, ...) matched verbatim against the
gate's own findings -- this module never interprets it, preserving the
literal+reason readability iss_ad8731d4 asked for (as opposed to an opaque
hash). There is no "permanent" tier: every entry has a real, finite
``expires`` date, by design -- an allowlist that can silently opt an entry
out of ever re-justifying itself is the ratchet iss_c0db0880 describes.

Construction fails loud, not silently, on any missing or malformed field
(see ``AllowlistEntryError``) -- a loader that would otherwise default a
missing owner/reason/expires to an empty string is exactly the failure mode
this schema exists to close.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_TAG_FIELD_PATTERN = re.compile(
    r"(?P<label>owner|reason|expires):\s*(?P<value>.*?)(?=\s+(?:owner|reason|expires):|\s*$)",
    re.DOTALL,
)
_MANDATORY_FIELDS = ("owner", "reason", "expires")


class AllowlistEntryError(ValueError):
    """A tracked-debt allowlist line is missing, or misformats, a mandatory field."""


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One tracked-debt allowlist entry under the mandatory owner/reason/expires schema."""

    key: str
    owner: str
    reason: str
    expires: date
    source: Path
    lineno: int

    @property
    def is_expired(self) -> bool:
        return date.today() > self.expires


def parse_allowlist_line(raw_line: str, *, source: Path, lineno: int) -> AllowlistEntry | None:
    """Parse one line of a tracked-debt allowlist file.

    Returns ``None`` for a blank line or a full-line ``#`` comment (free-text
    provenance commentary above an entry is untouched -- only the entry line
    itself is validated). Raises ``AllowlistEntryError`` for a non-comment
    line with no ``#`` tag at all, a tag missing ``owner:``, ``reason:``, or
    ``expires:``, an empty field value, or an ``expires:`` value that is not
    an ISO ``YYYY-MM-DD`` date.
    """

    line = raw_line.rstrip("\n")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    key_part, sep, tag_part = line.partition("#")
    key = key_part.strip()
    if not sep:
        raise AllowlistEntryError(
            f"{source}:{lineno}: entry has no '# owner: ... reason: ... expires: ...' tag: {line!r}"
        )
    fields = {
        match.group("label"): match.group("value").strip()
        for match in _TAG_FIELD_PATTERN.finditer(tag_part)
    }
    missing = [name for name in _MANDATORY_FIELDS if not fields.get(name)]
    if missing:
        raise AllowlistEntryError(
            f"{source}:{lineno}: entry missing mandatory field(s) {missing}: {line!r}"
        )
    try:
        expires = date.fromisoformat(fields["expires"])
    except ValueError as exc:
        raise AllowlistEntryError(
            f"{source}:{lineno}: 'expires' is not an ISO date (YYYY-MM-DD): {fields['expires']!r}"
        ) from exc
    return AllowlistEntry(
        key=key,
        owner=fields["owner"],
        reason=fields["reason"],
        expires=expires,
        source=source,
        lineno=lineno,
    )


def load_allowlist_entries(path: Path) -> tuple[AllowlistEntry, ...]:
    """Read every mandatory-tagged entry from `path`, in file order.

    Raises ``FileNotFoundError`` if `path` is missing -- a missing allowlist
    file is a gate mis-configuration, never an implicitly-empty allowlist.
    """

    if not path.exists():
        raise FileNotFoundError(f"allowlist file not found: {path}")
    entries: list[AllowlistEntry] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        entry = parse_allowlist_line(raw_line, source=path, lineno=lineno)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def active_keys(entries: Iterable[AllowlistEntry], *, today: date | None = None) -> frozenset[str]:
    """Keys of entries not yet expired as of `today` (default: ``date.today()``).

    An expired entry drops out of the returned set -- from the gate's
    perspective its finding simply resurfaces, unsuppressed, on the next
    run. That is the actual lifecycle fix for iss_9b0ad3d5 / iss_c0db0880: a
    lapsed exemption stops being honored instead of quietly becoming
    permanent policy.
    """

    cutoff = today if today is not None else date.today()
    return frozenset(entry.key for entry in entries if entry.expires >= cutoff)


def expired_entries(
    entries: Iterable[AllowlistEntry], *, today: date | None = None
) -> tuple[AllowlistEntry, ...]:
    """Entries whose expiry has passed as of `today` -- for a gate's own diagnostic output."""

    cutoff = today if today is not None else date.today()
    return tuple(entry for entry in entries if entry.expires < cutoff)


def describe_expired(entries: Iterable[AllowlistEntry], *, today: date | None = None) -> tuple[str, ...]:
    """Human-readable one-line-per-entry summary of lapsed entries, for a gate to print."""

    return tuple(
        f"EXPIRED {entry.source.name}:{entry.lineno}: {entry.key!r} "
        f"(owner={entry.owner}, expired {entry.expires.isoformat()})"
        for entry in expired_entries(entries, today=today)
    )


def load_allowlist(path: Path, *, today: date | None = None) -> frozenset[str]:
    """Convenience wrapper for a flat string-keyed gate: parse `path` and warn+return only its
    currently-active keys.

    Equivalent to ``active_keys(load_allowlist_entries(path), today=today)``,
    with expired entries also printed to stderr so a lapsed exemption is
    visible at the moment it starts blocking again, not just inferred from a
    new finding appearing with no explanation.
    """

    entries = load_allowlist_entries(path)
    for line in describe_expired(entries, today=today):
        print(line, file=sys.stderr)
    return active_keys(entries, today=today)
