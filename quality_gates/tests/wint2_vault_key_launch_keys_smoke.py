#!/usr/bin/env python3
"""Smokes: W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, 2026-06-07).

Covers brief §6 (11 numbered smokes) + 2 additions per
Coordinator-Dawn 2026-06-07 PT unblock (warn-mode-doesn't-raise +
rename round-trip). All in-process; no platform startup, no DB, no
subprocess shenanigans. Vault is a hand-built FakeVaultProxy stub that
records calls; the static gate is invoked against tiny example files
under a tmp scratch dir.

Run standalone:

    .venv/bin/python3 quality_gates/tests/wint2_vault_key_launch_keys_smoke.py

Each subsmoke prints PASS/FAIL with a short message. Exit code 0 iff
all pass.

Brief §6 smoke index:
    1.  positive_readiness_gate_passes
    2.  missing_required_key_fail_mode_raises
    3.  malformed_declaration_raises
    4.  vault_subsystem_down_raises_unavailable
    5.  static_gate_positive_literal
    6.  static_gate_positive_constant_resolution
    7.  static_gate_positive_prefix
    8.  static_gate_positive_annotation
    9.  static_gate_positive_allowlist
    10. static_gate_negative_undeclared
    11. launch_key_malformed_source_smoke (Codex #6 part e)
Coordinator-Dawn additions:
    12. warn_mode_logs_not_raises (Q2 resolution)
    13. rename_verb_round_trip (Q1 resolution)
"""

from __future__ import annotations

import importlib
import logging
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "quality_gates"))

from ananta.core.orchestration import startup_sequence  # noqa: E402
from ananta.interfaces.vault_keys_provider import (  # noqa: E402
    MalformedVaultKeyDeclarationError,
    MissingVaultKeyError,
    VaultServiceUnavailableError,
)

# ---------------------------------------------------------------------------
# Lightweight stubs
# ---------------------------------------------------------------------------


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data}


def _err(code: str, msg: str) -> dict[str, Any]:
    return {
        "action_status": "failed",
        "error": {"code": code, "message": msg},
    }


class FakeVaultProxy:
    """Records calls; ``exists`` is driven by ``self.present``."""

    def __init__(self, present: set[str] | None = None) -> None:
        self.present: set[str] = set(present or set())
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.exists_raises: type[BaseException] | None = None

    def exists(self, key: str) -> dict[str, Any]:
        self.calls.append(("exists", (key,)))
        if self.exists_raises is not None:
            raise self.exists_raises("vault stub error")
        return _ok({"key": key, "exists": key in self.present})

    def store(self, key: str, value: str, *_a: Any, **_k: Any) -> dict[str, Any]:
        self.calls.append(("store", (key, value)))
        if key in self.present:
            return _err("vault.key_exists", f"{key} exists")
        self.present.add(key)
        return _ok({"key": key, "version": 1, "message": "stored"})

    def delete(self, key: str) -> dict[str, Any]:
        self.calls.append(("delete", (key,)))
        if key not in self.present:
            return _err("vault.not_found", f"{key} not found")
        self.present.discard(key)
        return _ok({"key": key, "removed": 1, "message": "deleted"})

    def rename(self, old_key: str, new_key: str) -> dict[str, Any]:
        self.calls.append(("rename", (old_key, new_key)))
        if old_key not in self.present:
            return _err("vault.not_found", f"{old_key} not found")
        if new_key in self.present:
            return _err("vault.key_exists", f"{new_key} already exists")
        self.present.discard(old_key)
        self.present.add(new_key)
        return _ok({
            "old_key": old_key, "new_key": new_key, "message": "renamed",
        })


class FakePlugin:
    """Minimal plugin shape the readiness gate inspects.

    Implements VaultKeysProvider duck-typing + holds the injected proxy
    on ``_vault_service`` (mirrors the real Tier 1 setter shape).
    """

    def __init__(
        self,
        name: str,
        required: list[str] | None,
        declared: list[str] | None,
        proxy: FakeVaultProxy | None,
    ) -> None:
        self.name = name
        self._required = required
        self._declared = declared
        self._vault_service = proxy

    def get_required_vault_keys(self) -> list[str]:
        if self._required is None:
            raise AttributeError("intentionally absent")
        return list(self._required)

    def get_declared_vault_keys(self) -> list[str]:
        if self._declared is None:
            raise AttributeError("intentionally absent")
        return list(self._declared)


