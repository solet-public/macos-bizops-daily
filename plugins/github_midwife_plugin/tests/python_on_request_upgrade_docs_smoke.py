"""Smoke: every shipped upgrade instruction marks ``python@3.13`` installed-on-request first.

``iss_d62aeab7`` / ``dec_b08cf4c7``.  r61 drops the Manager formula's dependency on
``python@3.13``; on a real install that Python is recorded ``installed_on_request=false``, so
from then on ``brew autoremove`` would delete it from under every solet.  Until then the only
protection is the order of the upgrade command, and the documents are read by solet sessions
that copy what they find.  This smoke fails if any shipped surface tells a reader to run a
bare ``brew upgrade solet``.

Coverage:

1. The surfaces are DISCOVERED, not listed by hand: every Markdown and template file under
   ``plugins/github_midwife_plugin/knowledge_base/`` and ``plugins/seed_factory_plugin/knowledge_base/``
   (retrieval-test companions excepted), the tap README, the root README, every Manager source
   file, and the two newest ``RELEASE_NOTES.md`` entries.
2. The detector fires on a synthetic bare upgrade and stays quiet on the synthetic guarded one
   and on the sentence that describes the bare form as what NOT to rely on (anti-vacuity).
3. No discovered surface trips the detector.
4. The four documents that carry an upgrade instruction (seed-update runbook, update guide,
   troubleshooting runbook, tap README) each carry the exact guarded command.
5. None of those documents carries r61's install guard yet: the install line is r61's to change.
   (The seed README template is not part of a born clone, so ``seed_readme_smoke.py`` in the
   seed-factory plugin owns its half: it points at the runbook and carries no second brew command.)
6. The r60 release-notes entry names ``manager-v0.1.0-r60`` and says r60 still depends on
   ``python@3.13``, and the formula template still declares that dependency. The two statements
   must change together in r61; this pair fails the day only one does.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/tests/python_on_request_upgrade_docs_smoke.py``.
Pass ``--root <tree>`` to run it against another checkout of the same layout, for example an
extraction of an earlier commit.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parents[3]
_GUARDED = "brew tab --installed-on-request python@3.13 && brew upgrade solet"
_KB_DIRS = (
    Path("plugins/github_midwife_plugin/knowledge_base"),
    Path("plugins/seed_factory_plugin/knowledge_base"),
)
_RETRIEVAL_COMPANION_SUFFIX = ".retrieval_test.yaml"
_SURFACE_SUFFIXES = (".md", ".template")
_UPGRADE_RE = re.compile(r"brew upgrade\s+(?:solet-public/tap/)?solet\b")
_BARE_RE = re.compile(r"\bbare\b", re.IGNORECASE)
_GUARDED_DOCS = (
    Path("plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md"),
    Path("plugins/github_midwife_plugin/knowledge_base/06_seed_update_operator_guide.md"),
    Path("plugins/github_midwife_plugin/knowledge_base/09_homebrew_install_troubleshooting_runbook.md"),
    Path("solet_cli/homebrew/README.md"),
)
_FORMULA = Path("solet_cli/homebrew/Formula/solet.rb.template")
_NOTES = Path("RELEASE_NOTES.md")

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _bare_upgrades(text: str) -> list[str]:
    """Each ``brew upgrade solet`` not directly preceded by the guard and not described as the bare form."""

    flat = re.sub(r"\s+", " ", text)
    hits: list[str] = []
    for match in _UPGRADE_RE.finditer(flat):
        if flat[: match.start()].rstrip().endswith("brew tab --installed-on-request python@3.13 &&"):
            continue
        sentence_start = max(flat.rfind(". ", 0, match.start()), flat.rfind("; ", 0, match.start())) + 1
        sentence_end = flat.find(". ", match.end())
        sentence = flat[sentence_start : len(flat) if sentence_end == -1 else sentence_end]
        if _BARE_RE.search(sentence):
            continue
        hits.append(flat[max(0, match.start() - 60) : match.end() + 20])
    return hits


def _surfaces(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory in _KB_DIRS:
        base = root / directory
        found.extend(
            sorted(
                path
                for path in base.rglob("*")
                if path.is_file() and path.suffix in _SURFACE_SUFFIXES and not path.name.endswith(_RETRIEVAL_COMPANION_SUFFIX)
            )
        )
    found.extend([root / "solet_cli/homebrew/README.md", root / "README.md"])
    found.extend(sorted((root / "solet_cli/src/solet_manager").glob("*.py")))
    return found


def _newest_two_notes_entries(text: str) -> str:
    headings = [match.start() for match in re.finditer(r"^## ", text, re.MULTILINE)]
    return text[headings[0] : headings[2]] if len(headings) >= 3 else text


def _assert_detector_is_not_vacuous() -> None:
    _check(_bare_upgrades("Run `brew upgrade solet`, then update.") != [], "the detector missed a bare `brew upgrade solet`")
    _check(_bare_upgrades("Run `brew upgrade solet-public/tap/solet` now.") != [], "the detector missed the tap-qualified bare upgrade")
    _check(_bare_upgrades(f"Run `{_GUARDED}`, then update.") == [], "the detector flagged the guarded command")
    wrapped = "Run `brew tab --installed-on-request python@3.13 &&\nbrew upgrade solet`."
    _check(_bare_upgrades(wrapped) == [], "the detector flagged the guarded command when it wraps a line")
    _check(
        _bare_upgrades("This release still depends on it, so a bare `brew upgrade solet` loses nothing today.") == [],
        "the detector flagged the sentence that describes the bare form",
    )


def _assert_no_surface_tells_a_reader_to_upgrade_bare(root: Path) -> None:
    surfaces = _surfaces(root)
    _check(len(surfaces) > 20, f"discovery found only {len(surfaces)} surfaces; the globs no longer match the layout")
    for path in surfaces:
        hits = _bare_upgrades(path.read_text(encoding="utf-8"))
        _check(hits == [], f"{path.relative_to(root)} tells a reader to upgrade without the guard: {hits}")
    notes = _newest_two_notes_entries((root / _NOTES).read_text(encoding="utf-8"))
    hits = _bare_upgrades(notes)
    _check(hits == [], f"the two newest RELEASE_NOTES.md entries tell a reader to upgrade without the guard: {hits}")


def _assert_guarded_command_is_in_each_document(root: Path) -> None:
    for relative in _GUARDED_DOCS:
        flat = re.sub(r"\s+", " ", (root / relative).read_text(encoding="utf-8"))
        _check(_GUARDED in flat, f"{relative} does not carry the exact guarded command `{_GUARDED}`")


def _assert_r61_install_guard_is_not_documented_early(root: Path) -> None:
    for relative in _GUARDED_DOCS:
        _check("HOMEBREW_NO_INSTALL_UPGRADE" not in (root / relative).read_text(encoding="utf-8"), f"{relative} carries r61's install guard before r61")


def _assert_r60_still_depends_and_says_so(root: Path) -> None:
    notes = (root / _NOTES).read_text(encoding="utf-8")
    entries = [entry for entry in re.split(r"(?m)^(?=## )", notes) if re.match(r"## .*\br60\b", entry)]
    _check(len(entries) == 1, f"RELEASE_NOTES.md must carry exactly one r60 entry, found {len(entries)}")
    entry = re.sub(r"\s+", " ", entries[0])
    _check("manager-v0.1.0-r60" in entry, "the r60 entry does not name manager-v0.1.0-r60")
    _check("r60 still depends on `python@3.13`" in entry, "the r60 entry does not say r60 still depends on python@3.13")
    _check('depends_on "python@3.13"' in (root / _FORMULA).read_text(encoding="utf-8"), "the formula no longer declares python@3.13: update the r60 wording with r61")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    root = parser.parse_args().root.resolve()
    _assert_detector_is_not_vacuous()
    _assert_no_surface_tells_a_reader_to_upgrade_bare(root)
    _assert_guarded_command_is_in_each_document(root)
    _assert_r61_install_guard_is_not_documented_early(root)
    _assert_r60_still_depends_and_says_so(root)
    print(f"python_on_request_upgrade_docs_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
