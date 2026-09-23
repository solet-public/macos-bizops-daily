import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method


class C:
    global slot
    slot = forbidden_module.target_adapter


slot()
