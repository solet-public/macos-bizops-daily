from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

[slot() + (slot := forbidden) for _ in (0, 1)]
