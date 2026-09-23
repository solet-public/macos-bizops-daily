"""Header-aware recursive chunker for knowledge base articles.

Single source of truth for chunking. The runtime knowledge plugin and
the offline reload tool both import ``chunk_by_headers`` from here.

Design rules (per
``workbench/2026-04-29_claude_knowledge_architecture_plan.md``
section 2.4):

- Respects header hierarchy. ``#``, ``##``, ``###``, ``####`` are real
  semantic boundaries.
- Recursive descent. A section that fits in the budget becomes one
  chunk. A section that exceeds the budget is split at its children.
- Parent-context breadcrumbs. Each chunk is prefixed with a
  ``Path: # Doc Title > ## Section > ### Subsection`` line so the
  embedding sees hierarchical context.
- Budget reserves room for the indexer's preamble. The chunker's
  effective budget is ``max_chars - preamble_reserve`` (default 500
  chars reserved). The indexer can prepend its preamble (Source line +
  Article Role + Article Tags) without overflowing the embedding ceiling.
- Never merge across ``#`` (top-level) boundaries. Each ``#`` is a
  logical document.
- Within-parent merging only. Adjacent ``###`` siblings under the same
  ``##`` parent may merge if both fit; ``###`` siblings under different
  parents may NOT.
- Last-resort splitting. If a leaf is still over budget after recursion
  exhausts header levels, split by paragraph; if a paragraph is still
  over budget, split by sentence. Never split mid-sentence.
- No mid-merge across budget violations. The pre-existing greedy
  chunker would happily merge unrelated sections to "use up" remaining
  budget. The new chunker only merges siblings that share a parent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .constants import METADATA_BLOCK_RECOGNIZED_KEYS

DEFAULT_MAX_CHARS = 3000
"""Hard ceiling enforced by the indexer (``len(enriched) > max_chars``)."""

DEFAULT_PREAMBLE_RESERVE = 500
"""Bytes reserved for the indexer-added preamble (Source line + Article
Role + Article Tags) plus the breadcrumb line. The chunker keeps content
under ``max_chars - preamble_reserve`` so the eventual enriched chunk
fits."""

MIN_USEFUL_BUDGET = 400
"""Hard floor on the effective budget. If the caller passes very small
``max_chars``, we clamp to this rather than producing absurdly small
chunks."""

_HEADER_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
"""Match level-1 through level-4 headers. ``#####`` and ``######`` are
treated as body content (rare in this corpus, and they almost never
indicate a real top-level boundary)."""

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")

_METADATA_LINE_PREFIXES = (
    "Article Role:",
    "Article Tags:",
    "Article Layer:",
    "Tags:",
)


def strip_article_metadata_preamble(content: str) -> str:
    """Remove the metadata block from the top of an article.

    Indexer metadata (``Article Role:``, ``Article Tags:``,
    ``Article Layer:``, and bare ``Tags:`` manifest-tag declarations)
    must not leak into chunk content (the indexer adds its own preamble)
    or into prompts assembled from articles loaded directly via
    ``read_file``. The metadata zone is the run of metadata-style lines
    (optionally interleaved with blank lines) at the very top of the
    document, optionally preceded by a single ``# Title`` heading line.

    Two arrangements are recognized:

    1. Metadata at the very top, before any heading::

           Article Layer: 1

           Body...

    2. Title followed by metadata, then body::

           # Title

           Article Layer: 1

           Article Role: ...

           Tags: knowledge:tag:foo

           Body...

    In case (1) the metadata block is removed. In case (2) the title is
    preserved and the metadata block between the title and the body is
    removed. Body text is left untouched. Metadata lines that appear
    deeper in the body (very rare) are not affected.
    """
    lines = content.split("\n")
    n = len(lines)
    cursor = 0

    title_idx: int | None = None
    if cursor < n and lines[cursor].startswith("# "):
        title_idx = cursor
        cursor += 1

    saw_metadata = False
    while cursor < n:
        stripped = lines[cursor].strip()
        if stripped.startswith(_METADATA_LINE_PREFIXES):
            saw_metadata = True
            cursor += 1
            continue
        if stripped == "":
            cursor += 1
            continue
        break

    if not saw_metadata:
        return content

    body = "\n".join(lines[cursor:])
    if title_idx is not None:
        if body:
            return f"{lines[title_idx]}\n\n{body}"
        return lines[title_idx]
    return body


@dataclass
class _Section:
    """One header-bounded section in the parsed document tree."""

    level: int
    """1=#, 2=##, 3=###, 4=####, 0=document root."""

    heading_line: str
    """Raw heading line including leading hashes; empty string for root."""

    heading_text: str
    """Heading text without leading hashes; used for breadcrumbs."""

    intro: str
    """Body text between this heading and the first child heading.
    Stripped of leading/trailing newlines."""

    children: list[_Section] = field(default_factory=list)

    def total_size(self) -> int:
        """Approximate rendered size including all descendants.

        Used by the chunker to decide whether the whole section fits in
        a chunk, or whether it must descend.
        """
        size = len(self.heading_line) + len(self.intro)
        if self.heading_line and self.intro:
            size += 2  # blank line between heading and intro
        for child in self.children:
            size += child.total_size() + 2  # +2 for the blank line separator
        return size

    def render(self) -> str:
        """Render this section + descendants as plain markdown."""
        parts: list[str] = []
        if self.heading_line:
            parts.append(self.heading_line)
        if self.intro:
            parts.append(self.intro)
        for child in self.children:
            parts.append(child.render())
        return "\n\n".join(p for p in parts if p)


