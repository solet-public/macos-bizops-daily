"""Shared constants for github_midwife_plugin — the BIRTH SPINE.

Factory-only constants (seed-manifest/bundle filenames, scratch root, the
seal identity, secret/scratch globs, GH/archive timeouts, README template name,
the ship-invariant plugin names) moved to ``seed_factory_plugin.constants`` in
the 2026-07-20 split. What stays here is the genesis surface PLUS the genuinely
shared names the factory imports cross-plugin (git timeouts, KB-dir names, the
profile-baseline/template subdir names, SOLET_NAME_ENV).
"""

from __future__ import annotations

import re
from typing import Final

# Slice B — profile-driven allowlist install
PIP_INSTALL_TIMEOUT_S: Final[int] = 300

# Finding F8 (2026-07-11) — the PEP 517 build backend a stock Python 3.13
# venv lacks (ensurepip dropped setuptools in 3.12). Both venv-prep seams
# (venv_provision.create_venv_and_install_seed, profile_install.install_
# profile_allowlist) `pip install --upgrade` these before any
# `--no-build-isolation -e` editable install, else pip fails BackendUnavailable.
BUILD_BACKEND_PACKAGES: Final[tuple[str, ...]] = ("pip", "setuptools", "wheel")

# Slice A / B — knowledge-base-relative paths. SHARED with the seed factory
# (assemble reads profile_templates/, the resolver reads profile_baseline/, both
# from THIS plugin's shipped KB) — imported cross-plugin by seed_factory_plugin.
PROFILE_TEMPLATES_SUBDIR: Final[str] = "profile_templates"
PROFILE_BASELINE_SUBDIR: Final[str] = "profile_baseline"
KNOWLEDGE_BASE_DIRNAME: Final[str] = "knowledge_base"

# Slice D — genesis step machine
NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")


def is_valid_solet_name(name: str) -> bool:
    """True iff ``name`` FULLY matches :data:`NAME_PATTERN`.

    The single source of truth for "is this a safe solet name" across every
    Layer-1 name-derivation boundary (``steps.validate_name``,
    ``credential_seed``, ``venv_provision``). Uses ``fullmatch`` deliberately, NOT
    ``match``: ``$`` matches just BEFORE a trailing newline, so a name like
    ``"x\nhost=evil"`` would slip past ``NAME_PATTERN.match`` yet break out of the
    SQL / libpq-conninfo sinks these boundaries feed. ``fullmatch`` requires the
    whole string, closing that hole. (Layer 0's ``bootstrap.py`` is stdlib-only
    and cannot import this module, so it inlines the identical fullmatch check —
    keep the two in sync.)
    """
    return NAME_PATTERN.fullmatch(name) is not None
MANIFEST_MARKER_PATH: Final[str] = "profile/data/github_midwife/attempt.json"
# A platform clone is identified by ananta/ + plugins/ ONLY. knowledge_bases/
# is deliberately NOT a marker: on the seed axis it is genesis OUTPUT — the
# MINT assemble bans symlinks, so the aggregation dir ships EMPTY, and git
# does not track empty directories → a fresh `git clone` of a published seed
# has NO knowledge_bases/ until the materialize_kb_symlinks spine step
# creates+populates it (caught live on the first published seed, 2026-07-12).
REQUIRED_CLONE_MARKERS: Final[tuple[str, ...]] = ("ananta", "plugins")

# Git timeouts — SHARED. Used birth-side by git_init (init/add/commit of the born
# worktree, Workstream A) AND by the seed factory (seal/publish/content-scan git
# ops), which imports them cross-plugin. Query ops are light; init/add/commit are
# heavier. (The factory's own GIT_ARCHIVE_TIMEOUT_S + GH_NETWORK_TIMEOUT_S live in
# seed_factory_plugin.constants — they have no birth-side use.)
GIT_QUERY_TIMEOUT_S: Final[int] = 30
GIT_COMMIT_TIMEOUT_S: Final[int] = 60

# The env var the minting solet's identity derives from — SHARED (the seed
# factory's content validator fail-closes if it is unset, §4.2 #4). Kept here
# because the birth spine also reads SOLET_NAME across genesis.
SOLET_NAME_ENV: Final[str] = "SOLET_NAME"
