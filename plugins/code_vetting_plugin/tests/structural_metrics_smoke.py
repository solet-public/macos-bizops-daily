"""structural_metrics_smoke.py — R8-1: the lizard-backed structural-metrics scanner.

Pins the ruling's contract with EXACT deterministic values (ruling §A/§B/§C/§D/§E):

  * Roster: ONE UNIVERSAL, parse-only (`executes_target_code=False`) scanner; the roster is 18.
  * Pin + languages: lizard is pinned exact (a drift raises); `supported_languages()` includes
    typescript/tsx/javascript/python and EXCLUDES dart (a lizard upgrade that silently changes
    the supported set turns this red instead of drifting foreign coverage unnoticed).
  * EXACT per-function metrics on a TS+Python fixture: CCN/NLOC/params/nesting are deterministic,
    so we assert the VALUES (not shapes) — CCN via lizard's boolean-op counting, nesting depth via
    the `nd` extension, a fresh analyzer per file (no cross-file accumulation).
  * Band findings (universal tier / FOREIGN): CCN>30 → MEDIUM, params>8 / nesting>6 → LOW; a clean
    function crosses nothing; severity NEVER exceeds MEDIUM; dimension is COMPLEXITY and zero-FP-
    promoted. On a SELF-vet the project_local tier SUPPRESSES the finding rows — same payload, no rows.
  * Report section + persistence: the `## Structural Quality Metrics` block carries the fixed
    radon-vs-lizard provenance note and NO A–F rank; the payload round-trips onto the run record
    and the vetting_runs schema carries the column.
  * Size cap: a >1MB file is skipped with a coverage disclosure (ran-with-disclosure, not a gap row).

Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
import tempfile
from pathlib import Path

import lizard

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.live_state import get_vetting_runs_schema
from code_vetting_plugin.models import Dimension, Finding, Severity
from code_vetting_plugin.report import DEFAULT_ZERO_FP_DIMENSIONS
from code_vetting_plugin.run_record import AllowlistDelta, RunTarget, build_run_metrics
from code_vetting_plugin.runner import SCANNERS, Applicability
from code_vetting_plugin.scanners.structural_metrics import (  # noqa: PLC2701 — pin the internal metric/band logic
    LIZARD_PINNED_VERSION,
    _is_tracked_debt,
    _python_nested_loops,
    render_structural_metrics_section,
    scan,
    supported_languages,
)
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


_MOD_PY = (
    "def clean(a, b):\n"
    "    return a + b\n\n\n"
    "def many_params(a, b, c, d, e, f, g, h, i, j):\n"
    "    return a\n\n\n"
    "def deeply_nested(x):\n"
    "    if x > 0:\n"
    "        for i in range(x):\n"
    "            while i > 0:\n"
    "                if i % 2 == 0:\n"
    "                    for j in range(i):\n"
    "                        if j > 1:\n"
    "                            if j < 5:\n"
    "                                return j\n"
    "    return 0\n"
)
_APP_TS = (
    "export function score(a: number, b: number): number {\n"
    "  if (a > 0 && b > 0) {\n"
    "    return a + b;\n"
    "  }\n"
    "  return 0;\n"
    "}\n"
)


def _big_py() -> str:
    """A function with 35 branches → CCN 36 (crosses the >30 MEDIUM band deterministically)."""
    lines = ["def big_branch(v):", "    total = 0"]
    for n in range(35):
        lines += [f"    if v == {n}:", f"        total += {n}"]
    lines.append("    return total")
    return "\n".join(lines) + "\n"


# EXACT probed metrics (file, name) -> (ccn, nloc, params, nesting). Deterministic under lizard 1.23.0.
_EXPECTED: dict[tuple[str, str], tuple[int, int, int, int]] = {
    ("big.py", "big_branch"): (36, 73, 1, 35),
    ("mod.py", "clean"): (1, 2, 2, 0),
    ("mod.py", "many_params"): (1, 2, 10, 0),
    ("mod.py", "deeply_nested"): (8, 10, 1, 7),
    ("src/app.ts", "score"): (3, 6, 2, 2),
}


def _fixture(root: Path, *, foreign: bool) -> TargetTree:
    (root / "src").mkdir(parents=True)
    (root / "mod.py").write_text(_MOD_PY, encoding="utf-8")
    (root / "big.py").write_text(_big_py(), encoding="utf-8")
    (root / "src" / "app.ts").write_text(_APP_TS, encoding="utf-8")
    if foreign:
        return TargetTree.from_walk(root)
    tracked = ("mod.py", "big.py", "src/app.ts")
    return TargetTree(root=root, tracked=tracked, enumeration="git", foreign=False)


def _check_roster_and_pin() -> None:
    _check("roster is 20 (structural_metrics + python_type_check + rulebook_sync added)", len(SCANNERS) == 20, str(len(SCANNERS)))
    spec = next((s for s in SCANNERS if s.name == "structural_metrics"), None)
    _check(
        "structural_metrics: UNIVERSAL, parse-only (executes_target_code=False)",
        spec is not None and spec.applicability is Applicability.UNIVERSAL and spec.executes_target_code is False,
        str(spec),
    )
    _check("lizard pinned exact to the running version", LIZARD_PINNED_VERSION == "1.23.0" == lizard.version, f"{LIZARD_PINNED_VERSION} / {lizard.version}")


def _check_languages() -> None:
    langs = supported_languages()
    _check("supported languages include ts/tsx/js/python", {"typescript", "tsx", "javascript", "python"} <= set(langs), str(langs))
    _check("dart is NOT supported (flagged Phase-2)", "dart" not in langs, str(langs))


def _check_exact_metrics(tree: TargetTree) -> None:
    report = scan(tree, "vr-r8").structural_metrics
    _check("payload present", report is not None, "")
    assert report is not None
    _check("files_parsed == 3", report.files_parsed == 3, str(report.files_parsed))
    _check("functions_analyzed == 5", report.functions_analyzed == 5, str(report.functions_analyzed))
    _check("languages == (python, typescript)", report.languages == ("python", "typescript"), str(report.languages))
    by_key = {(fn.file, fn.name): fn for fn in report.worst_offenders}
    for key, (ccn, nloc, params, nesting) in _EXPECTED.items():
        fn = by_key.get(key)
        _check(
            f"EXACT metrics for {key[1]} ({key[0]})",
            fn is not None and (fn.ccn, fn.nloc, fn.params, fn.nesting) == (ccn, nloc, params, nesting),
            f"got {None if fn is None else (fn.ccn, fn.nloc, fn.params, fn.nesting)} want {(ccn, nloc, params, nesting)}",
        )
    _check("CCN distribution exact", report.ccn_distribution == {"1-5": 3, "6-10": 1, "11-15": 0, "16-30": 0, ">30": 1}, str(report.ccn_distribution))
    _check("median CCN == 3.0", report.median_ccn == 3.0, str(report.median_ccn))
    _check("p90 CCN == 36.0", report.p90_ccn == 36.0, str(report.p90_ccn))
    _check("worst offender is big_branch", report.worst_offenders[0].name == "big_branch", report.worst_offenders[0].name)


def _check_foreign_nested_loop_rows(
    findings: list[Finding], got: set[tuple[str, str, str]]
) -> set[tuple[str, str, str]]:
    """The nested-loop class, asserted SEPARATELY and just as exactly as the bands.

    Split out of `_check_foreign_findings` to keep that function under the CC gate, not because
    the assertions are optional. Returns the rows so the caller can still prove every emitted
    finding is either a band crossing or this class.

    `deeply_nested` in the fixture is for → while → for, so the class must register at depth 2
    (LOW) and depth 3 (MEDIUM); `_MOD_PY`'s if-chains are ALSO deep, which is precisely the
    distinction lizard's any-nesting `nd` metric cannot draw and this detector must.
    """
    nested = {row for row in got if row[1] == "complexity:nested_loops"}
    _check(
        "foreign emits EXACTLY the 2 nested-loop findings (depth 2 LOW, depth 3 MEDIUM)",
        nested == {("mod.py", "complexity:nested_loops", "low"), ("mod.py", "complexity:nested_loops", "medium")},
        f"got {sorted(nested)}",
    )
    # The set above pins WHICH rows; it cannot pin HOW MANY, because duplicates collapse into it.
    # A walker that double-visited a node would emit the same finding N times and still satisfy
    # the set. Count on the finding list itself so the "EXACTLY" in that label is load-bearing.
    nested_count = len([f for f in findings if f.constraint_violated == "complexity:nested_loops"])
    _check(
        "…and emits them exactly ONCE each (a set cannot see duplicates)",
        nested_count == 2,
        f"got {nested_count} nested-loop finding rows",
    )
    return nested


def _check_foreign_findings(tree: TargetTree) -> None:
    findings = scan(tree, "vr-r8").findings
    got = {(f.file, f.constraint_violated, f.severity.value) for f in findings}
    band = {row for row in got if row[1].startswith("structural:")}
    want = {
        ("big.py", "structural:ccn>30", "medium"),
        ("big.py", "structural:nesting>6", "low"),
        ("mod.py", "structural:nesting>6", "low"),
        ("mod.py", "structural:params>8", "low"),
    }
    _check("foreign emits EXACTLY the 4 band findings", band == want, f"got {sorted(band)}")
    nested = _check_foreign_nested_loop_rows(findings, got)
    _check(
        "every emitted finding is either a band crossing or the named nested-loop class",
        band | nested == got,
        f"unclassified: {sorted(got - band - nested)}",
    )
    _check("severity NEVER exceeds MEDIUM (no HIGH/BLOCKER)", all(f.severity in {Severity.LOW, Severity.MEDIUM} for f in findings), str([f.severity.value for f in findings]))
    _check("all findings are COMPLEXITY dimension", all(f.dimension is Dimension.COMPLEXITY for f in findings), "")
    _check("COMPLEXITY is zero-FP-promoted (renders without an L3 pass)", Dimension.COMPLEXITY in DEFAULT_ZERO_FP_DIMENSIONS, "")
    _check("a clean function (ccn 1) emits no finding", not any(f.evidence.startswith("python function 'clean'") for f in findings), "")


def _check_self_suppression(tree: TargetTree) -> None:
    result = scan(tree, "vr-r8-self")
    _check("self-vet SUPPRESSES metric finding rows (project_local tier)", result.findings == [], str(result.findings))
    # The nested-loop denominator must be disclosed on a SELF-vet too. Suppressing the finding
    # rows must not also suppress the statement of what was examined — otherwise a self-vet
    # reads as "nothing to report" when the truth may be "not examined for this class".
    # It rides the payload + report section, NOT gap_reason: a note appended on every run makes
    # "was anything reduced?" permanently true and buries the genuine reductions.
    assert result.structural_metrics is not None
    _check(
        "self-vet still discloses the nested-loop denominator (on the payload)",
        result.structural_metrics.nested_loop_files_examined == 2,
        str(result.structural_metrics.nested_loop_files_examined),
    )
    _check(
        "…and gap_reason is NOT populated when nothing was actually reduced",
        result.coverage.gap_reason is None,
        str(result.coverage.gap_reason),
    )
    _check("self-vet still carries the full payload", result.structural_metrics.functions_analyzed == 5, "")
    _check("coverage always ran=True (lizard structurally present)", result.coverage.ran is True and result.coverage.files_examined == 3, str(result.coverage))


def _check_section_and_persistence(tree: TargetTree) -> None:
    report = scan(tree, "vr-r8").structural_metrics
    section = render_structural_metrics_section(report)
    _check("section header present", section.startswith("## Structural Quality Metrics"), section[:60])
    _check("section carries the radon-vs-lizard provenance note", "numerically comparable" in section and "boolean operators" in section, "")
    _check("section renders the distribution + worst offenders", "CCN band" in section and "Worst offenders" in section and "big_branch" in section, "")
    _check("section renders NO A–F rank vocabulary", not any(f" {letter} " in section for letter in ("A", "B", "C", "D", "E", "F")), "rank letter leaked")
    _check("None payload → stable placeholder", render_structural_metrics_section(None) == "## Structural Quality Metrics\n\n_No structural metrics recorded._", "")
    # Persistence: the payload rides the run record + the schema carries the column.
    assert report is not None
    metrics = build_run_metrics(
        run_id="vr-r8", target=RunTarget(repo="t", ref="r", scope="s"), started="a", finished="b",
        substrate="heuristic", layers_run=[], findings=[], coverage=[], allowlist_delta=AllowlistDelta(totals={}),
        structural_metrics=report.to_dict(),
    )
    row = metrics.to_dict()
    _check("run record carries structural_metrics", isinstance(row.get("structural_metrics"), dict), str(type(row.get("structural_metrics"))))
    _check("persisted payload round-trips the distribution", row["structural_metrics"]["ccn_distribution"][">30"] == 1, str(row["structural_metrics"]))
    _check("vetting_runs schema declares the structural_metrics column", "structural_metrics" in get_vetting_runs_schema().tables["vetting_runs"].columns, "")


def _check_size_cap_and_debt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "big"
        root.mkdir()
        (root / "huge.py").write_text("x = 1\n" + "# pad\n" * 400_000, encoding="utf-8")  # > 1 MB
        (root / "small.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
        result = scan(TargetTree.from_walk(root), "vr-r8-cap")
        report = result.structural_metrics
        assert report is not None
        _check("oversize file skipped (not parsed)", report.files_parsed == 1 and report.files_skipped == 1, str((report.files_parsed, report.files_skipped)))
        _check("skip disclosed on a ran=True coverage row (not a gap)", result.coverage.ran is True and "skipped" in (result.coverage.gap_reason or ""), str(result.coverage))
    # tracked-debt matcher (self-vet annotation): <path-suffix>::<function> convention.
    allow = frozenset({"plugins/x/src/x/mod.py::heavy"})
    _check("tracked-debt matches path-suffix::function", _is_tracked_debt("plugins/x/src/x/mod.py", "heavy", allow), "")
    _check("tracked-debt does not match a different function", not _is_tracked_debt("plugins/x/src/x/mod.py", "light", allow), "")


def _check_literal_frequency() -> None:
    """R9-B: repeated non-trivial literals are surfaced; the trivial/sparse floor drops the rest."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "lit"
        root.mkdir()
        base = 'a = "config_value_here"\nb = 7777\nc = "ab"\nd = 1\ne = 100\n'  # non-trivial x2 + trivial x3
        (root / "f0.py").write_text(base + 'z = "single_file_literal"\n' * 6 + 'p = "two_site_only"\n', encoding="utf-8")
        (root / "f1.py").write_text(base + 'p = "two_site_only"\n', encoding="utf-8")
        (root / "f2.py").write_text(base, encoding="utf-8")
        (root / "f3.py").write_text(base, encoding="utf-8")
        report = scan(TargetTree.from_walk(root), "vr-r9-lit").structural_metrics
        assert report is not None
        values = {(literal.kind, literal.value) for literal in report.literals}
        _check("literal-freq: repeated non-trivial string included (4 sites/4 files)", ("string", "config_value_here") in values, str(values))
        _check("literal-freq: repeated non-trivial number included", ("number", "7777") in values, str(values))
        _check("literal-freq FLOOR: short string (<4 chars) excluded", ("string", "ab") not in values, str(values))
        _check("literal-freq FLOOR: trivial number 1 excluded", ("number", "1") not in values, str(values))
        _check("literal-freq FLOOR: round power of ten (100) excluded", ("number", "100") not in values, str(values))
        _check("literal-freq FLOOR: single-file literal excluded (<2 files)", ("string", "single_file_literal") not in values, str(values))
        _check("literal-freq FLOOR: two-site literal excluded (<4 sites)", ("string", "two_site_only") not in values, str(values))


