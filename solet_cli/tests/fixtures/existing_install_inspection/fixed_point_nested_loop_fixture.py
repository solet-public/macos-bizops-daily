from safe_adapter import harmless_method as relay
from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

for _outer in (0, 1, 2):
    for _ in (0,):
        slot()
        slot = relay
    relay = forbidden
