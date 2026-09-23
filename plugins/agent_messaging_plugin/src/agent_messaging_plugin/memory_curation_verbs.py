"""Maintenance-verbs M2.2 — ambient-index curation & placement decay
(the 2026-08-10 maintenance-verbs M2 memory-curation charter draft,
accepted design ``workbench/2026-08-10_m2_memory_curation_design_questions_mverbs-impl.md``).

Two pieces, both pure functions here — the I/O (fetching memory records via
the injected ``memory_service``, dispatching the async job) lives in
``plugin.py``, mirroring ``choreography_verbs.py``'s own split (pure
validation/ranking here, orchestration there):

1. ``slug_to_slot_tag`` / slug resolution support for cite->reinforce: a
   memory's slot tag is ``agent_memory:slot:<origin>:<slug>`` (origin fixed
   as ``claude_code.<solet_name>``, matching the existing hydrate/drain
   convention in ``.claude/hooks/memory_passthrough/*.py`` exactly — this
   module does not invent a new tag shape).
2. ``build_curation_report``: ranks the CALLER-supplied current head lines
   (a platform verb cannot import ``.claude/hooks/memory_passthrough/
   index_render.py`` — that module lives in the local Claude Code CLI's
   filesystem, a different process/package tree entirely; the caller reads
   and splits the head locally and passes the lines as a plain argument)
   against a fact index built from a live ``memory_service`` query, per the
   accepted design's MAX-aggregation-per-line, pin-excludes-from-candidacy
   rules.

Both are ``VerbError``-raising pure functions, no ``AsyncJobManager``/
``state_service``/``memory_service`` awareness — easy to unit-test and
mutation-prove without faking the whole choreography-worker machinery.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from collections.abc import Mapping

ACTION_GENERATE_CURATION_REPORT = "generate_curation_report"

_ORIGIN_AGENT_KIND = "claude_code"
_PINNED_TAG = "agent_memory:pinned"
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")


def slug_to_slot_tag(solet_name: str, slug: str) -> str:
    """The exact slot-tag shape ``.claude/hooks/memory_passthrough`` already
    writes and reads (``agent_memory:slot:<origin>:<slug>``) — single source
    of the tag format, so a future convention change only needs to land
    here, not be re-derived at every call site."""
    return f"agent_memory:slot:{_ORIGIN_AGENT_KIND}.{solet_name}:{slug}"


def origin_tag(solet_name: str) -> str:
    """The origin-scoped tag every ``agent_memory`` record for this
    checkout carries (``agent_memory:origin:<origin>``) — the same tag
    ``export_memories``'s hydrate contract already filters on."""
    return f"agent_memory:origin:{_ORIGIN_AGENT_KIND}.{solet_name}"


def resolve_memory_id_by_slug(
    matches: list[dict[str, Any]], slug: str,
) -> str:
    """Extract the ``id`` from a ``get_memories_by_tag`` result already
    filtered to one slug's slot tag. Raises ``slug_not_found`` on zero
    matches (never silently reinforces nothing) and ``slug_ambiguous`` on
    more than one (the slot tag is meant to be unique per slug — more than
    one match is a data-integrity signal worth surfacing loud, not picking
    one arbitrarily)."""
    if not matches:
        raise VerbError(
            "slug_not_found",
            f"no memory record carries a slot tag for slug {slug!r} — check "
            "the slug matches an actual local memory file's name (no "
            "'.md', no path), and that it has been drained to canonical "
            "at least once.",
        )
    if len(matches) > 1:
        ids = ", ".join(str(m.get("id")) for m in matches)
        raise VerbError(
            "slug_ambiguous",
            f"slug {slug!r} matched {len(matches)} memory records ({ids}) — "
            "the slot tag is expected to be unique per slug; this is a "
            "data-integrity signal, not something to resolve by picking one.",
        )
    memory_id = str(matches[0].get("id") or "")
    if not memory_id:
        raise VerbError(
            "slug_not_found",
            f"the single record matching slug {slug!r} has no usable id.",
        )
    return memory_id


