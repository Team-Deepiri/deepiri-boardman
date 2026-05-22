from __future__ import annotations

from pathlib import Path

from boardman.readiness import FAIL, PENDING, build_readiness_report, load_env_file


def _write_minimum_repo(root: Path) -> None:
    (root / "scripts").mkdir()
    (root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (root / "scripts" / "deploy_preflight.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "scripts" / "deploy_smoke.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "docker-compose.prod.yml").write_text(
        """
services:
  boardman:
    image: boardman:test
  boardman-worker:
    image: boardman:test
  boardman-nginx:
    image: boardman-nginx:test
    ports:
      - "8088:80"
""".lstrip(),
        encoding="utf-8",
    )


def test_load_env_file_hides_export_comments_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
export PLAKY_API_KEY='fake-plaky-key' # local comment
GITHUB_PAT="fake-github-token"
GITHUB_WEBHOOK_SECRET=fake-webhook-secret
""".lstrip(),
        encoding="utf-8",
    )

    env = load_env_file(env_file)

    assert env["PLAKY_API_KEY"] == "fake-plaky-key"
    assert env["GITHUB_PAT"] == "fake-github-token"
    assert env["GITHUB_WEBHOOK_SECRET"] == "fake-webhook-secret"


def test_readiness_flags_placeholders_and_missing_decisions(tmp_path):
    _write_minimum_repo(tmp_path)
    (tmp_path / ".env").write_text(
        """
PLAKY_API_KEY=your_plaky_api_key_here
GITHUB_PAT=your_github_personal_access_token_here
GITHUB_WEBHOOK_SECRET=your_github_webhook_secret_here
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "repos.yml").write_text("repos: {}\n", encoding="utf-8")
    (tmp_path / "team_assignments.yml").write_text(
        """
plaky_field_keys:
  engineer: ""
  qa: ""
  repo: ""
  github_repos: ""
""".lstrip(),
        encoding="utf-8",
    )

    report = build_readiness_report(tmp_path)
    failed_names = {check.name for check in report.checks if check.status == FAIL}
    pending_names = {check.name for check in report.checks if check.status == PENDING}

    assert {"PLAKY_API_KEY", "GITHUB_PAT", "GITHUB_WEBHOOK_SECRET"} <= failed_names
    assert "credential rotation gate" in pending_names
    assert "repo placement" in pending_names
    assert "field inventory" in pending_names
    assert "BOARDMAN_TARGET_ENV" in pending_names


def test_readiness_passes_complete_offline_config(tmp_path):
    _write_minimum_repo(tmp_path)
    (tmp_path / ".env").write_text(
        """
PLAKY_API_KEY=fake-plaky-service-key
GITHUB_PAT=fake-github-service-token
GITHUB_WEBHOOK_SECRET=fake-webhook-secret
WORKER_INTERNAL_SECRET=fake-worker-secret
ROUTE_SECRET=fake-route-secret
BOARDMAN_SECRETS_ROTATED=true
BOARDMAN_TARGET_ENV=vps
GITHUB_AUTH_MODE=pat
BOARDMAN_PUBLIC_URL=https://boardman.example.com
GITHUB_WEBHOOK_EVENTS=issues,pull_request,pull_request_review,pull_request_review_comment,issue_comment
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "repos.yml").write_text(
        """
defaults:
  plaky_board_id: board-123
  plaky_group_id: group-456
repos: {}
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "team_assignments.yml").write_text(
        """
plaky_field_keys:
  engineer: field-engineer
  qa: field-qa
  repo: field-repo
  github_repos: field-github-repos
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "boardman.db").touch()

    report = build_readiness_report(tmp_path)

    assert report.failures == 0
    assert report.pending == 0
    assert report.ready_for_go_live is True
