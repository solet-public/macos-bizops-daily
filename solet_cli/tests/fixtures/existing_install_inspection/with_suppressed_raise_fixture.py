import contextlib

import forbidden_module

with contextlib.suppress(ValueError):
    raise ValueError

forbidden_module.target_adapter()
