#!/usr/bin/env python3
"""Pin compatibility for a captured persisted fresh-shell probe contract."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_SOLET_CLI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SOLET_CLI_ROOT / "src"))

from solet_manager import contracts as contracts_module  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.errors import ContractError  # noqa: E402

_FIXTURE_ROOT = (
    Path(__file__).parent
    / "fixtures/contracts/reconcile_contract_legacy_ab2a9d6d"
)
_JOURNAL = _FIXTURE_ROOT / "journal.json"
_PERMISSIONS_MANIFEST = (
    _SOLET_CLI_ROOT
    / "../plugins/github_midwife_plugin/knowledge_base/permissions_manifest.json"
).resolve()
_DIGEST = "sha256:ab2a9d6df8098fee294c9886d9e3a39a9bb555b4d28efe6feaea731f7741105f"
_CHECKS = 0


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _expect_contract_error(callback: object, expected: str, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    try:
        callback()  # type: ignore[operator]
    except ContractError as exc:
        if expected not in str(exc):
            raise AssertionError(f"{label}: expected {expected!r}, got {exc!s}") from exc
    else:
        raise AssertionError(f"{label}: expected ContractError")


def _persisted_identity() -> tuple[str, str]:
    journal = json.loads(_JOURNAL.read_text(encoding="utf-8"))
    source_revision = journal["flow_source_revision"]
    digest = journal["flow_contract_digest"]
    if not isinstance(source_revision, str) or not isinstance(digest, str):
        raise AssertionError("captured journal identity must contain strings")
    return source_revision, digest


def _materialize_contract(destination: Path) -> None:
    destination.mkdir()
    for path in _FIXTURE_ROOT.glob("*.json"):
        if path.name != _JOURNAL.name:
            shutil.copy2(path, destination / path.name)
    shutil.copy2(_PERMISSIONS_MANIFEST, destination / _PERMISSIONS_MANIFEST.name)


def _load(directory: Path, *, resume_compatibility: bool) -> ContractBundle:
    source_revision, digest = _persisted_identity()
    _check(digest == _DIGEST, "captured journal retains its persisted ab2 contract digest")
    return ContractBundle.load(
        source_revision=source_revision,
        directory=directory,
        expected_digest=digest,
        resume_compatibility=resume_compatibility,
    )


def main() -> int:
    original_flow = (_FIXTURE_ROOT / "macos_setup_flow.json").read_bytes()
    with tempfile.TemporaryDirectory(prefix="legacy_resume_fresh_shell_") as raw:
        contracts = Path(raw) / "contracts"
        _materialize_contract(contracts)
        _expect_contract_error(
            lambda: _load(contracts, resume_compatibility=False),
            "fresh_shell_python_valid",
            "captured persisted contract rejects without resume compatibility",
        )
        bundle = _load(contracts, resume_compatibility=True)
        operations = bundle.flow["operations"]
        assert isinstance(operations, dict)
        install_python = operations["install_python_runtime"]
        assert isinstance(install_python, dict)
        idempotency = install_python["idempotency"]
        assert isinstance(idempotency, dict)
        _check(
            idempotency["postcondition_probe_refs"] == ["python_version_valid"],
            "compatibility normalization removes only the later fresh-shell postcondition",
        )
        _check(
            (_FIXTURE_ROOT / "macos_setup_flow.json").read_bytes() == original_flow,
            "compatibility normalization preserves captured contract bytes",
        )
        legacy_only = contracts_module._LEGACY_RESUME_CONTRACT_DIGESTS - {_DIGEST}
        with patch.object(contracts_module, "_LEGACY_RESUME_CONTRACT_DIGESTS", legacy_only):
            _expect_contract_error(
                lambda: _load(contracts, resume_compatibility=True),
                "fresh_shell_python_valid",
                "removing ab2 from the exact allowlist makes the persisted target RED",
            )
    print(f"legacy_resume_fresh_shell_probe_smoke: {_CHECKS}/{_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
