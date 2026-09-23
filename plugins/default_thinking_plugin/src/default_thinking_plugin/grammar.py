"""Substrate-neutral grammar composition for thinking-model authoring.

Extracts the per-artifact-type loaders + message-sequence assembly
that ``ArtifactAuthoringService`` uses today.  Exposes
``ComposedGrammarPayload`` as the structured handoff between grammar
lookup and model invocation, so future consumers (W-CG2 Claude-Code
spawned session; W-CG3 A/B substrates) can compose the same payload
without instantiating the thinking plugin's Qwen client.

A **context grammar** (per the v2 substrate strategy at
``workbench/2026-06-03_inference_substrate_strategy_design_v2.md``
§3) is a per-``artifact_type`` triple of articles:

* system frame (``artifact_system_frame_<type>.md``)
* authoring guidance (``artifact_guidance_<type>.md``)
* directive reinforcement (``artifact_reinforcement_<type>.md``)

plus an optional specification article (Pipeline Spec schema, WBS
specs, etc.).  Composition consumes a grammar + project-supplied
parent artifacts + style anchors + directive and produces a
message sequence ready for any substrate (Qwen-local today; a
spawned Claude-Code session under W-CG2).

This module is pure: no I/O beyond ``article_loader.load_article``
(caller-supplied), no thinking-model invocation, no state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .artifact_messages import build_artifact_messages
from .artifact_registry import ArticleRole, load_article

if TYPE_CHECKING:
    from .artifact_authoring import ArticleLoader


@dataclass(frozen=True, slots=True)
class ComposedGrammarPayload:
    """Three-bucket decomposition of an artifact-authoring message sequence.

    Matches the v2 substrate-strategy W-CG1 shape: a frozen,
    substrate-neutral handoff between grammar lookup and model
    invocation.

    * ``system_frame`` — the system-message content (empty string
      means no system message will be emitted by ``as_messages``).
    * ``assistant_messages`` — parent context, resolved guidance,
      and support articles, in the canonical order
      ``build_artifact_messages`` produces.
    * ``user_directive`` — the final user-role message content
      (reinforcement prefix + project directive).
    """

    system_frame: str
    assistant_messages: list[dict[str, str]]
    user_directive: str

    def as_messages(self) -> list[dict[str, str]]:
        """Flatten to the legacy ``[system?, *assistant, user]`` shape.

        Round-trips ``build_artifact_messages``'s output exactly: a
        non-empty ``system_frame`` becomes the first message, the
        assistant messages follow in order, and the user directive
        terminates the list.
        """
        messages: list[dict[str, str]] = []
        if self.system_frame:
            messages.append({"role": "system", "content": self.system_frame})
        messages.extend(self.assistant_messages)
        messages.append({"role": "user", "content": self.user_directive})
        return messages


# ── Grammar lookups (per-artifact-type article loaders) ──────────────


def load_system_frame(
    artifact_type: str,
    article_loader: ArticleLoader,
    *,
    fallback: str = "",
) -> str:
    """Load the ``system_frame`` article for ``artifact_type``.

    ``fallback`` (typically the thinking plugin's constructor-supplied
    artifact_system_prompt) is returned when the article file is
    missing — preserves the legacy behavior of
    ``ArtifactAuthoringService._load_system_frame``.
    """
    return load_article(
        artifact_type,
        ArticleRole.SYSTEM_FRAME,
        article_loader,
        fallback=fallback,
    )


def load_guidance(artifact_type: str, article_loader: ArticleLoader) -> str:
    """Load the ``guidance`` article for ``artifact_type``."""
    return load_article(
        artifact_type, ArticleRole.GUIDANCE, article_loader,
    )


def load_reinforcement(artifact_type: str, article_loader: ArticleLoader) -> str:
    """Load the ``reinforcement`` article, suffixed with ``"\\n\\n"``.

    Matches the legacy ``ArtifactAuthoringService._load_reinforcement``
    semantics: empty string when the article is missing; otherwise the
    article content stripped of trailing whitespace and re-suffixed
    with a blank-line separator so the caller can concatenate the
    project directive verbatim.
    """
    content = load_article(
        artifact_type, ArticleRole.REINFORCEMENT, article_loader,
    )
    return content.strip() + "\n\n" if content else ""


# ── Composition ──────────────────────────────────────────────────────


def compose_grammar_payload(
    *,
    artifact_type: str,
    article_loader: ArticleLoader,
    guidance: str | None,
    guidance_placeholders: dict[str, str],
    parent_context: list[dict[str, str]],
    support_articles: list[str] | None,
    directive: str,
    directive_reinforcement: str,
    system_frame_fallback: str = "",
) -> ComposedGrammarPayload:
    """Build a ``ComposedGrammarPayload`` from a grammar + composition inputs.

    Internally delegates message assembly to ``build_artifact_messages``
    so the per-message contents and ordering are bit-identical to what
    ``ArtifactAuthoringService._build_artifact_messages`` produces today,
    then decomposes the flat list into the three-bucket payload shape.

    ``system_frame_fallback`` is forwarded as the
    ``ArticleLoader.load_article`` fallback for the system-frame
    lookup — pass the thinking plugin's
    ``artifact_system_prompt`` here to preserve legacy behavior.
    """
    system_frame = load_system_frame(
        artifact_type, article_loader, fallback=system_frame_fallback,
    )
    flat_messages = build_artifact_messages(
        system_prompt=system_frame,
        guidance=guidance,
        guidance_placeholders=guidance_placeholders,
        parent_context=parent_context,
        support_articles=support_articles,
        directive=directive,
        directive_reinforcement=directive_reinforcement,
        article_loader=article_loader,
    )
    assistant_messages: list[dict[str, str]] = []
    user_directive = ""
    for msg in flat_messages:
        role = msg["role"]
        if role == "system":
            continue  # captured separately as system_frame
        if role == "user":
            user_directive = msg["content"]
            continue
        assistant_messages.append(msg)
    return ComposedGrammarPayload(
        system_frame=system_frame,
        assistant_messages=assistant_messages,
        user_directive=user_directive,
    )


__all__ = [
    "ComposedGrammarPayload",
    "compose_grammar_payload",
    "load_guidance",
    "load_reinforcement",
    "load_system_frame",
]
