"""Round-34 with-state, annotation-order, and class-rebinding regressions."""

from __future__ import annotations

import ast
from pathlib import Path

from existing_install_inspection_call_boundary import called_symbols

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "existing_install_inspection"


def _called(source: str) -> set[str]:
    return called_symbols(ast.parse(source))


def main() -> int:
    sources = (
        "from contextlib import suppress\nimport safe_adapter\nimport forbidden_module\nslot=safe_adapter.harmless_method\nwith suppress(ZeroDivisionError):\n slot=forbidden_module.target_adapter\n 1/0\n slot=safe_adapter.harmless_method\nslot()",  # noqa: E501
        "from contextlib import suppress\nimport safe_adapter\nimport forbidden_module\nslot=safe_adapter.harmless_method\nwith suppress(ZeroDivisionError):\n if True:\n  slot=forbidden_module.target_adapter\n  1/0\n  slot=safe_adapter.harmless_method\nslot()",  # noqa: E501
        "import safe_adapter\nimport forbidden_module\nslot=safe_adapter.harmless_method\nclass C:\n global slot\n from forbidden_module import target_adapter as slot\nslot()",  # noqa: E501
        "import safe_adapter\nimport forbidden_module\nslot=safe_adapter.harmless_method\nclass C:\n class D:\n  global slot\n  slot=forbidden_module.target_adapter\nslot()",  # noqa: E501
        "import safe_adapter\nimport forbidden_module\ndef outer():\n slot=safe_adapter.harmless_method\n class C:\n  nonlocal slot\n  slot=forbidden_module.target_adapter\n slot()\nouter()",  # noqa: E501
    )
    for source in sources:
        assert "<dynamic>" in _called(source)
    for name in (
        "round34_sequential_intermediate_fixture.py",
        "round34_multi_header_intermediate_fixture.py",
        "round34_async_intermediate_fixture.py",
        "round34_class_global_assignment_fixture.py",
    ):
        assert "<dynamic>" in _called((_FIXTURES / name).read_text(encoding="utf-8"))
    order = _called(
        "import safe_adapter\nimport forbidden_module\nslot=safe_adapter.harmless_method\ndef f(a:(slot:=forbidden_module.target_adapter), /, b:slot()): pass"  # noqa: E501
    )
    assert "safe_adapter.harmless_method" in order
    print("existing_install_inspection_round34_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
