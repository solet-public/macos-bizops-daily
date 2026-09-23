from safe_adapter import harmless_method as slot  # noqa: F401
from target_adapter import target_adapter as forbidden

for slot in (forbidden,):  # noqa: F402
    slot()
