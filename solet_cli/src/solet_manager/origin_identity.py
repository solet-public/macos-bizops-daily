"""One judgement of whether a target's ``origin`` URL names a given GitHub repository.

A real pre-Manager clone may have been made over SSH (``git@github.com:owner/repo.git``) or over
HTTPS without the ``.git`` suffix; each names the same repository as the canonical
``https://github.com/owner/repo.git``. Owner and repository are compared exactly (no case folding),
and a URL of any other shape matches only itself, so the set of repositories accepted never widens.
The two key spaces are disjoint by prefix: no unrecognised string (a scheme-less
``github.com/owner/repo``, a relative path) can ever produce a recognised URL's key.
The origin is never fetched by the Manager (candidates come from its own cache), so a spelling
choice carries no credential or transport requirement.
"""

from __future__ import annotations

import re

_GITHUB = re.compile(r"^(?:https://github\.com/|git@github\.com:)(?P<owner>[A-Za-z0-9-]+)/(?P<name>[A-Za-z0-9._-]+?)(?:\.git)?$")


def repository_key(url: str) -> str:
    """``github:<owner>/<repo>`` for a recognised GitHub HTTPS or SSH spelling; ``url:<the URL>`` otherwise."""
    match = _GITHUB.fullmatch(url)
    return f"url:{url}" if match is None else f"github:{match['owner']}/{match['name']}"


def names_repository(origin: str, repository: str) -> bool:
    return repository_key(origin) == repository_key(repository)


def any_names_repository(origins: tuple[str, ...], repository: str) -> bool:
    return any(names_repository(origin, repository) for origin in origins)


def only_names_repository(origins: tuple[str, ...], repository: str) -> bool:
    """Exactly one origin URL, and it names ``repository``."""
    return len(origins) == 1 and names_repository(origins[0], repository)
