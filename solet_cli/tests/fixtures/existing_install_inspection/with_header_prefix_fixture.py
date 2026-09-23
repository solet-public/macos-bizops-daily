import contextlib

import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method
try:
    with contextlib.nullcontext(forbidden_module.target_adapter) as slot:
        1 / 0  # noqa: B018 - deliberate potential-exception fixture
        slot = safe_adapter.harmless_method
except Exception:
    slot()
