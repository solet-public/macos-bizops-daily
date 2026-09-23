"""code_vetting_plugin — the AI code-vetting suite's runtime verb surface.

Two EDGE ``@platform_process`` verbs expose the **deterministic** layer of the
suite (A0 R6): ``vet_codebase`` runs the full L1 scanner pipeline;
``scan_quality_guidelines`` runs the platform quality-gate toolchain (advisory,
R5). Both return fast with a bounded metrics summary + a bounded markdown report.
The minutes-long L2 critic / L3 inference-verify stages are **not** verbs (they
would blow the action-queue fast-return contract) — they are joseki-dispatched
agent sessions (W3-C).

Target anchoring is deploy-invariant: the scanned tree is the git **worktree**,
located from ``APP_HOME`` (NOT ``__file__``, which points at a materialized
release copy with no ``.git`` in a blue-green deploy). The gate SCRIPTS still
come from the code root via the engine's ``platform_gates`` wrappers (which run
``cwd=<worktree>`` with the shipped ``quality_gates/`` snapshot), so the worktree
is what gets scanned. Local-profile only: a cloud solet has no git worktree,
so ``locate_worktree_root`` fail-louds there — exclude from cloud manifests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from ananta.interfaces.memory_service_interface import MemoryServiceInterface
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.types.schema_types import SchemaDefinition

from .live_state import get_vetting_runs_schema, read_recent_runs, read_vetting_run
from .metrics import persist_run_sync
from .models import ContextProfile, Finding, Layer
from .report import ReportRenderer
from .run_context import allowlist_totals, git_head, system_clock
from .run_record import (
    AllowlistDelta,
    CoverageRecord,
    RunTarget,
    build_run_metrics,
    counts_by_dimension,
    counts_by_severity,
    coverage_gaps,
)
from .runner import L1ReportData, run_all
from .scanners import dead_code, platform_gates
from .scheduled_vet import REGRESSION_MEMORY_TAG, SingleSlotVetExecutor, is_regression
from .source_roles import (
    RoleOverride,
    RoleOverrideValidationError,
    build_source_role_partition,
    parse_role_overrides,
)
from .stacks import detect_stacks, render_stacks
from .targets import TargetTree
from .technology_fingerprint import build_technology_fingerprint

if TYPE_CHECKING:
    # Annotation-only imports for the B3c inference SEAM. The inference subsystem is
    # imported FUNCTION-LOCALLY in the seam methods (not at module top), so it never
    # enters the L1-only verbs' runtime import closure (Reviewer-C A2 Focus-5 invariant).
    from .driver import VettingDriver
    from .inference_wiring import InferenceCompleter
    from .metrics import MetricsWriter
    from .verify.inference import LocalInferenceSkepticTransport, SubprocessSkepticTransport
    from .verify.substrate import Substrate

_PLUGIN_NAME = "code_vetting_plugin"
_VET_RESULT_TYPE = "code_vetting_run_result"
_SCAN_RESULT_TYPE = "code_vetting_quality_guidelines_result"
_GET_RUN_RESULT_TYPE = "code_vetting_run_record"
_REPORT_CHAR_CAP = 24000
# The substrate string for an L1-only (no-inference) persisted run — matches VettingDriver's default.
_L1_SUBSTRATE = "heuristic"
_ROLE_OVERRIDES_ABSENT = object()

# A scan step: (target tree, run id) -> (findings, coverage, L1ReportData). Both the full L1
# pipeline (run_all) and the quality-gate subset share this shape; the gate subset carries an empty
# L1ReportData (no report-supplementary payloads), the full pipeline carries the R8-1/R9-A payloads.
ScanFn = Callable[[TargetTree, str], tuple[list[Finding], list[CoverageRecord], L1ReportData]]


def _run_target(
    *,
    root: Path,
    ref: str,
    scope: str,
    tree: TargetTree,
    findings: list[Finding],
    overrides: tuple[RoleOverride, ...],
    overall_by_dimension: dict[str, int],
    foreign: bool,
) -> RunTarget:
    """Build the self-compatible target or the additive foreign partition."""

    if not foreign:
        return RunTarget(repo=root.name, ref=ref, scope=scope)
    return RunTarget(
        repo=root.name,
        ref=ref,
        scope=scope,
        source_role_partition=build_source_role_partition(
            tree=tree,
            findings=findings,
            overrides=overrides,
            overall_by_dimension=overall_by_dimension,
        ),
        technology_fingerprint=build_technology_fingerprint(
            tree=tree,
            overrides=overrides,
        ),
    )


def _foreign_inventory(
    tree: TargetTree, foreign: bool
) -> tuple[str | None, int | None, str]:
    if foreign:
        return tree.enumeration, len(tree.all_files()), "foreign"
    return None, None, "self"


def _run_allowlist_delta(root: Path, foreign: bool) -> AllowlistDelta:
    if foreign:
        return AllowlistDelta(totals={})
    return AllowlistDelta(totals=allowlist_totals(root))


def locate_worktree_root(app_home: Path) -> Path:
    """The git worktree root, by walking UP from ``APP_HOME`` (deploy-invariant).

    Anchors at ``APP_HOME`` (``<worktree>/profile``), NOT ``__file__`` — a
    blue-green deploy runs the platform from a materialized release copy (no ``.git``)
    spawned with the SAME app_home, so ``APP_HOME`` resolves the worktree
    identically in the direct-launch and release-copy contexts. Returns the first
    ancestor carrying BOTH ``quality_gates/`` and ``.git``; fail-loud otherwise (a
    cloud solet has no git worktree — this plugin is local-profile only).
    """
    anchor = Path(app_home).resolve()
    for candidate in (anchor, *anchor.parents):
        if (candidate / "quality_gates").is_dir() and (candidate / ".git").exists():
            return candidate
    raise RuntimeError(
        f"{_PLUGIN_NAME}: could not locate the git worktree root from app_home "
        f"{anchor} — no ancestor carries both quality_gates/ and .git. The vetting "
        "verbs require the platform git worktree (local-profile only)."
    )


def _collect_quality_gates(
    tree: TargetTree, run_id: str
) -> tuple[list[Finding], list[CoverageRecord], L1ReportData]:
    """Run ONLY the platform quality-gate toolchain (the ``scan_quality_guidelines`` surface).

    Includes the rebuilt dead-code scanner (R9-A, replacing the old vulture stub this surface always
    ran), so its candidate-dead-symbols payload rides through; the structural-metrics section belongs
    to the full ``vet_codebase`` pipeline, so that payload stays None here.
    """
    results = [
        platform_gates.scan_code_quality(tree, run_id),
        platform_gates.scan_sql_access(tree, run_id),
        dead_code.scan(tree, run_id),
    ]
    findings = [finding for result in results for finding in result.findings]
    coverage = [result.coverage for result in results]
    dead_symbols = next((result.dead_symbols for result in results if result.dead_symbols is not None), None)
    return findings, coverage, L1ReportData(dead_symbols=dead_symbols)


def _bound_report(report: str) -> str:
    """Cap the rendered report; append an explicit truncation marker (no silent cap)."""
    if len(report) <= _REPORT_CHAR_CAP:
        return report
    return (
        f"{report[:_REPORT_CHAR_CAP]}\n\n"
        f"… report truncated at {_REPORT_CHAR_CAP} chars — run the L1 CLI "
        "(`python -m code_vetting_plugin.l1_cli`) for the full findings register."
    )


def _vetting_data_schema() -> ReturnValueSchema:
    """The ``data`` payload shape shared by both verbs (bounded metrics + report)."""
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="One deterministic vetting run: a bounded metrics summary plus a bounded markdown report.",
        properties={
            "run_id": ParameterMetadata(type=ParameterType.STRING, description="Unique run id (vr-<tag>-<ref>-<stamp>)."),
            "target": ParameterMetadata(
                type=ParameterType.OBJECT,
                description=(
                    "What was examined: {repo, ref, scope}; a foreign run also carries "
                    "the bounded source_role_partition and technology_fingerprint "
                    "(scoped declarations/topology plus deterministic capability needs; "
                    "routing is not execution). ref is empty in walk-enumeration mode "
                    "(no invented provenance)."
                ),
            ),
            "target_class": ParameterMetadata(
                type=ParameterType.STRING,
                description="Derived target class: 'self' (the platform's own worktree) or 'foreign' (an external target_path).",
            ),
            "enumeration": ParameterMetadata(
                type=ParameterType.STRING,
                description="How the file view was built: 'git' (git ls-files) or 'walk' (structural walk fallback for a non-git target).",
            ),
            "files_enumerated": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Number of files enumerated in the target tree.",
            ),
            "stacks": ParameterMetadata(
                type=ParameterType.LIST,
                description="Detected language stacks (python/typescript/javascript) — the engine's provenance for what the target is; gates the stack-specific scanners.",
            ),
            "started": ParameterMetadata(type=ParameterType.STRING, description="ISO-8601 UTC run start."),
            "finished": ParameterMetadata(type=ParameterType.STRING, description="ISO-8601 UTC run finish."),
            "total_findings": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Total findings emitted across every scanner (all severities/layers).",
            ),
            "counts_by_severity": ParameterMetadata(
                type=ParameterType.OBJECT,
                description="Severity histogram (blocker/high/medium/low/advisory -> count).",
            ),
            "counts_by_dimension": ParameterMetadata(
                type=ParameterType.OBJECT,
                description="Dimension histogram (secrets/code_quality/deps/... -> count).",
            ),
            "scanners_ran": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Scanners that executed (a tool absent records a coverage gap instead of silently passing).",
            ),
            "scanners_total": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Total scanners in this verb's pipeline.",
            ),
            "coverage_gaps": ParameterMetadata(
                type=ParameterType.LIST,
                description="Scanners that could not run, each with its reason — surfaced, not swallowed.",
            ),
            "report": ParameterMetadata(
                type=ParameterType.STRING,
                description="Severity-ranked markdown report (bounded; truncation-marked if capped).",
            ),
        },
    )


def _scope_param(default: str) -> dict[str, ParameterMetadata]:
    """The single optional ``scope`` label param, defaulted per verb."""
    return {
        "scope": ParameterMetadata(
            type=ParameterType.STRING,
            required=False,
            default=default,
            description=f"Optional scope label recorded on the run (default '{default}'). Cosmetic; the target defaults to the local worktree.",
        )
    }


class TargetValidationError(ValueError):
    """A caller-supplied ``target_path`` failed a fail-loud validation check (FT-1).

    The message names the specific check that failed so the refusal is actionable.
    """


_TARGET_PATH_PARAM = ParameterMetadata(
    type=ParameterType.STRING,
    required=False,
    description=(
        "Optional ABSOLUTE path to an EXTERNAL repo/source tree to vet (a FOREIGN "
        "target). Absent -> self-vet of the platform's own worktree (default, byte-compatible "
        "with today). Fail-loud validation: the path must be absolute, exist, and be "
        "a directory, and must NOT resolve inside/around the platform's own worktree (self-vet "
        "is the default there). Read-only: the engine never writes into the target. "
        "Enumeration is `git ls-files` when the target has a `.git`, else a structural "
        "filesystem walk (curated excludes, NO gitignore emulation); platform-canon scanners "
        "(code_quality / sql_access / orphan_kb / prior_pass) skip EXECUTION on a foreign "
        "target but stay in the coverage ledger as `not_applicable`."
    ),
)


_EXECUTE_TOOLCHAIN_PARAM = ParameterMetadata(
    type=ParameterType.BOOLEAN,
    required=False,
    default=False,
    description=(
        "Opt-in (default false) to run the TARGET's OWN toolchain — the strict TypeScript "
        "typecheck (tsc) and the eslint sweep (the materialized-deps tier, R7). WARNING: this "
        "EXECUTES TARGET-CONTROLLED CODE on the scan host — the target's eslint config is executed "
        "JavaScript, its parser/plugins load from the target's node_modules, and the tsc/eslint "
        "binaries under node_modules/.bin are target-supplied. Prefer the Phase-2 sandbox for "
        "foreign repos; on-host execution is a DISCOURAGED, explicitly-flagged escape hatch. When "
        "unset, tsc/eslint record an opt-in coverage gap instead of running (every other scanner is "
        "unaffected). Also requires the target's node_modules to be MATERIALIZED out-of-band — the "
        "engine never installs (read-only invariant)."
    ),
)


_PERSIST_PARAM = ParameterMetadata(
    type=ParameterType.BOOLEAN,
    required=False,
    default=False,
    description=(
        "Opt-in (default false) to PERSIST this run to the vetting_runs trail so get_vetting_run can read "
        "it back by run_id (the joseki's payload-by-run_id seam). Default-off keeps the bare verb a pure "
        "read-through with byte-identical output; when true the return payload is UNCHANGED (persist is a "
        "side-effect) and the run is written under substrate 'heuristic' (L1-only, no inference), bounded by "
        "the same retention as the driver's trail. Self-anchored deterministic runs; no effect on the report."
    ),
)

_ROLE_OVERRIDES_PARAM = ParameterMetadata(
    type=ParameterType.LIST,
    required=False,
    description=(
        "Optional foreign-target-only source-role declarations. At most 32 objects, "
        "each exactly {path_prefix, role, doctrine_locator, declaring_session}. "
        "path_prefix is a literal normalized tree-relative path and must match an enumerated file; role "
        "is product | test | docs | examples_reference | vendored | build_tooling. "
        "Longest boundary-aware prefix wins. Overrides partition only surface-quality "
        "findings; repository risks such as secrets and dependencies remain repo-wide. "
        "Path-convention candidates never move findings."
    ),
)


def _vet_codebase_params() -> dict[str, ParameterMetadata]:
    """vet_codebase params — scope, target, role attribution, target toolchain, persistence."""
    return {
        **_scope_param("whole-tree self-vet"),
        "target_path": _TARGET_PATH_PARAM,
        "role_overrides": _ROLE_OVERRIDES_PARAM,
        "execute_target_toolchain": _EXECUTE_TOOLCHAIN_PARAM,
        "persist": _PERSIST_PARAM,
    }


def _get_vetting_run_params() -> dict[str, ParameterMetadata]:
    """get_vetting_run params — the run_id to read back (the joseki binds <<step_1.result.run_id>>)."""
    return {
        "run_id": ParameterMetadata(
            type=ParameterType.STRING,
            required=True,
            description="The run id of a PERSISTED vetting run (the vr-<tag>-<ref>-<stamp> value a prior driver-backed run returned). Reads the stored record from the vetting_runs trail.",
        )
    }


def _vetting_run_record_schema() -> ReturnValueSchema:
    """The ``data`` shape get_vetting_run returns: the PERSISTED vetting_runs row + the report text.

    This is the read-back shape of a driver-persisted run (RunMetrics.to_dict), NOT the inline
    vet_codebase payload — the joseki's L2/L3 steps read report + evidence by run_id from here.
    """
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="One persisted vetting run: the bounded metrics row (severity/dimension histograms, coverage, substrate) + the structural-metrics and candidate-dead-symbols evidence payloads + the severity-ranked markdown report.",
        properties={
            "run_id": ParameterMetadata(type=ParameterType.STRING, description="Unique run id (the read key)."),
            "target": ParameterMetadata(
                type=ParameterType.OBJECT,
                description=(
                    "What was examined: {repo, ref, scope}; persisted foreign runs "
                    "also carry the bounded source_role_partition and "
                    "technology_fingerprint in this existing JSON object."
                ),
            ),
            "started": ParameterMetadata(type=ParameterType.STRING, description="ISO-8601 UTC run start."),
            "finished": ParameterMetadata(type=ParameterType.STRING, description="ISO-8601 UTC run finish."),
            "substrate": ParameterMetadata(type=ParameterType.STRING, description="Which inference engine reviewed/refuted: heuristic | local_inference | subscription."),
            "layers_run": ParameterMetadata(type=ParameterType.LIST, description="Layers executed (L1/L2/L3)."),
            "files_examined": ParameterMetadata(type=ParameterType.LIST, description="Per-scanner coverage evidence."),
            "counts_by_severity": ParameterMetadata(type=ParameterType.OBJECT, description="Severity histogram (blocker/high/medium/low/advisory -> count)."),
            "counts_by_dimension": ParameterMetadata(type=ParameterType.OBJECT, description="Dimension histogram (secrets/code_quality/deps/... -> count)."),
            "survival_rate": ParameterMetadata(type=ParameterType.FLOAT, description="L2->L3 precision proxy; null when nothing was verified."),
            "coverage_gaps": ParameterMetadata(type=ParameterType.LIST, description="Scanners that could not run, each with its reason."),
            "allowlist_delta": ParameterMetadata(type=ParameterType.OBJECT, description="Tracked-debt burn-down snapshot."),
            "structural_metrics": ParameterMetadata(type=ParameterType.OBJECT, description="R8-1: per-run complexity/length/params/nesting distribution + aggregates + worst-offenders + the repeated-literal table (magic-string candidates) + the named nested-loop pattern class (nested_loops[] {file,line,language,depth} capped, nested_loops_total = the true pre-cap count, nested_loop_languages[] + nested_loop_files_examined = the class's own denominator, narrower than files_parsed because the detector is language-limited); null when the structural-metrics scanner did not run."),
            "dead_symbols": ParameterMetadata(type=ParameterType.OBJECT, description="R9-A: candidate-dead-symbols payload (60%-confidence family — L2 targeting evidence, the test_reach pattern); null when the dead-code scanner did not run."),
            "report": ParameterMetadata(type=ParameterType.STRING, description="The severity-ranked markdown report text for this run; null for a metrics-only subset run."),
        },
    )


def _scheduled_receipt_schema() -> ReturnValueSchema:
    """The trigger_scheduled_self_vet receipt: whether the daily self-vet started or the slot was busy."""
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="A fast heartbeat receipt for the scheduled self-vet trigger.",
        properties={
            "self_vet": ParameterMetadata(
                type=ParameterType.STRING,
                description="'started' — the daily self-vet was submitted to its single background slot; 'already_running' — a run is in-flight (the slot IS the lease) so this fire is a no-op.",
            ),
        },
    )


def _counts_of(row: Mapping[str, object]) -> dict[str, int]:
    """The counts_by_severity histogram of a persisted vetting_runs row (severity -> int); {} when absent."""
    counts = row.get("counts_by_severity")
    return {key: value for key, value in counts.items() if isinstance(value, int)} if isinstance(counts, dict) else {}


class CodeVettingPlugin(PluginBase, EdgeProcessProvider):
    """Runtime verb surface for the AI code-vetting suite (deterministic layer only)."""

    name: str = _PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = _PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)
        self._worktree_root: Path | None = None
        # W3C-3: the single-slot lease for the daily self-vet (the slot IS the lease — a fire while a run is
        # in-flight is skipped-busy). A lightweight semaphore holder; the scan runs on a daemon thread.
        self._vet_executor = SingleSlotVetExecutor()

    def prepare_for_readiness(self) -> None:
        """Anchor the scan target at the git worktree (via APP_HOME), then mark ready."""
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{_PLUGIN_NAME}: orchestrator_ref not injected before readiness")
        self._worktree_root = locate_worktree_root(Path(self.orchestrator_ref.APP_HOME))
        self.logger.info("%s ready — worktree root %s", _PLUGIN_NAME, self._worktree_root)
        self.set_ready()

    def _worktree(self) -> Path:
        if self._worktree_root is None:
            raise RuntimeError(f"{_PLUGIN_NAME}: not ready (prepare_for_readiness not run)")
        return self._worktree_root

    def _resolve_target(self, target_path: str | None) -> tuple[Path, bool]:
        """Resolve the scan root + DERIVE the target class (FT-1 ruling A.2 / B.1).

        Absent ``target_path`` -> ``(own worktree, foreign=False)`` (self-vet). A
        supplied path is validated FAIL-LOUD — absolute, exists, is a directory —
        and REFUSED if it resolves inside/around the own worktree (self-vet is the
        default there; a target under our own tree is a confused call). The class
        is DERIVED (own worktree -> self, else foreign), never caller-set.
        """
        worktree = self._worktree()
        if not target_path:
            return worktree, False
        raw = Path(target_path)
        if not raw.is_absolute():
            raise TargetValidationError(f"target_path must be an absolute path, got {target_path!r}")
        if not raw.exists():
            raise TargetValidationError(f"target_path does not exist: {target_path}")
        if not raw.is_dir():
            raise TargetValidationError(f"target_path is not a directory: {target_path}")
        resolved = raw.resolve()
        own = worktree.resolve()
        if resolved == own or own in resolved.parents or resolved in own.parents:
            raise TargetValidationError(
                f"target_path {resolved} resolves inside/around the platform's own worktree ({own}) "
                "— self-vet is the default for our own tree; call vet_codebase with no "
                "target_path to scan it."
            )
        return resolved, True

    @staticmethod
    def _build_tree(root: Path, *, foreign: bool) -> tuple[TargetTree, str]:
        """Build the TargetTree (git-preferred, walk-fallback) + its ref (FT-1 A.3).

        A target carrying a ``.git`` is enumerated via read-only ``git ls-files``
        (honors the target's own gitignore); a non-git target via the read-only
        structural walk. ``ref`` is the git HEAD in git mode, empty in walk mode
        (no invented provenance).
        """
        if (root / ".git").exists():
            return TargetTree.from_git(root, foreign=foreign), git_head(root)
        return TargetTree.from_walk(root), ""

    def _run(
        self,
        scan_fn: ScanFn,
        *,
        scope: str,
        tag: str,
        target_path: str | None = None,
        role_overrides: object = _ROLE_OVERRIDES_ABSENT,
        persist: bool = False,
    ) -> dict[str, Any]:
        """Resolve the target, build its TargetTree, run ``scan_fn``, pack the payload.

        ``persist`` (default false, the vet_codebase opt-in) writes the run to the vetting_runs trail as a
        pure SIDE-EFFECT (substrate 'heuristic', L1-only) so get_vetting_run can read it back by run_id; the
        returned payload is byte-identical whether or not it persists.

        Every foreign run receives an additive source-role partition. Validated
        overrides are report-stage attribution only: scanners and the overall
        dimension histogram are unchanged.
        """
        root, foreign = self._resolve_target(target_path)
        tree, ref = self._build_tree(root, foreign=foreign)
        overrides_provided = role_overrides is not _ROLE_OVERRIDES_ABSENT
        parsed_overrides = parse_role_overrides(
            role_overrides,
            tree,
            foreign=foreign,
            provided=overrides_provided,
        )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"vr-{tag}-{ref or tree.enumeration}-{stamp}"
        started = system_clock()
        findings, coverage, report_data = scan_fn(tree, run_id)
        finished = system_clock()
        overall_by_dimension = counts_by_dimension(findings)
        target = _run_target(
            root=root,
            ref=ref,
            scope=scope,
            tree=tree,
            findings=findings,
            overrides=parsed_overrides,
            overall_by_dimension=overall_by_dimension,
            foreign=foreign,
        )
        file_count = len(tree.all_files())
        stacks = detect_stacks(tree)
        report_enumeration, report_file_count, target_class = _foreign_inventory(
            tree, foreign
        )
        report = ReportRenderer().render(
            run_id=run_id,
            target=target,
            context_profile=ContextProfile.PRODUCTION,
            generated_at=finished,
            findings=findings,
            coverage=coverage,
            enumeration=report_enumeration,
            file_count=report_file_count,
            stacks=render_stacks(stacks),
            structural_metrics=report_data.structural_metrics,
            dead_symbols=report_data.dead_symbols,
        )
        if persist:
            # allowlist_totals reads the platform's OWN tracked-debt files (a self-vet concept); a foreign target
            # has none, so its delta is empty rather than fail-loud on the absent quality_gates/ files.
            allowlist_delta = _run_allowlist_delta(root, foreign)
            self._persist_run(run_id, target, started, finished, findings, coverage, report_data, report, allowlist_delta)
        return {
            "run_id": run_id,
            "target": target.to_dict(),
            "target_class": target_class,
            "enumeration": tree.enumeration,
            "files_enumerated": file_count,
            "stacks": sorted(stack.value for stack in stacks),
            "started": started,
            "finished": finished,
            "total_findings": len(findings),
            "counts_by_severity": counts_by_severity(findings),
            "counts_by_dimension": overall_by_dimension,
            "scanners_ran": sum(1 for record in coverage if record.ran),
            "scanners_total": len(coverage),
            "coverage_gaps": coverage_gaps(coverage),
            "report": _bound_report(report),
        }

    def _persist_run(
        self,
        run_id: str,
        target: RunTarget,
        started: str,
        finished: str,
        findings: list[Finding],
        coverage: list[CoverageRecord],
        report_data: L1ReportData,
        report: str,
        allowlist_delta: AllowlistDelta,
    ) -> None:
        """Persist an L1-only vet run to the vetting_runs trail (the ``persist`` opt-in) so get_vetting_run
        serves it by run_id. substrate='heuristic' (no inference), layers_run=[L1_deterministic]; the FULL
        report (not the inline-capped payload copy) + the structural/dead-symbols evidence at the joseki
        field paths. A SYNC write (the verb is sync; state_service is sync)."""
        structural = report_data.structural_metrics
        dead = report_data.dead_symbols
        metrics = build_run_metrics(
            run_id=run_id,
            target=target,
            started=started,
            finished=finished,
            substrate=_L1_SUBSTRATE,
            layers_run=[Layer.L1_DETERMINISTIC],
            findings=findings,
            coverage=coverage,
            allowlist_delta=allowlist_delta,
            structural_metrics=structural.to_dict() if structural is not None else None,
            dead_symbols=dead.to_dict() if dead is not None else None,
            report=report,
        )
        persist_run_sync(self._state_service(), metrics)

    @staticmethod
    def _envelope(data: dict[str, Any]) -> dict[str, Any]:
        """Wrap a verb payload in the mandatory platform result envelope."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _error_envelope(code: str, message: str) -> dict[str, Any]:
        """Structured fail-loud refusal envelope (e.g. FT-1 target_path validation)."""
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message, "details": {}},
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """The plugin OWNS the ``vetting_runs`` trail — declare it so the platform creates the table at
        BOOT (``collect_schemas`` before ``start_services``), guaranteeing it exists before ANY write path
        upserts: the inference-backed VettingDriver's ``MetricsWriter`` AND the sync ``persist_run_sync`` the
        vet_codebase ``persist`` opt-in drives. This makes CodeVettingPlugin a ``SchemaProvider`` (the
        runtime-checkable capability Protocol). Declarative + boot-time — never a lazy ``ensure_`` on the
        write path; both writers share the SAME boot-created table so they cannot drift (Writing-Plugins §2.3a).
        """
        return [get_vetting_runs_schema()]

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Mandatory decorated<->declared parity for every EDGE verb (none of the three returns a blob key)."""
        return {
            "vet_codebase": EdgeProcessDefinition(
                name="vet_codebase",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_VET_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=False),
            ),
            "scan_quality_guidelines": EdgeProcessDefinition(
                name="scan_quality_guidelines",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_SCAN_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=False),
            ),
            "get_vetting_run": EdgeProcessDefinition(
                name="get_vetting_run",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_GET_RUN_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(retryable=False),
            ),
        }

    @platform_process(
        name="vet_codebase",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters=_vet_codebase_params(),
        output_type="object",
        output_description="Deterministic L1 vetting run: bounded metrics summary + bounded markdown report.",
        return_value_schema=_vetting_data_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(result_type=_VET_RESULT_TYPE),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def vet_codebase(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Run the full deterministic L1 scanner pipeline over the target; return fast.

        Default (no ``target_path``) is a self-vet of the platform's own worktree, byte-compatible
        with today. An optional absolute ``target_path`` vets a FOREIGN repo/source tree
        read-only (FT-1): git-preferred enumeration with a structural walk fallback, the
        derived target class in the payload, and platform-canon scanners skipped as
        ``not_applicable`` on a foreign target. Runs all 20 L1 scanners
        (``runner.run_all``) synchronously — NO inference; the L2 critic / L3 verify
        stages are joseki-dispatched (W3-C), never inline (action-queue fast-return
        contract). ``execute_target_toolchain`` (default false) opts into the tsc/eslint
        target-toolchain tier, which EXECUTES TARGET-CONTROLLED CODE on the scan host —
        default-off, sandbox-preferred (R7-4). Returns a bounded metrics summary + a
        bounded markdown report.
        """
        del state
        scope = str(params.get("scope") or "whole-tree self-vet")
        target_path_raw = params.get("target_path")
        target_path = str(target_path_raw).strip() if target_path_raw and str(target_path_raw).strip() else None
        role_overrides = (
            params["role_overrides"]
            if "role_overrides" in params
            else _ROLE_OVERRIDES_ABSENT
        )
        execute_target_toolchain = bool(params.get("execute_target_toolchain"))
        persist = bool(params.get("persist"))
        self.logger.info(
            "%s.vet_codebase scope=%s target=%s role_overrides=%s execute_toolchain=%s persist=%s",
            _PLUGIN_NAME,
            scope,
            target_path or "self",
            len(role_overrides) if isinstance(role_overrides, list) else 0,
            execute_target_toolchain,
            persist,
        )
        scan_fn = partial(run_all, execute_target_toolchain=execute_target_toolchain)
        try:
            return self._envelope(
                self._run(
                    scan_fn,
                    scope=scope,
                    tag="l1",
                    target_path=target_path,
                    role_overrides=role_overrides,
                    persist=persist,
                )
            )
        except TargetValidationError as err:
            return self._error_envelope("invalid_target_path", str(err))
        except RoleOverrideValidationError as err:
            return self._error_envelope("invalid_role_overrides", str(err))

    @platform_process(
        name="scan_quality_guidelines",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters=_scope_param("platform quality surface"),
        output_type="object",
        output_description="Platform quality-gate toolchain run (advisory): bounded metrics summary + bounded report.",
        return_value_schema=_vetting_data_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(result_type=_SCAN_RESULT_TYPE),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def scan_quality_guidelines(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Run the platform quality-gate toolchain over the worktree (ADVISORY, R5).

        The ``code_quality`` aggregate (ruff / pyright-strict / the radon + god-class
        coherence trio / whole-tree-integration / service-interface AST / return-shape),
        plus the ``sql_access`` lockdown gate and ``vulture`` dead-code — allowlisted
        findings preserved as visible tracked debt but non-blocking. ADVISORY only:
        Git-Controller's canonical gate run stays the sole commit authority (R5).
        """
        del state
        scope = str(params.get("scope") or "platform quality surface")
        self.logger.info("%s.scan_quality_guidelines scope=%s", _PLUGIN_NAME, scope)
        return self._envelope(self._run(_collect_quality_gates, scope=scope, tag="gates"))

    @platform_process(
        name="get_vetting_run",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters=_get_vetting_run_params(),
        output_type="object",
        output_description="A persisted vetting run read by run_id: the metrics row + the structural-metrics/candidate-dead-symbols evidence + the markdown report.",
        return_value_schema=_vetting_run_record_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(result_type=_GET_RUN_RESULT_TYPE),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def get_vetting_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Read ONE persisted vetting run by ``run_id`` — the joseki's payload-by-run_id read seam (W3-C C3a).

        The W3-C ruling routes the AI pass's payloads by run_id through read-verbs ONLY (deterministic
        steps cannot pipe a prior step's runtime result), so the joseki's L2/L3 inference steps read the
        report + the structural-metrics + candidate-dead-symbols evidence from HERE rather than carrying
        vet_codebase's returned payload forward. Reads the persisted ``vetting_runs`` row via the
        sanctioned single-namespace ``query_state`` filter (own namespace, no join, no raw SQL). An
        unknown run_id is a typed ``run_not_found`` refusal (fail-loud, never an empty success). Rows are
        written by the inference-backed driver (the joseki / dogfood path); the bare vet_codebase verb
        returns inline and does not persist (a verb-level persist opt-in is a separate slice).
        """
        del state
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            return self._error_envelope("invalid_run_id", "run_id is required and must be non-empty.")
        row = read_vetting_run(self._state_service(), run_id)
        if row is None:
            return self._error_envelope("run_not_found", f"no vetting run persisted for run_id {run_id!r}.")
        self.logger.info("%s.get_vetting_run run_id=%s", _PLUGIN_NAME, run_id)
        return self._envelope(dict(row))

    def _state_service(self) -> StateServiceProtocol:
        """The bound platform state service; fail-loud if unbound (get_vetting_run reads the trail through it)."""
        service = self.orchestrator_ref.get_service("state_service") if self.orchestrator_ref else None
        if not isinstance(service, StateServiceProtocol):
            raise RuntimeError(
                f"{_PLUGIN_NAME}: get_vetting_run requires the platform state_service, but it is unbound."
            )
        return service

    @platform_process(
        name="trigger_scheduled_self_vet",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="A fast heartbeat receipt: 'started' if the daily self-vet was submitted, 'already_running' if the single slot was busy.",
        return_value_schema=_scheduled_receipt_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
    )
    def trigger_scheduled_self_vet(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Fire the DAILY L1-only self-vet on the single-slot background executor (W3C-3 — EDGE_SINK cron heartbeat).

        Returns PROMPTLY (action-queue fast-return contract — a full L1 self-vet is MINUTES, so it must NOT run
        inline): submits the run to the single slot and returns ``{"self_vet": "started" | "already_running"}``.
        The slot IS the lease — a fire while a run is in-flight is skipped-busy honestly (no new lease machinery).
        The background run goes through the PERSISTING L1 path (a ``vetting_runs`` row per run — the R9-3 trend
        baseline; substrate ``heuristic``) and queues a regression memory note ONLY when its blocker/high counts
        worsen vs the prior run (else silent). Cron-fired (EDGE_SINK terminal — no result-processor); also
        on-demand invokable.
        """
        del params, state
        outcome = "started" if self._vet_executor.submit(self._run_scheduled_self_vet) else "already_running"
        self.logger.info("%s.trigger_scheduled_self_vet %s", _PLUGIN_NAME, outcome)
        return self._envelope({"self_vet": outcome})

    def _run_scheduled_self_vet(self) -> None:
        """Background body (single-slot daemon thread, OFF the dispatch path): the daily L1-only self-vet over
        the platform's own worktree, PERSISTED, with a regression note. Reads the PRIOR row BEFORE the run so the compare
        is against the last run, not this one. All state/memory access is here (runtime), never in the L1 verb
        import closure (R6)."""
        prior = read_recent_runs(self._state_service(), limit=1)
        scan_fn = partial(run_all, execute_target_toolchain=False)
        payload = self._run(scan_fn, scope="scheduled self-vet", tag="cron", persist=True)
        if prior and is_regression(payload["counts_by_severity"], _counts_of(prior[0])):
            self._notify_regression(payload, prior[0])

    def _notify_regression(self, run: dict[str, Any], prior: Mapping[str, object]) -> None:
        """Queue the regression note (ruled Option A): a persisted memory the operator/heartbeat surfaces async
        (never a live interrupt). Silent on a clean/improved run (the caller gates on ``is_regression``)."""
        new_counts = run["counts_by_severity"]
        old_counts = _counts_of(prior)
        content = (
            f"Code-vetting scheduled self-vet REGRESSION on run {run['run_id']}: "
            f"blocker {new_counts.get('blocker', 0)} / high {new_counts.get('high', 0)} "
            f"(prior blocker {old_counts.get('blocker', 0)} / high {old_counts.get('high', 0)}). "
            "Read get_vetting_run on the run_id for the finding detail."
        )
        self._memory_service().remember(content=content, tags=[REGRESSION_MEMORY_TAG])

    def _memory_service(self) -> MemoryServiceInterface:
        """The bound memory service — the scheduled self-vet's regression note is queued through it. Obtained on
        the BACKGROUND job ONLY (runtime), never in the verb's L1 import closure (R6); fail-loud if unbound."""
        service = self.orchestrator_ref.get_service("memory_service") if self.orchestrator_ref else None
        if not isinstance(service, MemoryServiceInterface):
            raise RuntimeError(
                f"{_PLUGIN_NAME}: the scheduled self-vet regression note requires memory_service, but it is unbound."
            )
        return service

    # --- W3-B B3c: the inference-dispatch SEAM (STAGED — NOT reached by either verb) ---
    # vet_codebase / scan_quality_guidelines stay L1-only (A0 R6). These methods build the
    # inference-backed L3 substrate (the B1 selector / dispatcher / off-operator redaction)
    # for W3-C's joseki to drive — the same staged pattern B1 shipped. The inference
    # subsystem is imported FUNCTION-LOCALLY here so it never enters the verbs' import
    # closure (Reviewer-C A2 Focus-5 invariant). Wiring detail lives in inference_wiring.py.

    def _inference_service(self) -> InferenceCompleter:
        """The bound platform inference service (LOCAL/PRIVACY substrate); fail-loud if vacant.

        ``inference_service`` can boot declared-VACANT (INF-03; not a required service), so a
        LOCAL/PRIVACY substrate that needs it fails loud rather than silently degrading to no
        L3. The narrow is structural (a ``runtime_checkable`` Protocol) so it is wrapper-safe.
        """
        from .inference_wiring import InferenceCompleter

        service = self.orchestrator_ref.get_service("inference_service") if self.orchestrator_ref else None
        if not isinstance(service, InferenceCompleter):
            raise RuntimeError(
                f"{_PLUGIN_NAME}: the LOCAL/PRIVACY inference substrate requires the platform "
                "inference_service (local models), but it is unbound / declared-vacant. Use the "
                "SUBSCRIPTION substrate, or bind a local inference provider."
            )
        return service

    def build_local_skeptic_transport(self) -> LocalInferenceSkepticTransport:
        """SEAM factory: the ON-MACHINE binding-2 transport over ``inference_service.generate_completion``."""
        from .inference_wiring import make_local_infer_fn
        from .verify.inference import LocalInferenceSkepticTransport

        return LocalInferenceSkepticTransport(infer_fn=make_local_infer_fn(self._inference_service()))

    def build_subscription_skeptic_transport(self) -> SubprocessSkepticTransport:
        """SEAM factory: the OFF-MACHINE ``claude -p`` transport, rooted at the git worktree."""
        from .verify.inference import SubprocessSkepticTransport

        return SubprocessSkepticTransport(cwd=str(self._worktree()))

    def build_inference_driver(self, substrate: Substrate, *, metrics_writer: MetricsWriter) -> VettingDriver:
        """STAGED SEAM (B3c): assemble the inference-backed driver for W3-C's joseki / the dogfood.

        NOT reached by either verb (A0 R6). Binds the transport the substrate requires
        (SUBSCRIPTION -> ``claude -p``; LOCAL/PRIVACY -> local inference), then delegates to
        ``assemble_inference_driver`` (which resolves the off-operator-safe ``SubstrateSelection``
        and assembles L1 + substrate-selected L3 + report). The rulebook loads from the
        worktree-anchored path (B3a). ``metrics_writer`` is supplied by the caller (the live
        state writer, or an in-memory one under test).
        """
        from .inference_wiring import assemble_inference_driver
        from .verify.rulebook import load_rulebook
        from .verify.substrate import Substrate

        worktree = self._worktree()
        subscription = self.build_subscription_skeptic_transport() if substrate is Substrate.SUBSCRIPTION else None
        local = self.build_local_skeptic_transport() if substrate in (Substrate.LOCAL, Substrate.PRIVACY) else None
        return assemble_inference_driver(
            root=worktree,
            substrate=substrate,
            subscription_transport=subscription,
            local_transport=local,
            rulebook=load_rulebook(),  # W3-C: the assembled in-package artifact (B3a worktree-anchor retired)
            metrics_writer=metrics_writer,
            clock=system_clock,
            context_profile=ContextProfile.PRODUCTION,
        )
