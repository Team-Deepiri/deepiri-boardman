import json

import boardman.plaky.client
import boardman.settings as boardman_settings
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
    assert len(build_all_tools(allow_writes=True)) == 23


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
    async def test_add_comment_prefers_public_item_comments_with_board_id(self, monkeypatch):
        calls: list[tuple[str, str, str]] = []

        async def stub_public(self, board_id: str, item_id: str, text: str) -> dict:
            calls.append((board_id, item_id, text))
            return {"ok": True, "status": 201, "comment": {"ok": True}}

        monkeypatch.setattr(
            boardman.plaky.client.PlakyClient,
            "add_item_comment_public",
            stub_public,
            raising=True,
        )
        from boardman.plaky.client import PlakyClient

        c = PlakyClient(api_key="x", base_url="https://api.plaky.com/v1/public")
        r = await c.add_comment("6079528", "**PR:** http://example", board_id="218760")
        assert r["ok"] is True
        assert r.get("route") == "item_public"
        assert calls == [("218760", "6079528", "**PR:** http://example")]

    @pytest.mark.asyncio
    async def test_add_comment_resolve_board_then_item_comment(self, monkeypatch):
        calls: list[str] = []

        async def stub_public(self, board_id: str, item_id: str, text: str) -> dict:
            calls.append(board_id)
            return {"ok": True, "status": 200, "comment": {}}

        async def stub_resolve(self, item_id: str, *, skip_board_ids=None):
            assert item_id == "99"
            return "board-found"

        monkeypatch.setattr(
            boardman.plaky.client.PlakyClient,
            "add_item_comment_public",
            stub_public,
            raising=True,
        )
        monkeypatch.setattr(
            boardman.plaky.client.PlakyClient,
            "_resolve_board_id_for_item_public",
            stub_resolve,
            raising=True,
        )
        monkeypatch.setattr(boardman_settings.settings, "plaky_pr_tracking_board_id", "")

        from boardman.plaky.client import PlakyClient

        r = await PlakyClient(api_key="x", base_url="https://api.plaky.com/v1/public").add_comment("99", "hi")
        assert r["ok"] is True
        assert calls == ["board-found"]

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
        assert len(tools) == 9
        tool_names = [t.name for t in tools]
        assert "plaky_list_boards" in tool_names
        assert "plaky_match_board" in tool_names
        assert "plaky_match_group" in tool_names
        assert "plaky_board_schema" in tool_names
        assert "plaky_list_tasks" in tool_names
        assert "plaky_get_task" in tool_names
        assert "plaky_get_board_item" in tool_names
        assert "plaky_list_workspace_users" in tool_names
        assert "plaky_save_task_preferences" in tool_names

    def test_plaky_tools_build_with_writes(self):
        from boardman.agent.tools.plaky_tools import build_plaky_tools

        tools = build_plaky_tools(allow_writes=True)
        assert len(tools) == 14
        tool_names = [t.name for t in tools]
        assert "plaky_create_task" in tool_names
        assert "plaky_update_task" in tool_names
        assert "plaky_add_comment" in tool_names
        assert "plaky_create_subtask" in tool_names
        assert "plaky_patch_item_fields" in tool_names

    @pytest.mark.asyncio
    async def test_plaky_update_task_tool_passes_auto_assign_fields(self, monkeypatch):
        """Agent update tool should mirror CLI UpdateTaskInput (QA roster + placement)."""

        captured: dict = {}

        async def stub(task_id: str, req):
            captured["task_id"] = task_id
            captured["req"] = req
            return {"ok": True}

        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.update_task_internal",
            stub,
        )
        monkeypatch.setattr(
            "boardman.agent.tool_context.get_context_plaky_board_id",
            lambda: "board-from-context",
        )

        from boardman.agent.tools.plaky_tools import _plaky_update_task

        await _plaky_update_task(
            "task-99",
            status="In QA",
            auto_assign_qa=True,
            github_repo="bare-repo-name",
            board_id="",
        )
        assert captured["task_id"] == "task-99"
        inp = captured["req"]
        assert inp.auto_assign_qa is True
        assert inp.github_repo == "bare-repo-name"
        assert inp.plaky_board_id == "board-from-context"

        await _plaky_update_task("task-100", qa_plaky_id="user-qa-1", board_id="explicit-board")
        inp2 = captured["req"]
        assert inp2.qa_plaky_id == "user-qa-1"
        assert inp2.plaky_board_id == "explicit-board"

    @pytest.mark.asyncio
    async def test_plaky_create_subtask_tool_uses_internal_mutation_and_context(self, monkeypatch):
        captured: dict = {}

        async def stub(req):
            captured["req"] = req
            return {"ok": True, "subtask": {"id": "sub-1"}}

        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.create_subtask_internal",
            stub,
        )
        monkeypatch.setattr(
            "boardman.agent.tool_context.get_context_plaky_board_id",
            lambda: "board-from-context",
        )

        from boardman.agent.tools.plaky_tools import _plaky_create_subtask

        await _plaky_create_subtask("task-1", "Investigate logs", "Check API traces")
        req = captured["req"]
        assert req.parent_task_id == "task-1"
        assert req.title == "Investigate logs"
        assert req.description == "Check API traces"
        assert req.plaky_board_id == "board-from-context"

        await _plaky_create_subtask("task-2", "Write tests", board_id="explicit-board")
        req2 = captured["req"]
        assert req2.parent_task_id == "task-2"
        assert req2.plaky_board_id == "explicit-board"


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
