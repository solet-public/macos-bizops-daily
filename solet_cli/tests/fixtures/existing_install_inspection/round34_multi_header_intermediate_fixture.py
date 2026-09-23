from contextlib import nullcontext, suppress

import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method
with suppress(ZeroDivisionError), nullcontext(slot := forbidden_module.target_adapter), (1 / 0):
    slot = safe_adapter.harmless_method
slot()
