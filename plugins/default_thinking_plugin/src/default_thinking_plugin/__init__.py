"""Default Thinking Plugin.

Structured reasoning with local LLM for planning, analysis, and deliberation.
Wraps the thinking_service interface with per-task context management.
"""

from .plugin import DefaultThinkingPlugin

__all__ = ["DefaultThinkingPlugin"]
