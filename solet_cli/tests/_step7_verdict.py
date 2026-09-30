"""The verdict rows every Step-7 scenario shares (design section 5.7), as one context manager.

After a scenario's last command: ``db_spy`` clean; ``OpaqueStores`` digests
unchanged; ``<home>/.claude/settings.json`` unchanged; ``profile/config/**`` and
``profile/data/**`` unchanged except the log file and the paths the scenario
declares; the observed git argv set inside Step 6's read-only vocabulary plus
``fetch``/``merge --ff-only``; and the tree snapshot outside ``profile/data``
differs from the pre-run snapshot only on the declared paths -- the test-side
twin of section 6.6's Manager-side rule.

Every scenario declares the Claude coordination-hook manifest (iss_fa27466f): the
fixtures ship bare ``python3`` hooks, as the real population does, and the
runtime's plugin-cache refresh pins them.  A change there must be exactly the
installer pin of the ``HEAD`` blob to ``<target>/.venv/bin/python3``.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import db_spy, git  # noqa: E402
from _step6_support import GitArgvObserver, observe_git  # noqa: E402
from _step7_support import RealStyleFixture, tree_snapshot  # noqa: E402
from solet_setup_contracts.hook_interpreter_pin import CLAUDE_HOOK_MANIFEST, instance_interpreter, pin_hook_interpreter  # noqa: E402

__all__ = ["Verdict", "verdict", "without_installer_pin_entry", "without_installer_pin_revision"]


@dataclass
class Verdict:
    """What the scenario may change; everything else must be byte-identical afterwards."""

    fixture: RealStyleFixture
    allowed: set[str] = field(default_factory=lambda: set())
    allowed_prefixes: tuple[str, ...] = ()
    git: GitArgvObserver | None = None
    before_target: dict[str, str] = field(default_factory=lambda: {})
    before_settings: bytes = b""
    before_stores: dict[str, str] = field(default_factory=lambda: {})

    def allow(self, *paths: str) -> None:
        self.allowed.update(paths)

    def allow_prefix(self, *prefixes: str) -> None:
        self.allowed_prefixes = (*self.allowed_prefixes, *prefixes)

    def changed_paths(self) -> dict[str, tuple[str | None, str | None]]:
        after = tree_snapshot(self.fixture.target)
        keys = set(self.before_target) | set(after)
        return {key: (self.before_target.get(key), after.get(key)) for key in sorted(keys) if self.before_target.get(key) != after.get(key)}

    def assert_rows(self) -> None:
        fixture = self.fixture
        stores = fixture.stores
        assert stores is not None and stores.digests() == self.before_stores, "opaque store bytes moved"
        assert (fixture.home / ".claude" / "settings.json").read_bytes() == self.before_settings, "operator settings.json moved"
        changed = self.changed_paths()
        undeclared = {path: delta for path, delta in changed.items() if path not in self.allowed and not path.startswith(self.allowed_prefixes)}
        assert not undeclared, f"tree changed outside the declared paths: {undeclared}"
        if CLAUDE_HOOK_MANIFEST in changed:
            assert changed[CLAUDE_HOOK_MANIFEST][1] == _installer_pin_digest(fixture.target), f"the hook manifest changed to anything but the installer pin: {changed[CLAUDE_HOOK_MANIFEST]}"
        if self.git is not None:
            self.git.assert_forward_only()


@contextmanager
def verdict(fixture: RealStyleFixture, *, transition: tuple[str, ...] = (), prefixes: tuple[str, ...] = ()) -> Iterator[Verdict]:
    """Wrap a scenario: ``db_spy`` poisons every database import, git argv is observed, and the shared rows assert on exit."""
    stores = fixture.stores
    assert stores is not None
    row = Verdict(fixture, {*transition, CLAUDE_HOOK_MANIFEST}, prefixes, None, tree_snapshot(fixture.target), (fixture.home / ".claude" / "settings.json").read_bytes(), stores.digests())
    with db_spy(), observe_git() as observer:
        row.git = observer
        yield row
    row.assert_rows()


def without_installer_pin_revision(fixture: RealStyleFixture, revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The journal's revisions less exactly one: ``plugin_cache_refresh`` pinning the bare hook manifest to the exact pin."""
    pins = [row for row in revisions if row.get("operation_id") == "plugin_cache_refresh"]
    expected = {"operation_id": "plugin_cache_refresh", "paths": [CLAUDE_HOOK_MANIFEST], "before": {CLAUDE_HOOK_MANIFEST: None}, "after": {CLAUDE_HOOK_MANIFEST: _installer_pin_digest(fixture.target)}}
    assert len(pins) == 1 and {key: value for key, value in pins[0].items() if key != "at"} == expected, f"one revision pins the hook manifest to the installer pin: {pins}"
    return [row for row in revisions if row is not pins[0]]


def without_installer_pin_entry(fixture: RealStyleFixture, snapshot: dict[str, Any]) -> dict[str, Any]:
    """A local-state snapshot less exactly one preserved tracked entry: the hook manifest at the installer pin's bytes."""
    pinned = _installer_pin_bytes(fixture.target)
    entries = snapshot["preserved_tracked_paths"]
    pins = [item for item in entries if item["path"] == CLAUDE_HOOK_MANIFEST]
    expected = {"path": CLAUDE_HOOK_MANIFEST, "sha256": None if pinned is None else hashlib.sha256(pinned).hexdigest(), "size": None if pinned is None else len(pinned)}
    assert pins == [expected], f"the snapshot holds the hook manifest at the installer pin: {pins}"
    return {**snapshot, "preserved_tracked_paths": [item for item in entries if item["path"] != CLAUDE_HOOK_MANIFEST]}


def _installer_pin_bytes(target: Path) -> bytes | None:
    """``pin_hook_interpreter`` applied to the ``HEAD`` blob, bound to the target's own venv interpreter."""
    return pin_hook_interpreter(git(target, "show", f"HEAD:{CLAUDE_HOOK_MANIFEST}").encode("utf-8"), instance_interpreter(target))


def _installer_pin_digest(target: Path) -> str | None:
    pinned = _installer_pin_bytes(target)
    return None if pinned is None else hashlib.sha256(pinned).hexdigest()