# --- the named NESTED-LOOP pattern class (loops within loops) ------------------------
#
# Ruling B (2026-07-31) keeps static bad-pattern detection in scope and names loops-within-
# loops as the example. lizard's `nd` reports ANY-nesting depth, so before this the class was
# caught only incidentally and unnamed. These cases pin the DISTINCTION (a loop-free but
# deeply-branched function must NOT register) and the two ways the detector could make a
# FALSE claim: counting a closure's loop as nested, and counting a loop keyword in a string.

# A file the walker cannot examine. `_walk_python_loops` costs one Python frame per AST level,
# so a long chained expression exhausts the stack while parsing fine and staying FAR under the
# 1MB `_MAX_FILE_BYTES` guard. MEASURED at authoring time against the unguarded walk: 500 terms
# walks clean, 1000 terms raises RecursionError (2,045 bytes, default limit 1000). 2000 gives
# margin for a host with a deeper limit without making the fixture slow.
_DEEP_CHAIN_SOURCE = "x = " + "+".join(["1"] * 2000) + "\nfor a in y:\n    for b in a:\n        pass\n"

# `None` as the expectation means NOT EXAMINED — distinct from `()`, "examined, nothing found".
# The two must never be written the same way: that conflation is what let an unparseable file be
# counted in the coverage denominator.
_NESTED_LOOP_CASES: tuple[tuple[str, str, tuple[int, ...] | None], ...] = (
    ("a single flat loop is not nested", "for a in x:\n    pass\n", ()),
    ("two sibling loops are not nested", "for a in x:\n    pass\nfor b in y:\n    pass\n", ()),
    ("a loop inside a loop is depth 2", "for a in x:\n    for b in y:\n        pass\n", (2,)),
    ("a while inside a for is depth 2", "for a in x:\n    while b:\n        pass\n", (2,)),
    ("a triple loop reports depth 2 and 3", "for a in x:\n    for b in y:\n        for c in z:\n            pass\n", (2, 3)),
    (
        "an if between two loops does not break the nest",
        "for a in x:\n    if cond:\n        for b in y:\n            pass\n",
        (2,),
    ),
    (
        "a loop inside a def inside a loop is NOT a nested loop (closure resets depth)",
        "for a in x:\n    def inner():\n        for b in y:\n            pass\n",
        (),
    ),
    (
        "a genuine nest inside that closure still counts",
        "for a in x:\n    def inner():\n        for b in y:\n            for c in z:\n                pass\n",
        (2,),
    ),
    (
        "a loop keyword inside a string literal is not a loop",
        'msg = "for a in x: for b in y:"\nfor a in x:\n    pass\n',
        (),
    ),
    ("nested async for is depth 2", "async def f():\n    async for a in x:\n        async for b in y:\n            pass\n", (2,)),
    # Both of these are NOT-EXAMINED, not clean. Returning () here would report a file nobody
    # could look at as one that was looked at and found innocent.
    ("an unparseable file reports NOT EXAMINED rather than crashing", "def broken(:\n", None),
    ("a file that out-nests the walker reports NOT EXAMINED rather than aborting the run", _DEEP_CHAIN_SOURCE, None),
    # DELIBERATE v1 exclusion, pinned so a later change to it is a decision and not a drift.
    ("a multi-generator comprehension is not counted in v1", "vals = [q for a in x for b in y]\n", ()),
)


