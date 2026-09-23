"""Shared message array construction for thinking-model artifact authoring.

Pure functions for building the canonical artifact-authoring message array
and injecting support articles.  Used by both ``ArtifactAuthoringService``
and ``WbsAuthoringService``.

Article key registries and load functions have moved to
``artifact_registry.py``.

No plugin or service dependencies — all inputs are passed as parameters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .artifact_helpers import extract_markdown_section

if TYPE_CHECKING:
    from default_thinking_plugin.artifact_authoring import ArticleLoader

logger = logging.getLogger(__name__)


# ── Message assembly ────────────────────────────────────────────────


def build_artifact_messages(
    *,
    system_prompt: str,
    guidance: str | None,
    guidance_placeholders: dict[str, str],
    parent_context: list[dict[str, str]],
    support_articles: list[str] | None,
    directive: str,
    directive_reinforcement: str,
    article_loader: ArticleLoader,
) -> list[dict[str, str]]:
    """Build the canonical artifact-authoring message array.

    MSG[0]       SYSTEM:    artifact-authoring contract
    MSG[1..P]    ASSISTANT: parent context (manifest, sketch, intake)
    MSG[P+1]     ASSISTANT: authoring guidance (with placeholders resolved)
    MSG[P+2..N]  ASSISTANT: support articles
    MSG[N+1]     USER:      reinforced directive
    """
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.extend(parent_context)

    if guidance:
        resolved = guidance
        for placeholder, value in guidance_placeholders.items():
            resolved = resolved.replace(placeholder, value)
        messages.append({"role": "assistant", "content": resolved})

    inject_support_articles(messages, support_articles, article_loader)

    messages.append({
        "role": "user",
        "content": directive_reinforcement + directive,
    })
    return messages


def inject_support_articles(
    messages: list[dict[str, str]],
    article_filenames: list[str] | None,
    article_loader: ArticleLoader,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Load and inject support articles as assistant context messages."""
    if not article_filenames:
        return
    for filename in article_filenames:
        content = article_loader.load_article(filename)
        if content:
            if placeholders:
                for placeholder, value in placeholders.items():
                    content = content.replace(placeholder, value)
            messages.append({"role": "assistant", "content": content})
            logger.info(
                "SUPPORT_ARTICLE: Injected %s (%d chars)",
                filename, len(content),
            )


def extract_section_from_parent(
    parent_messages: list[dict[str, str]],
    heading: str,
) -> list[dict[str, str]]:
    """Extract a specific section from parent context messages.

    When the target section heading is known deterministically, use
    this instead of loading the full parent document.  Searches each
    parent message for a matching markdown heading and returns a new
    message list with only the extracted section content.

    If no match is found in any message, returns the original list
    unchanged (fail-open for compatibility).
    """
    for msg in parent_messages:
        section = extract_markdown_section(msg.get("content", ""), heading)
        if section:
            logger.info(
                "SECTION_EXTRACT: Extracted '%s' (%d chars) from parent context",
                heading, len(section),
            )
            return [{"role": msg["role"], "content": section}]
    logger.info(
        "SECTION_EXTRACT: Heading '%s' not found in parent context — using full context",
        heading,
    )
    return parent_messages
