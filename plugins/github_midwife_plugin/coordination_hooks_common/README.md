# Coordination hook common policy

This directory is the canonical source for the runner-neutral Git-Controller
decision layer. Claude Code and Codex cache their plugins independently, so
each shipped hook directory carries a materialized copy of these three files:

- `_git_controller_lex.py`
- `_git_controller_walker.py`
- `_git_policy.py`

Run `python3 materialize.py --check` to verify both plugin copies or
`python3 materialize.py --write` after changing the canonical source. Runner
identity, hook payload parsing, dispatch, and block presentation remain in the
runner-specific `git_controller_gate.py` adapters and are never materialized
from here.

The origin checkout's unshipped gate is a third adapter. It imports these
modules through the project directory's common-policy directory; it must not
carry local `_git_policy.py`, `_git_controller_walker.py`, or
`_git_controller_lex.py` copies. Fleet-local configuration remains there, not
in materialized adopter artifacts.
