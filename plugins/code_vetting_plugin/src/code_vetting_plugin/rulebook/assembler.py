"""assembler.py — the deterministic rulebook composer (W3-C C1, build-step).

Reads the tier-tagged SOURCES — FT-2's in-code directive clauses (``verify/lenses``) and DNF rules
(``verify/rulebook``), the v1 rulebook doc's tier sections, and Claude-A's two universal-tier guidance
articles — and composes ONE ``assembled_rulebook.json`` (content + three-level manifest). It COMPOSES,
never CLASSIFIES: every tier tag comes from its source (the in-code tags; the doc's own section
headings). Run it as a BUILD STEP (``python -m code_vetting_plugin.rulebook.assembler``) and COMMIT the
regenerated artifact — it is never assembled at first-load (a release copy ships the package, not the
``workbench/`` sources), and the committed artifact makes every rulebook change a GC-gated diff.

Determinism: the same sources always produce a byte-identical artifact (canonical JSON, stable order).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..run_context import repo_root
from ..verify.lenses import REFUTE_CLAUSES
from ..verify.rulebook import KEYWORD_RULES, SCOPE_RULE, TEST_ANY_RULE, DoNotFlagRule
from ..verify.tiers import PolicyTier
from .manifest import build_artifact

ARTIFACT_NAME = "assembled_rulebook.json"
ARTIFACT_PATH = Path(__file__).resolve().parent / ARTIFACT_NAME

# Sources read at BUILD time (never at runtime). The doc's tier sections + A's universal guidance.
_RULEBOOK_DOC_RELPATH = Path("workbench") / "2026-07-19_vetting_rulebook_v1.md"
_GUIDANCE_RELPATHS: tuple[Path, ...] = (
    Path("plugins/code_vetting_plugin/knowledge_base/guidance_magic_strings.md"),
    Path("plugins/code_vetting_plugin/knowledge_base/guidance_dead_symbols.md"),
)

# The doc's OWN section headings declare each preamble section's tier — a composition of the doc's
# declared structure, not a classification. §2 (the platform rulebook) + §4 (the platform-policy DO-NOT-FLAG
# moat) are project_local (they drop on a foreign target); the universal-correctness / stakes framing
# stays universal.
_DOC_TIER_BY_HEADING: tuple[tuple[str, PolicyTier], ...] = (
    ("Project tier", PolicyTier.PROJECT_LOCAL),
    ("DO NOT FLAG", PolicyTier.PROJECT_LOCAL),
)


def _dnf_entry(rule: DoNotFlagRule, kind: str) -> dict[str, Any]:
    return {"rule_id": rule.rule_id, "summary": rule.summary, "tier": rule.tier.value, "triggers": list(rule.triggers), "kind": kind}


def _directives_content() -> dict[str, list[list[str]]]:
    """The refute directives as ``{lens: [[tier, text], ...]}`` — clauses stored VERBATIM so the
    loader renders byte-identical to ``lenses.refute_directive`` (the regression bar)."""
    return {
        lens.value: [[clause.tier.value, clause.text] for clause in clauses]
        for lens, clauses in REFUTE_CLAUSES.items()
    }


def _dnf_content() -> list[dict[str, Any]]:
    keyword = [_dnf_entry(rule, "keyword") for rule in KEYWORD_RULES]
    structural = [_dnf_entry(SCOPE_RULE, "scope"), _dnf_entry(TEST_ANY_RULE, "test_any")]
    return keyword + structural


def _tier_for_heading(heading: str) -> str:
    for marker, tier in _DOC_TIER_BY_HEADING:
        if marker in heading:
            return tier.value
    return PolicyTier.UNIVERSAL.value


def _preamble_sections(doc_text: str) -> list[dict[str, str]]:
    """Split the v1 doc into ``## ``-headed blocks, each tagged by its heading's declared tier."""
    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    body: list[str] = []
    for line in doc_text.splitlines():
        if line.startswith("## "):
            if current is not None:
                current["body"] = "\n".join(body).strip()
                sections.append(current)
            heading = line[3:].strip()
            current = {"tier": _tier_for_heading(heading), "heading": heading}
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        current["body"] = "\n".join(body).strip()
        sections.append(current)
    return sections


def _guidance_content(root: Path) -> list[dict[str, str]]:
    return [
        {"name": relpath.name, "tier": PolicyTier.UNIVERSAL.value, "text": (root / relpath).read_text(encoding="utf-8")}
        for relpath in _GUIDANCE_RELPATHS
    ]


def _tier_sections(content: dict[str, Any]) -> dict[str, Any]:
    """The per-tier content subsets — for the manifest's per-tier hashes (localize a diff, W3C-1b)."""
    tiers: dict[str, list[Any]] = {PolicyTier.UNIVERSAL.value: [], PolicyTier.PROJECT_LOCAL.value: []}
    for lens, clauses in content["directives"].items():
        for tier, text in clauses:
            tiers[tier].append(["directive", lens, text])
    for rule in content["dnf_rules"]:
        tiers[rule["tier"]].append(["dnf", rule["rule_id"]])
    for section in content["preamble_sections"]:
        tiers[section["tier"]].append(["preamble", section["heading"]])
    for article in content["guidance"]:
        tiers[article["tier"]].append(["guidance", article["name"]])
    return tiers


def assemble(root: Path | None = None) -> dict[str, Any]:
    """Compose the full artifact (content + three-level manifest) from the live sources."""
    resolved = root if root is not None else repo_root()
    content: dict[str, Any] = {
        "version": "f2-v1",
        "directives": _directives_content(),
        "dnf_rules": _dnf_content(),
        "preamble_sections": _preamble_sections((resolved / _RULEBOOK_DOC_RELPATH).read_text(encoding="utf-8")),
        "guidance": _guidance_content(resolved),
    }
    sources = [
        {"anchor": "verify/lenses.py::REFUTE_CLAUSES", "content": content["directives"]},
        {"anchor": "verify/rulebook.py::DNF", "content": content["dnf_rules"]},
        *({"anchor": f"rulebook_doc::{section['heading']}", "content": section} for section in content["preamble_sections"]),
        *({"anchor": f"guidance::{article['name']}", "content": article} for article in content["guidance"]),
    ]
    return build_artifact(content, sources, _tier_sections(content))


def write_artifact(path: Path = ARTIFACT_PATH, root: Path | None = None) -> None:
    """Assemble + write the committed artifact (canonical JSON — deterministic, GC-diffable)."""
    artifact = assemble(root)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_artifact()
    print(f"assembled rulebook written to {ARTIFACT_PATH}")
