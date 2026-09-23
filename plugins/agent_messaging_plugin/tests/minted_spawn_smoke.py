#!/usr/bin/env python3
"""Ordering and refusal controls for the register-adopting spawn wrapper."""
# ruff: noqa: E402, I001
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(ROOT / "ananta" / "src"),
    str(ROOT / "plugins" / "agent_messaging_plugin" / "src"),
    str(Path(__file__).parent),
]

from ananta.core.orchestration.register_adoption import spawn_with_register_adoption  # noqa: E402
from managed_dispatch_smoke import _spec  # noqa: E402


def check(value: bool, label: str) -> None:
    if not value:
        raise AssertionError(label)
    print(f"PASS {label}")


def main() -> None:
    with __import__("tempfile").TemporaryDirectory() as directory:
        spec = _spec(Path(directory))
        events: list[str] = []
        class Client:
            def mint_unit(self, **_kwargs: object) -> None:
                events.append("row")

            def file_unminted_debt(self, **_kwargs: object) -> None:
                events.append("debt")

        client = Client()

        def dispatch(*_args: object) -> dict[str, object]:
            events.append("session")
            return {"attempt": {}}
        spawn_with_register_adoption(None, spec, object(), no_mint=False, register_client=client, dispatch=dispatch)
        check(events == ["row", "session"], "row is minted before session dispatch")
        events.clear()
        class FailingClient(Client):
            def mint_unit(self, **_kwargs: object) -> None:
                events.append("row")
                raise RuntimeError("down")

        try:
            spawn_with_register_adoption(
                None, spec, object(), no_mint=False, register_client=FailingClient(), dispatch=dispatch,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("mint failure must refuse")
        check(events == ["row"], "mint failure has no session side effect")
        events.clear()
        result = spawn_with_register_adoption(None, spec, object(), no_mint=True, register_client=client, dispatch=dispatch)
        check(events == ["debt", "session"], "no-mint files debt then dispatches")
        check(result["register_adoption"] == "bypassed", "no-mint result is loud")

if __name__ == "__main__":
    main()
