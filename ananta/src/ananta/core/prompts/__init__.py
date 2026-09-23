"""Prompt Pipeline - Observable prompt assembly for inference requests.

This package provides a centralized, observable pipeline for assembling prompts.
Each stage records what it did, enabling full traceability.

Usage:
    from ananta.core.prompts import PromptPipeline, PromptContext
    from ananta.core.prompts.stages import TemplateStage, FormatStage, ContextStage, APIStage

    pipeline = PromptPipeline([
        TemplateStage(template_engine),
        FormatStage(prompts_dir),
        ContextStage(context_builder),
        APIStage(),
    ])

    ctx, manifest = pipeline.execute(
        flow_id="flow_123",
        action_name="evaluate_input",
        session_id="sess_456",
        action_params=params,
    )
"""

from ananta.core.prompts.context import PromptContext
from ananta.core.prompts.manifest import PromptManifest
from ananta.core.prompts.pipeline import PromptPipeline

__all__ = [
    "PromptContext",
    "PromptManifest",
    "PromptPipeline",
]
