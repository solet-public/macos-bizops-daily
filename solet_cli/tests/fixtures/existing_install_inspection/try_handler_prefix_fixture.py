import safe_adapter
from target_adapter import target_adapter as forbidden

slot = safe_adapter.harmless_method
slot()


def might_raise() -> None:
    pass


try:
    slot = forbidden
    might_raise()
    slot = safe_adapter.harmless_method
except ValueError:
    slot()
