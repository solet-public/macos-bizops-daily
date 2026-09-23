"""Prompt Pipeline Stages.

Each stage performs a specific transformation and records decisions
for observability.

Available stages:
    - TemplateStage: Resolves <<<PATTERN>>> templates
    - FormatStage: Formats system/user prompts
    - ContextStage: Injects memory and conversation context
    - PlanStateStage: Parses focused plan into structured state
    - CatalogStage: Emits process catalog blocks
    - GuidanceStage: Emits step guidance blocks
    - DecodeContractStage: Adjusts output schema
    - ArtifactPromptStage: Converts artifact prompt payloads to MessageBlocks
    - APIStage: Builds final API payload (terminal)
"""

from ananta.core.prompts.stages.api import APIStage
from ananta.core.prompts.stages.artifact_prompt import ArtifactPromptStage
from ananta.core.prompts.stages.base import PromptStage
from ananta.core.prompts.stages.catalog import CatalogStage
from ananta.core.prompts.stages.context import ContextStage
from ananta.core.prompts.stages.decode_contract import DecodeContractStage
from ananta.core.prompts.stages.format import FormatStage
from ananta.core.prompts.stages.guidance import GuidanceStage
from ananta.core.prompts.stages.plan_state import PlanStateStage
from ananta.core.prompts.stages.template import TemplateStage

__all__ = [
    "APIStage",
    "ArtifactPromptStage",
    "CatalogStage",
    "ContextStage",
    "DecodeContractStage",
    "FormatStage",
    "GuidanceStage",
    "PlanStateStage",
    "PromptStage",
    "TemplateStage",
]
