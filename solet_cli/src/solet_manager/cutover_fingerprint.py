"""Manager-side canonical approval fingerprint for one cutover ceremony."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .cutover_receipts import CutoverTerms
from .errors import StateConflictError
from .models import JsonValue

__all__ = ["cutover_fingerprint"]


def cutover_fingerprint(
    *,
    name: str,
    target: str,
    seed: dict[str, JsonValue],
    files: tuple[dict[str, JsonValue], ...],
    terms: CutoverTerms,
) -> str:
    """Hash the entire approved act, including every cutover precondition.

    The caller supplies the fixed-contract surface digests. Computing those
    digests is deliberately not duplicated here: the target-side shared digest
    implementation is the sole authority for their construction.
    """

    if not name or not Path(target).is_absolute() or not files:
        raise StateConflictError("cutover fingerprint identity is incomplete")
    payload: dict[str, JsonValue] = {
        "name": name,
        "target": target,
        "seed": seed,
        "files": list(files),
        "cutover": terms.to_dict(),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
