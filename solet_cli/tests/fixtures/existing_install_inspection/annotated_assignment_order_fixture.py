import safe_adapter
from target_adapter import target_adapter as forbidden

holder = {}
slot = safe_adapter.harmless_method
holder[slot()]: slot() = (slot := forbidden)
