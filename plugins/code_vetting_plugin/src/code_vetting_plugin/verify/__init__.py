"""Layer-3 adversarial verifier — the false-positive killer (Stream L3).

Consumes L1/L2 candidate findings (F1 records) and, per finding, runs
perspective-diverse skeptics **prompted to refute** the finding against the
platform rulebook (F2). A finding is confirmed only if the skeptics cannot
refute it; anything a skeptic can knock down — or that no majority upholds —
is dropped. Precision is the goal, so uncertainty resolves to *refuted*.

Module map:
  - ``lenses``   — the three refute perspectives (correctness / policy / reproduce)
  - ``rulebook`` — loads the F2 context object; classifies DO-NOT-FLAG + RB-SCOPE
  - ``prompts``  — assembles the skeptic prompt the inference substrate will send
  - ``dispatch`` — the skeptic-dispatch seam + a deterministic heuristic dispatcher
  - ``verifier`` — vote aggregation, F1 stamping, run summary/metrics
  - ``serde``    — F1 candidate/verified register JSON I/O
  - ``cli``      — the dogfood entry point

Interim F1 binding: this package imports the finding record + enums from the
sibling ``code_vetting_plugin.finding`` shim. At Wave-2 integration that shim
retires to Stream O's canonical ``code_vetting_plugin.models`` and the import
re-points there (the record is renamed ``Finding`` → ``FindingRecord``); L3 only
ever *stamps* ``verdict``/``layer`` — neither is a ``finding_id`` hash input — so
it never recomputes the canonical hash.
"""

from __future__ import annotations
