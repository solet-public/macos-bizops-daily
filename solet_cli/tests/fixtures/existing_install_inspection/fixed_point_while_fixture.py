from safe_adapter import harmless_method as relay
from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

marker = True
while marker:
    slot()
    slot = relay
    relay = forbidden
