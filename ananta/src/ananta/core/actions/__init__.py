"""
Actions Package

Manages the complete action lifecycle from definition through execution.

This package provides:
- Action management and orchestration
- Action factory and creation
- Action processing and execution
- Action validation
- Action queue management
- Action registration and metadata
- Action statistics and tracking
- Action-specific services

Modules:
- action_manager: Main action orchestrator
- action_factory: Action creation
- action_processor: Action processing
- action_execution_engine: Execution engine
- action_queue_poller: Queue management
- action_validator: Validation
- action_validation_manager: Validation coordination
- action_template_validator: Template validation
- action_definition_manager: Definition management
- action_definition_processor: Definition processing
- action_definition_service: Definition services
- action_registration_manager: Registration
- action_metadata: Metadata handling
- action_stats_service: Statistics
- action_process_key_service: Process key resolution
- action_suggestion_service: Suggestions
- action_submission_types: Type definitions
"""

__all__ = [
    "action_manager",
    "action_factory",
    "action_processor",
    "action_execution_engine",
    "action_queue_poller",
    "action_validator",
    "action_validation_manager",
    "action_template_validator",
    "action_definition_manager",
    "action_definition_processor",
    "action_definition_service",
    "action_registration_manager",
    "action_metadata",
    "action_stats_service",
    "action_process_key_service",
    "action_suggestion_service",
    "action_submission_types",
]
