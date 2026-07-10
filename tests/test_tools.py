import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import boardman.plaky.client
import boardman.settings as boardman_settings
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
    from boardman.agent.guardrails import WRITE_TOOLS
    from boardman.agent.tools import build_all_tools

    ro = build_all_tools(allow_writes=False)
    rw = build_all_tools(allow_writes=True)
    ro_names = {t.name for t in ro}
    rw_names = {t.name for t in rw}
    assert rw_names - ro_names == set(WRITE_TOOLS)
    assert len(rw) - len(ro) == len(WRITE_TOOLS)


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

        r = await PlakyClient(api_key="x", base_url="https://api.plaky.com/v1/public").add_comment(
            "99", "hi"
        )
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


_PLAKY_TOOL_NAMES_READONLY = frozenset(
    {
        "plaky_list_boards",
        "plaky_match_board",
        "plaky_match_group",
        "plaky_board_schema",
        "plaky_list_tasks",
        "plaky_get_task",
        "plaky_get_board_item",
        "plaky_list_workspace_users",
        "plaky_save_task_preferences",
        "plaky_review_board",
    }
)
_PLAKY_TOOL_NAMES_WRITE = frozenset(
    {
        "plaky_create_task",
        "plaky_patch_item_fields",
        "plaky_update_task",
        "plaky_add_comment",
        "plaky_link_prs",
        "plaky_create_subtask",
    }
)


