"""Code-level repo signals from ONE GitHub tree call.

The planning context reads docs, issues and commits — good for direction, useless for
"find the real problems in this repo", which needs *source* evidence. This module derives
that evidence from a single recursive tree fetch (blob sizes are included), so it stays
cheap enough to run on every audit-style question.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from boardman.settings import settings

_log = logging.getLogger(__name__)

_SOURCE_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".swift",
    ".kt",
    ".scala",
    ".sh",
}
_TEST_MARKERS = ("test_", "_test.", "/tests/", "/test/", ".spec.", ".test.")
_VENDOR_MARKERS = ("node_modules/", "vendor/", "site-packages/", "dist/", "build/", ".venv/")

# Things that should essentially never be committed. Each is a finding on its own.
#
# MAINTENANCE: this is a security-sensitive detection list and it is only as complete as
# the last person to look at it. New key formats, new database engines and new tool
# artifacts appear all the time (id_ed25519, .kdbx, .jks, .tfstate, service-account JSON),
# so review it whenever the org adopts a new tool, and treat a miss as a gap in this list
# rather than a clean repo. A deployment can extend it without a code change via
# GITHUB_EXTRA_ARTIFACT_RULES ("marker:why; marker:why") -- see _artifact_rules() below.
_BUILTIN_ARTIFACT_RULES: tuple[tuple[str, str], ...] = (
    (".env", "environment file with likely secrets is tracked in git"),
    # ORDER MATTERS: the matcher breaks on the first hit and ".db" matches ".db-wal" by
    # substring, so the more specific markers have to come first or they never fire.
    (".db-wal", "SQLite write-ahead log committed to the repo"),
    (".db-shm", "SQLite shared-memory file committed to the repo"),
    (".db", "SQLite database committed to the repo"),
    (".sqlite", "SQLite database committed to the repo"),
    (".pem", "private key material tracked in git"),
    (".p12", "keystore tracked in git"),
    ("id_rsa", "private SSH key tracked in git"),
)

# The report is capped, so a repo with 30 committed .env files must not push a private key
# off the end of it. Lower number = reported first. Extra (configured) rules sit between:
# somebody thought them worth adding, but nobody reviewed what they mean.
_ARTIFACT_PRIORITY: dict[str, int] = {
    ".pem": 0,
    ".p12": 0,
    "id_rsa": 0,
    ".env": 1,
    ".db": 2,
    ".sqlite": 2,
    ".db-wal": 3,
    ".db-shm": 3,
}
_EXTRA_RULE_PRIORITY = 1
_MAX_REPORTED_ARTIFACTS = 20
# At most this many paths per rule. This is what actually keeps 30 committed .env files
# from filling the report and hiding a single .pem -- and it needs no guess about how
# severe a configured rule is. Reading an operator's prose for words like "secret" got
# ".parquet:data extract, no secrets inside" promoted to the private-key band; a fixed
# per-rule quota gives every rule a seat without interpreting anybody's sentence.
_MAX_PATHS_PER_RULE = 5

# What may follow an extension marker and still be the same file: a version digit
# (db.sqlite3), a rotation or backup segment (prod.db.backup, server.pem.bak, key.p12~),
# or both. Anything else -- ".md", "f", "file" -- is a different file that merely contains
# the marker's letters, which is how README.db.md got reported as a committed database.
_ARTIFACT_TAIL_RE = re.compile(
    r"^\d*(?:[.\-](?:bak|backup|old|orig|save|copy|tmp|\d+))*~?$",
    re.IGNORECASE,
)


def _extension_hit(base: str, marker: str) -> bool:
    """True when `marker` is `base`'s extension, allowing version/backup tails."""
    idx = base.rfind(marker)
    while idx != -1:
        if _ARTIFACT_TAIL_RE.match(base[idx + len(marker) :]):
            return True
        idx = base.rfind(marker, 0, idx)
    return False


_DEFAULT_EXTRA_REASON = "matched a configured artifact rule (GITHUB_EXTRA_ARTIFACT_RULES)"
# A configured marker must be at least this long. "db" as a substring rule matches
# dbutils.py and adbc_client.go and buries every real finding under noise.
_MIN_EXTRA_MARKER_LEN = 3
# A public key is not a private key, and neither is a template. Never a finding.
_NEVER_AN_ARTIFACT = (".example", ".sample", ".template", ".pub")
# Conventional names for a committed .env TEMPLATE. These live in the repo on purpose and
# hold placeholders, so they are the opposite of the finding the .env rule is after.
# .env siblings that are committed on purpose: templates with placeholder values, and
# files that are already encrypted at rest (dotenv-vault, sops, git-crypt, age).
# Deliberately NOT ".ci" or ".test": a .env.ci or .env.test routinely holds real CI
# credentials, and it is flagged for the same reason .env.production is.
_ENV_NOT_A_FINDING_SUFFIXES = (
    # templates
    ".dist",
    ".defaults",
    ".default",
    ".schema",
    ".tpl",
    ".tmpl",
    # encrypted at rest
    ".vault",
    ".enc",
    ".encrypted",
    ".gpg",
    ".pgp",
    ".age",
    ".sops",
)


