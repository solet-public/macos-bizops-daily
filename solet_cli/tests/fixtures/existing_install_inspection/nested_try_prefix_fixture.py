import safe_adapter
from target_adapter import target_adapter as forbidden

flag = True
slot = safe_adapter.harmless_method


def might_raise() -> None:
    pass


try:
    if flag:
        slot = forbidden
        might_raise()
    else:
        pass
    slot = safe_adapter.harmless_method
except ValueError:
    slot()
