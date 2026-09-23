"""Shared primitives for the session-ledger repository.

W5.O cycle 1 (`workbench/2026-06-13_w5o_session_ledger_repository_decomposition_design.md`
§3.10): module-level helpers consumed across multiple domain mixins, lifted
verbatim from the monolithic ``repository.py``.

What lives here:

- **SQL primitives** consumed by Read + Search + Repair domains:

  * :func:`_full` — table-name formatter ``NAMESPACE__table``.

  (The ``_columns_from_select_sql`` / ``_split_select_pieces`` SELECT-column
  parser + its ``_SELECT_FROM_RE`` / ``_TRAILING_AS_RE`` regexes were removed
  when the ledger's last raw-SQL read, ``list_canonical_contributors``, migrated
  onto the typed read seam — SQL-lockdown — retiring ``base._fetch_all``, the
  parser's only consumer.)

- **Sanitization primitives** consumed by Ingest + Annotation + Summarize:

  * :func:`_strip_nuls` — TEXT-write NUL byte sanitization (per
    ``knowledge_bases/ananta_platform/19_session_ledger/02_nul_byte_sanitization_seam.md``,
    pointer updated for cycle 1 W5.O C3 fold).
  * :func:`_strip_nuls_in_json` — JSONB-write NUL sanitization (2026-06-13 Bug C
    amendment).

- **Type/value coercion helpers** consumed by all domains:

  * :func:`_optional_str`, :func:`_coerce_json_dict`, :func:`_new_id`,
    :func:`_row_to_source`.

- **Dataclass type fixtures** consumed by Read + Ingest (W5.O C8 fold):

  * :class:`SourceRow`, :class:`SessionRow`.

What does NOT live here: the per-domain verb implementations (mixin modules,
cycles 2-10), the diamond-root base class (:mod:`base`), or the per-event-type
shape validators (those go into :mod:`ingest` in cycle 3 since the only
consumer is :func:`SessionLedgerIngestMixin.append_event`).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from ananta.llm.session_ledger.schema import (
    NAMESPACE,
)
from ananta.llm.session_ledger.types import (
    IngestSourceKind,
    SourceVendor,
)

# Module-level RELOAD_SAFE marker — pure helpers, no module-level mutable
# state, no held service references.
RELOAD_SAFE = True


# Underscore-prefixed names indicate "private to the package" rather than
# "private to this module"; the package's other modules (repository.py +
# every mixin file in cycles 2-10) import them by name. The explicit
# ``__all__`` block tells pyright + readers which symbols form that
# package-internal export surface.
__all__ = [
    "RELOAD_SAFE",
    "SessionRow",
    "SourceRow",
    "_as_aware_utc",
    "_coerce_json_dict",
    "_full",
    "_new_id",
    "_optional_str",
    "_row_to_source",
    "_strip_nuls",
    "_strip_nuls_in_json",
    "derive_event_external_id",
]

# ─── Idempotent-ingest event external_id derivation (GAP-5) ────────────────
# The stable per-event idempotency key for the ``(session_id, external_id)``
# upsert. Used by BOTH the live importer and the one-time backfill — they MUST
# derive identically or re-ingest stops deduping, so this is the single home.

_EXTERNAL_ID_DERIVED_PREFIX = "derv:"
# Hex chars of the sha256 kept (128-bit). Collisions only matter WITHIN a
# session's event set (the conflict scope), so this is astronomically safe; the
# ``(session_id, sequence)`` unique is a further backstop.
_EXTERNAL_ID_HASH_LEN = 32


def _canonical_event_at(event_at: datetime) -> str:
    """Naive-UTC ISO form of an event time, matching what the write path stores.

    The ledger stores ``event_at`` as naive-UTC (``_naive_utc``, the F1 seam).
    The live importer (event time possibly tz-aware) and the slice-2 backfill
    (naive, read back from the DB) MUST canonicalize IDENTICALLY here, else their
    derived ``external_id``s diverge and re-ingest no longer dedups. (Inlined
    rather than importing ``base._naive_utc`` to avoid a base↔shared cycle.)
    """
    naive = (
        event_at.astimezone(UTC).replace(tzinfo=None)
        if event_at.tzinfo is not None
        else event_at
    )
    return naive.isoformat()


def derive_event_external_id(
    *,
    vendor_event_id: str | None,
    session_id: str,
    event_type: str,
    role: str | None,
    content_key: str,
    event_at: datetime,
    ordinal: int,
) -> str:
    """The event's NON-NULL idempotency key for the ``(session_id, external_id)`` upsert.

    ``vendor_event_id`` verbatim when present (~90% of events — exact + stable).
    Otherwise a deterministic, NUL-safe ``derv:``-prefixed sha256 over the
    event's natural identity plus the source-order OCCURRENCE ``ordinal`` (the
    0-based k-th identical ``(event_type, role, content_key, event_at)`` event in
    the session stream). Best-effort for null-vendor events (operator-ruled): it
    dedups when the source replays in order; reorder/drop diverges the ordinal →
    some duplicates, accepted as regenerable. NEVER returns null.
    """
    if vendor_event_id is not None:
        return vendor_event_id
    payload = json.dumps(
        [
            session_id,
            event_type,
            role,
            _strip_nuls(content_key),
            _canonical_event_at(event_at),
            ordinal,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_EXTERNAL_ID_DERIVED_PREFIX}{digest[:_EXTERNAL_ID_HASH_LEN]}"


def _full(table: str) -> str:
    """Return the fully-qualified ``NAMESPACE__table`` form for a bare name."""
    return f"{NAMESPACE}__{table}"


# ─── Type fixtures (dataclass module-level declarations) ──────────────────


@dataclass(frozen=True, slots=True)
class SourceRow:
    id: str
    source_kind: IngestSourceKind
    root_uri: str
    account_label: str | None
    enabled: bool
    config_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: str
    source_id: str
    external_session_id: str
    vendor: SourceVendor
    vendor_session_label: str | None
    project_path: str | None
    first_event_at: datetime
    last_event_at: datetime
    event_count: int


# ─── Type/value coercion + ID minting ─────────────────────────────────────


def _row_to_source(row: Mapping[str, object]) -> SourceRow:
    return SourceRow(
        id=str(row["id"]),
        source_kind=IngestSourceKind(str(row["source_kind"])),
        root_uri=str(row["root_uri"]),
        account_label=_optional_str(row.get("account_label")),
        enabled=bool(row.get("enabled", True)),
        config_json=_coerce_json_dict(row.get("config_json")) or {},
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_json_dict(value: object) -> dict[str, Any] | None:
    from ananta.llm.session_ledger.base import LedgerRepositoryError

    if value is None:
        return None
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if isinstance(value, str):
        if not value:
            return None
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return cast(dict[str, Any], loaded)
        raise LedgerRepositoryError(f"expected JSON object, got {type(loaded).__name__}")
    raise LedgerRepositoryError(f"cannot coerce {type(value).__name__} to JSON dict")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _as_aware_utc(value: object) -> datetime:
    """Coerce a state-primitive timestamp cell to an aware-UTC ``datetime``.

    The ledger stores timestamps as Postgres ``TIMESTAMP`` (no tz) holding the
    naive-UTC wall-clock (the 2026-06-12 F1 TZ-storage seam), so the typed-txn
    ``query_state`` read path surfaces them as **naive** datetimes (or naive ISO
    strings via ``provider._serialize_for_json``); an aware ``datetime`` can
    still arrive from an in-memory test double on a ``timestamptz`` column. All
    normalize to an **aware UTC** ``datetime`` — naive cells are taken as UTC —
    so a Python ``min``/``max`` against a vendor-supplied event time (the
    read-compute-write replacements for SQL ``LEAST``/``GREATEST``) never mixes
    naive and aware operands and is instant-correct rather than lexicographic.
    Fail fast on any other cell type — a non-timestamp here is an upstream
    contract violation, not something to silently coerce.

    (``inverted_bounds_repair._as_dt`` is an identical helper landed in the 3b
    slice; a future cleanup can dedupe both onto this canonical home.)
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise TypeError(
            f"expected a datetime or ISO-8601 string timestamp cell, got "
            f"{type(value).__name__!r}",
        )
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# ─── NUL-byte sanitization (TEXT + JSONB write seams) ─────────────────────


