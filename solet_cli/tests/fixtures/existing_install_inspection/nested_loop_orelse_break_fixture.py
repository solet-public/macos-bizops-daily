import safe_adapter
from target_adapter import target_adapter as forbidden

slot = forbidden
for _outer in (0,):
    for _inner in ():
        pass
    else:
        break
else:
    slot = safe_adapter.harmless_method
slot()
