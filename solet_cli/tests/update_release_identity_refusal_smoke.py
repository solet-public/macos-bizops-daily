"""The exact-candidate-release-identity refusal names every failed fact and fits its repair; the check itself is unchanged (iss_414f2f74, wgr_a737ee5b).

Hermetic and in-process: ``_Execution._verify_advanced`` is driven with the inspection result replaced by crafted facts (the real
inspection needs a real target; the postcondition under test is the pure judgement of those facts).  Every one of the 128 subsets of
the seven facts is broken in turn -- the empty subset is the control -- and each is checked against ``_ORIGINAL``, a verbatim copy of
the one boolean this check was before the change:

- it refuses exactly when ``_ORIGINAL`` is false, with the same ``error_kind`` and exit code, for the control, every single fact, every pair
  and the combined case (strictness: the matrix runs the same on base, where only the naming checks below fail);
- the message keeps its lead phrase, names every failed fact with its measured value and expected value, and names no fact that holds;
- the repair keeps ``Do not reset`` and carries a sentence for each cause present and for no other;
- hostile measured text (a credentialed origin URL, a ``token=`` branch name, a Homebrew keg path) is printed redacted, never the raw
  value, with no printed ``git switch`` for a branch that could not be printed, and both texts pass the REAL Manager
  ``public_string`` validator while the raw text fails it (the control).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from itertools import chain, combinations, product
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "plugins" / "github_midwife_plugin" / "src"), str(_ROOT), str(_ROOT / "solet_cli" / "tests")]
import solet_manager.update_execution as execution  # noqa: E402
from existing_install_inspection_contract_smoke import _facts  # noqa: E402
from solet_manager.adapter_validation import public_string, public_value  # noqa: E402
from solet_manager.errors import SourceTransitionIncompleteError  # noqa: E402
from solet_manager.existing_install_inspection import ExistingInstallFacts, InspectionAnchorKind, InspectionStatus, ObservedBoolean, RepositoryRelation  # noqa: E402
from solet_manager.origin_identity import only_names_repository  # noqa: E402

_CHECKS = 0
_COMMIT = "a" * 40
_TREE = "b" * 40
_CANONICAL = "https://github.com/example-owner/example-seed.git"
_BRANCH = "main"
_LEAD = "target is not at the exact candidate release identity"
_TARGET = Path("/tmp/example-target")


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _original(facts: ExistingInstallFacts, recorded: str | None = _BRANCH) -> bool:
    """The check as it stood before iss_414f2f74: one boolean over seven conjuncts."""
    return bool(
        facts.head_commit == _COMMIT
        and facts.head_tree == _TREE
        and facts.identity_status is InspectionStatus.VERIFIED
        and facts.anchor_kind is InspectionAnchorKind.CURRENT_CHANNEL
        and facts.detached is ObservedBoolean.FALSE
        and facts.branch == recorded
        and only_names_repository(facts.origins, _CANONICAL)
    )


def _good() -> ExistingInstallFacts:
    return replace(_facts(RepositoryRelation.CANONICAL), head_commit=_COMMIT, head_tree=_TREE, branch=_BRANCH, origins=(_CANONICAL,))


#: fact name -> (the facts with that one fact broken, the measured value the message must print, the token that marks its repair,
#: the expected value the message must print)
_BREAKERS: dict[str, tuple[dict[str, object], str, str, str]] = {
    "head_commit": ({"head_commit": "c" * 40}, "c" * 40, "approved bytes", _COMMIT),
    "head_tree": ({"head_tree": "d" * 40}, "d" * 40, "approved bytes", _TREE),
    "identity_status": ({"identity_status": InspectionStatus.FAILED}, "failed", "sealed identity", "verified"),
    "anchor_kind": ({"anchor_kind": InspectionAnchorKind.DEVELOPMENT_CHECKOUT}, "development_checkout", "sealed identity", "current_channel"),
    "detached": ({"detached": ObservedBoolean.TRUE}, "true", "not on a branch", "false"),
    "branch": ({"branch": "feature"}, "feature", "checked-out branch changed", _BRANCH),
    "origins": ({"origins": ("https://github.com/other-owner/other.git",)}, "1 (https://github.com/other-owner/other.git)", "origin remote must be exactly one", f"exactly one, naming {_CANONICAL}"),
}
_ALL_REPAIR_TOKENS = ("approved bytes", "sealed identity", "not on a branch", "checked-out branch changed", "origin remote must be exactly one")


def _broken(facts_names: tuple[str, ...]) -> ExistingInstallFacts:
    changes: dict[str, object] = {}
    for name in facts_names:
        changes.update(_BREAKERS[name][0])
    return replace(_good(), **cast(dict[str, object], changes))


def _verify(facts: ExistingInstallFacts, *, branch: str | None = _BRANCH, target: Path = _TARGET) -> SourceTransitionIncompleteError | None:
    """Run the real ``_Execution._verify_advanced`` over crafted facts; the refusal, or ``None`` when the postcondition holds."""
    subject = object.__new__(execution._Execution)
    channel = SimpleNamespace(canonical_repository=_CANONICAL)
    object.__setattr__(subject, "request", SimpleNamespace())
    object.__setattr__(subject, "record", SimpleNamespace(name="example", channel=channel))
    object.__setattr__(subject, "descriptor", SimpleNamespace(metadata=SimpleNamespace()))
    object.__setattr__(subject, "candidate", SimpleNamespace(fields=SimpleNamespace(commit=_COMMIT, tree_hash=_TREE)))
    object.__setattr__(subject, "journal", {"baseline": {"branch": branch}})
    object.__setattr__(subject, "last_observed", None)
    object.__setattr__(subject, "local_state_report", None)
    with patch.object(execution, "_inspect", lambda *_: SimpleNamespace(facts=facts)), patch.object(execution, "_target_path", lambda _: target), patch.object(execution._Execution, "_local_state_matches", lambda *_: "held"):
        try:
            subject._verify_advanced()
        except SourceTransitionIncompleteError as refusal:
            return refusal
    return None


def _subsets() -> tuple[tuple[str, ...], ...]:
    names = tuple(_BREAKERS)
    return tuple(chain.from_iterable(combinations(names, size) for size in range(len(names) + 1)))


def _check_strictness() -> None:
    """Phase one holds on base and candidate alike: the same subsets refuse, with the same class, error_kind and exit code."""
    for broken in _subsets():
        facts = _broken(broken)
        refusal = _verify(facts)
        label = ",".join(broken) or "control"
        _check(_original(facts) is (not broken), f"{label}: the crafted facts match the original boolean")
        _check((refusal is None) is _original(facts), f"{label}: refuses exactly when the original boolean is false")
        if refusal is not None:
            _check(type(refusal) is SourceTransitionIncompleteError and refusal.error_kind == "source_transition_incomplete" and refusal.exit_code == 3, f"{label}: same error class, error_kind and exit code")
            _check(str(refusal).startswith(_LEAD), f"{label}: the lead phrase is kept")
            _check((refusal.repair or "").startswith("Do not reset"), f"{label}: the repair keeps Do not reset")
    ssh = "git@github.com:example-owner/example-seed.git"
    plain = "https://github.com/example-owner/example-seed"
    other = "https://github.com/other-owner/other.git"
    for origins in ((), (_CANONICAL, _CANONICAL), (_CANONICAL, other), (other, _CANONICAL), (ssh,), (plain,), (other,), ("",)):
        facts = replace(_good(), origins=origins)
        _check((_verify(facts) is None) is _original(facts), f"origins {origins}: refuses exactly when the original boolean is false")
    _check_enum_product()
    print(f"strictness phase: {len(_subsets())} subsets, eight origin shapes and the full enum product refuse exactly as the original boolean does")


def _check_enum_product() -> None:
    """Every member of each enum fact, and None for each optional one, against the original boolean: a conjunct loosened to ``is not X`` is seen."""
    commits, trees, branches = (_COMMIT, "c" * 40, None), (_TREE, "d" * 40, None), (_BRANCH, "feature", None)
    cases = skipped = 0
    for status, anchor, detached in product(InspectionStatus, InspectionAnchorKind, ObservedBoolean):
        for commit, tree, observed_branch, recorded in product(commits, trees, branches, (_BRANCH, None)):
            try:
                facts = replace(_good(), identity_status=status, anchor_kind=anchor, detached=detached, head_commit=commit, head_tree=tree, branch=observed_branch)
            except ValueError:
                skipped += 1  # the facts object refuses an inconsistent status/anchor pair itself
                continue
            cases += 1
            _check((_verify(facts, branch=recorded) is None) is _original(facts, recorded), f"{status},{anchor},{detached},{commit},{tree},{observed_branch},{recorded}: refuses exactly as the original boolean does")
    _check(cases > 1000 and skipped < cases, f"the enum product ran ({cases} cases, {skipped} skipped as inconsistent)")


def _check_named_facts(broken: tuple[str, ...], message: str) -> None:
    """Each fact is named with its measured and expected value exactly when it failed."""
    for name, (_, measured, _, expected) in _BREAKERS.items():
        named = f"{name} is {measured}, expected {expected}" in message
        _check(named is (name in broken), f"{','.join(broken)}: {name} is named with its measured and expected value exactly when it failed: {message}")


def _check_repair_sentences(broken: tuple[str, ...], repair: str) -> None:
    """One sentence per cause present and none for a cause absent; a detached HEAD already carries the branch repair."""
    causes = {_BREAKERS[name][2] for name in broken if not (name == "branch" and "detached" in broken)}
    for token in _ALL_REPAIR_TOKENS:
        _check((token in repair) is (token in causes), f"{','.join(broken)}: repair carries the sentence for {token!r} exactly when its cause is present: {repair}")
    for token in causes:
        _check(repair.count(token) == 1, f"{','.join(broken)}: the sentence for {token!r} appears once however many facts share the cause: {repair}")


def _check_naming() -> None:
    """Phase two is the fix: every failed fact is named with its measured and expected value, and each cause has its own repair."""
    for broken in _subsets()[1:]:
        refusal = _verify(_broken(broken))
        assert refusal is not None
        _check_named_facts(broken, str(refusal))
        _check_repair_sentences(broken, refusal.repair or "")
        _printed(refusal, ",".join(broken))
    several = _verify(replace(_good(), origins=(_CANONICAL, _CANONICAL)))
    _check(several is not None and "origins is 2 (" in str(several), "two origins are counted and shown")


def _printed(refusal: SourceTransitionIncompleteError | None, label: str) -> str:
    """The refusal's whole public text, after proving both parts pass the real validator."""
    _check(refusal is not None, f"{label}: refused")
    assert refusal is not None
    for part, text in (("message", str(refusal)), ("repair", refusal.repair or "unreachable")):
        public_string(text, f"{label} {part}", maximum=4096)
        public_value(text, f"{label} {part}")
    return f"{refusal}\n{refusal.repair}"


