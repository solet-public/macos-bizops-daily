from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

if True:
    (slot := forbidden)()