@dataclass
class _Chunk:
    """A single emitted chunk with its breadcrumb context."""

    breadcrumb: tuple[str, ...]
    body: str

    def render(self) -> str:
        """Final rendered chunk text including the breadcrumb prefix."""
        if not self.breadcrumb:
            return self.body.strip()
        crumb = "Path: " + " > ".join(self.breadcrumb)
        return f"{crumb}\n\n{self.body.strip()}"


def chunk_by_headers(
    content: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    preamble_reserve: int = DEFAULT_PREAMBLE_RESERVE,
) -> list[str]:
    """Split a markdown document into chunks suitable for indexing.

    Returns a list of rendered chunk strings, each beginning with a
    ``Path: ...`` breadcrumb line (when the chunk is a descendant of a
    titled section) followed by the content. Each chunk's rendered
    length is bounded by ``max_chars - preamble_reserve`` so the
    indexer can safely prepend its preamble without overflowing
    ``max_chars``.

    Empty / whitespace-only documents return an empty list.
    """
    budget = max(max_chars - preamble_reserve, MIN_USEFUL_BUDGET)

    if not content.strip():
        return []

    body = strip_article_metadata_preamble(content)
    root = _parse_document(body)
    chunks: list[_Chunk] = []
    _emit(root, breadcrumb=(), chunks=chunks, budget=budget)

    return [c.render() for c in chunks if c.body.strip()]


def _match_recognized_key(line: str) -> str | None:
    """Return the recognized §4 key a line declares, or ``None``.

    Matches by ``"<Key>:"`` prefix against the shared recognized key-set. No
    recognized key is a prefix of another, so first-match is unambiguous.
    """
    for key in METADATA_BLOCK_RECOGNIZED_KEYS:
        if line.startswith(f"{key}:"):
            return key
    return None


def split_title_metadata_body(
    content: str,
) -> tuple[str | None, list[tuple[str, str]], list[str]]:
    """Split content into ``(title_line, metadata_pairs, body_lines)``.

    ``title_line`` is the raw first line when it is a top-level ``# `` heading
    (else ``None``); ``metadata_pairs`` is the ordered ``(key, value)`` list
    drawn from the contiguous run of recognized ``Key: value`` lines
    immediately following the title (leading blank lines skipped); ``body_lines``
    is everything from the first line that ends the metadata run (a blank line
    or the first non-recognized-key line) onward. The run stops there so body
    prose never leaks into the metadata block. This is the ONE definition the
    tolerant title_block chunker, the create-path metadata validator, and the
    archive_file §4 stamper all share (plan §6 drift risk).
    """
    lines = content.split("\n")
    n = len(lines)
    cursor = 0

    title_line: str | None = None
    if cursor < n and lines[cursor].startswith("# "):
        title_line = lines[cursor].rstrip()
        cursor += 1

    while cursor < n and lines[cursor].strip() == "":
        cursor += 1

    pairs: list[tuple[str, str]] = []
    while cursor < n:
        stripped = lines[cursor].strip()
        if stripped == "":
            break
        matched_key = _match_recognized_key(stripped)
        if matched_key is None:
            break
        value = stripped[len(matched_key) + 1:].strip()  # +1 for the ``:``
        pairs.append((matched_key, value))
        cursor += 1

    return title_line, pairs, lines[cursor:]


def parse_title_and_metadata(content: str) -> tuple[str | None, list[tuple[str, str]]]:
    """Return the leading ``# Title`` and the contiguous §4 metadata run.

    Thin wrapper over :func:`split_title_metadata_body` for callers that do
    not need the post-block body (the chunker and the create-path validator).
    """
    title_line, pairs, _ = split_title_metadata_body(content)
    return title_line, pairs


