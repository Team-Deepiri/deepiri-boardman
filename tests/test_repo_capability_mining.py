"""Own PyDriller-based capability mining: real local git repos, no network."""

from __future__ import annotations

import subprocess

from boardman.github import repo_capability_mining as rcm


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(tmp_path) -> str:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    d = str(repo_dir)
    _git("init", "-b", "main", cwd=d)
    _git("config", "user.email", "alice@example.com", cwd=d)
    _git("config", "user.name", "Alice A", cwd=d)

    (repo_dir / "a.py").write_text("x = 1\n" * 30)
    _git("add", "a.py", cwd=d)
    _git("commit", "-m", "alice: add a.py", cwd=d)

    _git("config", "user.email", "bob@example.com", cwd=d)
    _git("config", "user.name", "Bob B", cwd=d)
    (repo_dir / "b.py").write_text("y = 2\n")
    _git("add", "b.py", cwd=d)
    _git("commit", "-m", "bob: tiny fix", cwd=d)

    return d


def test_mine_author_stats_matches_by_email_only(tmp_path):
    repo_path = _make_repo(tmp_path)
    stats = rcm.mine_author_stats(repo_path, author_emails={"alice@example.com"})
    assert stats.commits == 1
    assert stats.lines_added == 30
    assert stats.files_touched == {"a.py"}
    assert stats.churn == 30


def test_mine_author_stats_matches_by_name_when_email_differs(tmp_path):
    repo_path = _make_repo(tmp_path)
    stats = rcm.mine_author_stats(repo_path, author_names={"bob b"})
    assert stats.commits == 1
    assert stats.files_touched == {"b.py"}


def test_mine_author_stats_no_identity_given_returns_empty(tmp_path):
    repo_path = _make_repo(tmp_path)
    stats = rcm.mine_author_stats(repo_path)
    assert stats.commits == 0


def test_mine_author_stats_no_match_returns_empty(tmp_path):
    repo_path = _make_repo(tmp_path)
    stats = rcm.mine_author_stats(repo_path, author_emails={"nobody@example.com"})
    assert stats.commits == 0


def test_mined_author_stats_for_clone_url_failure_returns_none(monkeypatch):
    def _boom(url, dest):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(rcm, "clone_full", _boom)
    result = rcm.mined_author_stats_for_clone_url(
        "https://example.invalid/x.git", author_emails={"a@example.com"}
    )
    assert result is None


def test_mined_author_stats_for_clone_url_redacts_secret_on_failure(monkeypatch, caplog):
    """A private-repo clone URL carries a PAT for git's own HTTPS auth -- a failure
    must never leak it into logs (CI/Actions logs are not secret-redacted for a value
    the workflow itself never references)."""

    def _boom(url, dest):
        raise RuntimeError(f"auth failed for {url}")

    monkeypatch.setattr(rcm, "clone_full", _boom)
    with caplog.at_level("WARNING"):
        result = rcm.mined_author_stats_for_clone_url(
            "https://SUPER_SECRET_TOKEN@github.com/org/repo.git",
            author_emails={"a@example.com"},
            redact_secret="SUPER_SECRET_TOKEN",
        )
    assert result is None
    assert "SUPER_SECRET_TOKEN" not in caplog.text
    assert "***@github.com/org/repo.git" in caplog.text


def test_redact_no_secret_returns_text_unchanged():
    assert rcm._redact("hello world", "") == "hello world"


def test_demonstrated_tier_no_qualifying_evidence_returns_floor_tier1():
    stats = rcm.AuthorRepoStats(commits=1, lines_added=1, lines_removed=0)
    assert rcm.demonstrated_tier_from_repo_stats([(3, stats)]) == 1


def test_demonstrated_tier_real_tier3_activity_wins():
    thin = rcm.AuthorRepoStats(commits=1, lines_added=1, lines_removed=0)
    substantial = rcm.AuthorRepoStats(commits=5, lines_added=200, lines_removed=50)
    assert rcm.demonstrated_tier_from_repo_stats([(1, thin), (3, substantial)]) == 3


def test_demonstrated_tier_only_tier1_activity_stays_tier1():
    substantial_t1 = rcm.AuthorRepoStats(commits=10, lines_added=500, lines_removed=100)
    assert rcm.demonstrated_tier_from_repo_stats([(1, substantial_t1)]) == 1


def test_demonstrated_tier_empty_input_is_floor_tier1():
    assert rcm.demonstrated_tier_from_repo_stats([]) == 1
