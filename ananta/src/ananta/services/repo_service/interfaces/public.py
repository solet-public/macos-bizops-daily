"""Repo Service Public API.

``@service_interface_process``-decorated surface for the six read-only verbs of
:class:`RepoServiceInterface`. The bound provider (``platform_dev_surface_plugin``)
inherits the plain contract ABC and is reachable via
``service_interface::repo_service::*`` keys. Matching KB JSONs live at
``ananta/knowledge_base/processes/repo_service/*.json`` (dual-write).

Every verb is EDGE (structured return) with BOTH processor-customization blocks
on the decorator; ``requires_call_context=True`` logs the server-built principal
per repo read (audit). All returned fields are the platform's OWN source /
git / tool output (never user data) and are rated public (0.0) — the security
boundary is repo-root confinement + the denylist + the secret scrub, not
per-field redaction notes live with each verb's docs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext

PROVIDER = "repo_service"

_PATH_PARAM = ParameterMetadata(
    description="Repo-root-relative path. Resolved + confined to the worktree; '..'/symlink-escape/"
    "absolute-outside and denylisted paths (.git/, profile/ secrets, key material) are typed-rejected.",
    required=True,
    type=ParameterType.STRING,
)
_OPTIONAL_PATH_PARAM = ParameterMetadata(
    description="Optional repo-root-relative path (confined as above). Defaults to the repo root.",
    required=False,
    type=ParameterType.STRING,
)


def _obj(desc: str, props: dict[str, ParameterMetadata]) -> ReturnValueSchema:
    return ReturnValueSchema(type=ParameterType.OBJECT, description=desc, properties=props)


def _p(ptype: ParameterType, desc: str) -> ParameterMetadata:
    return ParameterMetadata(type=ptype, description=desc)




class RepoServicePublicAPI(ABC):
    """AI-discoverable read-only repo-inspection surface.

    Access via: ``service_interface::repo_service::{verb}``
    """

    @service_interface_process(
        name="search",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "query": ParameterMetadata(type=ParameterType.STRING, required=True,
                                       description="Regex/text query (rg-backed)."),
            "path_glob": ParameterMetadata(type=ParameterType.STRING, required=False,
                                           description="Optional include glob (e.g. '*.py')."),
            "max_results": ParameterMetadata(type=ParameterType.INTEGER, required=False, default=50,
                                             description="Result cap (hard-capped server-side at 200)."),
        },
        return_value_schema=_obj("Ranked search hits.", {
            "query": _p(ParameterType.STRING, "The query run."),
            "hits": _p(ParameterType.LIST, "[{path, line, snippet}] — snippets secret-redacted."),
            "truncated": _p(ParameterType.BOOLEAN, "True iff the result cap was hit."),
            "hit_count": _p(ParameterType.INTEGER, "Number of hits returned."),
        }),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="repo_search_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        requires_call_context=True,
    )
    @abstractmethod
    def search(self, query: str, path_glob: str | None = None, max_results: int = 50,
               *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Repo-root-confined rg content search; denylist-excluded, snippets redacted."""

    @service_interface_process(
        name="read_file",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "path": _PATH_PARAM,
            "start_line": ParameterMetadata(type=ParameterType.INTEGER, required=False,
                                            description="1-based inclusive start line."),
            "end_line": ParameterMetadata(type=ParameterType.INTEGER, required=False,
                                          description="1-based inclusive end line."),
        },
        return_value_schema=_obj("File content (optionally a line range).", {
            "path": _p(ParameterType.STRING, "Repo-root-relative path read."),
            "content": _p(ParameterType.STRING, "File content (bounded)."),
            "truncated": _p(ParameterType.BOOLEAN, "True iff byte/line cap trimmed the content."),
            "total_lines": _p(ParameterType.INTEGER, "True total line count of the file."),
        }),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="repo_read_file_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None,
                  *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read a confined file; REFUSES the whole file on a credential-shape hit (Q2)."""

    @service_interface_process(
        name="list_files",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "path": _OPTIONAL_PATH_PARAM,
            "depth": ParameterMetadata(type=ParameterType.INTEGER, required=False, default=1,
                                       description="Recursion depth (>=1)."),
            "glob": ParameterMetadata(type=ParameterType.STRING, required=False,
                                      description="Optional file-name glob filter."),
        },
        return_value_schema=_obj("Directory listing.", {
            "base": _p(ParameterType.STRING, "The listed directory (repo-root-relative or '.')."),
            "entries": _p(ParameterType.LIST, "[{path, type}] — denylisted entries excluded."),
            "truncated": _p(ParameterType.BOOLEAN, "True iff the entry cap was hit."),
        }),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="repo_list_files_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def list_files(self, path: str | None = None, depth: int = 1, glob: str | None = None,
                   *, call_context: CallContext | None = None) -> dict[str, Any]:
        """List a confined directory (bounded depth), excluding denylisted paths."""

    @service_interface_process(
        name="git_status",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={},
        return_value_schema=_obj("Porcelain working-tree status.", {
            "branch": _p(ParameterType.STRING, "Current branch."),
            "staged": _p(ParameterType.LIST, "Staged paths."),
            "unstaged": _p(ParameterType.LIST, "Unstaged/unmerged paths."),
            "untracked": _p(ParameterType.LIST, "Untracked paths."),
        }),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="repo_git_status_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        requires_call_context=True,
    )
    @abstractmethod
    def git_status(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read-only ``git status --porcelain=v2 --branch``."""

    @service_interface_process(
        name="git_diff",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "ref": ParameterMetadata(type=ParameterType.STRING, required=False,
                                     description="Optional ref/commit to diff against."),
            "path": _OPTIONAL_PATH_PARAM,
            "staged": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, default=False,
                                        description="Diff the staged (--cached) changes."),
        },
        return_value_schema=_obj("Read-only diff + stat.", {
            "diff": _p(ParameterType.STRING, "Unified diff (bounded)."),
            "truncated": _p(ParameterType.BOOLEAN, "True iff the diff cap trimmed output."),
            "diff_chars_total": _p(ParameterType.INTEGER, "True total diff length before capping."),
            "stat": _p(ParameterType.STRING, "The --stat summary."),
        }),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="repo_git_diff_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        requires_call_context=True,
    )
    @abstractmethod
    def git_diff(self, ref: str | None = None, path: str | None = None, staged: bool = False,
                 *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read-only ``git diff`` (optional ref/path, staged flag)."""

    @service_interface_process(
        name="propose_patch",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "unified_diff": ParameterMetadata(type=ParameterType.STRING, required=True,
                                              description="The unified diff to store as an artifact (bounded)."),
        },
        return_value_schema=_obj("Stored patch-proposal artifact reference.", {
            "patch_id": _p(ParameterType.STRING, "Artifact id the operator/Git-Controller reads to apply."),
            "applies_cleanly": _p(ParameterType.BOOLEAN, "Result of a read-only `git apply --check`."),
            "path_count": _p(ParameterType.INTEGER, "Number of confined target paths in the diff."),
        }),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="repo_propose_patch_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def propose_patch(self, unified_diff: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Store a unified-diff artifact + apply-check flag; return patch_id. NO apply verb."""
