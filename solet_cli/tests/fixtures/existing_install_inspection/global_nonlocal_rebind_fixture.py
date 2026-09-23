"""AST-only adversary: nested global and nonlocal writes invalidate outer aliases."""

from safe_adapter import harmless_method as global_slot
from safe_adapter import harmless_method as safe
from target_adapter import target_adapter as forbidden


def outer() -> None:
    local_slot = safe

    def mutate_nonlocal() -> None:
        nonlocal local_slot
        local_slot = forbidden

    mutate_nonlocal()
    local_slot()


def mutate_global() -> None:
    global global_slot
    global_slot = forbidden


outer()
mutate_global()
global_slot()
