"""CatalogStage — inject process catalog and plugin notices.

Runs after PlanStateStage, before GuidanceStage.  Enriches
``ctx.system_prompt`` with the frozen process catalog and plugin
availability notice.  Prepares discovered schema text for APIStage
to classify into a block.  Does NOT touch ctx.messages — that's
APIStage's job.

Self-sufficient: calls platform functions from ``process_catalog.py``
with an injected ``CatalogDataSource`` protocol.  No plugin callbacks.
"""

from __future__ import annotations

import logging
from typing import Any

from ananta.core.prompts.catalogs.process_catalog import (
    EXECUTION_PLANS_PREAMBLE,
    CatalogDataSource,
    build_discovered_schema_text,
    build_process_catalog,
)
from ananta.core.prompts.context import PromptContext

logger = logging.getLogger(__name__)

# Section headers used for stripping stale catalog blocks from system prompt.
_STALE_SECTION_HEADERS = (
    "## Built-in Processes",
    "## Execution Plans",
    "## Core Processes",
    "## IO Processes",
    "## Plan Execution",
    "## Plugin Process Availability",
    "## Audio Processes",
    "## Plugin Processes",
)

_PLUGIN_AVAILABILITY_NOTICE = (
    "\n\n## Plugin Process Availability\n\n"
    "Plugin process keys may be mentioned in text, but plugin processes "
    "are not directly listed with invocation schemas in this initial turn."
)


def _strip_section(content: str, header: str) -> str:
    """Remove a ``## Header`` section from content if present."""
    if header not in content:
        return content
    start = content.index(header)
    end = content.find("\n## ", start + len(header))
    if end == -1:
        end = len(content)
    result = content[:start].rstrip() + content[end:]
    return result.strip()


def _strip_stale_sections(content: str) -> str:
    """Remove all known stale catalog sections from the system prompt."""
    for header in _STALE_SECTION_HEADERS:
        content = _strip_section(content, header)
    return content


def _schema_allows_plugin(output_schema: dict[str, Any] | None) -> bool:
    """Check if the output schema allows plugin provider_type."""
    if not output_schema:
        return False
    properties = output_schema.get("properties")
    if not isinstance(properties, dict):
        return False
    actions = properties.get("actions")
    if not isinstance(actions, dict):
        return False
    items = actions.get("items")
    if not isinstance(items, dict):
        return False
    process = (items.get("properties") or {}).get("process")
    if not isinstance(process, dict):
        return False
    provider_type = (process.get("properties") or {}).get("provider_type")
    if not isinstance(provider_type, dict):
        return False
    enum_values = provider_type.get("enum", [])
    return "plugin" in enum_values


class CatalogStage:
    """Enrich system prompt with process catalog and plugin notices.

    Self-sufficient: calls ``build_process_catalog`` and
    ``build_discovered_schema_text`` from ``process_catalog.py``
    using an injected ``CatalogDataSource`` protocol.  No plugin
    callback.
    """

    stage_name = "catalog"

    def __init__(
        self,
        *,
        catalog_source: CatalogDataSource,
    ) -> None:
        self._source = catalog_source

    @property
    def name(self) -> str:
        return self.stage_name

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Enrich system prompt and prepare discovered schema text."""
        self._enrich_system_prompt(ctx)
        self._prepare_discovered_schema(ctx)
        self._maybe_append_plugin_notice(ctx)
        return ctx

    def _enrich_system_prompt(self, ctx: PromptContext) -> None:
        """Build and append the frozen process catalog to ctx.system_prompt."""
        catalog = build_process_catalog(self._source)

        content = _strip_stale_sections(ctx.system_prompt)
        content = f"{content}\n\n{EXECUTION_PLANS_PREAMBLE}\n\n{catalog}"
        ctx.system_prompt = content

        logger.info(
            "SYSTEM_PROMPT: CatalogStage enriched system prompt: %d chars",
            len(content),
        )
        ctx.add_decision(
            self.stage_name,
            f"system prompt enriched: {len(content)} chars",
        )

    def _prepare_discovered_schema(self, ctx: PromptContext) -> None:
        """Compute discovered schema text and store on ctx for APIStage."""
        schema_text = build_discovered_schema_text(
            ctx.raw_observation_dict, self._source,
        )
        if schema_text:
            ctx.discovered_schema_text = schema_text
            logger.info("DISCOVERED_SCHEMA: Prepared %d chars", len(schema_text))
            ctx.add_decision(
                self.stage_name,
                f"discovered schema: {len(schema_text)} chars",
            )

    def _maybe_append_plugin_notice(self, ctx: PromptContext) -> None:
        """Append Plugin Process Availability notice if appropriate."""
        if ctx.tool_observation:
            return
        if ctx.current_step_process_keys:
            return
        if _schema_allows_plugin(ctx.output_schema):
            return

        ctx.system_prompt = ctx.system_prompt + _PLUGIN_AVAILABILITY_NOTICE
        logger.info("SYSTEM_PROMPT: Appended Plugin Process Availability notice")
        ctx.add_decision(self.stage_name, "appended plugin availability notice")
