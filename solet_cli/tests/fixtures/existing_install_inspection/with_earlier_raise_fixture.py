import contextlib

import forbidden_module

for _ in (None,):
    with contextlib.suppress(ZeroDivisionError):
        1 / 0  # noqa: B018 - deliberate suppression fixture
        break
forbidden_module.target_adapter()