class TestPlakyTools:
    def test_plaky_tools_build_readonly(self):
        from boardman.agent.tools.plaky_tools import build_plaky_tools

        names = frozenset(t.name for t in build_plaky_tools(allow_writes=False))
        assert names == _PLAKY_TOOL_NAMES_READONLY

    def test_plaky_tools_build_with_writes(self):
        from boardman.agent.tools.plaky_tools import build_plaky_tools

        names = frozenset(t.name for t in build_plaky_tools(allow_writes=True))
        assert names == _PLAKY_TOOL_NAMES_READONLY | _PLAKY_TOOL_NAMES_WRITE

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
        monkeypatch.setattr(
            "boardman.agent.tool_context.get_context_plaky_group_id",
            lambda: "group-from-context",
        )

        from boardman.agent.tools.plaky_tools import _plaky_create_subtask

        await _plaky_create_subtask("task-1", "Investigate logs", "Check API traces")
        req = captured["req"]
        assert req.parent_task_id == "task-1"
        assert req.title == "Investigate logs"
        assert req.description == "Check API traces"
        assert req.plaky_board_id == "board-from-context"
        assert req.plaky_group_id == "group-from-context"

        await _plaky_create_subtask(
            "task-2", "Write tests", board_id="explicit-board", group_id="explicit-group"
        )
        req2 = captured["req"]
        assert req2.parent_task_id == "task-2"
        assert req2.plaky_board_id == "explicit-board"
        assert req2.plaky_group_id == "explicit-group"

    @pytest.mark.parametrize("bad_key", ["made-up", "unknown_field", "not-in-schema"])
    @pytest.mark.asyncio
    async def test_create_task_rejects_unknown_schema_key(self, monkeypatch, bad_key):
        """plaky_create_task must reject field_values whose keys are not on the board schema."""

        async def fake_bundle(board_id: str):
            return {
                "ok": True,
                "message": "",
                "normalized": {
                    "fields": [
                        {"name": "Priority", "key": "priority-key", "options": ["High", "Low"]}
                    ]
                },
            }

        called = {"create": 0}

        async def stub_create(req):
            called["create"] += 1
            return {"ok": True}

        class _StubCfg:
            plaky_field_repo = ""
            plaky_field_github_repos = ""

        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.fetch_board_schema_bundle", fake_bundle
        )
        monkeypatch.setattr("boardman.agent.tools.plaky_tools.create_task_internal", stub_create)
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.load_team_assignments", lambda: _StubCfg()
        )
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.infer_plaky_field_keys_from_normalized",
            lambda normalized: {},
        )
        monkeypatch.setattr(
            "boardman.agent.tool_context.get_context_plaky_board_id", lambda: "board-x"
        )
        monkeypatch.setattr("boardman.agent.tool_context.get_context_plaky_group_id", lambda: "")
        monkeypatch.setattr("boardman.agent.tool_context.get_tool_db_session", lambda: None)
        monkeypatch.setattr("boardman.agent.tool_context.get_agent_session_pk", lambda: None)

        from boardman.agent.tools.plaky_tools import _plaky_create_task

        raw = await _plaky_create_task(
            title="t",
            description="d",
            field_values_json=json.dumps({bad_key: "x"}),
        )
        out = json.loads(raw)
        assert out["ok"] is False
        err_blob = " ".join(out.get("errors") or [])
        assert bad_key in err_blob
        assert "priority-key" in err_blob
        assert called["create"] == 0

    @pytest.mark.asyncio
    async def test_plaky_create_task_happy_path_no_field_values(self, monkeypatch):
        called = {"n": 0}

        async def stub_create(req):
            called["n"] += 1
            return {"ok": True, "task": {"id": "new-1"}}

        monkeypatch.setattr("boardman.agent.tools.plaky_tools.create_task_internal", stub_create)
        monkeypatch.setattr("boardman.agent.tool_context.get_context_plaky_board_id", lambda: "")
        monkeypatch.setattr("boardman.agent.tool_context.get_context_plaky_group_id", lambda: "")

        from boardman.agent.tools.plaky_tools import _plaky_create_task

        raw = await _plaky_create_task(title="Ship feature", description="Details")
        out = json.loads(raw)
        assert out.get("ok") is True
        assert called["n"] == 1

    @pytest.mark.asyncio
    async def test_plaky_patch_item_fields_happy_path_valid_option(self, monkeypatch):
        async def fake_bundle(board_id: str):
            return {
                "ok": True,
                "message": "",
                "normalized": {
                    "fields": [
                        {
                            "name": "Priority",
                            "key": "priority-key",
                            "options": ["High", "Medium", "Low"],
                        }
                    ]
                },
            }

        calls: list[tuple[str, str, dict]] = []

        async def fake_patch(self, board_id, item_id, fields):
            calls.append((board_id, item_id, fields))
            return {"ok": True}

        class _StubCfg:
            plaky_field_repo = ""
            plaky_field_github_repos = ""

        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.fetch_board_schema_bundle", fake_bundle
        )
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.load_team_assignments", lambda: _StubCfg()
        )
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.infer_plaky_field_keys_from_normalized",
            lambda normalized: {},
        )
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.PlakyClient.patch_item_field_values",
            fake_patch,
        )

        from boardman.agent.tools.plaky_tools import _plaky_patch_item_fields

        raw = await _plaky_patch_item_fields("board-x", "item-1", '{"priority-key": "High"}')
        out = json.loads(raw)
        assert out["ok"] is True
        assert len(calls) == 1
        assert calls[0][0] == "board-x" and calls[0][1] == "item-1"
        assert calls[0][2].get("priority-key") == "High"

    @pytest.mark.asyncio
    async def test_patch_item_fields_rejects_invalid_option_value(self, monkeypatch):
        """plaky_patch_item_fields must reject an option value that is not in the allowed set."""

        async def fake_bundle(board_id: str):
            return {
                "ok": True,
                "message": "",
                "normalized": {
                    "fields": [
                        {
                            "name": "Priority",
                            "key": "priority-key",
                            "options": ["High", "Medium", "Low"],
                        }
                    ]
                },
            }

        called = {"patch": 0}

        async def fake_patch(self, board_id, item_id, fields):
            called["patch"] += 1
            return {"ok": True}

        class _StubCfg:
            plaky_field_repo = ""
            plaky_field_github_repos = ""

        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.fetch_board_schema_bundle", fake_bundle
        )
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.load_team_assignments", lambda: _StubCfg()
        )
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.infer_plaky_field_keys_from_normalized",
            lambda normalized: {},
        )
        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.PlakyClient.patch_item_field_values",
            fake_patch,
        )

        from boardman.agent.tools.plaky_tools import _plaky_patch_item_fields

        raw = await _plaky_patch_item_fields("board-x", "item-1", '{"priority-key": "urgent"}')
        out = json.loads(raw)
        assert out["ok"] is False
        errors_text = " ".join(out.get("errors") or [])
        assert "priority-key" in errors_text
        assert "urgent" in errors_text
        assert "High" in errors_text and "Medium" in errors_text and "Low" in errors_text
        assert called["patch"] == 0

    @pytest.mark.asyncio
    async def test_plaky_review_board_returns_diagnosis(self, monkeypatch):
        """plaky_review_board summarizes duplicates + missing acceptance criteria."""

        async def fake_list_items(self, board_id, *, max_pages=15):
            return {
                "ok": True,
                "items": [
                    {
                        "id": "i1",
                        "name": "Refactor agent runner",
                        "description": "Touch up the runner.",
                    },
                    {
                        "id": "i2",
                        "name": "Refactor agent runner",
                        "description": "Acceptance: tool traces preserved.",
                    },
                    {
                        "id": "i3",
                        "name": "Add OpenRouter provider",
                        "description": "Wire the new provider.",
                    },
                ],
            }

        monkeypatch.setattr(
            "boardman.agent.tools.plaky_tools.PlakyClient.list_board_items",
            fake_list_items,
        )
        monkeypatch.setattr("boardman.agent.tool_context.get_context_plaky_board_id", lambda: "")
        monkeypatch.setattr("boardman.agent.tool_context.get_context_plaky_group_id", lambda: "")

        from boardman.agent.tools.plaky_tools import _plaky_review_board

        raw = await _plaky_review_board(board_id="board-x")
        out = json.loads(raw)
        assert out["ok"] is True
        assert out["board_id"] == "board-x"
        assert out["items_scanned"] == 3
        assert out["duplicate_cluster_count"] == 1
        clusters = out.get("duplicate_clusters") or []
        assert clusters and clusters[0]["title_key"] == "refactor agent runner"
        assert out["missing_acceptance_count"] >= 2
        assert any("duplicate" in s.lower() for s in (out.get("recommended_actions") or []))


