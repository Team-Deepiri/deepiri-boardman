"""Validation for caller-supplied ``owner/name`` repository slugs.

Several routes take a repo from the request and hand it to a subprocess argv
(``boardman.services.direction_init`` shells out to ``gh``/``git``) or splice it
into a GitHub API path. There is no shell anywhere in that path, so this is not
shell injection -- but an argv element that begins with ``-`` is read as a
*flag* by those tools, so an unvalidated owner like ``--repo=other/repo`` was
interpreted as an option rather than a repository name.

The pattern below is the shared chokepoint: exactly one ``owner/name`` pair,
each half restricted to the characters GitHub actually permits and required to
start with an alphanumeric (which is what rejects ``-x/evil``).
"""

from __future__ import annotations

import re

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

INVALID_REPO_MESSAGE = "repo must be owner/name using only letters, digits, '.', '_' or '-'"


def is_valid_repo_slug(value: str | None) -> bool:
    """True when ``value`` is a single well-formed ``owner/name`` slug."""
    return bool(value) and REPO_SLUG_RE.match(value) is not None


def split_repo_slug(value: str) -> tuple[str, str] | None:
    """Return ``(owner, name)`` for a valid slug, or None if it is not one."""
    if not is_valid_repo_slug(value):
        return None
    owner, _, name = value.partition("/")  # type: ignore[union-attr]
    return owner, name