def _check_nested_loop_detection() -> None:
    for label, source, expected in _NESTED_LOOP_CASES:
        loops = _python_nested_loops("probe.py", source)
        if expected is None:
            # Asserted as `is None`, NOT as falsy: `[]` is falsy too, and `[]` is the answer
            # this case exists to rule out.
            _check(f"nested-loop: {label}", loops is None, f"expected None (not examined), got {loops}")
            continue
        _check(
            f"nested-loop: {label}",
            loops is not None and tuple(sorted(loop.depth for loop in loops)) == tuple(sorted(expected)),
            f"expected {expected}, got {loops if loops is None else tuple(sorted(found.depth for found in loops))}",
        )


def _check_nested_loop_denominator_is_declared() -> None:
    """The denominator must travel with the rows: an empty list must be distinguishable from
    'nothing was examined'. Asserted on a tree containing a NON-Python file so the examined
    count is genuinely smaller than files_parsed."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "denominator"
        (root / "src").mkdir(parents=True)
        (root / "src" / "loops.py").write_text("def f(x):\n    for a in x:\n        for b in a:\n            pass\n", encoding="utf-8")
        (root / "src" / "app.ts").write_text("export function f(){ for(;;){ for(;;){} } }\n", encoding="utf-8")
        # from_walk (not a hand-built TargetTree): `enumeration` is git|walk and `foreign` is
        # DERIVED, not caller-settable — inventing values for either would be test-only vocabulary.
        result = scan(TargetTree.from_walk(root), "rid")
    report = result.structural_metrics
    assert report is not None
    _check(
        "the nested-loop payload carries its own language denominator",
        report.nested_loop_languages == ("python",),
        str(report.nested_loop_languages),
    )
    _check(
        "the examined-file count is narrower than files_parsed (the TS file was not examined)",
        report.nested_loop_files_examined == 1 and report.files_parsed == 2,
        f"examined={report.nested_loop_files_examined} parsed={report.files_parsed}",
    )
    _check(
        "the Python nested loop was detected",
        [loop.depth for loop in report.nested_loops] == [2],
        str(report.nested_loops),
    )
    # The serialized payload must carry the denominator too — this is what a consumer reads.
    payload = report.to_dict()
    _check(
        "the serialized payload carries rows AND denominator together",
        payload["nested_loop_languages"] == ["python"] and payload["nested_loop_files_examined"] == 1,
        str({k: payload.get(k) for k in ("nested_loop_languages", "nested_loop_files_examined")}),
    )
    # The denominator is disclosed in the REPORT SECTION (human-readable) and on the payload
    # (machine-readable) — not in gap_reason, which is reserved for genuine reductions.
    _check(
        "the report section states the nested-loop denominator",
        "Examined 1 of 2 parsed file(s)" in render_structural_metrics_section(report),
        render_structural_metrics_section(report)[:400],
    )
    _check(
        "gap_reason stays None when nothing was truncated or skipped",
        result.coverage.gap_reason is None,
        str(result.coverage.gap_reason),
    )
    _check(
        "a foreign run emits the nested loop as a named finding",
        any(f.constraint_violated == "complexity:nested_loops" for f in result.findings),
        str([f.constraint_violated for f in result.findings]),
    )
    section = render_structural_metrics_section(report)
    _check(
        "the report section names the class and discloses the language limit",
        "Nested loops" in section and "were NOT examined" in section,
        section[:400],
    )


def _check_unexaminable_python_is_not_counted_as_examined() -> None:
    """A Python file the detector could NOT examine must not inflate its own denominator.

    RED-FIRST: the increment used to run unconditionally after the parser call, so a file whose
    parse failed — the parser having swallowed the error and returned ``[]`` — was reported as
    examined. That is the precise inversion of what the field is for: it exists so a reader can
    tell "none found" from "not examined", and it was answering "examined" for the second case.

    Both unexaminable shapes are covered: a syntax error, and a file that out-nests the walker.
    Two of the three files here are Python, and only ONE of them is examinable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "unexaminable"
        (root / "src").mkdir(parents=True)
        (root / "src" / "ok.py").write_text("def f(x):\n    for a in x:\n        for b in a:\n            pass\n", encoding="utf-8")
        (root / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        (root / "src" / "deep.py").write_text(_DEEP_CHAIN_SOURCE, encoding="utf-8")
        result = scan(TargetTree.from_walk(root), "rid-unexaminable")
    report = result.structural_metrics
    assert report is not None
    _check(
        "an unparseable / over-nested file is NOT counted as examined",
        report.nested_loop_files_examined == 1,
        f"examined={report.nested_loop_files_examined} (expected 1 of {report.files_parsed} parsed)",
    )
    _check(
        "…while all three files still count as PARSED (the two denominators stay distinct)",
        report.files_parsed == 3,
        f"files_parsed={report.files_parsed}",
    )
    _check(
        "the disclosure reports the honest ratio, not the flattering one",
        "Examined 1 of 3 parsed file(s)" in render_structural_metrics_section(report),
        render_structural_metrics_section(report)[:400],
    )
    _check(
        "the examinable file's own nested loop is still found (the guard did not suppress it)",
        [loop.depth for loop in report.nested_loops] == [2],
        str(report.nested_loops),
    )


def _check_no_functions_still_renders_the_nested_loop_block() -> None:
    """"No functions analyzed" must not swallow the nested-loop block.

    RED-FIRST: `render_structural_metrics_section` early-returned `_empty_section` on
    `functions_analyzed == 0`, dropping the block whose own docstring says it is ALWAYS
    rendered with its denominator — in exactly the case where loops WERE found.

    Reachability is MEASURED, not assumed: lizard returns `function_list == []` for
    module-level script code, so a bare for/for file analyses zero functions while the
    nested-loop detector still finds the nest and `scan` still emits the finding.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "modlevel"
        (root / "src").mkdir(parents=True)
        (root / "src" / "script.py").write_text("for a in range(3):\n    for b in range(3):\n        print(a, b)\n", encoding="utf-8")
        result = scan(TargetTree.from_walk(root), "rid-modlevel")
    report = result.structural_metrics
    assert report is not None
    _check(
        "the fixture really does reach the zero-functions path (else this proves nothing)",
        report.functions_analyzed == 0,
        f"functions_analyzed={report.functions_analyzed}",
    )
    _check(
        "…and the nested loop was still detected",
        len(report.nested_loops) == 1 and any(f.constraint_violated == "complexity:nested_loops" for f in result.findings),
        f"loops={report.nested_loops} findings={[f.constraint_violated for f in result.findings]}",
    )
    section = render_structural_metrics_section(report)
    _check(
        "the zero-functions section STILL renders the nested-loop block",
        "Nested loops" in section,
        section[:400],
    )
    _check(
        "…with its denominator, so the report never contradicts the run record",
        "Examined 1 of 1 parsed file(s)" in section,
        section[:400],
    )


def _check_nested_loop_total_survives_the_cap() -> None:
    """A capped payload must still report the TRUE total.

    RED-FIRST: rendering ``len(report.nested_loops)`` as the total understates it on any tree
    above the cap — a shipped report making a false count claim, which is the exact defect
    class this scanner emits findings about. Built deliberately OVER the cap.
    """
    over_cap = 130
    body = "".join(f"    for a{i} in x:\n        for b{i} in a{i}:\n            pass\n" for i in range(over_cap))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "overcap"
        root.mkdir(parents=True)
        (root / "many.py").write_text(f"def f(x):\n{body}", encoding="utf-8")
        result = scan(TargetTree.from_walk(root), "rid")
    report = result.structural_metrics
    assert report is not None
    _check(
        "the payload is capped (bounded-row invariant still holds)",
        len(report.nested_loops) == 100 and report.nested_loops_total == over_cap,
        f"carried={len(report.nested_loops)} total={report.nested_loops_total}",
    )
    _check(
        "the rendered section reports the TRUE total, not the truncated length",
        f"{over_cap} found" in render_structural_metrics_section(report),
        render_structural_metrics_section(report)[-400:],
    )
    _check(
        "truncation is disclosed on the coverage record",
        result.coverage.gap_reason is not None and f"found {over_cap} nested loops" in result.coverage.gap_reason,
        str(result.coverage.gap_reason),
    )


def main() -> int:
    try:
        _check_roster_and_pin()
        _check_languages()
        _check_literal_frequency()
        _check_nested_loop_detection()
        _check_nested_loop_denominator_is_declared()
        _check_unexaminable_python_is_not_counted_as_examined()
        _check_no_functions_still_renders_the_nested_loop_block()
        _check_nested_loop_total_survives_the_cap()
        with tempfile.TemporaryDirectory() as tmp:
            _check_exact_metrics(_fixture(Path(tmp) / "f1", foreign=True))
        with tempfile.TemporaryDirectory() as tmp:
            _check_foreign_findings(_fixture(Path(tmp) / "f2", foreign=True))
        with tempfile.TemporaryDirectory() as tmp:
            _check_self_suppression(_fixture(Path(tmp) / "f3", foreign=False))
        with tempfile.TemporaryDirectory() as tmp:
            _check_section_and_persistence(_fixture(Path(tmp) / "f4", foreign=True))
        _check_size_cap_and_debt()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"structural_metrics_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
