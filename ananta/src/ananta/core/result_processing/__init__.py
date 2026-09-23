"""Result processing — step-level dispatch for successful tool results.

The package owns:

- :class:`ResultProcessorKind` enum (``enums.py``)
- result-contract value objects, validation, and typed exceptions
  (``contracts.py``)
- step-level dispatch coordinator (``coordinator.py``, Assignment 3)
- deterministic continuation processor
  (``deterministic_continuation.py``, Assignment 3)
- shared process-level error-handler dispatcher (``error_dispatch.py``,
  Assignment 4)

Only the enum is re-exported at the package root.  ``contracts.py``
depends on :mod:`ananta.core.plans.types`, which in turn imports
:class:`ResultProcessorKind`; eagerly re-exporting contract symbols
from this ``__init__`` would create a circular import.  Callers that
need validators or value objects import them directly from
``ananta.core.result_processing.contracts``.
"""

from __future__ import annotations

from ananta.core.result_processing.enums import (
    ErrorProcessorKind,
    ResultProcessorKind,
)

__all__ = ["ErrorProcessorKind", "ResultProcessorKind"]
