#!/usr/bin/env python3
"""Two-tier MEMORY.md index rendering, shared by hydrate and the standalone tool.

The index has two budgets, not one. The harness reads MEMORY.md into every
session and nags on both:

  * bytes — "approaching the 24.4 KB read limit; compact to under 17.1 KB"
  * lines — "MEMORY.md is 186 lines, approaching the 200-line read limit;
    compact it to under 140 lines"

A flat one-line-per-fact index fails the line budget no matter how short the
lines are: 171 facts is 171 lines. Only a GROUPED index (several facts per
line) satisfies both. That is why this module packs entries per line and
derives the packing from the line budget rather than picking a fixed layout.

Two tiers, because they answer different questions:

  * HEAD — curated by a human or an agent, preserved VERBATIM. It records
    judgment that is not present in any file: which lanes have an open next
    action, and what that action is. No generator can derive that from
    frontmatter, so no generator may overwrite it.
  * TAIL — generated from frontmatter. It answers "what facts exist" and is
    regenerated in full on every run.

The head ends at :data:`GENERATED_MARKER`. Text above the marker is copied
through untouched; everything below it belongs to this module.

Budget policy is DECLARED, never silent. When the tail does not fit, entries
are shortened first and dropped only as a last resort, and the rendered output
always states how many facts it indexed and how many it omitted. An index that
silently truncates reads as complete when it is not.

Stdlib-only: the hydrate path runs this via Bash, outside the venv.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# The head/tail boundary. Text at or above this line is never regenerated.
GENERATED_MARKER: Final[str] = (
    "<!-- MEMORY:GENERATED — everything below is rebuilt by "
    "index_render.py; edit the per-fact files, not this section -->"
)

# The harness nags at "under 17.1 KB" and "under 140 lines". Render below both
# rather than at them: landing exactly on a limit means the next fact added
# re-trips the nag, which is the treadmill this renderer exists to end.
DEFAULT_BYTE_BUDGET: Final[int] = 17_000
DEFAULT_LINE_BUDGET: Final[int] = 132

# Longest-first: the first hook length whose render fits both budgets wins.
_HOOK_TIERS: Final[tuple[int, ...]] = (96, 72, 52, 36, 24, 0)

# Entries per line, loosest first. Packing is the LINE lever: merging entries
# removes one "- " prefix and one newline each, so it buys lines cheaply and
# bytes barely. Capped where a line stops being scannable.
_PACKING_STEPS: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 6, 8, 10, 12)

# Most-actionable first. A fact whose type is unknown sorts last but is never
# dropped ahead of a known type.
_KIND_ORDER: Final[tuple[str, ...]] = ("project", "feedback", "reference", "user")
_KIND_FALLBACK: Final[str] = "other"

_SEPARATOR: Final[str] = " · "


class IndexRenderError(RuntimeError):
    """The index cannot be rendered — abort loud rather than half-write."""


@dataclass(frozen=True)
class Fact:
    """One per-fact memory file, reduced to what the index needs."""

    filename: str
    name: str
    description: str
    kind: str


@dataclass(frozen=True)
class BudgetReport:
    """What the render actually did, so the caller can state it plainly."""

    indexed: int
    omitted: int
    hook_chars: int
    per_line: int
    total_bytes: int
    total_lines: int
    omitted_kinds: tuple[str, ...]

    def fits(self, byte_budget: int, line_budget: int) -> bool:
        return self.total_bytes <= byte_budget and self.total_lines <= line_budget


def parse_frontmatter(content: str, source: str) -> tuple[str, str, str]:
    """Return ``(name, description, kind)`` from a record's leading ``---`` block.

    Fail loud when the block is absent or carries no ``name``: a fact that
    cannot be identified must not be silently skipped, because a skipped fact
    is indistinguishable from a fact that does not exist.
    """
    if not content.startswith("---"):
        raise IndexRenderError(f"{source}: content has no frontmatter block")
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise IndexRenderError(f"{source}: unterminated frontmatter block")
    name = ""
    description = ""
    kind = ""
    for raw in parts[1].splitlines():
        stripped = raw.strip()
        if stripped.startswith("name:"):
            name = stripped[len("name:"):].strip()
        elif stripped.startswith("description:"):
            description = stripped[len("description:"):].strip().strip('"')
        elif stripped.startswith("type:"):
            kind = stripped[len("type:"):].strip()
    if not name:
        raise IndexRenderError(f"{source}: frontmatter missing required 'name'")
    return name, description, kind or _KIND_FALLBACK


def collect_facts(memory_dir: Path) -> list[Fact]:
    """Read every per-fact ``.md`` in ``memory_dir`` (MEMORY.md itself excluded)."""
    facts: list[Fact] = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        content = path.read_text(encoding="utf-8")
        name, description, kind = parse_frontmatter(content, path.name)
        facts.append(
            Fact(filename=path.name, name=name, description=description, kind=kind),
        )
    return facts


def split_head(existing: str) -> str:
    """Return the curated head of an existing index, without the marker line.

    A file with no marker is treated as ALL head. That is the safe direction:
    adopting this renderer never silently discards curation that predates it.
    The caller decides where to place the marker on first adoption.
    """
    if GENERATED_MARKER not in existing:
        return existing.rstrip("\n")
    return existing.split(GENERATED_MARKER, 1)[0].rstrip("\n")


def _shorten(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars on a word boundary where one is close."""
    collapsed = " ".join(text.split())
    if limit <= 0 or len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit].rstrip()
    space = cut.rfind(" ")
    if space >= limit - 12:
        cut = cut[:space].rstrip()
    return f"{cut}…"


