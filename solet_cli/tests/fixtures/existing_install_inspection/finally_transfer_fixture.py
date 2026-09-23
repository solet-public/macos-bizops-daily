import safe_adapter
from target_adapter import target_adapter as forbidden

slot = safe_adapter.harmless_method

for _ in (0,):
    try:
        slot = forbidden
        raise ValueError
    except ValueError:
        slot = safe_adapter.harmless_method
    finally:
        break  # noqa: B012 - deliberate preservation fixture

slot()
