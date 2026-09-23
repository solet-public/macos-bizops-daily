"""Repo-root location for the platform dev-surface plugin.

Both service interfaces this plugin serves (``quality_service`` running
``quality_gates/*.py``; ``repo_service`` confining reads to the repo tree)
need the platform's GIT WORKTREE root at runtime.

Anchor at **APP_HOME**, NOT this module's ``__file__``. The platform runs
from a MATERIALIZED RELEASE COPY (``~/.ananta/releases/<name>/rel-.../code/``)
whose tree has no ``.git`` — and whose ``code/`` dir DOES contain a
``quality_gates/`` snapshot. A ``__file__``-anchored walk would therefore
either fail-loud in the release context (the original deploy fatality) or,
worse, silently resolve the FROZEN release snapshot as "the repo" and gate an
immutable copy forever instead of the working tree where development happens.

``app_home`` (the launch ``--app-home`` = ``<worktree>/profile``) is
deploy-invariant: the release-copy process is spawned with the SAME
``app_home`` the supervisor was launched with (macos_self_deployment
``supervisor._make_spawn``: ``interpreter=<release copy>`` but
``app_home=<worktree profile>``). Its parent IS the worktree, so anchoring
here resolves the WORKTREE root identically in the direct-launch and
release-copy contexts.

Fail-loud when no ancestor carries BOTH markers: a cloud solet has no
git repo, so the typed failure is CORRECT there — this plugin simply must not
appear in cloud manifests (local-profile only; see ``plugin.yaml``).
"""

from __future__ import annotations

from pathlib import Path


def locate_repo_root(app_home: Path) -> Path:
    """Return the git-worktree root by walking UP from ``app_home``.

    Checks ``app_home`` and each ancestor for BOTH ``quality_gates/`` and
    ``.git``; returns the first that carries both. Raises ``RuntimeError`` if
    none does — never guesses a root, because a wrong root would silently
    widen the ``repo_service`` confinement boundary or gate the wrong tree.
    """
    anchor = Path(app_home).resolve()
    for candidate in (anchor, *anchor.parents):
        if (candidate / "quality_gates").is_dir() and (candidate / ".git").exists():
            return candidate
    raise RuntimeError(
        "platform_dev_surface_plugin: could not locate the git worktree root from "
        f"app_home {anchor} — no ancestor carries both quality_gates/ and .git. "
        "This plugin requires the platform git worktree (local-profile only); "
        "exclude it from cloud manifests."
    )
