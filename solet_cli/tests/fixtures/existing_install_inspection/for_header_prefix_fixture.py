import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method
try:
    for slot in [forbidden_module.target_adapter]:
        1 / 0  # noqa: B018 - deliberate potential-exception fixture
        slot = safe_adapter.harmless_method
except Exception:
    slot()
