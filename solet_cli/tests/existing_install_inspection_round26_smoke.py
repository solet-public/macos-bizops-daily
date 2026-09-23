"""Round-26 return, target-order, and TryStar regression fixtures."""

from __future__ import annotations

import ast
from pathlib import Path

from existing_install_inspection_call_boundary import called_symbols

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "existing_install_inspection"


def main() -> int:
    expectations: dict[str, set[str]] = {
        "with_suppressed_return_expression_fixture.py": {
            "contextlib.suppress",
            "forbidden_module.target_adapter",
        },
        "unpacking_assignment_order_fixture.py": {"<dynamic>"},
        "with_target_subexpression_fixture.py": {
            "contextlib.nullcontext",
            "forbidden_module.target_adapter",
        },
        "trystar_handler_sequence_fixture.py": {"<dynamic>"},
    }
    for name, expected in expectations.items():
        called = called_symbols(ast.parse((_FIXTURES / name).read_text(encoding="utf-8")))
        assert expected <= called, (name, called)
    print("existing_install_inspection_round26_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