_GITHUB_TOOL_NAMES = frozenset(
    {
        "github_list_workspace_repos",
        "github_list_open_issues",
        "github_fetch_direction",
        "github_fetch_file",
        "github_repo_planning_context",
        "github_repo_structure",
    }
)


class TestGitHubTools:
    def test_build_github_tools_names(self):
        from boardman.agent.tools.github_tools import build_github_tools

        names = frozenset(t.name for t in build_github_tools())
        assert names == _GITHUB_TOOL_NAMES

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

    def test_get_routing_short_yaml_key(self, tmp_path, monkeypatch):
        from boardman.repos_config import (
            get_routing,
            reload_repos_config,
            repos_yaml_canonical_repo_key,
        )
        from boardman.settings import settings

        yml = tmp_path / "repos.yml"
        yml.write_text(
            "repos:\n  myrepo:\n    category: backend\n    plaky_table: SomeTable\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(settings, "repos_yml_path", str(yml))
        monkeypatch.setattr(settings, "github_org", "deepiri-org")
        monkeypatch.setattr(settings, "github_bare_repo_owner", "Team-Deepiri")
        reload_repos_config()

        r = get_routing("Team-Deepiri/myrepo", "myrepo", "deepiri-org")
        assert r is not None
        assert r.plaky_table == "SomeTable"
        assert repos_yaml_canonical_repo_key("Team-Deepiri/myrepo") == "myrepo"


class TestToolBuilding:
    def test_build_all_tools_readonly(self):
        from boardman.agent.guardrails import WRITE_TOOLS
        from boardman.agent.tools import build_all_tools

        ro = build_all_tools(allow_writes=False)
        rw = build_all_tools(allow_writes=True)
        ro_names = {t.name for t in ro}
        rw_names = {t.name for t in rw}
        assert rw_names - ro_names == set(WRITE_TOOLS)
        assert not (ro_names & set(WRITE_TOOLS))

    def test_build_all_tools_writes(self):
        from boardman.agent.guardrails import WRITE_TOOLS
        from boardman.agent.tools import build_all_tools

        ro = build_all_tools(allow_writes=False)
        rw = build_all_tools(allow_writes=True)
        assert {t.name for t in rw} == {t.name for t in ro} | set(WRITE_TOOLS)


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
