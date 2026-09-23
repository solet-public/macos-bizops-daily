"""The per-scanner result envelope — L1's one genuinely-local type.

A scanner returns a :class:`ScannerResult`: the findings it produced plus a
:class:`CoverageRecord` proving what it did — or could not — examine. The
``CoverageRecord`` type itself is Stream O's (``run_record.py``, the
``vetting_runs`` domain) so exactly one definition exists and L1's coverage feeds
O's report + metrics unchanged; it is re-exported here for scanner ergonomics.
The F1 ``Finding`` record is the shared ``models.py`` binding. A tool that is
absent records the gap on its ``CoverageRecord`` rather than silently passing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import Finding
from .run_record import CoverageRecord

if TYPE_CHECKING:
    from .scanners.dead_code import DeadSymbolsReport
    from .scanners.structural_metrics import StructuralMetricsReport

__all__ = ["CoverageRecord", "ScannerResult"]


@dataclass(frozen=True, slots=True)
class ScannerResult:
    """What each scanner returns: its findings plus its coverage evidence.

    ``structural_metrics`` (R8-1) / ``dead_symbols`` (R9-A) are the optional typed report payloads
    a scanner attaches for its report section + trend persistence; every other scanner leaves them
    None (ruling §E — extend ScannerResult, not CoverageRecord). ``run_all`` collects them into the
    ``L1ReportData`` container so a report-supplementary payload never grows run_all's return arity.
    """

    findings: list[Finding]
    coverage: CoverageRecord
    structural_metrics: StructuralMetricsReport | None = None
    dead_symbols: DeadSymbolsReport | None = None
