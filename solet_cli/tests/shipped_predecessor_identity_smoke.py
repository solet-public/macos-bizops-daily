"""Shipped predecessor/flow identity fields never carry a placeholder value (iss_3272e5f6).

Scans every shipped contract and flow document under ``plugins/*/knowledge_base/``
and ``solet_cli/src/solet_manager/released_metadata/`` for the placeholder
signatures a hand-typed stub tends to leave behind: a repeated-hex-character
run, the RFC/example ``123e4567-e89b-12d3-a456-426614174xxx`` UUID family, an
all-zero or all-``f`` hex run, and a bare ``TODO``/``FIXME``/``XXX``/
``CHANGEME``/``PLACEHOLDER``/``EXAMPLE`` token used as a value. Matching is on
whole leaf string values only (never substrings of natural-language prose,
which floods false positives across the many process-description KB
articles this walk otherwise reaches).

An exact, narrow allowlist below covers the one known, evidence-verified
exception: ``existing_install_flow.json`` and its ``released_metadata`` mirror
both carry a ``legacy_anchor_id: "stable-pre-manager-seed-v1"`` row whose
identity fields are the deterministic output of
``existing_install_inspection_contract_smoke.py::_create_released_anchor_target``'s
own synthetic ``PROVENANCE.json`` fixture, not a real
``solet-public/macos-bizops`` commit (iss_3272e5f6, verified 2026-09-28: the
commit and tree are absent from every branch, tag and ref of that repository,
and independently re-deriving the fixture's git object hashes reproduces the
shipped values exactly). Any other hit is a real defect.

A self-test (``_check_detects_unallowlisted_injection``) proves the scanner
is live: it copies one shipped file into a temp directory, injects a fresh,
unallowlisted placeholder UUID, and asserts the scan flags exactly that copy
while leaving the real shipped tree byte-identical and unaffected.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]

_UUID_EXAMPLE_FAMILY = re.compile(r"^123e4567-e89b-12d3-a456-426614174\w{3}$")
_TODO_TOKEN = re.compile(r"^(TODO|FIXME|XXX|CHANGEME|PLACEHOLDER|EXAMPLE)([-_ ].{0,24})?$", re.IGNORECASE)
_REPEATED_HEX = re.compile(r"^[0-9a-fA-F]{16,}$")
_ZERO_OR_FF_RUN = re.compile(r"^0{16,}$|^[fF]{16,}$")

#: (file path relative to repo root, leaf value) pairs verified as an inert,
#: documented non-defect. Never add an entry here without the same evidence
#: standard as iss_3272e5f6's: an independent re-derivation showing the value
#: is a deliberate, load-bearing constant, not an unfinished stub.
_ALLOWED_HITS: frozenset[tuple[str, str]] = frozenset(
    {
        ("plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json", "b" * 64),
        ("plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json", "123e4567-e89b-12d3-a456-426614174001"),
        ("solet_cli/src/solet_manager/released_metadata/existing_install_inspection_anchors.v1.json", "b" * 64),
        ("solet_cli/src/solet_manager/released_metadata/existing_install_inspection_anchors.v1.json", "123e4567-e89b-12d3-a456-426614174001"),
    }
)

_checks = 0


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _shipped_document_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for pattern in ("plugins/*/knowledge_base/**/*.json", "plugins/*/knowledge_base/**/*.yaml", "plugins/*/knowledge_base/**/*.yml"):
        paths.extend(root.glob(pattern))
    paths.extend((root / "solet_cli" / "src" / "solet_manager" / "released_metadata").glob("*.json"))
    return tuple(sorted(paths))


def _leaf_strings(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaf_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaf_strings(item)
    elif isinstance(value, str):
        yield value


def _load(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _placeholder_hits(document: object) -> tuple[str, ...]:
    hits: list[str] = []
    for leaf in _leaf_strings(document):
        value = leaf.strip()
        if not value:
            continue
        if (
            _UUID_EXAMPLE_FAMILY.fullmatch(value)
            or (_REPEATED_HEX.fullmatch(value) and len(set(value.lower())) == 1)
            or _ZERO_OR_FF_RUN.fullmatch(value)
            or _TODO_TOKEN.fullmatch(value)
        ):
            hits.append(value)
    return tuple(hits)


def _scan(root: Path) -> dict[str, tuple[str, ...]]:
    findings: dict[str, tuple[str, ...]] = {}
    for path in _shipped_document_paths(root):
        try:
            document = _load(path)
        except (json.JSONDecodeError, yaml.YAMLError, UnicodeDecodeError):
            raise AssertionError(f"shipped document is unreadable: {path}") from None
        hits = _placeholder_hits(document)
        if hits:
            findings[str(path.relative_to(root))] = hits
    return findings


def _check_shipped_tree_is_clean_or_allowlisted() -> None:
    findings = _scan(_ROOT)
    unallowed: list[str] = []
    seen_allowed: set[tuple[str, str]] = set()
    for relative_path, hits in findings.items():
        for hit in hits:
            key = (relative_path, hit)
            if key in _ALLOWED_HITS:
                seen_allowed.add(key)
            else:
                unallowed.append(f"{relative_path}: {hit!r}")
    _check(not unallowed, "no unallowlisted shipped placeholder value: " + "; ".join(sorted(unallowed)))
    _check(seen_allowed == _ALLOWED_HITS, f"every allowlist entry is still present and still needed: missing={_ALLOWED_HITS - seen_allowed}")


def _check_detects_unallowlisted_injection() -> None:
    target = "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json"
    source = _ROOT / target
    original_bytes = source.read_bytes()
    with TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        document: dict[str, Any] = json.loads(original_bytes.decode("utf-8"))
        document["supported_predecessors"][0]["provenance_sha256"] = "0" * 64
        destination = tmp_root / "plugins" / "github_midwife_plugin" / "knowledge_base"
        destination.mkdir(parents=True)
        (destination / "existing_install_flow.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        (tmp_root / "solet_cli" / "src" / "solet_manager" / "released_metadata").mkdir(parents=True)
        findings = _scan(tmp_root)
        relative = str((destination / "existing_install_flow.json").relative_to(tmp_root))
        _check(relative in findings and "0" * 64 in findings[relative], "an injected, unallowlisted zero-run is caught red")
    _check(source.read_bytes() == original_bytes, "the real shipped file was never touched by the injection test")


def main() -> int:
    _check_detects_unallowlisted_injection()
    _check_shipped_tree_is_clean_or_allowlisted()
    print(f"shipped_predecessor_identity_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
