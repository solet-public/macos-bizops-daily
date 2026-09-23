# pyright: reportUnusedFunction=false
"""Module-level helpers extracted from ``wbs_authoring``.

WBS document normalization plus the ``ProcessSchemaLookup`` Protocol.
The multi-pass assembly / clamping helpers that used to live here were
retired with the qwen WBS push-authoring path (DEP-01). The owning
``WbsAuthoringService`` lives in ``wbs_authoring.py`` and imports from
this module.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from ananta.error_handling import FrameworkError

from default_thinking_plugin.constants import ErrorCode

logger = logging.getLogger(__name__)


class ProcessSchemaLookup(Protocol):
    """Lookup argument property schemas for a process key.

    Returns the full property dict (including ``minimum``, ``maximum``,
    ``enum`` constraints) for each argument, keyed by argument name.
    """

    def get_arg_properties(
        self, process_key: str,
    ) -> dict[str, dict[str, object]]: ...


_WBS_ID_LINE_RE = re.compile(r"^WBS ID:\s*\S+", re.MULTILINE)


def _normalize_wbs_id_line(content: str, canonical_id: str) -> str:
    """Ensure the WBS document's ``WBS ID:`` line matches the canonical ID.

    The thinking model sometimes writes a different ID than the platform
    assigned.  This rewrites the ``WBS ID:`` line so the stored document
    is always consistent with the DB record.
    """
    replacement = f"WBS ID: {canonical_id}"
    normalized, count = _WBS_ID_LINE_RE.subn(replacement, content, count=1)
    if count:
        logger.info("WBS_ID_NORMALIZE: rewrote WBS ID line to %s", canonical_id)
    return normalized


def _require_non_empty(value: str, name: str) -> None:
    if not value:
        raise FrameworkError(
            message=f"{name} is required",
            error_code=ErrorCode.PARAMETER_ERROR,
        )