def _strip_nuls(value: str | None) -> str | None:
    """Remove NUL (0x00) bytes from text bound for a Postgres TEXT column.

    Postgres TEXT columns reject embedded NUL bytes ('\\x00'), raising
    ``psycopg.DataError`` at write time and aborting the whole import
    batch. Some LLM session JSONL emitters (claude_code in particular)
    carry NULs in tool output / system recap text — typically encoding
    artifacts from binary blobs decoded as UTF-8 by upstream tooling,
    not malicious content.

    Operator ruling 2026-06-01 ratified option B (shared seam at the
    repository write boundary). The companion ``_strip_nuls_in_json``
    extends the same invariant to JSONB-bound payloads — see that
    docstring for the empirical reason the original ratification's
    "JSONB tolerates NUL via \\u0000" claim was wrong.

    Lossy: a NUL-only string becomes the empty string. Repository
    validators are content-presence-aware, not non-empty-only, so this
    round-trips cleanly.
    """
    if value is None:
        return None
    if "\x00" not in value:
        return value
    return value.replace("\x00", "")


def _strip_nuls_in_json(value: Any) -> Any:
    """Recursively strip NUL bytes from string values inside a JSON-bound payload.

    Postgres rejects ``\\u0000`` Unicode escapes inside JSONB strings at
    parse time (error 22P05 ``untranslatable_character``), even though
    the JSON spec permits them and ``json.dumps`` faithfully emits the
    6-char escape for any Python string containing ``\\x00``. The
    2026-06-01 TEXT-only sanitization seam was ratified on the
    (empirically false) premise that JSONB tolerates the round-trip;
    Bug C 2026-06-13 surfaced the gap when a codex tool-output capture
    routed a NUL-bearing payload through ``content_json`` and the
    INSERT aborted the whole ``poll_once`` call.

    Symmetric with ``_strip_nuls``: applied at the repository write
    boundary so the asymmetry between TEXT and JSONB does not leak into
    vendor-shape code. Walks dicts, lists, and tuples recursively;
    non-string scalar values (bool, int, float, None) pass through
    untouched.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: _strip_nuls_in_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_strip_nuls_in_json(item) for item in value]
    return value
