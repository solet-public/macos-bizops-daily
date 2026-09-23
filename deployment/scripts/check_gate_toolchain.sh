#!/usr/bin/env bash
# Fail-loud preflight for the platform commit gate's required toolchain.
#
# Single source of truth for the "hard-always" Class-B gate tools (ruff,
# pyright, radon) that the git-controller-commit skill's own procedure
# (Steps 2, 3, 4-5) invokes but never checks for and never declares
# anywhere. Undeclared-dependency audit:
# workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md
#
# Deliberately NOT absence-tolerant, unlike code_vetting_plugin's
# `typecheck`/`coverage` extras (correctly optional: a missing
# foreign-target scanner is a disclosed coverage gap, never a gate
# failure). These three back the commit gate itself: an absence-tolerant
# commit gate would silently skip static analysis on every commit.
#
# Checks each tool the way the gate actually invokes it, not a bare PATH
# lookup: ruff/pyright are run as `.venv/bin/<tool>` explicitly (per the
# skill's own Step 2/3 commands, never bare `ruff`/`pyright`); radon is
# imported as a Python library inside radon_cc_check.py/radon_mi_check.py,
# never shelled out to, so its check is an importability probe against the
# venv's own interpreter, not a binary-on-PATH check.
#
# Takes a required SET argument so this stays the one place the required
# sets are defined as Class A (OS-level system binaries, checked at
# `setup_clone.sh` time, before `pip install` can have run) grows a
# sibling set alongside `gate` -- see the audit doc's Class A section for
# the still-open, not-yet-built list. Only `gate` is implemented today.
#
# Called from TWO places today (do not add another definition of the
# `gate` set anywhere; extend this file instead):
#   1. .claude/skills/git-controller-commit/SKILL.md, Step 1.5 (gate time,
#      before Step 2's ruff -- the earliest tool consumer)
#   2. quality_gates/code_quality_check.py (direct/standalone runs)
# Deliberately NOT called from `setup_clone.sh`: that script is documented
# to run before `pip install`, so a `gate`-set check there would fail on
# every genuinely fresh clone regardless of whether the adopter went on to
# do the right thing next -- teaching "our tooling's errors are noise."
# When Class A's `system` set is built, `setup_clone.sh` calls THIS script
# with `system`, not `gate`.
#
# Exit 0: every tool in SET present. Exit 1: at least one missing, named,
# with the install remedy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python3"

SET_NAME="${1:-}"

if [[ "$SET_NAME" != "gate" ]]; then
  echo "usage: check_gate_toolchain.sh gate" >&2
  echo "  Only the 'gate' set (ruff/pyright/radon) is implemented. A 'system'" >&2
  echo "  set for Class-A OS-level binaries is tracked separately, not yet" >&2
  echo "  built -- see workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md" >&2
  exit 1
fi

_MISSING=()

for tool in ruff pyright; do
  if [[ ! -x "$REPO_ROOT/.venv/bin/$tool" ]]; then
    _MISSING+=("$tool")
  fi
done

if [[ ! -x "$VENV_PYTHON" ]] || ! "$VENV_PYTHON" -c "import radon" >/dev/null 2>&1; then
  _MISSING+=("radon")
fi

if [[ ${#_MISSING[@]} -gt 0 ]]; then
  echo "FAIL: the following gate-required tool(s) are not installed in .venv: ${_MISSING[*]}" >&2
  echo "These back the platform commit gate (git-controller-commit skill, Steps 2-5) and are" >&2
  echo "deliberately NOT optional -- unlike code_vetting_plugin's absence-tolerant scanners." >&2
  echo "Install with:" >&2
  echo "  .venv/bin/python -m pip install --no-build-isolation -e $REPO_ROOT/ananta[gate]" >&2
  exit 1
fi

exit 0
