"""AST-only nested rebinding adversary."""

import safe_adapter as safe
import target_adapter as x

target = safe.harmless_method
if True:
    target = x.target_adapter
target()
