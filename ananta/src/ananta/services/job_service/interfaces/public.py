"""Job Service Public API.

AI-discoverable asynchronous job operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.

Discoverability Policy (Task #47, 2026-05-24):
- ``get_latest_job`` declares ``is_discoverable=True`` explicitly. The base
  decorator default for ``@service_interface_process`` is
  ``is_discoverable=False`` (service methods are presumed internal); fetching
  the latest async job is agent-callable (diagnose stuck job, retrieve job_id
  before status monitoring).
- Adding a new method without ``is_discoverable=True`` will SILENTLY exclude it
  from ``process_search`` and the agent will not be able to find it.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process


class JobServiceAPI(ABC):
    """Public job service operations - AI-discoverable via process registry.

    This interface defines asynchronous job operations that can be discovered
    and invoked by the AI orchestration system:

    1. get_latest_job - Retrieve the most recently created asynchronous job
    2. get_job - Retrieve one asynchronous job by its job_id
    3. list_unreached_job_completions - List finished jobs whose completion
       had no channel to arrive on (bridge/CLI dispatches)

    Each method is decorated with complete metadata for process registry.
    """

    @service_interface_process(
        name="get_latest_job",
        is_discoverable=True,
        provider="job_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description=(
                    "Filter to jobs created by the specified plugin "
                    "(e.g., default_image_generation_plugin)"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "action_name": ParameterMetadata(
                description=(
                    "Filter to jobs created by the specified action (e.g., generate_image)"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "status": ParameterMetadata(
                description=(
                    "Filter to jobs in the specified status "
                    "(pending, processing, completed, failed, cancelled)"
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Asynchronous job metadata",
            type=ParameterType.OBJECT,
            properties={
                "job": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Job record from core__asynchronous_jobs",
                    required=False,
                ),
            },
            usage_patterns=[
                "Fetch the latest job for a plugin/action to retrieve job_id before status monitoring",
                "Diagnose async job failures by inspecting stored result/error payloads",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def get_latest_job(
        self,
        plugin_name: str | None = None,
        action_name: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve the most recently created asynchronous job.

        Service interface receives individual kwargs (action_processor pattern).
        Queries the core__job table with optional filters.
        """
        ...

    @service_interface_process(
        name="get_job",
        is_discoverable=True,
        provider="job_service",
        parameters={
            "job_id": ParameterMetadata(
                description=(
                    "Identifier of the job to fetch — the job_id a born-async "
                    "dispatch returned in its {job_id, status: queued} envelope"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Asynchronous job metadata for one specific job",
            type=ParameterType.OBJECT,
            properties={
                "job": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Job record from core__job, or null when no job carries "
                        "that job_id"
                    ),
                    required=False,
                ),
            },
            usage_patterns=[
                "Fetch the exact job a dispatch returned, by the job_id it handed back",
                "Poll one known job to a terminal status without racing other jobs",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def get_job(self, job_id: str) -> dict[str, Any]:
        """Retrieve one asynchronous job by its identifier.

        Service interface receives individual kwargs (action_processor pattern).
        Queries the core__job table by primary key. Unlike ``get_latest_job``
        this never guesses which job the caller meant — an unknown job_id is a
        successful call carrying ``job: None``.
        """
        ...

    @service_interface_process(
        name="list_unreached_job_completions",
        is_discoverable=True,
        provider="job_service",
        parameters={
            "limit": ParameterMetadata(
                description=(
                    "Maximum jobs to return, newest first (1-100, default 20)"
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Finished jobs whose completion had no channel to arrive on",
            type=ParameterType.OBJECT,
            properties={
                "jobs": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Job records, each with its stored result/error payload "
                        "attached, newest first"
                    ),
                    required=False,
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="How many job records are in this page",
                    required=False,
                ),
            },
            usage_patterns=[
                "Drain the results of background jobs dispatched from a CLI or "
                "direct process_call, which have no completion channel",
                "Audit which finished jobs nobody was ever told about",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def list_unreached_job_completions(self, limit: int = 20) -> dict[str, Any]:
        """List finished jobs whose completion had no channel to arrive on.

        Service interface receives individual kwargs (action_processor pattern).
        Matches on the reach stamp ``AsyncJobManager`` writes at completion
        time; jobs with no stamp are never included, because an absent stamp
        means unmeasured rather than unreachable.
        """
        ...

    @service_interface_process(
        name="detect_unresolved_completion_tokens",
        is_discoverable=True,
        provider="job_service",
        parameters={
            "limit": ParameterMetadata(
                description=(
                    "Maximum jobs to return, newest first (1-100, default 20)"
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Finished jobs whose flow token was never resolved",
            type=ParameterType.OBJECT,
            properties={
                "jobs": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Job records that read terminal while their flow token "
                        "sits in a non-terminal state, newest first"
                    ),
                    required=False,
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="How many job records are in this page",
                    required=False,
                ),
            },
            usage_patterns=[
                "Find jobs that look completed but left their flow open forever",
                "Diagnose a flow that never closed despite its job reporting success",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def detect_unresolved_completion_tokens(self, limit: int = 20) -> dict[str, Any]:
        """Report finished jobs whose flow token was never resolved.

        Service interface receives individual kwargs (action_processor pattern).
        READ-ONLY: it never resolves a token, because resolving one would
        manufacture a completion no worker produced. A job with no token row at
        all is not reported — absent evidence is not evidence of the defect.
        """
        ...

    @service_interface_process(
        name="sweep_stale_jobs",
        is_discoverable=True,
        provider="job_service",
        parameters={
            "max_age_minutes": ParameterMetadata(
                description=(
                    "How long a job may sit in 'processing' without reporting "
                    "progress before it is failed. Required — pick it from what "
                    "the target plugin's work actually takes"
                ),
                required=True,
                type=ParameterType.INTEGER,
            ),
            "plugin_name": ParameterMetadata(
                description="Sweep only jobs created by this plugin",
                required=False,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description=(
                    "Maximum jobs to sweep in one call (1-100, default 20); each "
                    "swept job fires its error continuation, so this bounds the burst"
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Jobs terminated by the staleness sweep",
            type=ParameterType.OBJECT,
            properties={
                "swept": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "One entry per swept job: job_id, provider_name, "
                        "last_updated_at, status_reason, update_accepted"
                    ),
                    required=False,
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="How many jobs were swept",
                    required=False,
                ),
            },
            usage_patterns=[
                "Clear jobs whose worker died mid-run, so their flows can close",
                "Bound a stuck-job backlog for one plugin after a crash",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def sweep_stale_jobs(
        self,
        max_age_minutes: int,
        plugin_name: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Fail jobs stuck in 'processing' past a caller-supplied window.

        Service interface receives individual kwargs (action_processor pattern).
        MUTATING: each swept job is failed through ``AsyncJobManager.update_job``,
        which fires its error continuation and resolves its flow token. The
        recorded status_reason names the sweep, so a swept job is never
        mistaken for a failure a worker actually reported.
        """
        ...
