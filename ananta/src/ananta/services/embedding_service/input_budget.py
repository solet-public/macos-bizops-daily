"""Split embedding inputs to a provider's declared budget; never truncate (iss_9166af93).

Every caller that sends text to ``embedding_service`` sizes its inputs here
instead of guessing a character window: Core AI refuses any input over its
2048-token ceiling (``coreai_embeddings.input_too_long``), where LM Studio
accepted an 8192-character window.  The budget and the counter come from the
provider (:meth:`EmbeddingServiceInterface.input_token_budget`), never from a
constant at the call site.

:func:`split_to_fit` returns contiguous windows of the input that together
cover every character (consecutive windows may overlap by a requested
margin), each accepted by the caller's ``fits`` predicate.  Cut points prefer
a paragraph, line, sentence, then word boundary in the back half of the
largest fitting window.  An input no split can fit -- one character already
over budget -- raises :class:`InputBudgetError`; nothing is dropped silently.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ananta.interfaces.embedding_service_interface import TokenBudget

_BOUNDARIES = ("\n\n", "\n", ". ", "? ", "! ", " ")


class InputBudgetError(ValueError):
    """An embedding input cannot be fitted to the provider's budget."""


def split_to_fit(text: str, fits: Callable[[str], bool], *, overlap_chars: int = 0) -> list[str]:
    """Contiguous windows of ``text`` covering it whole, each accepted by ``fits``.

    ``fits`` must accept every prefix of a window it accepts (true of a token
    count and of a character count).  ``overlap_chars`` repeats the tail of
    each window at the head of the next so a phrase on a cut still lands whole
    in one window; progress is guaranteed even when the overlap would not
    advance.
    """
    if overlap_chars < 0:
        raise ValueError(f"overlap_chars must be >= 0, got {overlap_chars}")
    if not text:
        raise InputBudgetError("an empty input has nothing to embed")
    if fits(text):
        return [text]
    windows: list[str] = []
    start = 0
    while True:
        if fits(text[start:]):
            windows.append(text[start:])
            return windows
        end = _snap(text, start, _largest_fitting_end(text, start, fits), fits)
        windows.append(text[start:end])
        following = end - overlap_chars
        start = following if following > start else end


def _largest_fitting_end(text: str, start: int, fits: Callable[[str], bool]) -> int:
    low, high = start + 1, len(text)
    if not fits(text[start:low]):
        raise InputBudgetError(f"the character at offset {start} alone exceeds the embedding budget")
    while low < high:
        middle = (low + high + 1) // 2
        if fits(text[start:middle]):
            low = middle
        else:
            high = middle - 1
    return low


def _snap(text: str, start: int, end: int, fits: Callable[[str], bool]) -> int:
    """Pull the cut back to the strongest natural boundary in the window's back half."""
    floor = start + (end - start) // 2
    for boundary in _BOUNDARIES:
        index = text.rfind(boundary, floor, end)
        if index != -1:
            cut = index + len(boundary)
            if cut > start and fits(text[start:cut]):
                return cut
    return end


def oversize_inputs(inputs: Sequence[str], budget: TokenBudget) -> list[tuple[int, int]]:
    """``(index, tokens)`` for every input over the budget, in input order."""
    counted = ((index, budget.count(text)) for index, text in enumerate(inputs))
    return [(index, tokens) for index, tokens in counted if tokens > budget.max_input_tokens]


__all__ = ["InputBudgetError", "oversize_inputs", "split_to_fit"]
