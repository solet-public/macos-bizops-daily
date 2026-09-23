"""Startup diagnostic consumer — NEVER BLOCKING.

Called from ``ananta.cli`` early in the orchestrator boot.  The diagnostic
walks REPO_ROOT, emits a structured report on drift, and ALWAYS returns
cleanly.  Schema-validation failures, YAML parse errors, and missing
manifests are all swallowed and logged as INFO — startup must never be
blocked from here per the 2026-06-15 PT operator lock.

Per-developer ignore patterns ride ``ANANTA_ROOT_MANIFEST_IGNORE_PATTERNS``
(colon-separated, additive to the manifest's diagnostic.ignore_patterns).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .classifier import classify_root_entries
from .report import format_report
from .types import ENV_EXTRA_IGNORE_PATTERNS, MANIFEST_FILENAME


def emit_startup_diagnostic(logger: logging.Logger, repo_root: Path) -> None:
    """Run the diagnostic + log a report on any drift.  NEVER raises."""
    try:
        manifest_path = repo_root / MANIFEST_FILENAME
        if not manifest_path.is_file():
            logger.info("root_manifest diagnostic: no manifest at %s (skipped)", manifest_path)
            return
        extra = _extra_ignore_patterns()
        classification = classify_root_entries(
            manifest_path, repo_root, extra_ignore_patterns=extra,
        )
        if classification.schema_validation_error is not None:
            logger.info(
                "root_manifest.yaml schema validation failed; falling back "
                "to UNGATED startup. error=%s",
                classification.schema_validation_error,
            )
            return
        if classification.has_any_drift:
            logger.info("\n%s", format_report(classification, severity="DIAGNOSTIC"))
        else:
            logger.info("root_manifest diagnostic: tree clean (0 drift)")
    except Exception as exc:  # noqa: BLE001 — third-layer must never raise
        logger.info("root_manifest diagnostic skipped (%s)", exc)


def _extra_ignore_patterns() -> tuple[str, ...]:
    raw = os.environ.get(ENV_EXTRA_IGNORE_PATTERNS, "")
    return tuple(part for part in raw.split(":") if part)
