import contextlib

import forbidden_module

boxes = {}
with contextlib.nullcontext(0) as boxes[forbidden_module.target_adapter()]:
    pass
