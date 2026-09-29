"""Smoke: every shipped upgrade instruction marks ``python@3.13`` installed-on-request first.

``iss_d62aeab7`` / ``dec_b08cf4c7``.  r61 drops the Manager formula's dependency on
``python@3.13``; on an install made before r61 that Python is recorded
``installed_on_request=false``, so ``brew autoremove`` would delete it from under every solet.
The order of the upgrade command is the protection, and the documents are read by solet
sessions that copy what they find.  This smoke fails if any shipped surface tells a reader to
run a bare ``brew upgrade solet``, or to install the Manager without r61's install guard.

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
5. r61's install guard: the install detector fires on the unguarded forms and stays quiet on
   the guarded one (anti-vacuity); no discovered surface and neither newest release-notes entry
   installs the Manager without ``HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13``
   immediately before the tap; and every document that documents the install carries the exact
   guarded command, except the README, which the release's own tap rewrites: it must carry the guarded
   install for exactly one tap, whichever tap that is. (``seed_readme_smoke.py`` in the seed-factory plugin also owns the seed
   README's single-install-line parser.)
6. The r60 release-notes entry still names ``manager-v0.1.0-r60`` and says r60 still depends on
   ``python@3.13`` (true of r60). The r61 entry names ``manager-v0.1.0-r61`` and says the Manager
   formula never installs or upgrades ``python@3.13`` and bootstraps its own pinned pip, and the
   formula template declares no Python
   dependency. The r61 statement and the formula must change together; this pair fails the day
   only one does.

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
_INSTALL_GUARD = "HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13"
_GUARDED_INSTALL = f"{_INSTALL_GUARD} solet-public/tap/solet"
# Any `brew install ... <owner>/<tap>/solet`, with whatever sits between `install` and the tap.
_INSTALL_RE = re.compile(r"(?<![A-Za-z0-9_.])brew install (?P<between>(?:[^\s`'\"]+ )*?)(?P<tap>[A-Za-z0-9-]+/[A-Za-z0-9-]+)/solet\b")
_README = Path("README.md")
_INSTALL_DOCS = (
    Path("README.md"),
    Path("plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md"),
    Path("plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md"),
    Path("plugins/github_midwife_plugin/knowledge_base/06_seed_update_operator_guide.md"),
    Path("plugins/github_midwife_plugin/knowledge_base/09_homebrew_install_troubleshooting_runbook.md"),
    Path("solet_cli/homebrew/README.md"),
)
# The seed factory ships in no capability bundle, so a born clone carries none of its templates. They are
# required wherever the factory package itself is present (the checkout) and are the source the shipped
# README, CLAUDE.md and AGENTS.md are rendered from.
_FACTORY_PACKAGE = Path("plugins/seed_factory_plugin/src/seed_factory_plugin")
_FACTORY_INSTALL_DOCS = (
    Path("plugins/seed_factory_plugin/knowledge_base/seed_agents.md.template"),
    Path("plugins/seed_factory_plugin/knowledge_base/seed_claude.md.template"),
    Path("plugins/seed_factory_plugin/knowledge_base/seed_readme.md.template"),
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


def _is_guarded_install(flat: str, match: re.Match[str]) -> bool:
    return match.group("between") == "python@3.13 " and flat[: match.start()].endswith("HOMEBREW_NO_INSTALL_UPGRADE=1 ")


def _unguarded_installs(text: str) -> list[str]:
    """Each Manager install not written as ``HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 <tap>``."""

    flat = re.sub(r"\s+", " ", text)
    return [flat[max(0, match.start() - 40) : match.end() + 10] for match in _INSTALL_RE.finditer(flat) if not _is_guarded_install(flat, match)]


def _guarded_install_taps(text: str) -> list[str]:
    """The tap of each Manager install written with r61's guard, whichever tap it names."""

    flat = re.sub(r"\s+", " ", text)
    return [match.group("tap") for match in _INSTALL_RE.finditer(flat) if _is_guarded_install(flat, match)]


def _readme_install_problem(text: str) -> str | None:
    """Why a README does not carry r61's guarded install, or ``None``.

    ``seed_readme.render_release_install`` rewrites the README's one install line to the release's own tap
    (``dwestgate/tap-validate`` for a daily seed), so the README's tap is not the stable ``solet-public/tap``.
    The guard is what r61 owes a reader: any tap is accepted, an unguarded install line is not.
    """

    taps = _guarded_install_taps(text)
    if len(taps) != 1:
        return f"expected exactly one guarded Manager install, found {len(taps)}"
    return f"{_unguarded_installs(text)}" if _unguarded_installs(text) else None


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


