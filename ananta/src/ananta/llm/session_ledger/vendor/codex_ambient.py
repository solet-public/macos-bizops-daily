"""Codex `~/.codex/ambient-suggestions/<sha>/ambient-suggestions.json` parser (M12).

Spec §17.3 M12 / architect v2 §6.3. One subdirectory per project (the
40-hex-char SHA = sha1(projectRoot)), each holding a single
``ambient-suggestions.json`` file. File shape (empirically confirmed
against operator's filesystem 2026-06-11):

::

    {
      "projectRoot": "/Users/alice/Workspace/my-project",
      "generatedAtMs": 1780799342996,  # epoch milliseconds
      "currentSuggestionIds": [<uuid>, <uuid>, <uuid>],
      "suggestions": [
        {
          "id": <uuid>,
          "title": <str>,
          "description": <str>,
          "prompt": <str>,
          "appIds": [<str>, ...],
          "status": <str>,
          "createdAtMs": <int>,
          "updatedAtMs": <int>
        },
        ...
      ]
    }

**Probe outcomes (carried over from §0.0 + §1.2 + §1.2.5 of dispatch):**

* **Timestamp unit (§1.2):** ``generatedAtMs`` and per-suggestion
  ``createdAtMs`` / ``updatedAtMs`` are MILLISECONDS. Empirically verified
  2026-06-11: raw value ``1780799342996`` → ``2026-06-07 02:29:02 UTC``
  when divided by 1000; treating as seconds overflows (year 58401). The
  ``_Ms`` suffix is honest (the values are milliseconds).

* **Write pattern (§1.2.5):** INDETERMINATE via canonical-source probe.
  Searched ``~/Workspace/codex-rs-wake-0.138.0/codex-rs/`` (and 0.137.0)
  for ``ambient-suggestions`` / ``AmbientSuggestion`` write site —
  zero hits. The persistence layer for ambient-suggestions does NOT live
  in the codex-rs runtime tree (which owns memories.rs / goals.rs /
  threads.rs for M11 / M19 / M20). It is likely in the JS-side codex
  package which is not mirrored locally. **Implementation fallback:**
  defensive parser — catch ``json.JSONDecodeError`` at
  read-time, log INFO ("likely mid-write; retry next poll"), DO NOT
  raise. On next poll mtime will have advanced and the content should
  be complete. If the same offset's mid-write content persists across
  N consecutive polls that's a genuine corruption signal; out of scope
  for M12, raise to operator visibility via WARNING.

**Per-suggestion content_json shape** (the source plugin's ``normalize``
lifts these into the SYSTEM event's content_json):

::

    {
      "subtype": "codex_ambient_suggestion",
      "id": <uuid>,
      "title": <str>,
      "description": <str>,
      "prompt": <str>,
      "appIds": [<str>, ...],
      "status": <str>,
      "createdAtMs": <int>,        # preserved as ms; downstream consumers
                                   # divide by 1000 if seconds are needed
      "updatedAtMs": <int>,
      "is_current": <bool>         # denormalized membership in the
                                   # file's currentSuggestionIds[] —
                                   # makes SQL filtering convenient
    }

Failure policy (KB "Critical Development Guidelines v2", with the documented write-pattern
exception): unrecognized shape (missing required field, wrong type)
raises ``ValueError``. ``JSONDecodeError`` on the file is treated as
transient mid-write (returns empty result) so the importer-side per-
session catch doesn't mark the session FAILED on a benign race.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Payload key sentinel — the source plugin's ``normalize`` reads this.
PAYLOAD_KIND_SUGGESTION = "codex_ambient_suggestion"
SUBTYPE_AMBIENT_SUGGESTION = "codex_ambient_suggestion"


@dataclass(frozen=True, slots=True)
class _ParsedSuggestion:
    """One suggestion from the file, ready to become a ``RawSessionEvent`` payload."""

    suggestion_id: str
    title: str
    description: str
    prompt: str
    app_ids: tuple[str, ...]
    status: str
    created_at_ms: int
    updated_at_ms: int
    is_current: bool
    event_at: datetime  # derived from file-level generatedAtMs (UTC)


@dataclass(frozen=True, slots=True)
class _ParsedFile:
    """The whole ``ambient-suggestions.json`` file, parsed.

    ``generated_at`` is the file-level write timestamp; carried separately
    from per-suggestion ``createdAtMs`` / ``updatedAtMs`` because the
    SYSTEM event's ``event_at`` uses the file-write time (whole-file
    replacement pattern) per spec §17.3.
    """

    project_root: str
    generated_at: datetime
    suggestions: tuple[_ParsedSuggestion, ...]


def _require_str(obj: dict[str, object], field: str, path: Path) -> str:
    value = obj.get(field)
    if not isinstance(value, str):
        raise ValueError(
            f"codex ambient: {path}: missing/invalid {field!r} string field",
        )
    return value


def _require_int(obj: dict[str, object], field: str, path: Path) -> int:
    value = obj.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"codex ambient: {path}: missing/invalid {field!r} integer field",
        )
    return value


def _coerce_str_list(value: object, field: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(
            f"codex ambient: {path}: {field!r} must be a list, got {type(value).__name__}",
        )
    return tuple(str(item) for item in value)


def _parse_suggestion(
    obj: dict[str, object],
    path: Path,
    *,
    current_ids: frozenset[str],
    generated_at: datetime,
) -> _ParsedSuggestion:
    sid = _require_str(obj, "id", path)
    title = _require_str(obj, "title", path)
    description = _require_str(obj, "description", path)
    prompt = _require_str(obj, "prompt", path)
    app_ids = _coerce_str_list(obj.get("appIds"), "appIds", path)
    status = _require_str(obj, "status", path)
    created_at_ms = _require_int(obj, "createdAtMs", path)
    updated_at_ms = _require_int(obj, "updatedAtMs", path)
    return _ParsedSuggestion(
        suggestion_id=sid,
        title=title,
        description=description,
        prompt=prompt,
        app_ids=app_ids,
        status=status,
        created_at_ms=created_at_ms,
        updated_at_ms=updated_at_ms,
        is_current=sid in current_ids,
        event_at=generated_at,
    )


def parse_ambient_file(path: Path) -> _ParsedFile | None:
    """Parse one ``ambient-suggestions.json`` file.

    Returns ``None`` when the file is in a transient mid-write state
    (``JSONDecodeError``) — the source plugin treats this as "retry
    next poll" per the §1.2.5 fallback. Returns the parsed file on
    success. Raises ``ValueError`` on genuine shape violations.
    """
    try:
        decoded = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"codex ambient: cannot read {path}: {exc}") from exc
    try:
        obj = json.loads(decoded)
    except json.JSONDecodeError as exc:
        # §1.2.5 fallback: write-pattern indeterminate; assume transient.
        logger.info(
            "codex ambient: JSON decode failure at %s (%s); treating as mid-write, "
            "will retry on next poll",
            path, exc,
        )
        return None
    if not isinstance(obj, dict):
        raise ValueError(
            f"codex ambient: {path}: top-level value is not a dict "
            f"({type(obj).__name__})",
        )
    project_root = _require_str(obj, "projectRoot", path)
    generated_at_ms = _require_int(obj, "generatedAtMs", path)
    generated_at = datetime.fromtimestamp(generated_at_ms / 1000, tz=UTC)
    current_raw = obj.get("currentSuggestionIds")
    current_ids = frozenset(_coerce_str_list(current_raw, "currentSuggestionIds", path))
    suggestions_raw = obj.get("suggestions")
    if not isinstance(suggestions_raw, list):
        raise ValueError(
            f"codex ambient: {path}: 'suggestions' must be a list, "
            f"got {type(suggestions_raw).__name__}",
        )
    parsed_suggestions: list[_ParsedSuggestion] = []
    for entry in suggestions_raw:
        if not isinstance(entry, dict):
            raise ValueError(
                f"codex ambient: {path}: suggestions[] entry is not a dict "
                f"({type(entry).__name__})",
            )
        parsed_suggestions.append(
            _parse_suggestion(
                entry,
                path,
                current_ids=current_ids,
                generated_at=generated_at,
            )
        )
    return _ParsedFile(
        project_root=project_root,
        generated_at=generated_at,
        suggestions=tuple(parsed_suggestions),
    )


def iter_suggestion_payloads(
    parsed: _ParsedFile,
) -> Iterable[dict[str, object]]:
    """Yield one RawSessionEvent payload dict per suggestion."""
    for s in parsed.suggestions:
        yield {
            "kind": PAYLOAD_KIND_SUGGESTION,
            "id": s.suggestion_id,
            "title": s.title,
            "description": s.description,
            "prompt": s.prompt,
            "appIds": list(s.app_ids),
            "status": s.status,
            "createdAtMs": s.created_at_ms,
            "updatedAtMs": s.updated_at_ms,
            "is_current": s.is_current,
            "event_at": s.event_at,
        }


__all__ = [
    "PAYLOAD_KIND_SUGGESTION",
    "SUBTYPE_AMBIENT_SUGGESTION",
    "iter_suggestion_payloads",
    "parse_ambient_file",
]
