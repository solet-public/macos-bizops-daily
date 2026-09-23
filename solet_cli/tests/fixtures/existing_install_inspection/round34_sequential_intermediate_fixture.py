from contextlib import suppress

import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method
with suppress(ValueError):
    pass
with suppress(ZeroDivisionError):
    slot = forbidden_module.target_adapter
    1 / 0  # noqa: B018 - deliberate suppression fixture
    slot = safe_adapter.harmless_method
slot()
