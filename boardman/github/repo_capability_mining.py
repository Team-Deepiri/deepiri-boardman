"""Own, dependency-free-of-any-vendor alternative to GitHunt/OSSInsight for measuring a
QA's DEMONSTRATED capability: mine their actual commit history in a repo (via PyDriller,
a real Apache-2.0 "Mining Software Repositories" library on PyPI), classify the repo's
difficulty with the same IDF-based tier_classifier already used everywhere else in this
codebase, and bucket "what tier of work has this person actually shipped."

Inspired by the metrics categories in chrkaatz/git-intelligence and
hoangsonww/GitIntel-MCP-Server (churn, contributor stats, complexity proxies via commit
history) -- both are real, MIT-licensed projects, but Node/TypeScript LOCAL-repo
analyzers with no hosted API for an arbitrary login. Rather than shelling out to a
second language runtime, this computes the same class of signal natively in Python
(PyDriller is a genuine PyPI package, `poetry add`-able directly, unlike either of
those).

Deliberately a BATCH-ONLY module: cloning a repo and walking its full commit history is
seconds-to-minutes of work, order of magnitude heavier than a single API call --
NEVER call this from a live request path (webhook, chat, roster load).
`boardman/assignment/config.py`'s live qa_tier resolution only ever reads the JSON
cache this produces, written by `scripts/mine_qa_repo_capability.py` run periodically
(cron / manual), the same convention as `sync_qa_capabilities.py`'s
`repo_signals.json` / `worker_team.json`.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field

from pydriller import Repository

_log = logging.getLogger(__name__)


@dataclass
class AuthorRepoStats:
    """One author's demonstrated activity in one already-cloned repo."""

    commits: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    files_touched: set[str] = field(default_factory=set)
    largest_commit_lines: int = 0

    @property
    def churn(self) -> int:
        return self.lines_added + self.lines_removed


def mine_author_stats(
    repo_path: str,
    *,
    author_emails: set[str] | None = None,
    author_names: set[str] | None = None,
) -> AuthorRepoStats:
    """Walk `repo_path`'s commit history (already cloned locally) and aggregate every
    commit whose author matches `author_emails`/`author_names` (case-insensitive).

    Matches on EITHER identity, since a person's git-commit email often differs from
    their GitHub profile/name and this codebase already treats GitHub-identity matching
    as inherently fuzzy (see assignment/identity_match.py) -- same spirit here.
    """
    emails = {e.strip().lower() for e in (author_emails or set()) if e and e.strip()}
    names = {n.strip().lower() for n in (author_names or set()) if n and n.strip()}
    stats = AuthorRepoStats()
    if not emails and not names:
        return stats

    for commit in Repository(repo_path).traverse_commits():
        a_email = (commit.author.email or "").strip().lower()
        a_name = (commit.author.name or "").strip().lower()
        if a_email not in emails and a_name not in names:
            continue
        stats.commits += 1
        stats.lines_added += commit.insertions
        stats.lines_removed += commit.deletions
        stats.files_touched.update(f.filename for f in commit.modified_files if f.filename)
        stats.largest_commit_lines = max(
            stats.largest_commit_lines, commit.insertions + commit.deletions
        )
    return stats


def clone_full(clone_url: str, dest_dir: str) -> None:
    """Clone `clone_url` into `dest_dir` (caller-owned, e.g. a TemporaryDirectory).

    Full history, not shallow -- PyDriller's traversal needs real history, and a
    shallow clone truncates exactly the older commits a capability-mining pass most
    wants for someone's long-tenured repos.
    """
    import git

    git.Repo.clone_from(clone_url, dest_dir)


def _redact(text: str, secret: str) -> str:
    """Scrub a known secret substring out of `text` before it ever reaches a log line.

    Most Team-Deepiri repos are private, so `clone_url` legitimately carries a PAT for
    git's own HTTPS auth (see scripts/mine_qa_repo_capability.py) -- but GitPython's own
    exceptions can embed the full command line (URL and all) verbatim. Logging the raw
    exception or URL on a clone failure would leak that token into CI/Actions logs,
    which are not secret-redacted for a value the workflow never itself references.
    This is a blunt string replace, not URL parsing, deliberately: it must catch the
    secret wherever it appears, not just where a URL parser expects credentials.
    """
    if not secret:
        return text
    return text.replace(secret, "***")


def mined_author_stats_for_clone_url(
    clone_url: str,
    *,
    author_emails: set[str] | None = None,
    author_names: set[str] | None = None,
    redact_secret: str = "",
) -> AuthorRepoStats | None:
    """Clone `clone_url` to a throwaway temp dir, mine it, then clean up.

    Returns None (rather than raising) on any clone/mining failure -- a single
    unreachable/renamed/huge repo must not abort an entire batch mining run.

    `redact_secret`: pass the raw credential embedded in `clone_url` (e.g. a PAT for a
    private repo) so it never appears in the failure log -- see `_redact`.
    """
    tmp_dir = tempfile.mkdtemp(prefix="qa-capability-mine-")
    try:
        clone_full(clone_url, tmp_dir)
        return mine_author_stats(tmp_dir, author_emails=author_emails, author_names=author_names)
    except Exception as exc:  # noqa: BLE001 - one bad repo must not sink the whole batch
        safe_url = _redact(clone_url, redact_secret)
        safe_exc = _redact(str(exc), redact_secret)
        _log.warning("repo_capability_mining: mining %s failed: %s", safe_url, safe_exc)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# Tiered thresholds for "has this person demonstrated real work at this repo's tier."
# A handful of trivial commits (typo fixes, single-line changes) should not count as
# demonstrated capability at a repo's tier -- these floors keep the signal meaningful.
_MIN_COMMITS_TO_COUNT = 2
_MIN_CHURN_TO_COUNT = 20


def demonstrated_tier_from_repo_stats(
    repo_tier_and_stats: list[tuple[int, AuthorRepoStats]],
) -> int:
    """Bucket a person's mined activity across N repos (each with its own
    tier_classifier-assigned tier) into a single starting qa_tier (1-3).

    Someone with real, substantive activity (see thresholds above) in at least one
    tier-3 repo demonstrates tier 3; failing that, real activity in a tier-2 repo
    demonstrates tier 2; otherwise (only tier-1 activity, or no qualifying activity at
    all) they demonstrate tier 1 -- this is a FLOOR a person has proven, not a guess,
    so "no qualifying evidence" still returns 1 rather than the caller's cold-start
    default; callers with zero input repos entirely should not call this at all and
    should fall through to qa_tier_cold_start_default instead.
    """
    best = 1
    for tier, stats in repo_tier_and_stats:
        if stats.commits < _MIN_COMMITS_TO_COUNT and stats.churn < _MIN_CHURN_TO_COUNT:
            continue
        if tier > best:
            best = tier
    return best if best in (1, 2, 3) else 1
