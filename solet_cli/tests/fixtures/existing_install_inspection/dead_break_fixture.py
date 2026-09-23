import safe_adapter
from target_adapter import target_adapter as forbidden

slot = forbidden
for _ in (0,):
    break
    slot = safe_adapter.harmless_method
slot()
