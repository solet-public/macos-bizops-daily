"""verb_import_closure_smoke.py — W3-B B3c: the R6 verb-import-closure invariant, PINNED.

A0 R6: the EDGE verbs (``vet_codebase`` / ``scan_quality_guidelines``) are L1-only — no
inference is reachable from them. Reviewer-C's A2 Focus-5 verified this structurally by
grepping the verb import closure. B3c adds an inference SEAM to the plugin (for W3-C's
joseki), so this smoke turns that one-time grep into a PERMANENT guard: importing the verb
surface (``code_vetting_plugin.plugin``) must NOT transitively import the inference subsystem
(``verify/*`` skeptic transports, ``inference_wiring``, the ``driver``/``l3_adapter`` L3 path).
The seam holds only because plugin.py imports those FUNCTION-LOCALLY (runtime) + under
``TYPE_CHECKING`` (annotations), so ``import plugin`` never pulls them in.

Each smoke runs in its own process (fresh ``sys.modules``), so this observes the real
first-import closure of the verb surface. Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys

# Import ONLY the verb surface — nothing from the inference seam.
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import code_vetting_plugin.plugin  # noqa: F401 — imported for its transitive closure, asserted below

# Modules that would mean inference leaked into the L1-only verbs' import path.
_FORBIDDEN_MARKERS = ("verify", "inference_wiring")
_FORBIDDEN_SUFFIXES = (".driver", ".l3_adapter")

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def main() -> int:
    loaded = sorted(m for m in sys.modules if m.startswith("code_vetting_plugin"))
    leaked = [
        m
        for m in loaded
        if any(marker in m.removeprefix("code_vetting_plugin") for marker in _FORBIDDEN_MARKERS)
        or m.endswith(_FORBIDDEN_SUFFIXES)
    ]
    # Non-vacuous: the verb surface itself MUST be loaded (guards against a no-op pass if the
    # import ever silently fails or the module is renamed).
    _check("code_vetting_plugin.plugin" in loaded, "code_vetting_plugin.plugin is in the import closure")
    _check(not leaked, f"R6: NO inference subsystem in the verb import closure (leaked: {leaked})")
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
