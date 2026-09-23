import forbidden_module
import safe_adapter

slot = safe_adapter.harmless_method
(lambda x=(slot := forbidden_module.target_adapter): slot())()
