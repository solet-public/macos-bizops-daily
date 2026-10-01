"""The bounded diff the seed shows for an adopted managed artifact (iss_d1f3371b, r65).

A ``launchd_plist`` that matches no release's render but is this solet's own ``legacy_direct`` plist is replaced through ``render_whole``.  The seed
reports what the replacement changes as one evidence item per artifact, with its own id prefix: the Manager decodes every ``artifact.``-prefixed
item as a state row, so a diff carrying that prefix would fail as an undeclared artifact.  Each line of the diff arrives as ``NNN=<line>`` so the
array is unique and every entry is a ``key=value`` pair; this module strips the index.
"""

from __future__ import annotations

from typing import cast

from .adapter_protocol import OperationResult
from .errors import AdapterProtocolError

__all__ = ["ADOPT_DIFF_EVIDENCE_PREFIX", "adopt_diffs"]

ADOPT_DIFF_EVIDENCE_PREFIX = "adopt_diff."


def adopt_diffs(result: OperationResult, declared_artifact_ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """The diff lines per adopted artifact id in a probe result; a diff for an artifact the plan did not declare is a protocol error."""
    diffs: dict[str, tuple[str, ...]] = {}
    for item in result.evidence:
        evidence_id = str(item["id"])
        if not evidence_id.startswith(ADOPT_DIFF_EVIDENCE_PREFIX):
            continue
        artifact_id = evidence_id.removeprefix(ADOPT_DIFF_EVIDENCE_PREFIX)
        if artifact_id not in declared_artifact_ids:
            raise AdapterProtocolError(f"adapter reported an adopt diff for an undeclared artifact {artifact_id!r}")
        observed = item["observed"]
        if not isinstance(observed, list) or not all(isinstance(line, str) and "=" in line for line in observed):
            raise AdapterProtocolError("adapter adopt diff must be an indexed string array")
        diffs[artifact_id] = tuple(line.partition("=")[2] for line in cast(list[str], observed))
    return diffs