def _entry(fact: Fact, hook_chars: int) -> str:
    """Render one entry.

    The link target is the filename and the link TEXT is the hook, so each
    filename is paid exactly once. Emitting both a name and a filename per
    entry costs roughly 8.6 KB across this store for no added information.

    At ``hook_chars == 0`` the entry degrades to a bare filename rather than a
    link with empty text, which stays readable and still pays the slug once.
    """
    if hook_chars <= 0:
        return f"`{fact.filename}`"
    hook = _shorten(fact.description or fact.name, hook_chars)
    return f"[{hook}]({fact.filename})"


def _group(facts: list[Fact]) -> list[tuple[str, list[Fact]]]:
    """Group facts by kind in _KIND_ORDER, unknown kinds last, stable by name."""
    buckets: dict[str, list[Fact]] = {}
    for fact in facts:
        buckets.setdefault(fact.kind, []).append(fact)
    ordered: list[tuple[str, list[Fact]]] = []
    for kind in _KIND_ORDER:
        if kind in buckets:
            ordered.append((kind, sorted(buckets.pop(kind), key=lambda f: f.filename)))
    for kind in sorted(buckets):
        ordered.append((kind, sorted(buckets[kind], key=lambda f: f.filename)))
    return ordered


def _render_tail(
    groups: list[tuple[str, list[Fact]]],
    *,
    hook_chars: int,
    per_line: int,
    omitted: int,
    omitted_kinds: tuple[str, ...],
) -> str:
    lines: list[str] = [GENERATED_MARKER, ""]
    total = sum(len(members) for _, members in groups)
    if omitted:
        dropped = ", ".join(omitted_kinds)
        lines.append(
            f"_{total} facts indexed; **{omitted} omitted** to stay inside the "
            f"index budget ({dropped}). Omitted facts still exist as files — "
            f"`ls` the memory directory._",
        )
    else:
        lines.append(f"_{total} facts indexed — complete._")
    lines.append("")
    for kind, members in groups:
        lines.append(f"### {kind} ({len(members)})")
        for start in range(0, len(members), per_line):
            chunk = members[start : start + per_line]
            rendered = _SEPARATOR.join(_entry(f, hook_chars) for f in chunk)
            lines.append(f"- {rendered}")
    return "\n".join(lines)


def _measure(text: str) -> tuple[int, int]:
    return len(text.encode("utf-8")), text.count("\n") + 1


def _best_fit(
    groups: list[tuple[str, list[Fact]]],
    *,
    head_text: str,
    omitted: int,
    omitted_kinds: tuple[str, ...],
    byte_budget: int,
    line_budget: int,
) -> tuple[str, BudgetReport] | None:
    """Longest hooks that fit both budgets, or ``None`` if nothing fits.

    Two independent levers, each aimed at the budget it actually moves:
    PACKING buys lines (merging entries removes a ``- `` prefix and a newline
    each, but no content), HOOK LENGTH buys bytes. So for each hook length,
    pack just enough to satisfy the line budget, then test bytes.

    Overhead is MEASURED, never predicted. Predicting the tail's line count
    and deriving packing from it was off by two here, and the cost of that
    off-by-two was a whole group of facts silently omitted.

    Once anything has been dropped, prose is no longer affordable: budget
    reclaimed by omitting facts must buy back COVERAGE, never longer hooks.
    Otherwise the renderer drops facts and spends the freed bytes
    re-lengthening the survivors' hooks — an incomplete index that reads as a
    rich one.
    """
    tiers = (0,) if omitted else _HOOK_TIERS
    entries = sum(len(members) for _, members in groups)
    for hook_chars in tiers:
        for per_line in _PACKING_STEPS:
            tail = _render_tail(
                groups,
                hook_chars=hook_chars,
                per_line=per_line,
                omitted=omitted,
                omitted_kinds=omitted_kinds,
            )
            text = f"{head_text}\n\n{tail}\n" if head_text else f"{tail}\n"
            total_bytes, total_lines = _measure(text)
            if total_lines > line_budget:
                continue  # pack tighter
            report = BudgetReport(
                indexed=entries,
                omitted=omitted,
                hook_chars=hook_chars,
                per_line=per_line,
                total_bytes=total_bytes,
                total_lines=total_lines,
                omitted_kinds=omitted_kinds,
            )
            if report.fits(byte_budget, line_budget):
                return text, report
            break  # lines fit at this packing; only bytes are short
    return None


