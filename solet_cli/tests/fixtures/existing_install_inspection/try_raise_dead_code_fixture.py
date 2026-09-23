import safe_adapter
from target_adapter import target_adapter as forbidden

slot = safe_adapter.harmless_method
slot()
try:
    slot = forbidden
    raise ValueError
    slot = safe_adapter.harmless_method
except ValueError:
    slot()
