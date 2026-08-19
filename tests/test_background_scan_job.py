from __future__ import annotations

import pytest

from boardman.jobs.handlers import JOB_HANDLERS


@pytest.mark.asyncio
async def test_repo_scan_is_registered_as_a_background_job(monkeypatch) -> None:
    import boardman.database.session as db_session
    import boardman.services.scan_handler as scan_handler

    events: list[str] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    class FakeSessionFactory:
        def __call__(self):
            return FakeSession()

    async def fake_scan(session, repo, *, dry_run, provider, model):
        assert repo == "deepiri/boardman"
        assert dry_run is True
        assert provider == "ollama"
        assert model == "small"
        assert session is not None
        return {"ok": True, "created": 3}

    monkeypatch.setattr(db_session, "async_session", FakeSessionFactory())
    monkeypatch.setattr(scan_handler, "run_repo_scan", fake_scan)

    result = await JOB_HANDLERS["boardman_repo_scan_job"](
        {
            "repo": "deepiri/boardman",
            "dry_run": True,
            "provider": "ollama",
            "model": "small",
        }
    )

    assert result == {"ok": True, "created": 3}
    assert events == ["commit"]


@pytest.mark.asyncio
async def test_scan_route_can_enqueue_without_running_the_scan(monkeypatch) -> None:
    import boardman.routes.agent as agent_route
    import boardman.settings as bs

    class FakeQueue:
        async def enqueue_job(self, kind, payload):
            assert kind == "boardman_repo_scan_job"
            assert payload == {
                "repo": "deepiri/boardman",
                "dry_run": True,
                "provider": "ollama",
                "model": "small",
            }

            class Job:
                job_id = "scan-job-1"

            return Job()

    async def no_rate_limit(_request):
        return None

    monkeypatch.setattr(bs.settings, "agent_async_enqueue_enabled", True)
    monkeypatch.setattr(agent_route, "get_job_queue", lambda: FakeQueue())
    monkeypatch.setattr(agent_route, "require_agent_rate_limit", no_rate_limit)
    monkeypatch.setattr(
        agent_route,
        "run_repo_scan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("queued scan must not run in the request")
        ),
    )

    result = await agent_route.agent_scan(
        agent_route.ScanRequest(
            repo="deepiri/boardman",
            dry_run=True,
            provider="ollama",
            model="small",
            queue=True,
        ),
        request=object(),
        session=object(),
    )

    assert result == {"ok": True, "queued": True, "job_id": "scan-job-1"}
