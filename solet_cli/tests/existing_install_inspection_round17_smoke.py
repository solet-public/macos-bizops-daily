"""Round-17 control-flow regression coverage for call-boundary preservation."""

from __future__ import annotations

import ast
from pathlib import Path

from existing_install_inspection_call_boundary import called_symbols

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "solet_cli" / "tests" / "fixtures" / "existing_install_inspection"


def main() -> int:
    expectations = {
        "fixed_point_for_fixture.py": ({"<dynamic>", "safe_adapter.harmless_method"}, set()),
        "fixed_point_while_fixture.py": ({"<dynamic>", "safe_adapter.harmless_method"}, set()),
        "fixed_point_nested_loop_fixture.py": (
            {"<dynamic>", "safe_adapter.harmless_method"},
            set(),
        ),
        "while_condition_recollection_fixture.py": (
            {"<dynamic>", "safe_adapter.harmless_method"},
            set(),
        ),
        "try_raise_dead_code_fixture.py": ({"<dynamic>", "safe_adapter.harmless_method"}, set()),
        "try_handler_prefix_fixture.py": ({"<dynamic>", "safe_adapter.harmless_method"}, set()),
        "nested_loop_orelse_break_fixture.py": ({"<dynamic>"}, {"safe_adapter.harmless_method"}),
        "dead_break_fixture.py": (
            {"target_adapter.target_adapter"},
            {"safe_adapter.harmless_method"},
        ),
        "long_relay_fixed_point_fixture.py": ({"<dynamic>", "safe_adapter.harmless_method"}, set()),
    }
    for name, (expected, forbidden) in expectations.items():
        called = called_symbols(ast.parse((_FIXTURES / name).read_text(encoding="utf-8")))
        assert expected <= called and not forbidden & called
    print("existing_install_inspection_round17_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
