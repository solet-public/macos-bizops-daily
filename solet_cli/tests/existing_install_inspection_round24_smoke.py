"""Round-24 exception, prefix, binding, import, and assignment regressions."""

from __future__ import annotations

import ast
from pathlib import Path

from existing_install_inspection_call_boundary import called_symbols

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "existing_install_inspection"


def main() -> int:
    expectations: dict[str, tuple[set[str], set[str]]] = {
        "with_suppressed_raise_fixture.py": (
            {"contextlib.suppress", "forbidden_module.target_adapter"},
            set(),
        ),
        "handler_suite_prefix_fixture.py": ({"<dynamic>"}, set()),
        "orelse_suite_prefix_fixture.py": ({"<dynamic>"}, set()),
        "for_header_prefix_fixture.py": ({"<dynamic>"}, set()),
        "with_header_prefix_fixture.py": (
            {"<dynamic>", "contextlib.nullcontext"},
            set(),
        ),
        "star_import_invalidation_fixture.py": ({"<dynamic>"}, set()),
        "chained_assignment_order_fixture.py": ({"forbidden_module.target_adapter"}, set()),
    }
    for name, (expected, forbidden) in expectations.items():
        called = called_symbols(ast.parse((_FIXTURES / name).read_text(encoding="utf-8")))
        assert expected <= called and not forbidden & called, (name, called)
    print("existing_install_inspection_round24_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
