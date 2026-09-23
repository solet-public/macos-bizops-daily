import contextlib

import forbidden_module
import safe_adapter


def resolve() -> None:
    slot = forbidden_module.target_adapter
    with contextlib.nullcontext():
        return
    slot = safe_adapter.harmless_method
    slot()
