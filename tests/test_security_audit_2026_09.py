"""Regression tests for the 2026-09 security audit.

These cover three defects found by auditing the production deployment:

1. CWE-306 -- privileged agent routes (init-direction, scan, job status, BYOK)
   had no authentication at all, and the API was published on 0.0.0.0:8090, so
   they were reachable anonymously from the internet.
2. CWE-88 -- ``init-direction`` passed an unvalidated ``owner`` straight into
   ``gh``/``git`` argv, where a leading "-" is parsed as a flag.
3. The production compose file bound the API to a public interface, bypassing
   the nginx vhost that is meant to be the only entry point.

Everything here is local and offline: no network, no database, no Plaky/GitHub
calls. The privileged handlers are exercised only far enough to prove the auth
gate runs before them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boardman.routes.agent import _REPO_SLUG_RE
from boardman.routes.agent import router as agent_router
from boardman.security.api_auth import require_internal_auth
from boardman.settings import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PROD = REPO_ROOT / "docker-compose.prod.yml"

PRIVILEGED_ROUTES = [
    ("POST", "/api/v1/agent/init-direction"),
    ("POST", "/api/v1/agent/scan"),
    ("GET", "/api/v1/agent/jobs/some-job-id"),
    ("POST", "/api/v1/agent/sessions/some-session/byok"),
    ("DELETE", "/api/v1/agent/sessions/some-session/byok"),
    ("GET", "/api/v1/agent/sessions/some-session/byok"),
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A minimal app mounting only the agent router, so the tests never reach the
    real database or the agent/LLM stack behind the other routers."""
    monkeypatch.setattr(settings, "boardman_api_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)

    app = FastAPI()
    app.include_router(agent_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


# --- CWE-306: privileged routes must not answer anonymous callers --------------


@pytest.mark.parametrize(("method", "path"), PRIVILEGED_ROUTES)
def test_privileged_route_rejects_missing_credentials(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path, json={})
    assert response.status_code == 401, (
        f"{method} {path} answered an unauthenticated caller with "
        f"{response.status_code}; the auth gate is not running"
    )


@pytest.mark.parametrize(("method", "path"), PRIVILEGED_ROUTES)
@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token",
        "Bearer ",  # empty token
        "Bearer test-token-extra",  # valid prefix, wrong length
        "test-token",  # missing scheme
        "Basic dGVzdC10b2tlbg==",  # wrong scheme
        "Bearer test token",  # internal space splits the token
    ],
)
def test_privileged_route_rejects_bad_token(
    client: TestClient, method: str, path: str, header: str
) -> None:
    response = client.request(method, path, json={}, headers={"Authorization": header})
    assert response.status_code == 401, f"{method} {path} accepted {header!r}"


@pytest.mark.parametrize("header", ["Bearer test-token", "bearer test-token", "Bearer  test-token"])
def test_internal_auth_tolerates_scheme_case_and_whitespace(
    client: TestClient, header: str
) -> None:
    """Whitespace after the scheme and scheme casing are HTTP framing, not
    credentials, so they are normalised rather than rejected. This is not a
    bypass: the token itself must still match exactly."""
    response = client.post(
        "/api/v1/agent/init-direction",
        json={"repo": "not-a-slug"},
        headers={"Authorization": header},
    )
    assert response.status_code == 200, f"{header!r} was not accepted as well-formed"
    assert response.json()["ok"] is False, "request should have reached the handler"


def test_init_direction_accepts_the_configured_token(client: TestClient) -> None:
    """The correct token must get past the gate. We assert on the *rejection* of
    the repo value, not on a PR being opened -- direction_init shells out to `gh`
    and this test must not touch GitHub."""
    response = client.post(
        "/api/v1/agent/init-direction",
        json={"repo": "not-a-slug"},
        headers={"Authorization": "Bearer test-token"},
    )
    # Auth passed, so we are now past 401/404 and inside the handler.
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_privileged_routes_fail_closed_without_a_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that forgets to set the token must 404, not serve openly."""
    monkeypatch.setattr(settings, "boardman_api_token", "", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)

    app = FastAPI()
    app.include_router(agent_router, prefix="/api/v1")
    client = TestClient(app, raise_server_exceptions=False)

    for method, path in PRIVILEGED_ROUTES:
        response = client.request(
            method, path, json={}, headers={"Authorization": "Bearer anything"}
        )
        assert response.status_code == 404, (
            f"{method} {path} returned {response.status_code} with no secret configured; "
            "it must fail closed"
        )


# --- CWE-88: owner/name must not be able to smuggle a flag into gh/git argv ----


@pytest.mark.parametrize(
    "repo",
    [
        "--repo=attacker-org/evil",  # leading dash -> read as a gh flag
        "-x/evil",
        "--help/evil",
        "owner/--upload-pack=touch /tmp/pwn",  # whitespace + shell metachars
        "owner/name/extra",  # not exactly one slash
        "owner",  # no slash
        "/name",  # empty owner
        "owner/",  # empty name
        "../../etc/passwd",  # traversal
        "owner/name;id",  # shell metacharacter
        "owner/na me",  # whitespace
        "",
    ],
)
def test_init_direction_rejects_untrusted_repo_values(client: TestClient, repo: str) -> None:
    response = client.post(
        "/api/v1/agent/init-direction",
        json={"repo": repo},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False, f"repo={repo!r} was accepted by init-direction"
    assert "owner/name" in body["message"]


@pytest.mark.parametrize(
    "repo",
    [
        "deepiri-org/deepiri-boardman",
        "Team-Deepiri/some_repo.js",
        "org.with.dots/repo-with-dashes",
        "a/b",
    ],
)
def test_init_direction_accepts_wellformed_repo_values(client: TestClient, repo: str) -> None:
    """Well-formed slugs must clear validation. Asserted against the regex
    directly so no `gh` process is ever spawned."""
    assert _REPO_SLUG_RE.match(repo), f"regex rejected the legitimate slug {repo!r}"


# --- The public interface that made the above reachable from the internet ------


def test_production_compose_does_not_publish_the_api_publicly() -> None:
    """docker-compose.prod.yml must bind the API to loopback. Publishing it on
    0.0.0.0 exposed every route above directly to the internet, bypassing the
    nginx vhost. Regression guard for that exact mistake."""
    compose = COMPOSE_PROD.read_text(encoding="utf-8")
    assert (
        '"8090:8090"' not in compose
    ), "production compose publishes 8090 on all interfaces; bind 127.0.0.1:8090:8090"
    assert '"127.0.0.1:8090:8090"' in compose


def test_internal_auth_dependency_is_importable_and_callable() -> None:
    """Belt-and-braces: the dependency other code may import must exist."""
    assert callable(require_internal_auth)