def _parse_head_line_slugs(line: str) -> list[str]:
    """Every ``[label](slug.md)``-style markdown link target in one head
    line, stripped to its slug (filename minus ``.md``). A line with no
    matching link (prose, a section heading) yields an empty list, not an
    error — the caller skips lines with no slugs rather than treating an
    unlinked line as a malformed one."""
    return [target[:-3] for target in _MD_LINK_RE.findall(line) if target.endswith(".md")]


def build_fact_index(
    memory_records: list[dict[str, Any]], solet_name: str,
) -> dict[str, dict[str, Any]]:
    """One ``get_memories_by_tag(origin_tag(...))`` call's worth of records
    -> ``{slug: {"strength": float, "pinned": bool, "memory_id": str}}``.
    Parses each record's OWN tags for its slot tag (never assumes a
    record's ``name``/``id`` is the slug — the slot tag is the one
    authoritative source, exactly as ``export_memories``'s payload already
    carries it) and for the standalone ``agent_memory:pinned`` tag."""
    prefix = f"agent_memory:slot:{_ORIGIN_AGENT_KIND}.{solet_name}:"
    index: dict[str, dict[str, Any]] = {}
    for record in memory_records:
        tags = record.get("tags")
        if not isinstance(tags, list):
            continue
        slug = next(
            (str(t)[len(prefix):] for t in tags if isinstance(t, str) and t.startswith(prefix)),
            None,
        )
        if slug is None:
            continue
        index[slug] = {
            "strength": float(record.get("strength") or 0.0),
            "pinned": _PINNED_TAG in tags,
            "memory_id": str(record.get("id") or ""),
        }
    return index


def _score_head_line(
    line: str, fact_index: Mapping[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """One head line's candidacy verdict — split out of
    :func:`build_curation_report` to keep it a straight-line aggregator
    (radon cc). Returns ``None`` for a line with no slugs at all (prose, a
    heading) or ANY pinned fact (excluded from candidacy entirely — pin =
    exemption from demotion candidacy, never reinforcement, so a pinned
    fact's honest, possibly-low activation never surfaces the line as a
    candidate). Returns ``{"unresolved": True, "line": line}`` when every
    slug is real but none resolves in ``fact_index`` (a dangling reference —
    worth the seat's attention, never silently dropped). Otherwise returns
    the scored candidate: the line's value is the MAX strength among its
    resolved facts, the conservative direction for traps — a line survives
    while ANY backing fact is strong."""
    slugs = _parse_head_line_slugs(line)
    if not slugs:
        return None
    resolved = [fact_index[s] for s in slugs if s in fact_index]
    if not resolved:
        return {"unresolved": True, "line": line}
    if any(f["pinned"] for f in resolved):
        return None
    return {"line": line, "slugs": slugs, "max_strength": max(f["strength"] for f in resolved)}


def build_curation_report(
    head_lines: list[str],
    fact_index: Mapping[str, dict[str, Any]],
    *,
    bottom_n: int,
    byte_budget: int,
    line_budget: int,
) -> dict[str, Any]:
    """The accepted design's ranking rules (per-line verdicts in
    :func:`_score_head_line`) applied across the whole head, plus
    head-pressure stats computed directly from ``head_lines`` (the caller's
    own already-split head, never re-measured from raw text here)."""
    head_bytes = sum(len(line.encode("utf-8")) + 1 for line in head_lines)
    head_line_count = len(head_lines)

    verdicts = [v for v in (_score_head_line(line, fact_index) for line in head_lines) if v]
    unresolved_lines = [v["line"] for v in verdicts if v.get("unresolved")]
    candidates = [v for v in verdicts if "max_strength" in v]
    candidates.sort(key=lambda c: c["max_strength"])

    return {
        "demotion_candidates": candidates[:bottom_n],
        "unresolved_lines": unresolved_lines,
        "head_bytes": head_bytes,
        "head_lines": head_line_count,
        "byte_budget": byte_budget,
        "line_budget": line_budget,
        "over_budget": head_bytes > byte_budget or head_line_count > line_budget,
    }


__all__ = [
    "ACTION_GENERATE_CURATION_REPORT",
    "build_curation_report",
    "build_fact_index",
    "origin_tag",
    "resolve_memory_id_by_slug",
    "slug_to_slot_tag",
]
