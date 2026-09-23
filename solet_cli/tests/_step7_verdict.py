"""The verdict rows every Step-7 scenario shares (design section 5.7), as one context manager.

After a scenario's last command: ``db_spy`` clean; ``OpaqueStores`` digests
unchanged; ``<home>/.claude/settings.json`` unchanged; ``profile/config/**`` and
``profile/data/**`` unchanged except the log file and the paths the scenario
declares; the observed git argv set inside Step 6's read-only vocabulary plus
``fetch``/``merge --ff-only``; and the tree snapshot outside ``profile/data``
differs from the pre-run snapshot only on the declared paths -- the test-side
twin of section 6.6's Manager-side rule.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import db_spy  # noqa: E402
from _step6_support import GitArgvObserver, observe_git  # noqa: E402
from _step7_support import RealStyleFixture, tree_snapshot  # noqa: E402

__all__ = ["Verdict", "verdict"]


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
        if self.git is not None:
            self.git.assert_forward_only()


@contextmanager
def verdict(fixture: RealStyleFixture, *, transition: tuple[str, ...] = (), prefixes: tuple[str, ...] = ()) -> Iterator[Verdict]:
    """Wrap a scenario: ``db_spy`` poisons every database import, git argv is observed, and the shared rows assert on exit."""
    stores = fixture.stores
    assert stores is not None
    row = Verdict(fixture, set(transition), prefixes, None, tree_snapshot(fixture.target), (fixture.home / ".claude" / "settings.json").read_bytes(), stores.digests())
    with db_spy(), observe_git() as observer:
        row.git = observer
        yield row
    row.assert_rows()
