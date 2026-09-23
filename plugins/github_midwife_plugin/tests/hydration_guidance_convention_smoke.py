"""Smoke: the `hydration_guidance.md` convention (Step 4c, generic offer).

Step 4c of the hydration runbook globs `plugins/*/knowledge_base/hydration_guidance.md`
directly on the filesystem (NOT a KB search -- a plugin can be present-but-dormant,
with no searchable KB yet) and pitches whatever it finds, each following the same
Pitch/Setup shape. This is a convention with no code enforcing its shape -- a future
plugin author who gets the header or section names wrong produces a file the runbook's
own instructions can't process predictably. This smoke is that enforcement.

Coverage:

1. At least the plugins known to carry this file today (g_suite_plugin,
   claude_code_filesystem_session_source_plugin, codex_filesystem_session_source_plugin)
   are found by the same glob the runbook instructs.
2. Every found file has the required header fields (Article Layer, Article Role:
   hydration_guidance, Article Tags, Embedding Description) and no placeholder text.
3. Every found file has both a `## Pitch` and a `## Setup` section, in that order.
4. Embedding Description strings are pairwise distinct across all found files (a cheap
   floor against an obvious copy-paste-forgot-to-edit mistake, not full cosine).
5. Session-ledger hydration guidance never tells operators to bind
   `session_ledger_service` in `service_bindings.json`; that service is constructed
   during startup and filesystem ingest is controlled by `ledger_allowed_roots` +
   source registration.
6. Session-ledger hydration guidance gives the driving agent no-MCP `<name> call`
   commands and names the restart boundary after changing `ledger_allowed_roots`.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/tests/hydration_guidance_convention_smoke.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Plugins KNOWN to carry hydration_guidance.md, used as the anti-vacuity floor.
# Intersected at run time with the plugin directories actually PRESENT in the tree
# (`_check_expected_plugins_found`) — a seed bundle prunes most of the roster, and a
# hardcoded list demanding absent plugins fails in every bundle while silently
# rotting at origin whenever a plugin is added or renamed. The floor is the
# intersection, never the literal set.
_EXPECTED_MINIMUM = {
    "g_suite_plugin",
    "claude_code_filesystem_session_source_plugin",
    "codex_filesystem_session_source_plugin",
}
_HYDRATION_RUNBOOK = _REPO_ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base" / "01_hydration_runbook.md"
_SEED_FACING_KB_ROOTS = (
    _REPO_ROOT / "ananta" / "knowledge_bases" / "ananta_platform",
    _REPO_ROOT / "plugins",
)
_TEXT_SUFFIXES = {".cloud_graviton", ".json", ".md", ".template", ".yaml", ".yml"}
# Fragmented (not written whole): this pattern IS the legacy marker the seal
# validator scans shipped bytes for, so it must be assembled rather than
# appear as a literal or the validator would refuse this file.
_LEGACY_ORIGIN_ENV_RE = re.compile(r"\bA" + r"DA_[A-Z0-9_]+\b")
_PLUGIN_CORE_PRODUCT_ENV_RE = re.compile(r"\bANANTA_[A-Z0-9_]+\b")
# NOT "..." here: the platform's own `<...>` prose notation (e.g. `vault::<...>.key`)
# is a legitimate placeholder convention already used in existing KB articles, not an
# unfinished-content marker. TODO/TBD are unambiguous; "..." is not.
_PLACEHOLDER_MARKERS = ("TODO", "TBD")

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _discover() -> list[Path]:
    return sorted(_REPO_ROOT.glob("plugins/*/knowledge_base/hydration_guidance.md"))


def _plugin_name(path: Path) -> str:
    return path.parents[1].name


def _iter_seed_facing_kb_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for root in _SEED_FACING_KB_ROOTS:
        if root.name == "plugins":
            kb_roots = sorted(root.glob("*/knowledge_base"))
            for kb_root in kb_roots:
                files.extend(
                    (p, "plugin") for p in kb_root.rglob("*") if p.is_file() and p.suffix in _TEXT_SUFFIXES
                )
        else:
            files.extend((p, "platform") for p in root.rglob("*") if p.is_file() and p.suffix in _TEXT_SUFFIXES)
    return sorted(files)


def _plugins_present() -> set[str]:
    """Plugin directories actually present in this tree (origin: the full checkout;
    a seed bundle: only the resolved subset)."""
    plugins_dir = _REPO_ROOT / "plugins"
    if not plugins_dir.is_dir():
        return set()
    return {p.name for p in plugins_dir.iterdir() if p.is_dir()}


def _check_expected_plugins_found(found: list[Path]) -> None:
    found_plugins = {_plugin_name(p) for p in found}
    # Anti-vacuity FIRST: a glob that matches nothing must never read as a clean pass.
    _check(
        "the hydration_guidance.md glob matches at least one file",
        bool(found_plugins),
        f"glob {_REPO_ROOT}/plugins/*/knowledge_base/hydration_guidance.md matched nothing",
    )
    expected = _EXPECTED_MINIMUM & _plugins_present()
    # If the ENTIRE known roster is absent the floor has gone vacuous — that is the
    # "the tree is not what this smoke thinks it is" signal, so fail rather than
    # silently drop to a no-op expectation.
    _check(
        "at least one plugin known to carry hydration_guidance.md is present in this tree",
        bool(expected),
        f"none of {sorted(_EXPECTED_MINIMUM)} is present under {_REPO_ROOT}/plugins",
    )
    missing = expected - found_plugins
    _check(
        "every PRESENT plugin known to carry hydration_guidance.md is found by the glob",
        not missing,
        f"present but missing the file: {sorted(missing)}",
    )


def _check_headers(path: Path, content: str) -> None:
    name = _plugin_name(path)
    _check(f"{name}: has Article Layer header", "Article Layer:" in content, "missing Article Layer:")
    _check(
        f"{name}: has Article Role: hydration_guidance header",
        "Article Role: hydration_guidance" in content,
        "missing or wrong Article Role",
    )
    _check(f"{name}: has Article Tags header", "Article Tags:" in content, "missing Article Tags:")
    _check(
        f"{name}: has Embedding Description header", "Embedding Description:" in content,
        "missing Embedding Description:",
    )
    for marker in _PLACEHOLDER_MARKERS:
        _check(f"{name}: no '{marker}' placeholder", marker not in content, f"found literal {marker!r}")


def _check_sections(path: Path, content: str) -> None:
    name = _plugin_name(path)
    pitch_match = re.search(r"^## Pitch$", content, re.MULTILINE)
    setup_match = re.search(r"^## Setup$", content, re.MULTILINE)
    _check(f"{name}: has a '## Pitch' section", pitch_match is not None, "missing '## Pitch' heading")
    _check(f"{name}: has a '## Setup' section", setup_match is not None, "missing '## Setup' heading")
    if pitch_match and setup_match:
        _check(
            f"{name}: Pitch precedes Setup",
            pitch_match.start() < setup_match.start(),
            "Setup appears before Pitch",
        )


def _check_session_ledger_guidance(path: Path, content: str) -> None:
    name = _plugin_name(path)
    if "session_ledger_service" not in content:
        return
    forbidden = (
        "Bind `session_ledger_service`",
        "bind `session_ledger_service`",
        "session_ledger_service is bound",
        "session_ledger_service` binding",
    )
    for marker in forbidden:
        _check(
            f"{name}: does not describe session_ledger_service as a service binding",
            marker not in content,
            f"found stale binding guidance marker {marker!r}",
        )
    _check(
        f"{name}: points filesystem ingest at ledger_allowed_roots",
        "ledger_allowed_roots" in content,
        "missing ledger_allowed_roots consent boundary",
    )
    _check(
        f"{name}: uses no-MCP list_sources command",
        "<name> call service_interface::session_ledger_service::list_sources '{}'" in content,
        "missing literal no-MCP list_sources command",
    )
    _check(
        f"{name}: uses no-MCP register_source command",
        "<name> call service_interface::session_ledger_service::register_source" in content,
        "missing literal no-MCP register_source command",
    )
    _check(
        f"{name}: uses no-MCP trigger_poll command",
        "<name> call service_interface::session_ledger_service::trigger_poll '{}'" in content,
        "missing literal no-MCP trigger_poll command",
    )
    _check(
        f"{name}: explicitly avoids MCP translation",
        "do not translate these steps into `mcp__<name>__process_call`" in content,
        "missing no-MCP-first instruction",
    )
    _check(
        f"{name}: restart after ledger_allowed_roots edit",
        "restart the solet before registering sources" in content,
        "missing restart boundary",
    )


def _extract_embedding_description(content: str) -> str:
    match = re.search(r"^Embedding Description: (.+)$", content, re.MULTILINE)
    return match.group(1) if match else ""


def _check_embedding_descriptions_distinct(found: list[Path]) -> None:
    descriptions = [(p, _extract_embedding_description(p.read_text(encoding="utf-8"))) for p in found]
    seen: dict[str, Path] = {}
    for path, desc in descriptions:
        if desc in seen:
            raise SmokeFailureError(
                f"embedding descriptions pairwise distinct: {_plugin_name(path)} and "
                f"{_plugin_name(seen[desc])} share the identical Embedding Description"
            )
        seen[desc] = path
    _CHECKS_RUN.append("embedding descriptions pairwise distinct")


def _check_hydration_runbook_seed_neutral_and_no_mcp_primary() -> None:
    # DELIBERATE EXEMPTION (2026-08-08): these literals are the banned-
    # instance-name guard's own check target, not a leak of it -- a marker
    # that forbids a token must contain that token to function. A whole-repo
    # sweep for this same token found the guard was real but scoped to only
    # this one file; do not "clean" these literals away, and do not let a
    # future sweep do it either. Widening this guard to cover shipping
    # surfaces generally (not just this runbook) is tracked separately.
    content = _HYDRATION_RUNBOOK.read_text(encoding="utf-8")
    for marker in ("Dax", "dax", "claude-dax"):
        _check(
            f"hydration runbook: no concrete Dax example marker {marker!r}",
            marker not in content,
            f"found {marker!r}",
        )
    _check(
        "hydration runbook: declares no-MCP primary path",
        "## Connectivity stance — no-MCP primary path" in content,
        "missing no-MCP primary section",
    )
    _check(
        "hydration runbook: MCP is not required for session history",
        "Nothing requires MCP: process calls, knowledge-base search,\nsession-history ingestion, role claiming, and message receive all run through\nthe `<name>` command" in content,
        "missing MCP avoidance guidance for history setup",
    )
    _check(
        "hydration runbook: existing CLAUDE.md gets managed block merge guidance",
        "insert or update only that managed block" in content
        and "<!-- BEGIN SOLET HYDRATION -->" in content
        and "<!-- END SOLET HYDRATION -->" in content,
        "missing managed-block guidance for existing CLAUDE.md",
    )
    _check(
        "hydration runbook: managed block carries implementation KB-first and router guidance",
        "implementation/debugging requirement to search the solet's own knowledge base first" in content
        and "router-vs-bridge distinction" in content,
        "missing implementation/debugging or router-vs-bridge managed-block guidance",
    )
    _check(
        "hydration runbook: coordination-hooks install uses the explicit CLI commands, not a hand JSON merge",
        "claude plugin marketplace add <clone>" in content
        and "claude plugin install coordination-hooks@<marketplace-name>" in content
        and "never a\nhand-merged declaration alone" in content,
        "missing explicit-CLI-install guidance for coordination-hooks",
    )
    _check(
        "hydration runbook: settings merge markers are documented",
        "SOLET_STEP_ZERO_HOOK=<name>" in content and "SOLET_ROLE_RECLAIM_HOOK=<name>" in content,
        "missing hook merge marker guidance",
    )


def _check_seed_facing_kb_no_legacy_origin_markers() -> None:
    hits: list[str] = []
    # The two literal markers below are fragmented (not written whole): they
    # are the legacy-shape strings the seal validator scans shipped bytes
    # for, so the test must assemble each one rather than appear as a
    # literal or the validator would refuse this file.
    for path, surface in _iter_seed_facing_kb_files():
        rel = path.relative_to(_REPO_ROOT)
        content = path.read_text(encoding="utf-8")
        if "mcp__" + "a" + "d" + "a" + "__" in content:
            hits.append(f"{rel}: mcp__" + "a" + "d" + "a" + "__")
        if "notifications/" + "a" + "d" + "a" + "/" in content:
            hits.append(f"{rel}: notifications/" + "a" + "d" + "a" + "/")
        for token in sorted(set(_LEGACY_ORIGIN_ENV_RE.findall(content))):
            hits.append(f"{rel}: {token}")
        if surface == "plugin":
            for token in sorted(set(_PLUGIN_CORE_PRODUCT_ENV_RE.findall(content))):
                hits.append(f"{rel}: {token}")
    _check(
        "seed-facing KB has no legacy origin-identity markers or plugin-level Ananta env markers",
        not hits,
        "found " + "; ".join(hits[:20]),
    )


def main() -> int:
    try:
        _check_hydration_runbook_seed_neutral_and_no_mcp_primary()
        _check_seed_facing_kb_no_legacy_origin_markers()
        found = _discover()
        _check_expected_plugins_found(found)
        for path in found:
            content = path.read_text(encoding="utf-8")
            _check_headers(path, content)
            _check_sections(path, content)
            _check_session_ledger_guidance(path, content)
        _check_embedding_descriptions_distinct(found)
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"hydration_guidance_convention_smoke OK: {len(_CHECKS_RUN)} checks passed ({len(found)} file(s) found)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