def _parse_extra_artifact_rules(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse GITHUB_EXTRA_ARTIFACT_RULES: "marker:why; marker:why".

    Rules are separated by ``;`` and NOT by ``,``, because reasons are prose and prose has
    commas in it. Splitting on the comma turned the tail of a reason into a live substring
    matcher -- "id_rsa:private ssh key, keys" started flagging `api_keys.py` as a committed
    secret. A marker is a filename fragment, so anything containing whitespace is a
    fragment of somebody's sentence and is refused rather than matched.

    The reason is optional: "id_ed25519;.tfstate" flags both files with a generic reason.
    A security detection must never fail *open* because the sentence explaining it is
    missing. Malformed entries are skipped rather than raising -- a typo in one env var
    must not stop the whole hotspot scan, and the built-in rules still apply.
    """
    out: list[tuple[str, str]] = []
    for chunk in (raw or "").split(";"):
        entry = chunk.strip()
        if not entry:
            continue
        marker, _, why = entry.partition(":")
        marker = marker.strip().lower()
        why = why.strip()
        if not marker:
            _log.warning(
                "ignoring GITHUB_EXTRA_ARTIFACT_RULES entry %r: no marker before the colon",
                entry,
            )
            continue
        if not any(ch.isalnum() for ch in marker):
            # A bare "." left by a trailing separator would match every file with an
            # extension and report the entire repository as committed secrets.
            _log.warning(
                "ignoring GITHUB_EXTRA_ARTIFACT_RULES entry %r: marker %r has no letters "
                "or digits, so it would match almost every path",
                entry,
                marker,
            )
            continue
        if len(marker) < _MIN_EXTRA_MARKER_LEN:
            _log.warning(
                "ignoring GITHUB_EXTRA_ARTIFACT_RULES entry %r: marker %r is shorter than "
                "%d characters and would match far too many paths (try '.%s')",
                entry,
                marker,
                _MIN_EXTRA_MARKER_LEN,
                marker,
            )
            continue
        if any(ch.isspace() for ch in marker):
            # Almost certainly the tail of a reason that used the wrong separator.
            # Matching on it would flag unrelated files as committed secrets.
            _log.warning(
                "ignoring GITHUB_EXTRA_ARTIFACT_RULES entry %r: a marker is a filename "
                "fragment and cannot contain spaces (rules are separated by ';')",
                entry,
            )
            continue
        out.append((marker, why or _DEFAULT_EXTRA_REASON))
    return tuple(out)


# Memoised on the raw setting string: this runs per scan, and re-parsing meant one
# malformed entry re-emitted its warning on every scan instead of once.
_rules_cache: tuple[str, tuple[tuple[str, str, bool], ...]] | None = None


def _artifact_rules() -> tuple[tuple[str, str, bool], ...]:
    """Built-in rules plus anything the deployment added, as (marker, why, suffix_only).

    Configured markers are suffix-only. Substring matching is what makes the built-in
    list work (`id_rsa` inside `id_rsa_backup`), but on a marker nobody reviewed it is a
    liability: "id_ed25519" would flag `id_ed25519.pub`, a PUBLIC key, as private key
    material, and every short marker matches half the repository.
    """
    global _rules_cache
    raw = getattr(settings, "github_extra_artifact_rules", "") or ""
    if _rules_cache is not None and _rules_cache[0] == raw:
        return _rules_cache[1]
    rules: tuple[tuple[str, str, bool], ...] = tuple(
        (marker, why, False) for marker, why in _BUILTIN_ARTIFACT_RULES
    ) + tuple((marker, why, True) for marker, why in _parse_extra_artifact_rules(raw))
    _rules_cache = (raw, rules)
    return rules


def _is_source(path: str) -> bool:
    low = path.lower()
    if any(v in low for v in _VENDOR_MARKERS):
        return False
    return any(low.endswith(e) for e in _SOURCE_EXTS)


def _is_test(path: str) -> bool:
    low = path.lower()
    return any(m in low for m in _TEST_MARKERS)


async def fetch_repo_hotspots(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    *,
    branch: str = "",
    top_n: int = 15,
) -> dict[str, Any] | None:
    """Largest source files, test ratio, directory weight, and tracked-artifact smells.

    Memoized: defect scan + every code search each re-fetched the same repo tree (2
    requests) before doing their real work. The tree does not change mid-conversation.
    """
    from boardman.github.read_cache import cached

    return await cached(
        f"hotspots:{owner}/{repo}@{branch}:{top_n}",
        lambda: _fetch_repo_hotspots_uncached(client, owner, repo, branch=branch, top_n=top_n),
        ok=lambda v: isinstance(v, dict),
    )


async def _fetch_repo_hotspots_uncached(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    *,
    branch: str = "",
    top_n: int = 15,
) -> dict[str, Any] | None:
    token = (settings.github_pat or "").strip()
    if not token:
        return None
    ref = (branch or "").strip()
    from boardman.github.repo_metadata import fetch_repo_identity, fetch_repo_tree

    identity = await fetch_repo_identity(client, owner, repo)
    if not identity:
        _log.debug("hotspots: repo identity unavailable for %s/%s", owner, repo)
        return None
    if not ref:
        ref = str(identity.get("default_branch") or "main")

    data = await fetch_repo_tree(client, owner, repo, ref)
    if not data:
        _log.debug("hotspots: tree unavailable for %s/%s", owner, repo)
        return None

    blobs = [n for n in (data.get("tree") or []) if isinstance(n, dict) and n.get("type") == "blob"]
    source: list[tuple[str, int]] = []
    tests = 0
    dir_counts: dict[str, int] = {}
    artifacts: list[tuple[int, str, dict[str, str]]] = []
    rules = _artifact_rules()

    for n in blobs:
        path = str(n.get("path") or "")
        if not path:
            continue
        size = int(n.get("size") or 0)
        low = path.lower()
        base = low.rsplit("/", 1)[-1]

        if base.endswith(_NEVER_AN_ARTIFACT):
            # A template, a sample, or a PUBLIC key. Never the thing the rule is after.
            pass
        else:
            for marker, why, suffix_only in rules:
                if marker == ".env":
                    # ".env" and its per-environment siblings (".env.local",
                    # ".env.production") hold the same secrets and are all findings.
                    # ".envrc" is a direnv config meant to be committed, so the dot is
                    # required; ".env.dist" and friends are templates, so they are not.
                    hit = base == marker or (
                        base.startswith(".env.") and not base.endswith(_ENV_NOT_A_FINDING_SUFFIXES)
                    )
                elif suffix_only or marker.startswith("."):
                    # An extension is an ENDING, allowing a version digit or a backup
                    # tail: ".db" catches prod.db and prod.db.backup but not README.db.md
                    # or data.dbf, and ".sqlite" still catches db.sqlite3.
                    hit = _extension_hit(base, marker)
                else:
                    # A name fragment like "id_rsa" is deliberately substring-matched, so
                    # id_rsa_backup and id_rsa.old are caught too.
                    hit = marker in base
                if hit:
                    rank = (
                        _EXTRA_RULE_PRIORITY
                        if suffix_only
                        else _ARTIFACT_PRIORITY.get(marker, _EXTRA_RULE_PRIORITY)
                    )
                    artifacts.append((rank, marker, {"path": path, "why": why}))
                    break

        if _is_source(path):
            top = path.split("/", 1)[0] if "/" in path else "(root)"
            dir_counts[top] = dir_counts.get(top, 0) + 1
            if _is_test(path):
                tests += 1
            else:
                source.append((path, size))

    # Stable sort on the rule's priority: key material outranks env files outranks
    # databases, and paths keep tree order inside each band.
    artifacts.sort(key=lambda t: t[0])
    per_rule: dict[str, int] = {}
    reported_artifacts: list[dict[str, str]] = []
    for _rank, marker, row in artifacts:
        if len(reported_artifacts) >= _MAX_REPORTED_ARTIFACTS:
            break
        if per_rule.get(marker, 0) >= _MAX_PATHS_PER_RULE:
            continue
        per_rule[marker] = per_rule.get(marker, 0) + 1
        reported_artifacts.append(row)
    hidden_artifacts = len(artifacts) - len(reported_artifacts)
    artifacts_note = (
        f"Showing {len(reported_artifacts)} of {len(artifacts)} committed-artifact findings, "
        f"most severe first, at most {_MAX_PATHS_PER_RULE} paths per rule."
        if hidden_artifacts
        else ""
    )

    source.sort(key=lambda t: -t[1])
    src_count = len(source)
    return {
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "truncated": bool(data.get("truncated")),
        "total_files": len(blobs),
        "source_files": src_count,
        "test_files": tests,
        "test_to_source_ratio": round(tests / src_count, 2) if src_count else 0.0,
        # Report SIZE, never a derived "line count": the tree API gives bytes, and a
        # bytes/35 estimate was being quoted verbatim as "~2,273 lines" when the file had
        # 1,792 — real files with invented precision. Size ranking answers the same
        # question ("which modules are oversized?") without asserting a number we lack.
        "largest_source_files": [
            {"path": p, "size_kb": round(s / 1024, 1), "rank": i + 1}
            for i, (p, s) in enumerate(source[:top_n])
        ],
        "size_note": (
            "Sizes are bytes from the git tree. Do NOT convert them to line counts or quote "
            "a line number you have not read; say 'largest module (~N KB)' instead."
        ),
        "files_by_top_dir": dict(sorted(dir_counts.items(), key=lambda kv: -kv[1])[:12]),
        "tracked_artifacts": reported_artifacts,
        # Silence would read as "that is all of them".
        "tracked_artifacts_note": artifacts_note,
    }
