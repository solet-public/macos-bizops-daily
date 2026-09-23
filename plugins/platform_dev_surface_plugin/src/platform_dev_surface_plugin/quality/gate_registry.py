"""Server-side quality-gate registry — the allowlist IS the security boundary.

Every gate a caller can run through ``quality_service::run_gate`` is declared
here with a FIXED command form. Callers pick a gate by NAME; they never pass a
path, argv, or flag. An unknown name resolves to ``None`` (a typed rejection at
the operation layer) — no free-form argv ever reaches the shell.

Two runnability classes:

* ``directly_runnable=True`` — self-contained: the script self-locates its own
  scope (the aggregate ``code_quality_check.py``) or walks the tree from its
  own anchors (the integration / sql / warn gates). Argv is
  ``[py, script, (--allowlist X), (--warn-only)]``.
* ``directly_runnable=False`` — the per-file coherence trio (god_class /
  radon_cc / radon_mi). These require the canonical per-file scope path list
  (``code_quality_check._per_file_gate_paths``). We deliberately do NOT
  re-derive that scope here — a fourth copy would drift from
  ``code_quality_check._per_file_gate_paths`` and the KB "Peer Pre-Completion
  Gate Procedure". They are ENUMERATED for discovery and run as
  part of the aggregate ``code_quality`` gate, which applies the canonical
  scope. ``list_gates`` marks them ``run_via: code_quality``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Per-gate timeout table (Q4). Heavy tree-walkers + the aggregate + the full
# smoke suite get 600s; the lighter tree-walking gates get 300s.
_TIMEOUT_AGGREGATE = 600
_TIMEOUT_HEAVY_WALK = 600
_TIMEOUT_WALK = 300

# The aggregate gate name — the C1 joseki card's verb target; covers ruff,
# pyright, the coherence trio (canonical scope), W-INT, and service-interface
# AST in one self-orchestrating run.
AGGREGATE_GATE_NAME = "code_quality"


@dataclass(frozen=True)
class GateSpec:
    """One declared gate + its fixed server-side command form."""

    name: str
    kind: str  # aggregate | integration | sql | warn | coherence
    description: str
    timeout_seconds: int
    script: str | None = None  # repo-relative path under quality_gates/
    allowlist: str | None = None  # repo-relative path under quality_gates/
    extra_flags: tuple[str, ...] = field(default_factory=tuple)
    directly_runnable: bool = True


_GATES: tuple[GateSpec, ...] = (
    GateSpec(
        name=AGGREGATE_GATE_NAME,
        kind="aggregate",
        description=(
            "Whole-tree aggregate commit gate: ruff, pyright --strict, the "
            "coherence trio (god_class / radon_cc / radon_mi at canonical "
            "scope), whole-tree integration, and service-interface AST. The "
            "authoritative single gate — the run_joseki C1 card's target."
        ),
        timeout_seconds=_TIMEOUT_AGGREGATE,
        script="quality_gates/code_quality_check.py",
    ),
    GateSpec(
        name="whole_tree_integration",
        kind="integration",
        description=(
            "Whole-tree integration gate (W-INT): walks service public.py, "
            "plugin.py, KB JSON dirs, and scheduling call-sites for cross-tree "
            "wiring drift. Self-locating; no scope-path argv."
        ),
        timeout_seconds=_TIMEOUT_HEAVY_WALK,
        script="quality_gates/whole_tree_integration_gate.py",
        allowlist="quality_gates/whole_tree_integration_gate_allowlist.txt",
    ),
    GateSpec(
        name="service_interface_ast",
        kind="integration",
        description=(
            "Service-interface AST consistency: decorator name == function "
            "name, every service-ABC abstractmethod carries the decorator, "
            "every processes/<provider>/*.json matches a registered verb."
        ),
        timeout_seconds=_TIMEOUT_WALK,
        script="quality_gates/service_interface_ast_check.py",
        allowlist="quality_gates/service_interface_ast_allowlist.txt",
    ),
    GateSpec(
        name="sql_access",
        kind="sql",
        description=(
            "SQL-access lockdown: no new raw SQL / hand-DDL / direct driver "
            "use outside the sanctioned state-interface owner. Tree-walking, "
            "allowlist-gated."
        ),
        timeout_seconds=_TIMEOUT_WALK,
        script="quality_gates/sql_access_gate.py",
        allowlist="quality_gates/sql_access_allowlist.txt",
    ),
    GateSpec(
        name="wint2_driver_import",
        kind="warn",
        description=(
            "W-INT Cycle 2 driver-import ratchet (WARN mode): flags NEW "
            "direct-driver-import drift. Findings print but do not block."
        ),
        timeout_seconds=_TIMEOUT_WALK,
        script="quality_gates/wint2_driver_import_check.py",
        allowlist="quality_gates/wint2_driver_import_allowlist.txt",
        extra_flags=("--warn-only",),
    ),
    GateSpec(
        name="wint2_vault_key_declaration",
        kind="warn",
        description=(
            "W-INT Cycle 2 vault-key-declaration ratchet (WARN mode): flags "
            "scoped vault reads missing a key declaration. Findings print but "
            "do not block."
        ),
        timeout_seconds=_TIMEOUT_WALK,
        script="quality_gates/wint2_vault_key_declaration_check.py",
        allowlist="quality_gates/wint2_vault_key_declaration_allowlist.txt",
        extra_flags=("--warn-only",),
    ),
    GateSpec(
        name="god_class",
        kind="coherence",
        description=(
            "God-class structure (coherence-aware): non-process LOC / method / "
            "attr thresholds. Per-file scope; run via the code_quality "
            "aggregate to apply the canonical scope."
        ),
        timeout_seconds=_TIMEOUT_WALK,
        script="quality_gates/god_class_check.py",
        allowlist="quality_gates/god_class_allowlist.txt",
        directly_runnable=False,
    ),
    GateSpec(
        name="radon_cc",
        kind="coherence",
        description=(
            "Cyclomatic-complexity gate (A/B only, tracked-debt allowlist). "
            "Per-file scope; run via the code_quality aggregate."
        ),
        timeout_seconds=_TIMEOUT_WALK,
        script="quality_gates/radon_cc_check.py",
        allowlist="quality_gates/radon_cc_allowlist.txt",
        directly_runnable=False,
    ),
    GateSpec(
        name="radon_mi",
        kind="coherence",
        description=(
            "Maintainability-index gate (A/B only, coherence-aware strip). "
            "Per-file scope; run via the code_quality aggregate."
        ),
        timeout_seconds=_TIMEOUT_WALK,
        script="quality_gates/radon_mi_check.py",
        allowlist="quality_gates/radon_mi_allowlist.txt",
        directly_runnable=False,
    ),
)

_GATES_BY_NAME: dict[str, GateSpec] = {spec.name: spec for spec in _GATES}


def all_gates() -> tuple[GateSpec, ...]:
    """Every declared gate (for ``list_gates`` discovery)."""
    return _GATES


def runnable_gate(name: str) -> GateSpec | None:
    """The directly-runnable gate for ``name``, or ``None`` if unknown / indirect.

    Returns ``None`` for an unknown name AND for the enumerated-only coherence
    trio — both are rejections at the operation layer (the caller must run the
    trio via the ``code_quality`` aggregate).
    """
    spec = _GATES_BY_NAME.get(name)
    if spec is None or not spec.directly_runnable:
        return None
    return spec


def runnable_gate_names() -> tuple[str, ...]:
    """The allowlist of names ``run_gate`` accepts (for the typed error message)."""
    return tuple(spec.name for spec in _GATES if spec.directly_runnable)


def build_gate_argv(spec: GateSpec, repo_root: Path, venv_python: Path) -> list[str]:
    """Build the fixed server-side argv for a directly-runnable gate."""
    if spec.script is None:
        raise ValueError(f"gate '{spec.name}' has no script — not directly runnable")
    argv = [str(venv_python), str(repo_root / spec.script)]
    if spec.allowlist is not None:
        argv += ["--allowlist", str(repo_root / spec.allowlist)]
    argv += list(spec.extra_flags)
    return argv
