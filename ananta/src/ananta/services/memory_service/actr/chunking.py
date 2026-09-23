"""Document Chunking for Memory Ingestion.

Provides strategies for splitting documents into memory-sized chunks.
"""

import re
from typing import Any

from .constants import (
    CHARS_PER_TOKEN,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    EMBEDDING_MAX_CHARS,
)


def chunk_document(
    content: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    source_file: str | None = None,
) -> list[dict[str, Any]]:
    """Chunk a document into memory-sized pieces.

    Uses a paragraph-aware approach:
    1. Split by double newlines (paragraphs/sections)
    2. Merge small paragraphs, split large ones
    3. Maintain overlap for context continuity

    Args:
        content: Document text to chunk
        chunk_size: Target tokens per chunk (approx 4 chars/token)
        chunk_overlap: Token overlap between chunks
        source_file: Optional source file path for metadata

    Returns:
        List of chunk dicts with 'content', 'source_file', 'start_line'
    """
    paragraphs = _extract_paragraphs(content)
    if not paragraphs:
        return []

    overlap_chars = chunk_overlap * CHARS_PER_TOKEN
    target_chars = (chunk_size * CHARS_PER_TOKEN) - overlap_chars

    state = _ChunkingState(source_file, target_chars, overlap_chars)

    for para in paragraphs:
        state.process_paragraph(para)

    state.finalize()
    return state.chunks


def _extract_paragraphs(content: str) -> list[str]:
    """Extract non-empty paragraphs from content."""
    if not content.strip():
        return []
    paragraphs = content.split("\n\n")
    return [p.strip() for p in paragraphs if p.strip()]


class _ChunkingState:
    """Manages state during document chunking."""

    def __init__(self, source_file: str | None, target_chars: int, overlap_chars: int) -> None:
        self.source_file = source_file
        self.target_chars = target_chars
        self.overlap_chars = overlap_chars
        self.chunks: list[dict[str, Any]] = []
        self.current_chunk = ""
        self.current_start_line = 0
        self.line_count = 0

    def process_paragraph(self, para: str) -> None:
        """Process a single paragraph."""
        para_lines = para.count("\n") + 1

        if self._fits_in_current_chunk(para):
            self._append_to_chunk(para)
        else:
            self._start_new_chunk_with(para)

        self.line_count += para_lines + 1

    def _fits_in_current_chunk(self, para: str) -> bool:
        """Check if paragraph fits in current chunk."""
        return len(self.current_chunk) + len(para) + 2 <= self.target_chars

    def _append_to_chunk(self, para: str) -> None:
        """Append paragraph to current chunk."""
        if self.current_chunk:
            self.current_chunk += "\n\n" + para
        else:
            self.current_chunk = para
            self.current_start_line = self.line_count

    def _start_new_chunk_with(self, para: str) -> None:
        """Save current chunk and start new one with paragraph."""
        if self.current_chunk:
            self._save_chunk(self.current_chunk)

        new_content = self._build_new_content(para)
        self._split_and_save_oversized(new_content)

    def _build_new_content(self, para: str) -> str:
        """Build new content with optional overlap from previous chunk."""
        if self.overlap_chars > 0 and self.current_chunk:
            overlap_text = self._extract_overlap()
            return overlap_text + "\n\n" + para
        return para

    def _extract_overlap(self) -> str:
        """Extract overlap text from current chunk end."""
        overlap_text = self.current_chunk[-self.overlap_chars :]
        last_space = overlap_text.rfind(" ")
        if last_space > 0:
            return overlap_text[last_space + 1 :]
        return overlap_text

    def _split_and_save_oversized(self, new_content: str) -> None:
        """Split oversized content into chunks and update current."""
        while len(new_content) > self.target_chars:
            break_point = _find_break_point(new_content, self.target_chars)
            self._save_chunk(new_content[:break_point].strip())
            new_content = new_content[break_point:].strip()

        self.current_chunk = new_content
        self.current_start_line = self.line_count

    def _save_chunk(self, content: str) -> None:
        """Save a chunk to the list."""
        self.chunks.append(
            {
                "content": content,
                "source_file": self.source_file,
                "start_line": self.current_start_line,
            }
        )

    def finalize(self) -> None:
        """Finalize any remaining content."""
        while self.current_chunk and len(self.current_chunk) > self.target_chars:
            break_point = _find_break_point(self.current_chunk, self.target_chars)
            self._save_chunk(self.current_chunk[:break_point].strip())
            self.current_chunk = self.current_chunk[break_point:].strip()

        if self.current_chunk:
            self._save_chunk(self.current_chunk)


def _find_break_point(text: str, target_chars: int) -> int:
    """Find a good break point in text."""
    for sep in [". ", ".\n", " ", "\n"]:
        pos = text[:target_chars].rfind(sep)
        if pos > target_chars // 2:
            return pos + len(sep)
    return target_chars


