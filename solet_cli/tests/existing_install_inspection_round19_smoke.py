"""Round-19 preservation-regression fixtures."""

from __future__ import annotations

import ast
from pathlib import Path

from existing_install_inspection_call_boundary import called_symbols

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "existing_install_inspection"


def main() -> int:
    expectations = {
        "conditional_terminator_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "nested_try_prefix_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "finally_transfer_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "finally_return_rebind_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "comprehension_walrus_second_pass_fixture.py": (
            {"<dynamic>", "safe_adapter.harmless_method"},
            set(),
        ),
        "assignment_order_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "annotated_assignment_order_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "nested_dead_break_fixture.py": ({"safe_adapter.harmless_method"}, {"<dynamic>"}),
    }
    for name, (expected, forbidden) in expectations.items():
        called = called_symbols(ast.parse((_FIXTURES / name).read_text(encoding="utf-8")))
        assert expected <= called and not forbidden & called
    print("existing_install_inspection_round19_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
