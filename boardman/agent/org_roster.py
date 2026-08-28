"""The list of repositories Team Deepiri actually has, injected into every turn.

Asked "what is aarflingo", Boardman answered that it did not recognise the name and
that it "doesn't appear in the Deepiri context" — while `deepiri-aarflingo` sat in the
org the whole time. It had resolved the bare word against repos.yml (one entry), found
nothing, and reported absence from a partial view.

Knowing what exists is the cheapest possible context and the whole premise of the role,
so the roster goes in the prompt rather than depending on the model choosing to look.
Names only: once it knows the repo is real, the existing repo-context tools fetch the
detail, and those answers are already accurate.
"""

from __future__ import annotations

import logging

import httpx

from boardman.observability.degradation import log_degraded
from boardman.settings import (
    DEFAULT_AGENT_ORG_ROSTER_MAX_NAMES,
    positive_or_default,
    settings,
)

logger = logging.getLogger(__name__)


def _max_names() -> int:
    """How many repo names the roster prints before truncating.

    Capped to keep the system prompt under the context budget: 200 names is ~1.7KB. An
    org larger than the cap is a real deployment difference rather than a code question,
    so the ceiling is a setting (AGENT_ORG_ROSTER_MAX_NAMES). Anything <= 0 means unset:
    a negative would slice `shorts[:-1]` and drop a real repo while the prompt still
    claimed the list was complete.
    """
    return positive_or_default(
        getattr(settings, "agent_org_roster_max_names", 0), DEFAULT_AGENT_ORG_ROSTER_MAX_NAMES
    )


async def org_repo_roster_markdown() -> str:
    """Markdown naming every repo in the org, or '' when it cannot be fetched."""
    org = (settings.github_org or "").strip()
    if not org or not (settings.github_pat or "").strip():
        return ""
    try:
        from boardman.github.http import github_http_client
        from boardman.github.org_repos import fetch_org_repository_full_names

        client = github_http_client()
        names = await fetch_org_repository_full_names(
            client, org, skip_archived=settings.github_skip_archived
        )
    except (httpx.HTTPError, OSError, ValueError):
        logger.warning("org roster unavailable for this turn", exc_info=True)
        return ""
    except Exception as exc:  # noqa: BLE001 - logged and handled
        log_degraded(logger, f"org roster for {org}", exc)
        return ""
    shorts = sorted({n.rsplit("/", 1)[-1] for n in names if n})
    if not shorts:
        return ""
    shown = shorts[: _max_names()]
    hidden = len(shorts) - len(shown)

    return "\n".join(
        [
            "",
            f"## Repositories in {org} ({len(shorts)})",
            "",
            (
                "This is the full list. "
                if not hidden
                else f"These are {len(shown)} of the {len(shorts)} repositories in the org; "
                f"{hidden} more are not shown here. "
            )
            + "A name the user says almost always maps to one of "
            "these, usually by dropping or adding the `deepiri-`/`diri-` prefix — "
            '"aarflingo" is `deepiri-aarflingo`, "cyrex" is `diri-cyrex`.',
            "",
            ", ".join(f"`{s}`" for s in shown)
            + (f" …and {hidden} more repositories not listed here" if hidden else ""),
            "",
            "**Never say a project does not exist or that you do not recognise it "
            "without checking this list first.** If it is here, it is real: read it with "
            "the repo-context tools and answer from what is actually in it."
            + (
                " Only after it is genuinely absent from this list may you say you "
                "cannot find it."
                if not hidden
                else " If it is NOT here, the list above is truncated — say the name is "
                "not in the portion you can see and try the repo-context tools anyway, "
                "rather than claiming the repository does not exist."
            ),
            "",
        ]
    )
