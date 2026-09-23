from safe_adapter import harmless_method as relay
from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

for _ in (0, 1, 2):
    slot()
    slot = relay
    relay = forbidden
