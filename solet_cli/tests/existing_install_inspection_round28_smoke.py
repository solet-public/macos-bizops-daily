"""Round-28 control-flow, prefix, comprehension, and lambda regressions."""

from __future__ import annotations

import ast
from pathlib import Path

from existing_install_inspection_call_boundary import called_symbols

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "existing_install_inspection"


def main() -> int:
    expected = {
        "with_earlier_raise_fixture.py": {"contextlib.suppress", "forbidden_module.target_adapter"},
        "comprehension_target_fixture.py": {"forbidden_module.target_adapter"},
        "lambda_default_walrus_fixture.py": {"<dynamic>"},
        "trystar_prefix_sequence_fixture.py": {"<dynamic>"},
    }
    for name, symbols in expected.items():
        called = called_symbols(ast.parse((_FIXTURES / name).read_text(encoding="utf-8")))
        assert symbols <= called, (name, called)
    print("existing_install_inspection_round28_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
