import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method
boxes = {}
other = None
slot, boxes[slot()] = other, forbidden_module.target_adapter
