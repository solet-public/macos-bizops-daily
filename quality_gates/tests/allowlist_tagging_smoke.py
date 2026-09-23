#!/usr/bin/env python3
"""Refuse untagged tracked-debt register entries.

Existing debt is deliberately grandfathered with a dated ``legacy:`` tag.
Every future entry must instead carry owner, reason, and expiry metadata.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEXT_REGISTERS = (
    "bundle_license_allowlist.txt",
    "cited_path_gate_allowlist.txt",
    "dependency_declaration_allowlist.txt",
    "embedding_description_bound_allowlist.txt",
    "flow_probe_registry_gate_allowlist.txt",
    "god_class_allowlist.txt",
    "radon_cc_allowlist.txt",
    "radon_mi_allowlist.txt",
    "return_shape_allowlist.txt",
    "service_interface_ast_allowlist.txt",
    "sql_access_allowlist.txt",
    "whole_tree_integration_gate_allowlist.txt",
    "wint2_driver_import_allowlist.txt",
    "wint2_vault_key_declaration_allowlist.txt",
)
JSON_REGISTER = "setup_flow_field_kind_allowlist.json"


def _has_tag(tag: str) -> bool:
    if "legacy:" in tag:
        return True
    return all(f"{field}:" in tag for field in ("owner", "reason", "expires"))


def _untagged_text_entries(path: Path) -> list[str]:
    missing: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        _, marker, tag = line.partition("#")
        if not marker or not _has_tag(tag):
            missing.append(f"{path.name}:{number}")
    return missing


def _untagged_json_entries(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return [f"{path.name}: expected list"]
    return [
        f"{path.name}:{index}"
        for index, entry in enumerate(payload, start=1)
        if not isinstance(entry, dict)
        or not (isinstance(entry.get("legacy"), str) or all(isinstance(entry.get(field), str) for field in ("owner", "reason", "expires")))
    ]


def _all_untagged(register_root: Path) -> list[str]:
    missing = [
        entry
        for name in TEXT_REGISTERS
        for entry in _untagged_text_entries(register_root / name)
    ]
    return missing + _untagged_json_entries(register_root / JSON_REGISTER)


def _assert_path_scoped_god_class_entry() -> None:
    source = "class ProcessRegistryManager:\n" + "\n".join(
        f"    def method_{index}(self) -> None:\n        pass" for index in range(16)
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        candidate = Path(temp_dir) / "new_collision.py"
        candidate.write_text(source + "\n", encoding="utf-8")
        result = subprocess.run(
            (
                str(REPO_ROOT / ".venv/bin/python3"),
                "quality_gates/god_class_check.py",
                str(candidate),
                "--allowlist",
                "quality_gates/god_class_allowlist.txt",
            ),
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert "ProcessRegistryManager" in result.stdout
        assert "[allowlisted]" not in result.stdout


def main() -> None:
    register_root = REPO_ROOT / "quality_gates"
    assert len(TEXT_REGISTERS) + 1 == 15
    assert _all_untagged(register_root) == []
    _assert_path_scoped_god_class_entry()
    with tempfile.TemporaryDirectory() as temp_dir:
        fixture_root = Path(temp_dir)
        for name in TEXT_REGISTERS:
            (fixture_root / name).write_text((register_root / name).read_text(encoding="utf-8"), encoding="utf-8")
        (fixture_root / JSON_REGISTER).write_text((register_root / JSON_REGISTER).read_text(encoding="utf-8"), encoding="utf-8")
        fixture = fixture_root / "flow_probe_registry_gate_allowlist.txt"
        fixture.write_text(fixture.read_text(encoding="utf-8") + "plugin::new::entry\n", encoding="utf-8")
        missing = _all_untagged(fixture_root)
        assert len(missing) == 1
        assert missing[0].startswith("flow_probe_registry_gate_allowlist.txt:")


if __name__ == "__main__":
    main()
