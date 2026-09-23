"""Shared lock, preview, approval, and apply ceremony for reconciliations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from .models import CommandResult
from .paths import ManagerPaths
from .state_io import instance_lock

Prepared = TypeVar("Prepared")


def run_reconciliation_ceremony(
    *,
    paths: ManagerPaths,
    name: str,
    dry_run: bool,
    approved_fingerprint: str | None,
    recover: Callable[[], bool],
    prepare: Callable[[], Prepared],
    preview_result: Callable[[Prepared, bool], CommandResult],
    approval_required: Callable[[CommandResult], CommandResult],
    drift_result: Callable[[CommandResult], CommandResult],
    apply: Callable[[Prepared], CommandResult],
    fingerprint: Callable[[Prepared], str],
) -> CommandResult:
    """Execute the common reconciliation control ceremony under one lock.

    Recovery can write an interrupted receipt even for a preview, so every
    invocation creates the same instance lock before it observes or changes
    reconciliation state.
    """

    with instance_lock(paths.lock_path(name), create=True):
        recovered = recover()
        prepared = prepare()
        preview = preview_result(prepared, recovered)
        if dry_run:
            return preview
        if approved_fingerprint is None:
            return approval_required(preview)
        if approved_fingerprint != fingerprint(prepared):
            return drift_result(preview)
        return apply(prepared)
