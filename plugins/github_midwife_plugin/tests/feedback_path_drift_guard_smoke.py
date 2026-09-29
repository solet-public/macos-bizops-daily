"""Smoke: no shipped surface tells a user to give feedback in a way a read-only account cannot follow.

An adopter's own GitHub account is an ordinary account on the seed repository. It can open
an issue, comment, and edit the issue it opened, and nothing more: issue creation through
the API silently drops a label it may not set, the sub-issues endpoint answers 404, and
``gh issue create --web`` needs a browser a headless solet does not have. The feedback
runbook and the ``/feedback`` skill template are the canonical text for the one path that
works. Every other shipped surface either points at them or matches them, and this smoke
fails if any of them reintroduces one of the contradictory steps.

Coverage:

1. The surfaces are DISCOVERED, not listed by hand: every Markdown, template and YAML file
   under ``plugins/github_midwife_plugin/knowledge_base/`` (retrieval-test companions
   excepted), ``CONTRIBUTING.md``, and every ``.github/ISSUE_TEMPLATE/*.yml``.
2. Each detector fires on a synthetic bad line and stays quiet on the synthetic allowed one
   (anti-vacuity): the ``--label`` flag, the sub-issues API path, "attach ... as a
   sub-issue", ``--web``, and an un-negated "submit a PR".
3. No discovered surface trips a detector. The canonical runbook and skill template may name
   the forbidden steps only inside their marked ``not-attempt`` block, which is stripped
   before scanning; an unbalanced marker fails.
4. Both canonical files carry the same required anchors: the ``Class:`` line, the
   ``Round: #`` line, ``--body-file``, the new-issue chooser URL for the no-``gh`` route, the
   parent-edit command and its comment fallback, and a ``not-attempt`` block that names
   every forbidden step.
5. The item forms tell a web filer how to name the round, and the round form tells the
   author to edit a checklist of item links into its own body.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/tests/feedback_path_drift_guard_smoke.py``.
Pass ``--root <tree>`` to run it against another checkout of the same layout, for example a
``git archive`` extraction of an earlier commit.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parents[3]
_KB_RELDIR = Path("plugins") / "github_midwife_plugin" / "knowledge_base"
_RUNBOOK_RELPATH = _KB_RELDIR / "07_upstream_feedback_runbook.md"
_SKILL_RELPATH = _KB_RELDIR / "hydration_templates" / "feedback_skill_SKILL.md.template"
_FORMS_RELDIR = Path(".github") / "ISSUE_TEMPLATE"
_SURFACE_SUFFIXES = (".md", ".template", ".yml", ".yaml")
_RETRIEVAL_COMPANION_SUFFIX = ".retrieval_test.yaml"

_EXEMPT_START = "<!-- not-attempt:start -->"
_EXEMPT_END = "<!-- not-attempt:end -->"

_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|removed|declin\w*|refus\w*|without|cannot|instead|whether)\b|n't\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# name -> pattern; a hit anywhere outside an exempt block is a violation
_PLAIN_DETECTORS: dict[str, re.Pattern[str]] = {
    "label flag": re.compile(r"(?<![\w-])--(?:add-)?label\b"),
    "sub-issues API": re.compile(r"sub_issues"),
    "sub-issue attach instruction": re.compile(
        r"\bas (?:a )?sub[- ]issues?\b"
        r"|\battach\w*\b[^.\n]{0,80}\bsub[- ]issues?\b"
        r"|\bsub[- ]issues? (?:of|under)\b",
        re.IGNORECASE,
    ),
    "web flag": re.compile(r"(?<![\w-])--web\b"),
}
# a sentence hit is a violation only when the same sentence carries no negation
_PULL_REQUEST_RE = re.compile(
    r"\b(?:submit|send|open|create|file|raise|push)\w*\s+(?:a\s+|the\s+|your\s+)?(?:pull requests?|PRs?)\b",
    re.IGNORECASE,
)
_PULL_REQUEST_DETECTOR = "pull-request instruction"

_ANCHORS = (
    "Class: ",
    "Round: #",
    "--body-file",
    "issues/new/choose",
    'gh issue edit "$PARENT_NUMBER"',
    "gh issue comment",
    _EXEMPT_START,
)
# each forbidden step must be NAMED inside the exempt block, so the filer is told explicitly
_EXEMPT_MUST_NAME = ("--label", "sub_issues", "--web", "pull request")

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


@dataclass(frozen=True)
class Finding:
    path: str
    detector: str
    excerpt: str


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}" if detail else label)


def _split_exempt(text: str) -> tuple[str, list[str]]:
    """Return (text outside exempt blocks, the exempt block bodies); unbalanced markers fail."""
    outside: list[str] = []
    inside: list[str] = []
    rest = text
    while _EXEMPT_START in rest:
        head, _, tail = rest.partition(_EXEMPT_START)
        _check("exempt block is closed", _EXEMPT_END in tail, "a not-attempt:start has no matching end")
        body, _, rest = tail.partition(_EXEMPT_END)
        outside.append(head)
        inside.append(body)
    _check("no stray exempt end marker", _EXEMPT_END not in rest, "a not-attempt:end has no matching start")
    outside.append(rest)
    return "".join(outside), inside


def _sentences(text: str) -> list[str]:
    return _SENTENCE_SPLIT_RE.split(re.sub(r"\s+", " ", text))


def _scan_text(path: str, text: str) -> list[Finding]:
    outside, _ = _split_exempt(text)
    findings: list[Finding] = []
    for name, pattern in _PLAIN_DETECTORS.items():
        for match in pattern.finditer(outside):
            findings.append(Finding(path, name, match.group(0)))
    for sentence in _sentences(outside):
        if _PULL_REQUEST_RE.search(sentence) and not _NEGATION_RE.search(sentence):
            findings.append(Finding(path, _PULL_REQUEST_DETECTOR, sentence.strip()[:120]))
    return findings


def _discover(root: Path) -> list[Path]:
    kb = root / _KB_RELDIR
    found = [
        p
        for p in sorted(kb.rglob("*"))
        if p.is_file() and p.suffix in _SURFACE_SUFFIXES and not p.name.endswith(_RETRIEVAL_COMPANION_SUFFIX)
    ]
    contributing = root / "CONTRIBUTING.md"
    if contributing.is_file():
        found.append(contributing)
    forms = root / _FORMS_RELDIR
    if forms.is_dir():
        found.extend(sorted(p for p in forms.glob("*.yml") if p.is_file()))
    return found


def _check_detectors_are_not_vacuous() -> None:
    bad = {
        "label flag": "gh issue create --repo R --label defect --body-file x.md",
        "sub-issues API": "gh api repos/R/issues/1/sub_issues -F sub_issue_id=2",
        "sub-issue attach instruction": "then attach each item's issue to it as a sub-issue",
        "web flag": "gh issue create --web",
        _PULL_REQUEST_DETECTOR: "For a design, open a pull request with the document.",
    }
    for name, line in bad.items():
        hit = [f.detector for f in _scan_text("synthetic", line)]
        _check(f"detector fires: {name}", name in hit, f"no {name!r} finding for {line!r}: {hit}")
    allowed = "Do not open a pull request. A label or a sub-issue link needs write access."
    _check("negated sentence is not flagged", not _scan_text("synthetic", allowed), allowed)
    question = "You are wondering whether to open a pull request or send a patch."
    _check("a whether-question is not an instruction", not _scan_text("synthetic", question), question)
    exempt = f"ok\n{_EXEMPT_START}\nDo not pass --label or --web.\n{_EXEMPT_END}\nok"
    _check("exempt block is not scanned", not _scan_text("synthetic", exempt), exempt)
    unbalanced_failed = False
    try:
        _scan_text("synthetic", f"{_EXEMPT_START} --label")
    except SmokeFailureError:
        unbalanced_failed = True
    _check("unbalanced exempt marker fails", unbalanced_failed)


def _check_no_surface_trips_a_detector(root: Path) -> list[Path]:
    surfaces = _discover(root)
    _check("surfaces discovered", len(surfaces) >= 8, f"only {len(surfaces)} shipped feedback surfaces found under {root}")
    findings: list[Finding] = []
    for path in surfaces:
        findings.extend(_scan_text(str(path.relative_to(root)), path.read_text(encoding="utf-8")))
    detail = "; ".join(f"{f.path} [{f.detector}] {f.excerpt!r}" for f in findings[:12])
    _check(
        "no shipped surface reintroduces a contradictory feedback step",
        not findings,
        f"{len(findings)} finding(s): {detail}",
    )
    return surfaces


def _check_canonical_anchors(root: Path) -> None:
    for relpath in (_RUNBOOK_RELPATH, _SKILL_RELPATH):
        path = root / relpath
        _check(f"canonical file present: {relpath.name}", path.is_file(), str(path))
        text = path.read_text(encoding="utf-8")
        missing = [anchor for anchor in _ANCHORS if anchor not in text]
        _check(f"{relpath.name} carries every canonical anchor", not missing, f"missing {missing}")
        _, blocks = _split_exempt(text)
        _check(f"{relpath.name} has exactly one not-attempt block", len(blocks) == 1, f"found {len(blocks)}")
        unnamed = [token for token in _EXEMPT_MUST_NAME if token not in blocks[0]]
        _check(f"{relpath.name} not-attempt block names every forbidden step", not unnamed, f"unnamed {unnamed}")


def _check_forms(root: Path) -> None:
    forms = root / _FORMS_RELDIR
    for name in ("01_defect.yml", "02_question.yml", "03_feature_request.yml", "04_closure_confirmation.yml"):
        text = (forms / name).read_text(encoding="utf-8")
        _check(f"{name} tells a web filer how to name the round", "(round #" in text, "no '(round #' in the item number field")
    round_form = (forms / "05_feedback_round.yml").read_text(encoding="utf-8")
    _check(
        "05_feedback_round.yml tells the author to edit a checklist into the round body",
        "- [ ] #<issue number>" in round_form and "you can edit it" in round_form,
        "the checklist instruction is missing",
    )


def main(argv: list[str]) -> int:
    root = Path(argv[argv.index("--root") + 1]).resolve() if "--root" in argv else _DEFAULT_ROOT
    try:
        _check_detectors_are_not_vacuous()
        surfaces = _check_no_surface_trips_a_detector(root)
        _check_canonical_anchors(root)
        _check_forms(root)
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"feedback_path_drift_guard_smoke OK: {len(_CHECKS_RUN)} checks passed ({len(surfaces)} surfaces scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
