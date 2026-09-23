"""AST-only stored literal-methodcaller adversary."""

import operator

target = operator.methodcaller("target_adapter")
target(object())