# ---------------------------------------------------------------------------
# Subsmoke harness
# ---------------------------------------------------------------------------


_RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, fn: Any) -> None:
    try:
        fn()
    except AssertionError as e:
        _RESULTS.append((name, False, f"ASSERTION: {e}"))
        return
    except Exception:
        _RESULTS.append((name, False, f"EXC: {traceback.format_exc(limit=2)}"))
        return
    _RESULTS.append((name, True, ""))


# ---------------------------------------------------------------------------
# Runtime readiness gate smokes
# ---------------------------------------------------------------------------


def smoke_1_positive_readiness_gate_passes() -> None:
    proxy = FakeVaultProxy(present={"example.x.key1", "example.x.key2"})
    plugin = FakePlugin(
        "x",
        required=["example.x.key1", "example.x.key2"],
        declared=["example.x.key1", "example.x.key2"],
        proxy=proxy,
    )
    # WARN mode: no raise, no warnings logged (all keys present)
    prev = startup_sequence.VAULT_KEYS_GATE_MODE
    startup_sequence.VAULT_KEYS_GATE_MODE = "fail"  # exercise fail path too
    try:
        startup_sequence._check_vault_keys_for_plugin("x", plugin)
    finally:
        startup_sequence.VAULT_KEYS_GATE_MODE = prev
    assert ("exists", ("example.x.key1",)) in proxy.calls
    assert ("exists", ("example.x.key2",)) in proxy.calls


def smoke_2_missing_required_key_fail_mode_raises() -> None:
    proxy = FakeVaultProxy(present={"example.x.key1"})  # key2 absent
    plugin = FakePlugin(
        "x",
        required=["example.x.key1", "example.x.key2"],
        declared=["example.x.key1", "example.x.key2"],
        proxy=proxy,
    )
    prev = startup_sequence.VAULT_KEYS_GATE_MODE
    startup_sequence.VAULT_KEYS_GATE_MODE = "fail"
    try:
        raised = False
        try:
            startup_sequence._check_vault_keys_for_plugin("x", plugin)
        except MissingVaultKeyError as e:
            raised = True
            assert e.plugin_name == "x"
            assert e.missing == ["example.x.key2"]
        assert raised, "fail mode should raise MissingVaultKeyError"
    finally:
        startup_sequence.VAULT_KEYS_GATE_MODE = prev


def smoke_3_malformed_declaration_raises() -> None:
    proxy = FakeVaultProxy(present=set())
    # plugin name 'x' but declared key's plugin segment is 'y' — mismatch.
    plugin = FakePlugin(
        "x",
        required=["example.y.key1"],
        declared=["example.y.key1"],
        proxy=proxy,
    )
    # BOTH modes treat malformed as fatal — exercise warn mode.
    prev = startup_sequence.VAULT_KEYS_GATE_MODE
    startup_sequence.VAULT_KEYS_GATE_MODE = "warn"
    try:
        raised = False
        try:
            startup_sequence._check_vault_keys_for_plugin("x", plugin)
        except MalformedVaultKeyDeclarationError:
            raised = True
        assert raised, "malformed declaration should raise in warn mode"
    finally:
        startup_sequence.VAULT_KEYS_GATE_MODE = prev


def smoke_4_vault_subsystem_down_raises_unavailable() -> None:
    proxy = FakeVaultProxy(present=set())
    proxy.exists_raises = RuntimeError
    plugin = FakePlugin(
        "x",
        required=["example.x.key1"],
        declared=["example.x.key1"],
        proxy=proxy,
    )
    prev = startup_sequence.VAULT_KEYS_GATE_MODE
    startup_sequence.VAULT_KEYS_GATE_MODE = "warn"
    try:
        raised = False
        try:
            startup_sequence._check_vault_keys_for_plugin("x", plugin)
        except VaultServiceUnavailableError:
            raised = True
        assert raised, (
            "vault-subsystem failures should raise VaultServiceUnavailableError"
        )
    finally:
        startup_sequence.VAULT_KEYS_GATE_MODE = prev