def _render_title_block(
    read_path_line: str, title_line: str, pairs: list[tuple[str, str]],
) -> str:
    """Render the single title_block chunk body from its parts."""
    body_lines = [read_path_line, title_line]
    body_lines.extend(f"{key}: {value}" for key, value in pairs)
    return "\n".join(body_lines)


def _fit_metadata_pairs(
    read_path_line: str,
    title_line: str,
    pairs: list[tuple[str, str]],
    budget: int,
) -> list[tuple[str, str]]:
    """Shrink the §4 block to fit ``budget`` — ``Summary`` first, then
    ``Embedding Description`` — never dropping a line or spilling to a second
    chunk. Best-effort for pathological inputs (a title alone over budget).
    """
    if len(_render_title_block(read_path_line, title_line, pairs)) <= budget:
        return pairs

    working = list(pairs)
    for target_key in ("Summary", "Embedding Description"):
        for idx, (key, value) in enumerate(working):
            if key != target_key:
                continue
            over = len(_render_title_block(read_path_line, title_line, working)) - budget
            if over <= 0:
                return working
            keep = max(len(value) - over - 1, 0)
            working[idx] = (key, f"{value[:keep].rstrip()}…" if keep else "…")
        if len(_render_title_block(read_path_line, title_line, working)) <= budget:
            return working
    return working


def chunk_title_block(
    content: str,
    relative_path: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    preamble_reserve: int = DEFAULT_PREAMBLE_RESERVE,
) -> list[str]:
    """Return exactly ONE discovery chunk: read path + title + §4 block.

    Used by the ``title_block`` chunking strategy (workbench). The chunk body
    carries the copyable KB-relative ``Read path:`` because the search
    result's own ``file_path`` field is the slash-normalized ``document_tag``
    provenance and is NOT a valid ``read_file`` argument (plan §2). The title
    falls back to the filename stem when the doc has no ``# `` heading; a doc
    with no §4 block contributes path + title only (legacy docs). The single
    chunk is bounded by ``max_chars - preamble_reserve``: when over, the
    ``Summary`` value is truncated first, then ``Embedding Description``, never
    a second chunk. Empty / whitespace-only documents return an empty list.
    """
    if not content.strip():
        return []

    budget = max(max_chars - preamble_reserve, MIN_USEFUL_BUDGET)

    title_line, pairs = parse_title_and_metadata(content)
    if title_line is None:
        title_line = Path(relative_path).stem

    read_path_line = f"Read path: {relative_path}"
    pairs = _fit_metadata_pairs(read_path_line, title_line, pairs, budget)
    return [_render_title_block(read_path_line, title_line, pairs)]


def _parse_document(content: str) -> _Section:
    """Build a header-tree from the document text."""
    root = _Section(level=0, heading_line="", heading_text="", intro="")
    stack: list[_Section] = [root]
    body_buf: list[str] = []

    def flush_body() -> None:
        text = "\n".join(body_buf).strip("\n")
        if text:
            target = stack[-1]
            if target.intro:
                target.intro = target.intro + "\n\n" + text
            else:
                target.intro = text
        body_buf.clear()

    for raw_line in content.splitlines():
        match = _HEADER_RE.match(raw_line)
        if match is None:
            body_buf.append(raw_line)
            continue
        flush_body()
        level = len(match.group(1))
        text = match.group(2)
        # Pop until we find the parent (a section with a strictly lower level).
        while stack and stack[-1].level >= level:
            stack.pop()
        if not stack:
            stack.append(root)
        new_section = _Section(
            level=level,
            heading_line=raw_line,
            heading_text=text,
            intro="",
        )
        stack[-1].children.append(new_section)
        stack.append(new_section)

    flush_body()
    return root


def _emit(
    section: _Section,
    breadcrumb: tuple[str, ...],
    chunks: list[_Chunk],
    budget: int,
) -> None:
    """Walk a section, emitting chunks under the given breadcrumb context."""
    overhead = _breadcrumb_overhead(breadcrumb)
    section_rendered = section.render()

    # Case 1: the whole section fits as one chunk.
    if len(section_rendered) + overhead <= budget:
        _emit_whole_section(section, section_rendered, breadcrumb, chunks)
        return

    # Case 2: section is too big. Emit intro (if any) and recurse into
    # children. Top-level boundaries (level 1) reset the breadcrumb so
    # we never carry a parent ``#`` heading across siblings.
    new_breadcrumb = (
        breadcrumb if section.level == 0 else breadcrumb + (section.heading_text,)
    )

    _emit_intro(section, breadcrumb, chunks, budget)
    _emit_children(section, new_breadcrumb, chunks, budget)


