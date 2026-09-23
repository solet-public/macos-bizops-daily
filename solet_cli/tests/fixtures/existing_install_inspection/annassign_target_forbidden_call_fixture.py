"""AST-only adversary: calls in annotated-assignment targets remain observable."""

from target_adapter import target_adapter as forbidden

bucket = {}
bucket[forbidden()]: object = 1