def _check_hostile_values() -> None:
    cases = (
        ("credentialed origin", {"origins": ("https://user:ghp_secret@github.com/other-owner/other.git",)}, "ghp_secret"),
        ("keg-path origin", {"origins": ("/opt/homebrew/Cellar/solet/1.0/libexec/seed.git",)}, "/Cellar/solet/"),
        ("secret-shaped branch", {"branch": "token=abc123xyz"}, "abc123xyz"),
        ("oversize origin", {"origins": ("https://github.com/" + "x" * 5000,)}, "x" * 200),
        ("many origins", {"origins": tuple(f"https://github.com/o/r{index}.git" for index in range(40))}, "r39.git"),
    )
    for label, changes, forbidden in cases:
        shown = _printed(_verify(replace(_good(), **cast(dict[str, object], changes))), label)
        _check(forbidden not in shown, f"{label}: the raw hostile text is not printed")
    keg_target = _printed(_verify(replace(_good(), head_commit="c" * 40), target=Path("/opt/homebrew/Cellar/solet/1.0/token=abc123xyz")), "hostile target path")
    _check("/Cellar/solet/" not in keg_target and "abc123xyz" not in keg_target, "a hostile target path is neutralised in the repair")


def _check_joined_text() -> None:
    """A value that ends in a secret keyword and separator is safe alone and secret-shaped once the text after it is joined on."""
    branch_shown = _printed(_verify(replace(_good(), branch="feature/token=")), "branch ending in token=")
    _check("feature/token=" in branch_shown or "[REDACTED]" in branch_shown, "the branch is named, redacted or not")
    origins = ("https://github.com/o/r.git?token=", "https://github.com/other-owner/other.git")
    _printed(_verify(replace(_good(), origins=origins)), "two origins, the first ending ?token=")
    detached_whole = _printed(_verify(replace(_good(), branch="a/password:", detached=ObservedBoolean.TRUE)), "detached with a keyword-ending branch")
    _check("password:" not in detached_whole.replace("[REDACTED]", ""), "no raw keyword value survives")