def _assert_install_detector_is_not_vacuous() -> None:
    _check(_unguarded_installs("Run `brew install solet-public/tap/solet` first.") != [], "the install detector missed the pre-r61 bare install")
    _check(_unguarded_installs("Run `brew install python@3.13 solet-public/tap/solet`.") != [], "the install detector missed an install without HOMEBREW_NO_INSTALL_UPGRADE")
    _check(_unguarded_installs("Run `brew install\n  dwestgate/tap-validate/solet`.") != [], "the install detector missed a line-wrapped bare install")
    _check(_unguarded_installs(f"Run `{_GUARDED_INSTALL}`.") == [], "the install detector flagged the guarded install")
    wrapped = "Run `HOMEBREW_NO_INSTALL_UPGRADE=1 brew install\n  python@3.13 dwestgate/tap-validate/solet`."
    _check(_unguarded_installs(wrapped) == [], "the install detector flagged the guarded install when it wraps a line")
    validate = "HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 dwestgate/tap-validate/solet"
    _check(_guarded_install_taps(f"```\n{validate}\n```") == ["dwestgate/tap-validate"], "the guarded-tap reader missed a release-rendered tap")
    _check(_readme_install_problem(f"```\n{validate}\n```") is None, "the README check refused the guarded install rendered for a release tap")
    for unguarded in (
        "brew install dwestgate/tap-validate/solet",
        "brew install python@3.13 dwestgate/tap-validate/solet",
        "HOMEBREW_NO_INSTALL_UPGRADE=1 brew install dwestgate/tap-validate/solet",
    ):
        _check(_readme_install_problem(f"```\n{unguarded}\n```") is not None, f"the README check accepted an unguarded install line: {unguarded}")
    _check(_readme_install_problem("A README with no install line.") is not None, "the README check accepted a README with no install at all")


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


def _assert_r61_install_guard_is_documented(root: Path) -> None:
    for path in _surfaces(root):
        hits = _unguarded_installs(path.read_text(encoding="utf-8"))
        _check(hits == [], f"{path.relative_to(root)} installs the Manager without r61's guard: {hits}")
    notes = _newest_two_notes_entries((root / _NOTES).read_text(encoding="utf-8"))
    _check(_unguarded_installs(notes) == [], f"the two newest RELEASE_NOTES.md entries install without r61's guard: {_unguarded_installs(notes)}")
    install_docs = _INSTALL_DOCS + (_FACTORY_INSTALL_DOCS if (root / _FACTORY_PACKAGE).is_dir() else ())
    for relative in install_docs:
        text = (root / relative).read_text(encoding="utf-8")
        if relative == _README:
            problem = _readme_install_problem(text)
            _check(problem is None, f"{relative} does not carry the guarded install `{_INSTALL_GUARD} <tap>/solet`: {problem}")
            continue
        _check(_GUARDED_INSTALL in re.sub(r"\s+", " ", text), f"{relative} does not carry the exact guarded install `{_GUARDED_INSTALL}`")


def _entry(notes: str, release: str) -> str:
    entries = [entry for entry in re.split(r"(?m)^(?=## )", notes) if re.match(rf"## .*\b{release}\b", entry)]
    _check(len(entries) == 1, f"RELEASE_NOTES.md must carry exactly one {release} entry, found {len(entries)}")
    return re.sub(r"\s+", " ", entries[0])


def _assert_r61_drops_the_dependency_and_says_so(root: Path) -> None:
    notes = (root / _NOTES).read_text(encoding="utf-8")
    r60 = _entry(notes, "r60")
    _check("manager-v0.1.0-r60" in r60, "the r60 entry does not name manager-v0.1.0-r60")
    _check("r60 still depends on `python@3.13`" in r60, "the r60 entry no longer says r60 still depends on python@3.13")
    r61 = _entry(notes, "r61")
    _check("manager-v0.1.0-r61" in r61, "the r61 entry does not name manager-v0.1.0-r61")
    _check(
        "The Manager formula never installs or upgrades `python@3.13`." in r61
        and "pinned pip 26.2.1" in r61
        and _GUARDED_INSTALL.replace("solet-public/tap/solet", "") in r61
        and "No Python 3.13 found" in r61
        and _GUARDED in r61,
        "the r61 entry does not name the install command, the formula's no-upgrade promise, the pip bootstrap, the odie recovery and the unchanged r60 upgrade",
    )
    formula = (root / _FORMULA).read_text(encoding="utf-8")
    _check(
        re.search(r'^\s*depends_on\s+"python', formula, re.MULTILINE) is None,
        "the formula still declares a Python dependency while the r61 entry says the Manager never upgrades python@3.13",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    root = parser.parse_args().root.resolve()
    _assert_detector_is_not_vacuous()
    _assert_install_detector_is_not_vacuous()
    _assert_no_surface_tells_a_reader_to_upgrade_bare(root)
    _assert_guarded_command_is_in_each_document(root)
    _assert_r61_install_guard_is_documented(root)
    _assert_r61_drops_the_dependency_and_says_so(root)
    print(f"python_on_request_upgrade_docs_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
