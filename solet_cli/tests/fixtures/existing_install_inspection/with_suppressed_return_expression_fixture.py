import contextlib

import forbidden_module


def f() -> object:
    with contextlib.suppress(ZeroDivisionError):
        return 1 / 0
    forbidden_module.target_adapter()
    return None
