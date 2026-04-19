import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from boardman.main import create_app


@pytest.mark.asyncio
async def test_health():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/health")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_openapi_has_agent_routes():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths") or {}
        assert any("agent" in p for p in paths)


def test_import_tools():
    from boardman.agent.tools import build_all_tools

    assert len(build_all_tools(allow_writes=False)) == 17
    assert len(build_all_tools(allow_writes=True)) == 22


@pytest.fixture
def plaky_key_cleared(monkeypatch):
    """PlakyClient(api_key=None) falls back to settings; clear it for missing-key tests."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "plaky_api_key", "")


class TestPlakyClient:
    @pytest.mark.asyncio
    async def test_create_task_missing_api_key(self, plaky_key_cleared):
        from boardman.plaky.client import PlakyClient
        c = PlakyClient(api_key=None)
        result = await c.create_task(title="Test", description="Desc")
        assert result["ok"] is False
        assert "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_get_tasks_missing_api_key(self, plaky_key_cleared):
        from boardman.plaky.client import PlakyClient
        c = PlakyClient(api_key=None)
        result = await c.get_tasks()
        assert result["ok"] is False
        assert "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_get_task_missing_api_key(self, plaky_key_cleared):
        from boardman.plaky.client import PlakyClient
        c = PlakyClient(api_key=None)
        result = await c.get_task("123")
        assert result["ok"] is False
        assert "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_add_comment_missing_api_key(self, plaky_key_cleared):
        from boardman.plaky.client import PlakyClient
        c = PlakyClient(api_key=None)
        result = await c.add_comment("123", "comment")
        assert result["ok"] is False
        assert "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_update_task_fields_missing_api_key(self, plaky_key_cleared):
        from boardman.plaky.client import PlakyClient
        c = PlakyClient(api_key=None)
        result = await c.update_task_fields("123", title="New")
        assert result["ok"] is False
        assert "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_create_subtask_missing_api_key(self, plaky_key_cleared):
        from boardman.plaky.client import PlakyClient
        c = PlakyClient(api_key=None)
        result = await c.create_subtask("123", "subtask")
        assert result["ok"] is False
        assert "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_get_board_missing_api_key(self, plaky_key_cleared):
        from boardman.plaky.client import PlakyClient
        c = PlakyClient(api_key=None)
        result = await c.get_board("b1")
        assert result["ok"] is False
        assert "missing" in result["message"].lower()


class TestPlakyTools:
    def test_plaky_tools_build_readonly(self):
        from boardman.agent.tools.plaky_tools import build_plaky_tools

        tools = build_plaky_tools(allow_writes=False)
        assert len(tools) == 10
        tool_names = [t.name for t in tools]
        assert "plaky_list_boards" in tool_names
        assert "plaky_match_board" in tool_names
        assert "plaky_match_group" in tool_names
        assert "plaky_board_schema" in tool_names
        assert "plaky_list_tasks" in tool_names
        assert "plaky_get_task" in tool_names
        assert "plaky_get_board_item" in tool_names
        assert "plaky_list_workspace_users" in tool_names
        assert "plaky_review_board" in tool_names
        assert "plaky_save_task_preferences" in tool_names

    def test_plaky_tools_build_with_writes(self):
        from boardman.agent.tools.plaky_tools import build_plaky_tools

        tools = build_plaky_tools(allow_writes=True)
        assert len(tools) == 15
        tool_names = [t.name for t in tools]
        assert "plaky_create_task" in tool_names
        assert "plaky_update_task" in tool_names
        assert "plaky_add_comment" in tool_names
        assert "plaky_create_subtask" in tool_names
        assert "plaky_patch_item_fields" in tool_names

    @pytest.mark.asyncio
    async def test_create_task_rejects_empty_title(self):
        from boardman.agent.tools.plaky_tools import _plaky_create_task

        raw = await _plaky_create_task(
            title="   ",
            description="x",
            board_id="b1",
            group_id="g1",
        )
        data = json.loads(raw)
        assert data["ok"] is False
        assert "title" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_create_task_rejects_unknown_schema_key(self, monkeypatch):
        import boardman.agent.tools.plaky_tools as pt

        async def fake_schema(_board_id: str):
            return {
                "ok": True,
                "normalized": {
                    "fields": [
                        {"name": "Status", "key": "status", "options": ["Todo", "Done"]},
                    ]
                },
            }

        async def fake_create_task(self, **kwargs):
            return {"ok": True, "task": {"id": "t1"}}

        monkeypatch.setattr(pt, "fetch_board_schema_bundle", fake_schema)
        monkeypatch.setattr(pt.PlakyClient, "create_task", fake_create_task)

        raw = await pt._plaky_create_task(
            title="Ship feature",
            description="desc",
            board_id="b1",
            group_id="g1",
            field_values_json='{"unknown_field":"x"}',
        )
        data = json.loads(raw)
        assert data["ok"] is False
        assert "invalid" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_update_task_rejects_invalid_status_option(self, monkeypatch):
        import boardman.agent.tools.plaky_tools as pt
        import boardman.agent.tool_context as tc

        async def fake_schema(_board_id: str):
            return {
                "ok": True,
                "normalized": {
                    "fields": [
                        {"name": "Status", "key": "status", "options": ["To Do", "Done"]},
                        {"name": "Priority", "key": "priority", "options": ["Low", "Medium", "High"]},
                    ]
                },
            }

        monkeypatch.setattr(pt, "fetch_board_schema_bundle", fake_schema)
        monkeypatch.setattr(tc, "get_context_plaky_board_id", lambda: "b1")

        raw = await pt._plaky_update_task(
            task_id="123",
            status="InvalidStatus",
            priority="high",
        )
        data = json.loads(raw)
        assert data["ok"] is False
        assert "status/priority" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_plaky_review_board_returns_diagnosis(self, monkeypatch):
        import boardman.agent.tools.plaky_tools as pt

        async def fake_list_board_items(self, board_id: str, max_pages: int = 1):
            return {
                "ok": True,
                "items": [
                    {
                        "id": "1",
                        "name": "Fix login bug",
                        "description": "No done criteria",
                        "status": "open",
                        "updatedAt": "2024-01-01T00:00:00Z",
                        "groupId": "g1",
                    },
                    {
                        "id": "2",
                        "name": "Fix login bug",
                        "description": "Acceptance: user can login",
                        "status": "open",
                        "updatedAt": "2024-02-01T00:00:00Z",
                        "groupId": "g1",
                    },
                ],
            }

        monkeypatch.setattr(pt.PlakyClient, "list_board_items", fake_list_board_items)
        raw = await pt._plaky_review_board(board_id="b1", group_id="g1", max_items=50)
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["duplicate_cluster_count"] >= 1
        assert data["missing_acceptance_count"] >= 1


class TestGitHubTools:
    def test_build_github_tools_count(self):
        from boardman.agent.tools.github_tools import build_github_tools

        assert len(build_github_tools()) == 5

    def test_github_tools_build(self):
        from boardman.agent.tools.github_tools import github_list_open_issues_tool

        tool = github_list_open_issues_tool()
        assert tool.name == "github_list_open_issues"

    @pytest.mark.asyncio
    async def test_list_open_issues_no_pat(self, monkeypatch):
        import boardman.repos_config as rc

        monkeypatch.setattr(rc.settings, "github_pat", None)
        from boardman.agent.tools.github_tools import _github_list_open_issues

        result = await _github_list_open_issues("owner/repo")
        data = json.loads(result)
        assert data["ok"] is False

    @pytest.mark.asyncio
    async def test_list_open_issues_invalid_format(self, monkeypatch):
        import boardman.repos_config as rc

        monkeypatch.setattr(rc.settings, "github_pat", "test-token")
        from boardman.agent.tools.github_tools import _github_list_open_issues

        result = await _github_list_open_issues("invalid")
        data = json.loads(result)
        assert data["ok"] is False


class TestGitHubWebhook:
    def test_verify_signature_missing_secret(self):
        from boardman.github.webhooks import verify_signature

        payload = b'{"action": "opened"}'
        assert verify_signature(payload, "sha256=abc", "") is True
        assert verify_signature(payload, "sha256=abc", None) is True

    def test_verify_signature_invalid(self):
        import hashlib
        import hmac

        from boardman.github.webhooks import verify_signature

        payload = b'{"action": "opened"}'
        secret = "test-secret"
        signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        assert verify_signature(payload, "sha256=wrong", secret) is False
        assert verify_signature(payload, f"sha256={signature}", secret) is True


class TestRepoConfig:
    def test_list_registered_repos(self):
        from boardman.repos_config import list_registered_repos

        repos = list_registered_repos()
        assert isinstance(repos, dict)

    def test_get_routing_unknown_repo(self):
        from boardman.repos_config import get_routing

        routing = get_routing("unknown/repo", "repo", "unknown-org")
        assert routing is None


class TestToolBuilding:
    def test_build_all_tools_readonly(self):
        from boardman.agent.tools import build_all_tools

        tools = build_all_tools(allow_writes=False)
        assert len(tools) == 17

    def test_build_all_tools_writes(self):
        from boardman.agent.tools import build_all_tools

        tools = build_all_tools(allow_writes=True)
        assert len(tools) == 22


class TestRepoScanTool:
    def test_scan_local_repo_returns_structured_payload(self, tmp_path: Path):
        from boardman.agent.tools.repo_tools import _scan_local_repo

        (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
        (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "plan.md").write_text("TODO: one\nFIXME: two\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='x'\n", encoding="utf-8")

        raw = _scan_local_repo(str(tmp_path), max_files=10)
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert "repo_map" in payload
        assert "docs" in payload
        assert "todo_summary" in payload
        assert any(f["path"] == "README.md" for f in payload["docs"]["files"])
        assert payload["todo_summary"]["todo_lines"] >= 1
        assert payload["todo_summary"]["fixme_lines"] >= 1
        assert len(tools) == 22