def _drop_least(
    working: list[Fact], victim_kind: str,
) -> tuple[list[Fact], int, str]:
    """Drop the fewest facts that could help, from the lowest-priority group.

    A tenth of the group at a time, from its tail. Dropping a whole group
    because the render was eighty bytes over costs fifty facts to save one
    line of prose.
    """
    survivors = [f for f in working if f.kind == victim_kind]
    cut = max(1, len(survivors) // 10)
    drop = {f.filename for f in survivors[-cut:]}
    return [f for f in working if f.filename not in drop], len(drop), victim_kind


def render_index(
    head: str,
    facts: list[Fact],
    *,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
    line_budget: int = DEFAULT_LINE_BUDGET,
) -> tuple[str, BudgetReport]:
    """Render head + generated tail inside BOTH budgets.

    Order of concessions, cheapest first: shorten hooks, then pack more entries
    per line, then omit whole low-priority groups. Omission is always reported
    in the rendered text — a truncated index that does not say so is worse than
    a short one that does.
    """
    head_text = head.rstrip("\n")
    head_bytes, head_lines = _measure(head_text) if head_text else (0, 0)
    if head_bytes >= byte_budget or head_lines >= line_budget:
        raise IndexRenderError(
            f"curated head alone exceeds the index budget "
            f"({head_bytes} B / {head_lines} lines against "
            f"{byte_budget} B / {line_budget} lines) — trim the head, "
            f"the generator cannot compensate for it",
        )

    working = list(facts)
    omitted_kinds: list[str] = []
    omitted = 0

    while True:
        # Once anything has been dropped, prose is no longer affordable: budget
        # reclaimed by omitting facts must buy back COVERAGE, never longer
        # hooks. Without this the renderer drops facts and then spends the
        # freed bytes re-lengthening the hooks of the survivors — the worst of
        # both, an incomplete index that reads as a rich one.
        groups = _group(working)
        fitted = _best_fit(
            groups,
            head_text=head_text,
            omitted=omitted,
            omitted_kinds=tuple(omitted_kinds),
            byte_budget=byte_budget,
            line_budget=line_budget,
        )
        if fitted is not None:
            return fitted

        if not working:
            raise IndexRenderError("index does not fit its budget even with no facts")
        working, dropped, victim_kind = _drop_least(working, groups[-1][0])
        omitted += dropped
        omitted_kinds = [f"{victim_kind} ×{omitted}"]


def main() -> int:
    """Standalone: rebuild MEMORY.md's generated tail from the fact files.

    Needs no platform, no export and no passthrough wiring — it reads the
    frontmatter already present in every fact file. ``--check`` renders and
    reports without writing, which is how to inspect a change before adopting
    it.
    """
    argv = sys.argv[1:]
    check_only = "--check" in argv
    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) != 1:
        print(
            "usage: index_render.py <memory_dir> [--check]",
            file=sys.stderr,
        )
        return 2

    memory_dir = Path(positional[0]).expanduser()
    if not memory_dir.is_dir():
        print(f"not a directory: {memory_dir}", file=sys.stderr)
        return 2

    index_path = memory_dir / "MEMORY.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""

    try:
        facts = collect_facts(memory_dir)
        head = split_head(existing)
        text, report = render_index(head, facts)
    except (IndexRenderError, OSError) as exc:
        print(f"INDEX RENDER FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        f"facts={report.indexed} omitted={report.omitted} "
        f"hook_chars={report.hook_chars} per_line={report.per_line} "
        f"bytes={report.total_bytes}/{DEFAULT_BYTE_BUDGET} "
        f"lines={report.total_lines}/{DEFAULT_LINE_BUDGET}",
    )
    if report.omitted:
        print(f"OMITTED: {', '.join(report.omitted_kinds)}", file=sys.stderr)
    if check_only:
        print("--check: nothing written", file=sys.stderr)
        return 0

    index_path.write_text(text, encoding="utf-8")
    print(f"wrote {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
