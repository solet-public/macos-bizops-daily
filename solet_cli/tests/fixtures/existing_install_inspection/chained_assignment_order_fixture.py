import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method
boxes = {}
slot = boxes[slot()] = forbidden_module.target_adapter
