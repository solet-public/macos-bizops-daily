import safe_adapter
from target_adapter import target_adapter as forbidden

flag = True
slot = safe_adapter.harmless_method
for _ in (0,):
    if flag:
        slot = forbidden
        continue
    else:
        slot = forbidden
        break
    slot = safe_adapter.harmless_method
slot()