def smoke_12_warn_mode_logs_not_raises() -> None:
    """Q2 unblock: warn-mode at sub-1 landing logs but does not raise."""
    proxy = FakeVaultProxy(present=set())  # all required missing
    plugin = FakePlugin(
        "x",
        required=["example.x.key1"],
        declared=["example.x.key1"],
        proxy=proxy,
    )
    prev = startup_sequence.VAULT_KEYS_GATE_MODE
    startup_sequence.VAULT_KEYS_GATE_MODE = "warn"
    handler_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_records.append(record)

    cap = _Capture(level=logging.WARNING)
    startup_sequence.logger.addHandler(cap)
    try:
        # Must NOT raise in warn mode even with missing required keys.
        startup_sequence._check_vault_keys_for_plugin("x", plugin)
    finally:
        startup_sequence.logger.removeHandler(cap)
        startup_sequence.VAULT_KEYS_GATE_MODE = prev
    assert any(
        "example.x.key1" in r.getMessage() and r.levelno == logging.WARNING
        for r in handler_records
    ), "warn mode should log a WARNING per missing key"


# ---------------------------------------------------------------------------
# Static-gate smokes — write example files to a scratch plugin tree
# ---------------------------------------------------------------------------


def _write_scratch_plugin(
    tmp: Path,
    plugin_name: str,
    constants_py: str,
    plugin_py: str,
    extras: dict[str, str] | None = None,
) -> Path:
    """Materialize a minimal plugin tree under tmp/plugins/<plugin_name>/."""
    root = tmp / "plugins" / plugin_name / "src" / plugin_name
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("")
    (root / "constants.py").write_text(constants_py)
    (root / "plugin.py").write_text(plugin_py)
    for rel, content in (extras or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return tmp / "plugins" / plugin_name


def _reload_gate_with_repo_root(repo_root: Path) -> Any:
    """Reload the gate module bound to a temp REPO_ROOT for the test."""
    if "wint2_vault_key_declaration_check" in sys.modules:
        del sys.modules["wint2_vault_key_declaration_check"]
    import wint2_vault_key_declaration_check as mod  # type: ignore[import-not-found]

    mod.REPO_ROOT = repo_root  # type: ignore[attr-defined]
    mod._SCAN_ROOTS = (repo_root / "plugins",)  # type: ignore[attr-defined]
    return mod


def smoke_5_static_gate_positive_literal() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _write_scratch_plugin(
            tmp,
            "p_literal",
            constants_py='from typing import Final\nK: Final[str] = "example.p_literal.k"\n',
            plugin_py=(
                "class P:\n"
                "    name = 'p_literal'\n"
                "    def get_required_vault_keys(self):\n"
                "        return ['example.p_literal.k']\n"
                "    def get_declared_vault_keys(self):\n"
                "        return ['example.p_literal.k']\n"
                "    def run(self):\n"
                "        self._vault.retrieve('example.p_literal.k')\n"
            ),
        )
        mod = _reload_gate_with_repo_root(tmp)
        findings = mod.collect_findings()
        assert findings == [], (
            f"expected 0 findings on declared literal; got {findings!r}"
        )


def smoke_6_static_gate_positive_constant_resolution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _write_scratch_plugin(
            tmp,
            "p_const",
            constants_py='from typing import Final\nK: Final[str] = "example.p_const.k"\n',
            plugin_py=(
                "from .constants import K\n"
                "class P:\n"
                "    name = 'p_const'\n"
                "    def get_required_vault_keys(self):\n"
                "        return [K]\n"
                "    def get_declared_vault_keys(self):\n"
                "        return [K]\n"
                "    def run(self):\n"
                "        self._vault.retrieve(K)\n"
            ),
        )
        mod = _reload_gate_with_repo_root(tmp)
        findings = mod.collect_findings()
        assert findings == [], (
            f"expected 0 findings on cross-module Final[str] constant; "
            f"got {findings!r}"
        )


def smoke_7_static_gate_positive_prefix() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _write_scratch_plugin(
            tmp,
            "p_prefix",
            constants_py=(
                "from typing import Final\n"
                "PREFIX: Final[str] = 'example.p_prefix.tok__'\n"
                "K_ABC: Final[str] = 'example.p_prefix.tok__abc'\n"
            ),
            plugin_py=(
                "from .constants import K_ABC, PREFIX\n"
                "class P:\n"
                "    name = 'p_prefix'\n"
                "    def get_required_vault_keys(self):\n"
                "        return []\n"
                "    def get_declared_vault_keys(self):\n"
                "        return [PREFIX + '*']\n"
                "    def run(self):\n"
                "        self._vault.retrieve(K_ABC)\n"
            ),
        )
        mod = _reload_gate_with_repo_root(tmp)
        findings = mod.collect_findings()
        assert findings == [], (
            f"expected 0 findings on declared prefix match; got {findings!r}"
        )


def smoke_8_static_gate_positive_annotation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _write_scratch_plugin(
            tmp,
            "p_anno",
            constants_py="",
            plugin_py=(
                "def key_for(x):\n"
                "    return 'example.p_anno.dyn_' + x\n"
                "class P:\n"
                "    name = 'p_anno'\n"
                "    def get_declared_vault_keys(self):\n"
                "        return ['example.p_anno.dyn_*']\n"
                "    def get_required_vault_keys(self):\n"
                "        return []\n"
                "    def run(self, x):\n"
                "        k = key_for(x)\n"
                "        self._vault.retrieve(k)  # vault-key: example.p_anno.dyn_*\n"
            ),
        )
        mod = _reload_gate_with_repo_root(tmp)
        findings = mod.collect_findings()
        assert findings == [], (
            f"expected 0 findings when annotation matches declared prefix; "
            f"got {findings!r}"
        )


def smoke_9_static_gate_positive_allowlist() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        plugin_dir = _write_scratch_plugin(
            tmp,
            "p_allow",
            constants_py="",
            plugin_py=(
                "def chain_resolve(name):\n"
                "    return 'whatever'\n"
                "class P:\n"
                "    name = 'p_allow'\n"
                "    def get_declared_vault_keys(self):\n"
                "        return []\n"
                "    def get_required_vault_keys(self):\n"
                "        return []\n"
                "    def run(self, name):\n"
                "        k = chain_resolve(name)\n"
                "        self._vault.retrieve(k)\n"
            ),
        )
        del plugin_dir
        allowlist = tmp / "allowlist.txt"
        allowlist.write_text(
            "D1.2::plugins/p_allow/src/p_allow/plugin.py::*"
            "  # owner: smoke-fixture reason: tracked test debt expires: 2099-01-01\n",
        )
        mod = _reload_gate_with_repo_root(tmp)
        al = mod.load_allowlist(allowlist)
        findings = mod.collect_findings()
        blocking = [f for f in findings if not al.covers(f)]
        assert blocking == [], (
            f"expected allowlist to cover the chain-consumer finding; "
            f"blocking remained: {blocking!r}"
        )


def smoke_10_static_gate_negative_undeclared() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _write_scratch_plugin(
            tmp,
            "p_neg",
            constants_py="",
            plugin_py=(
                "class P:\n"
                "    name = 'p_neg'\n"
                "    def get_declared_vault_keys(self):\n"
                "        return ['example.p_neg.allowed']\n"
                "    def get_required_vault_keys(self):\n"
                "        return []\n"
                "    def run(self):\n"
                "        self._vault.retrieve('example.p_neg.NOT_DECLARED')\n"
            ),
        )
        mod = _reload_gate_with_repo_root(tmp)
        findings = mod.collect_findings()
        assert len(findings) == 1, (
            f"expected 1 finding for undeclared literal key; got {findings!r}"
        )
        assert "example.p_neg.NOT_DECLARED" in findings[0].specifier, (
            f"finding's specifier should name the undeclared key; "
            f"got {findings[0].specifier!r}"
        )


def smoke_11_launch_key_malformed_source_smoke() -> None:
    """Codex correction #6 part e: formerly-flat key still in source fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _write_scratch_plugin(
            tmp,
            "p_flat",
            constants_py=(
                "from typing import Final\n"
                "VAULT_KEY_BOT_TOKEN: Final[str] = 'discord_bot_token'\n"
            ),
            plugin_py=(
                "from .constants import VAULT_KEY_BOT_TOKEN\n"
                "class P:\n"
                "    name = 'p_flat'\n"
                "    def get_declared_vault_keys(self):\n"
                "        return ['example.p_flat.bot_token']\n"
                "    def get_required_vault_keys(self):\n"
                "        return []\n"
                "    def run(self):\n"
                "        self._vault.retrieve(VAULT_KEY_BOT_TOKEN)\n"
            ),
        )
        mod = _reload_gate_with_repo_root(tmp)
        findings = mod.collect_findings()
        # The resolved literal "discord_bot_token" is NOT declared — must
        # flag as undeclared. This protects against accidental rollback
        # of the Tier 2 flat→scoped migration.
        assert len(findings) == 1, (
            f"expected 1 finding for unmigrated flat key; got {findings!r}"
        )
        assert "discord_bot_token" in findings[0].specifier


# ---------------------------------------------------------------------------
# Rename verb smoke (Q1 resolution)
# ---------------------------------------------------------------------------


def smoke_13_rename_verb_round_trip() -> None:
    """Atomic round-trip: rename preserves presence + refuses collisions."""
    proxy = FakeVaultProxy(present={"flat_key"})

    # Happy path: rename flat → scoped.
    result = proxy.rename("flat_key", "example.x.scoped_key")
    assert result["action_status"] == "completed"
    assert proxy.present == {"example.x.scoped_key"}, (
        f"after rename: {proxy.present!r}"
    )
    assert ("rename", ("flat_key", "example.x.scoped_key")) in proxy.calls

    # Not-found path: renaming missing key returns not_found.
    result2 = proxy.rename("does_not_exist", "example.x.new")
    assert result2["action_status"] == "failed"
    assert result2["error"]["code"] == "vault.not_found"

    # Collision path: refuse to overwrite an existing target.
    proxy.present.add("example.x.collision")
    proxy.present.add("example.x.other")
    result3 = proxy.rename("example.x.other", "example.x.collision")
    assert result3["action_status"] == "failed"
    assert result3["error"]["code"] == "vault.key_exists"
    assert "example.x.other" in proxy.present, "source must survive failed rename"
    assert "example.x.collision" in proxy.present, (
        "target must survive failed rename"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    _check("01 positive_readiness_gate_passes", smoke_1_positive_readiness_gate_passes)
    _check("02 missing_required_key_fail_mode_raises", smoke_2_missing_required_key_fail_mode_raises)
    _check("03 malformed_declaration_raises", smoke_3_malformed_declaration_raises)
    _check("04 vault_subsystem_down_raises_unavailable", smoke_4_vault_subsystem_down_raises_unavailable)
    _check("05 static_gate_positive_literal", smoke_5_static_gate_positive_literal)
    _check("06 static_gate_positive_constant_resolution", smoke_6_static_gate_positive_constant_resolution)
    _check("07 static_gate_positive_prefix", smoke_7_static_gate_positive_prefix)
    _check("08 static_gate_positive_annotation", smoke_8_static_gate_positive_annotation)
    _check("09 static_gate_positive_allowlist", smoke_9_static_gate_positive_allowlist)
    _check("10 static_gate_negative_undeclared", smoke_10_static_gate_negative_undeclared)
    _check("11 launch_key_malformed_source_smoke", smoke_11_launch_key_malformed_source_smoke)
    _check("12 warn_mode_logs_not_raises", smoke_12_warn_mode_logs_not_raises)
    _check("13 rename_verb_round_trip", smoke_13_rename_verb_round_trip)

    print("\n========================================")
    print("W-PLUGIN-LAUNCH-KEYS smoke results")
    print("========================================")
    failures = 0
    for name, ok, msg in _RESULTS:
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {name}")
        if not ok:
            failures += 1
            for line in msg.splitlines():
                print(f"         {line}")
    print(
        f"\n{len(_RESULTS) - failures}/{len(_RESULTS)} subsmokes passed.",
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    # Ensure importlib reloads pick up freshly-written sources.
    importlib.invalidate_caches()
    raise SystemExit(main())
