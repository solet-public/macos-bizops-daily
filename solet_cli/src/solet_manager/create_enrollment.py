"""``solet create`` enrolls what it created for ``solet-manager update`` (iss_836499b3).

Before this, create wrote only the v1 registry row, so every Manager-created instance was invisible
to ``update`` ("no v2 inventory record").  A verified create from the installed Manager's own seed
lock -- the file the inspection metadata calls the installed channel descriptor -- now runs the
create-origin import enrollment in-process, under the create's approval: the install decision is
the consent (rul_e45fb5b3), and enrollment writes Manager state only, never the target.

A create from an explicit ``--seed-lock`` or a named ``--seed`` is not a channel install; its
result says so.  An enrollment failure after a verified create is loud (``maintenance_enrollment_failed``
with the exact repair) and leaves the verified install untouched.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from .errors import ManagerError
from .existing_install_inspection import InspectionBoundaryViolation
from .import_enrollment import ImportRequest, enroll_import, import_resume_repair, preview_import
from .models import CommandResult, ExitCode, JsonValue
from .paths import ManagerPaths
from .release_lock import load_seed_lock


def installed_seed_lock_path() -> Path:
    """The formula seed lock ``solet create`` defaults to and the inspection metadata reads."""
    return Path(sys.prefix) / "share" / "solet" / "seed.lock.json"


def enroll_created_instance(
    paths: ManagerPaths, name: str, target: Path, seed_lock_path: Path, result: CommandResult
) -> CommandResult:
    """Attach ``maintenance_enrollment`` to a verified create result, enrolling when it is a channel install."""
    if seed_lock_path.resolve(strict=False) != installed_seed_lock_path().resolve(strict=False):
        return _with_enrollment(result, {"status": "not_applicable", "reason": "created from a seed lock no installed channel serves"})
    channel = load_seed_lock(seed_lock_path).channel_id
    if channel is None:
        return _with_enrollment(result, {"status": "not_applicable", "reason": "the installed seed lock names no channel"})
    request = ImportRequest(name, target, channel, paths)
    try:
        preview = preview_import(request)
        enrolled = enroll_import(request, preview.fingerprint)
    except (ManagerError, ValueError, OSError, InspectionBoundaryViolation) as exc:
        return _failed(result, request, exc)
    return _with_enrollment(
        result,
        {
            "status": "enrolled",
            "channel_id": channel,
            "instance_id": preview.instance_id,
            "operation_id": preview.operation_id,
            "management_origin": enrolled.management_origin.value,
        },
    )


def _failed(result: CommandResult, request: ImportRequest, exc: Exception) -> CommandResult:
    """The create stays verified; the enrollment is finished by a documented verb (review B3), every stage idempotent."""
    if isinstance(exc, ManagerError):
        error_kind = exc.error_kind
    elif isinstance(exc, ValueError):
        error_kind = str(exc)
    else:
        error_kind = type(exc).__name__
    repair = (
        f"The Solet is installed and verified. {import_resume_repair(request.name, str(request.target), request.channel)} "
        f"Or run `solet-manager update {request.name} --dry-run`, then `--yes` with the fingerprint it prints: "
        "that finishes the enrollment before the update."
    )
    enrollment: dict[str, JsonValue] = {"status": "failed", "error_kind": error_kind, "message": str(exc)}
    return replace(
        result,
        message=f"{result.message} Enrollment for `solet-manager update` failed: {exc}",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="maintenance_enrollment_failed",
        repair=repair,
        data={**result.data, "maintenance_enrollment": enrollment},
    )


def _with_enrollment(result: CommandResult, enrollment: dict[str, JsonValue]) -> CommandResult:
    return replace(result, data={**result.data, "maintenance_enrollment": enrollment})
