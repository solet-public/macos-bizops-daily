"""AST-only adversary: calls in assignment targets remain observable."""

from target_adapter import target_adapter as forbidden

bucket = {}
bucket[forbidden()] = 1
