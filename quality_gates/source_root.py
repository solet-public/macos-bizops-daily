"""Single explicit-root resolution for gate/test source code.

Gate and test machinery must resolve the CODE it runs from an explicitly
named tree, never from ``sys.path``, a bare ``__file__``-relative guess, or
whatever checkout happens to be invoking it — the invoking checkout and the
tree under test are routinely different copies (a materialized candidate, a
stale lane worktree, a linked worktree behind master). Confusing the two is
the wrong-tree defect class measured in ``iss_ec0db9c7`` / ``iss_77fe09ad``:
a caller built the right candidate content, then ran the identity gate
SCRIPT from wherever it happened to sit on disk in the invoking checkout,
silently checking base-ref bytes against a stale gate.

This module is the one place that resolves a gate/test source root and the
one place that locates a script inside it, so every consumer refuses the
same way instead of rediscovering the mistake ad hoc. It is intentionally
small: it has no default root and no fallback path, on purpose — a caller
that wants "the current checkout" must say so explicitly by passing its own
root, so the choice is visible at the call site rather than silently
inherited from ``cwd`` or ``sys.path``.
"""

from __future__ import annotations

from pathlib import Path


class SourceRootError(RuntimeError):
    """An explicit source root does not name the gate/test code it must."""


def resolve_source_root(explicit_root: Path) -> Path:
    """Return ``explicit_root``, resolved and verified, with no fallback.

    There is no default argument: a caller must always name the tree it
    means. The root must be a real directory that actually contains the
    ``quality_gates`` package — the one property every legitimate caller
    (a materialized candidate, the origin checkout itself) shares, and the
    one a wrong-tree pointer typically does not.
    """

    resolved = explicit_root.resolve()
    if not resolved.is_dir():
        raise SourceRootError(f"source root is not a directory: {resolved}")
    marker = resolved / "quality_gates" / "__init__.py"
    if not marker.is_file():
        raise SourceRootError(
            f"source root does not contain a quality_gates package: {resolved}"
        )
    return resolved


def gate_script_path(source_root: Path, relative_script: str) -> Path:
    """The absolute path to ``relative_script`` inside ``source_root``.

    Refuses an absolute ``relative_script``, one that would resolve outside
    ``source_root`` (a ``..`` escape), and one that does not exist — so a
    malformed relative path cannot silently point at a different tree either.
    """

    if Path(relative_script).is_absolute():
        raise SourceRootError(f"relative_script must be relative: {relative_script!r}")
    resolved_root = source_root.resolve()
    candidate = (resolved_root / relative_script).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise SourceRootError(
            f"{relative_script!r} escapes its source root {resolved_root}"
        ) from exc
    if not candidate.is_file():
        raise SourceRootError(f"gate script does not exist: {candidate}")
    return candidate


def assert_running_from_source_root(running_file: Path, source_root: Path) -> None:
    """Refuse when the CALLING script's own file is outside ``source_root``.

    This is the self-defense half of the mechanism: even when every caller
    builds its subprocess command correctly, a script cannot protect itself
    from some OTHER caller invoking it directly from the wrong tree unless it
    checks its own location against the root it was told to operate on. This
    is what makes the refusal a regression fixture can assert, not only a
    docstring: a gate that fails this check refuses loudly instead of
    silently scanning base-ref bytes with stale gate code.
    """

    own_root = running_file.resolve().parents[1]
    resolved_root = source_root.resolve()
    if own_root != resolved_root:
        raise SourceRootError(
            "gate script is running from a different tree than the one it was "
            f"told to scan: script tree={own_root} requested repo-root="
            f"{resolved_root}. This is the wrong-tree defect class "
            "(iss_ec0db9c7 / iss_77fe09ad): invoke this script from its OWN "
            "copy inside the tree under test, never from the invoking "
            "checkout or a stale worktree copy."
        )
