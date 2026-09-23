from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

r0 = slot
r1 = slot
r2 = slot
r3 = slot
r4 = slot
r5 = slot
r6 = slot
r7 = slot
r8 = slot

for _ in (0, 1):
    slot()
    slot = r0
    r0 = r1
    r1 = r2
    r2 = r3
    r3 = r4
    r4 = r5
    r5 = r6
    r6 = r7
    r7 = r8
    r8 = forbidden
