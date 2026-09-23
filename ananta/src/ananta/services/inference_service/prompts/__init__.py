"""Domain prompt loading — reads instruction articles from prompts/ dir.

Domain instructions that guide model behavior live in markdown files,
not in Python string literals.  A domain expert can edit these files
to change model behavior without touching code.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> list[str]:
    """Load a prompt article and split into instruction lines.

    Args:
        name: Article filename (e.g. ``"process_error_recovery.md"``).

    Returns:
        List of instruction strings (one per line), suitable for
        injection into an ``action_definition_template``'s
        ``instructions`` list.

    Raises:
        RuntimeError: If the article file does not exist.
    """
    path = _PROMPTS_DIR / name
    if not path.is_file():
        raise RuntimeError(
            f"Domain prompt article not found: {path}"
        )
    text = path.read_text(encoding="utf-8").rstrip("\n")
    return text.split("\n")