def chunk_by_turns(transcript: str) -> list[str]:
    """Split a conversation transcript by turns.

    Recognizes common turn markers:
    - "User:" / "Human:"
    - "Assistant:" / "Claude:"
    - "System:"

    Args:
        transcript: Conversation transcript text

    Returns:
        List of conversation turns (strings)
    """
    # Pattern matches lines starting with common role markers
    turn_pattern = r"\n(?=(?:User|Human|Assistant|Claude|System):)"

    turns = re.split(turn_pattern, transcript)
    turns = [t.strip() for t in turns if t.strip()]

    return turns


def chunk_code_file(
    content: str,
    source_file: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[dict[str, Any]]:
    """Chunk a code file intelligently.

    Tries to break at natural boundaries:
    - Function/method definitions
    - Class definitions
    - Blank lines between logical blocks

    Args:
        content: Source code content
        source_file: Source file path
        chunk_size: Target tokens per chunk

    Returns:
        List of chunk dicts
    """
    if not content.strip():
        return []

    lines = content.split("\n")
    target_chars = min(chunk_size * CHARS_PER_TOKEN, EMBEDDING_MAX_CHARS)

    state = _CodeChunkingState(source_file, target_chars)
    for i, line in enumerate(lines):
        state.process_line(i, line)

    state.finalize()
    return state.chunks


# Break patterns for code chunking
_CODE_BREAK_PATTERNS = [
    r"^def\s+",
    r"^class\s+",
    r"^async\s+def\s+",
    r"^function\s+",
    r"^export\s+",
    r"^import\s+",
    r"^\s*$",
]
_CODE_BREAK_REGEX = re.compile("|".join(_CODE_BREAK_PATTERNS))


class _CodeChunkingState:
    """Manages state during code file chunking."""

    def __init__(self, source_file: str | None, target_chars: int) -> None:
        self.source_file = source_file
        self.target_chars = target_chars
        self.chunks: list[dict[str, Any]] = []
        self.current_lines: list[str] = []
        self.current_start_line = 0

    def process_line(self, line_num: int, line: str) -> None:
        """Process a single line of code."""
        line_chars = len(line) + 1
        current_chars = self._current_char_count()

        if current_chars + line_chars >= self.target_chars and self.current_lines:
            self._handle_overflow(line_num, line, line_chars)
        else:
            self.current_lines.append(line)

    def _current_char_count(self) -> int:
        """Calculate current chunk character count."""
        return sum(len(ln) + 1 for ln in self.current_lines)

    def _handle_overflow(self, line_num: int, line: str, line_chars: int) -> None:
        """Handle when adding a line would overflow the chunk."""
        best_break = self._find_best_break(line_chars)
        self._save_chunk_up_to(best_break)

        remaining = self.current_lines[best_break:]
        self._handle_remaining(remaining, line, line_chars, line_num)

    def _find_best_break(self, line_chars: int) -> int:
        """Find the best break point in current lines."""
        best_break = len(self.current_lines)
        start = max(0, len(self.current_lines) - 20)

        for j in range(len(self.current_lines) - 1, start, -1):
            if _CODE_BREAK_REGEX.match(self.current_lines[j]):
                remaining_chars = sum(len(ln) + 1 for ln in self.current_lines[j:])
                if remaining_chars + line_chars < self.target_chars:
                    return j

        return best_break

    def _save_chunk_up_to(self, break_point: int) -> None:
        """Save chunk content up to break point."""
        chunk_content = "\n".join(self.current_lines[:break_point])
        if chunk_content.strip():
            self.chunks.append(
                {
                    "content": chunk_content,
                    "source_file": self.source_file,
                    "start_line": self.current_start_line,
                }
            )

    def _handle_remaining(
        self, remaining: list[str], line: str, line_chars: int, line_num: int
    ) -> None:
        """Handle remaining lines after saving a chunk."""
        remaining_chars = sum(len(ln) + 1 for ln in remaining)

        if remaining and remaining_chars + line_chars >= self.target_chars:
            self._save_remaining(remaining)
            self.current_lines = [line]
        else:
            self.current_lines = remaining + [line]

        self.current_start_line = line_num - len(self.current_lines) + 1

    def _save_remaining(self, remaining: list[str]) -> None:
        """Save remaining lines as a chunk if non-empty."""
        remaining_content = "\n".join(remaining)
        if remaining_content.strip():
            self.chunks.append(
                {
                    "content": remaining_content,
                    "source_file": self.source_file,
                    "start_line": self.current_start_line,
                }
            )

    def finalize(self) -> None:
        """Finalize any remaining lines."""
        if self.current_lines:
            chunk_content = "\n".join(self.current_lines)
            if chunk_content.strip():
                self.chunks.append(
                    {
                        "content": chunk_content,
                        "source_file": self.source_file,
                        "start_line": self.current_start_line,
                    }
                )
