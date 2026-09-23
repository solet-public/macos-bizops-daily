"""Round-22 recursive-prefix and termination regression fixtures."""

from __future__ import annotations

import ast
from pathlib import Path

from existing_install_inspection_call_boundary import called_symbols

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "existing_install_inspection"


def main() -> int:
    expectations = {
        "recursive_nested_prefix_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "unhandled_try_prefix_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "guarded_wildcard_fixture.py": ({"forbidden_module.target_adapter"}, set()),
        "with_terminator_fixture.py": (
            {"contextlib.nullcontext"},
            set(),
        ),
    }
    for name, (expected, forbidden) in expectations.items():
        called = called_symbols(ast.parse((_FIXTURES / name).read_text(encoding="utf-8")))
        assert expected <= called and not forbidden & called
    print("existing_install_inspection_round22_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