def _check_userinfo_with_at() -> None:
    """A password that itself contains ``@`` loses every part of the credential, not just up to the first ``@``."""
    shown = _printed(_verify(replace(_good(), origins=("https://user:p@ssw0rdY@github.com/o/r.git",))), "password with @")
    _check("ssw0rdY" not in shown and "p@" not in shown and "user:" not in shown, "no part of the credential is printed")
    _check("github.com/o/r.git" in shown, "the host and path stay readable")


def _check_detached_repair() -> None:
    """A detached HEAD reads as branch None: one switch sentence, the detached one; never the false 'branch changed' sentence."""
    refusal = _verify(replace(_good(), detached=ObservedBoolean.TRUE, branch=None))
    shown = _printed(refusal, "detached HEAD")
    _check("branch is none, expected main" in shown and "detached is true, expected false" in shown, "both facts are still named")
    _check("HEAD is not on a branch" in shown and "checked-out branch changed" not in shown, "only the detached repair sentence is printed")
    _check(shown.count(" switch main") == 1, "one switch command")
    still_branch = _printed(_verify(replace(_good(), branch="feature")), "branch changed alone")
    _check("checked-out branch changed" in still_branch and "HEAD is not on a branch" not in still_branch, "a branch change alone keeps its own sentence")


def _check_validator_control() -> None:
    """The real validator refuses raw secret-shaped text; without this the checks above would prove nothing."""
    try:
        public_string("origins is https://user:token=abc@github.com/o/r.git", "control", maximum=4096)
    except ValueError:
        _check(True, "control")
    else:
        raise AssertionError("the real validator accepted raw secret-shaped text; the check proves nothing")


def _check_branch_repair() -> None:
    """A branch whose name cannot be printed, or none recorded, gets no printed switch command."""
    unprintable = _printed(_verify(replace(_good(), detached=ObservedBoolean.TRUE, branch=None), branch="token=abc123xyz"), "unprintable branch")
    _check("git -C" in unprintable and " switch " not in unprintable and "abc123xyz" not in unprintable, "an unprintable branch gets no switch command")
    none_recorded = _printed(_verify(replace(_good(), detached=ObservedBoolean.TRUE), branch=None), "no recorded branch")
    _check(" switch " not in none_recorded and "recorded no branch" in none_recorded, "no recorded branch: no switch command")
    printable = _printed(_verify(replace(_good(), detached=ObservedBoolean.TRUE, branch=None)), "printable branch")
    _check("switch main" in printable, "a printable branch gets its switch command")


def main() -> int:
    _check_strictness()
    _check_naming()
    _check_hostile_values()
    _check_joined_text()
    _check_userinfo_with_at()
    _check_detached_repair()
    _check_validator_control()
    _check_branch_repair()
    print(f"update_release_identity_refusal_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
