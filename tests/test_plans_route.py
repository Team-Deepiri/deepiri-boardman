from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from boardman.main import create_app
from boardman.planning.huddle.models import MeetingPlan
from boardman.settings import settings

# plans/generate is privileged: it spends LLM budget on the server's key and
# writes to the planning output dir by default. These tests exercise the handler,
# so they authenticate; the anonymous case is covered in
# test_security_audit_2026_09.py.
AUTH = {"Authorization": "Bearer plans-test-token"}


@pytest.fixture(autouse=True)
def _auth_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "boardman_api_token", "plans-test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)


@pytest.mark.asyncio
async def test_openapi_has_plans_generate_route():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        assert "/api/v1/plans/generate" in (r.json().get("paths") or {})


@pytest.mark.asyncio
async def test_plans_generate_route_returns_markdown(monkeypatch):
    app = create_app()

    def _fake_generate_plan(request, **kwargs):  # noqa: ANN001
        return MeetingPlan(
            markdown="# Weekly\n\n## Purpose\n- Sync\n",
            provider_used="stub",
            model_used="stub-model",
            generated_at_iso="2026-06-16T12:00:00+00:00",
        )

    monkeypatch.setattr("boardman.routes.plans.generate_plan", _fake_generate_plan)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/plans/generate",
            headers=AUTH,
            json={
                "meeting_title": "Weekly",
                "meeting_type": "weekly-status-sync",
                "team_focus": "qa",
                "week_label": "next-week",
                "target_date_iso": "2026-06-16",
                "attendees_count": 12,
                "objectives": ["Align priorities"],
                "write_to_disk": False,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider_used"] == "stub"
    assert "## Purpose" in body["markdown"]
    assert body["output_path"] is None


@pytest.mark.asyncio
async def test_plans_generate_route_rejects_invalid_team():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/plans/generate",
            headers=AUTH,
            json={
                "meeting_title": "Weekly",
                "meeting_type": "weekly-status-sync",
                "team_focus": "invalid-team",
            },
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_plans_generate_route_rejects_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr("boardman.routes.plans.settings.planning_output_dir", str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for escape in ("../../etc/evil.md", "/etc/evil.md"):
            r = await client.post(
                "/api/v1/plans/generate",
                headers=AUTH,
                json={
                    "meeting_title": "Weekly",
                    "meeting_type": "weekly-status-sync",
                    "team_focus": "qa",
                    "write_to_disk": True,
                    "output_path": escape,
                },
            )
            assert r.status_code == 422, escape


@pytest.mark.asyncio
async def test_plans_generate_route_confines_output_path(monkeypatch, tmp_path):
    monkeypatch.setattr("boardman.routes.plans.settings.planning_output_dir", str(tmp_path))

    captured: dict[str, object] = {}

    def _fake_generate_plan(request, **kwargs):  # noqa: ANN001
        captured["output_path"] = kwargs.get("output_path")
        return MeetingPlan(
            markdown="# Weekly\n",
            provider_used="stub",
            model_used="stub-model",
            generated_at_iso="2026-06-16T12:00:00+00:00",
        )

    monkeypatch.setattr("boardman.routes.plans.generate_plan", _fake_generate_plan)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/plans/generate",
            headers=AUTH,
            json={
                "meeting_title": "Weekly",
                "meeting_type": "weekly-status-sync",
                "team_focus": "qa",
                "write_to_disk": True,
                "output_path": "sub/plan.md",
            },
        )
    assert r.status_code == 200
    resolved = captured["output_path"]
    assert resolved is not None
    assert str(resolved).startswith(str(tmp_path.resolve()))


@pytest.mark.asyncio
async def test_plans_generate_route_fallback_still_200(monkeypatch):
    app = create_app()

    def _fake_generate_plan(request, **kwargs):  # noqa: ANN001
        return MeetingPlan(
            markdown="# Fallback plan\n\n## Purpose\n- ok\n",
            provider_used="deterministic-fallback",
            model_used="n/a",
            generated_at_iso="2026-06-16T12:00:00+00:00",
        )

    monkeypatch.setattr("boardman.routes.plans.generate_plan", _fake_generate_plan)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/plans/generate",
            headers=AUTH,
            json={
                "meeting_title": "Weekly",
                "meeting_type": "weekly-status-sync",
                "team_focus": "qa",
                "write_to_disk": False,
            },
        )
    assert r.status_code == 200
    assert r.json()["provider_used"] == "deterministic-fallback"