def _emit_whole_section(
    section: _Section,
    section_rendered: str,
    breadcrumb: tuple[str, ...],
    chunks: list[_Chunk],
) -> None:
    """Emit a section that fits entirely within the chunk budget."""
    if section.level == 0:
        # The root has no heading of its own. If the entire root fits,
        # emit it as one chunk with no breadcrumb (rare for real documents).
        if section_rendered.strip():
            chunks.append(_Chunk(breadcrumb=breadcrumb, body=section_rendered))
        return
    chunks.append(_Chunk(breadcrumb=breadcrumb, body=section_rendered))


def _emit_intro(
    section: _Section,
    breadcrumb: tuple[str, ...],
    chunks: list[_Chunk],
    budget: int,
) -> None:
    """Emit the intro chunk (heading + intro text) when present."""
    if not section.intro.strip():
        return
    intro_body = (
        f"{section.heading_line}\n\n{section.intro}"
        if section.heading_line
        else section.intro
    )
    intro_overhead = _breadcrumb_overhead(breadcrumb)
    if len(intro_body) + intro_overhead <= budget:
        chunks.append(_Chunk(breadcrumb=breadcrumb, body=intro_body))
        return
    for piece in _paragraph_split(intro_body, budget - intro_overhead):
        chunks.append(_Chunk(breadcrumb=breadcrumb, body=piece))


def _emit_children(
    section: _Section,
    new_breadcrumb: tuple[str, ...],
    chunks: list[_Chunk],
    budget: int,
) -> None:
    """Recurse into a section's children, grouping where possible."""
    children = section.children
    if not children:
        return

    if section.level == 0:
        # Root: each child is its own logical document. No merging.
        for child in children:
            _emit(child, new_breadcrumb, chunks, budget)
        return

    # Group adjacent siblings under this parent. A small group whose
    # combined render fits in the budget becomes one chunk; a larger
    # child recurses on its own.
    child_overhead = _breadcrumb_overhead(new_breadcrumb)
    groups = _group_siblings(children, budget - child_overhead)
    for group in groups:
        if len(group) == 1:
            _emit(group[0], new_breadcrumb, chunks, budget)
        else:
            merged_body = "\n\n".join(c.render() for c in group)
            chunks.append(_Chunk(breadcrumb=new_breadcrumb, body=merged_body))


def _breadcrumb_overhead(breadcrumb: tuple[str, ...]) -> int:
    """Approximate cost of the rendered breadcrumb prefix."""
    if not breadcrumb:
        return 0
    return len("Path: " + " > ".join(breadcrumb)) + 2  # +2 for "\n\n"


def _group_siblings(
    children: list[_Section],
    budget: int,
) -> list[list[_Section]]:
    """Group adjacent siblings whose combined size fits the budget.

    Children whose own size exceeds the budget always become their own
    single-element group (the caller recurses on them). Smaller siblings
    are coalesced greedily so we don't emit hundreds of tiny chunks.
    """
    groups: list[list[_Section]] = []
    current: list[_Section] = []
    current_size = 0

    for child in children:
        size = child.total_size()
        # Big child: commit pending group and put this child alone.
        if size > budget:
            if current:
                groups.append(current)
                current = []
                current_size = 0
            groups.append([child])
            continue
        # Would adding this child overflow the running group?
        if current and current_size + size + 2 > budget:
            groups.append(current)
            current = [child]
            current_size = size
        else:
            current.append(child)
            current_size = current_size + size + (2 if len(current) > 1 else 0)

    if current:
        groups.append(current)
    return groups


def _paragraph_split(text: str, budget: int) -> list[str]:
    """Split a long block by paragraph (and then sentence) boundaries.

    Used when a single section's intro exceeds the budget — for
    example, a deeply nested specification with no internal headers.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > budget:
            # The paragraph itself is too big — fall through to sentence split.
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(_sentence_split(paragraph, budget))
            continue
        if current and len(current) + len(paragraph) + 2 > budget:
            chunks.append(current.strip())
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph

    if current.strip():
        chunks.append(current.strip())
    return chunks


def _sentence_split(paragraph: str, budget: int) -> list[str]:
    """Sentence-level fallback when a single paragraph exceeds the budget."""
    sentences = _SENTENCE_BOUNDARY_RE.split(paragraph)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > budget:
            # Single sentence too long for the budget — last-resort hard split.
            if current:
                chunks.append(current.strip())
                current = ""
            for offset in range(0, len(sentence), budget):
                chunks.append(sentence[offset : offset + budget])
            continue
        if current and len(current) + len(sentence) + 1 > budget:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks
