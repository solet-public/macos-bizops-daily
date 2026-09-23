from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

n = 0
while slot() and n < 1:
    slot = forbidden
    n += 1
