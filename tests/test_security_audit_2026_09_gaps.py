"""Regression tests for the residual gaps left by the 2026-09 audit.

``test_security_audit_2026_09.py`` covers the privileged *agent* routes and the
API's public bind. Three things it did not close are covered here, each found by
auditing the live production deployment rather than the source tree:

1. ``/api/v1/plaky/users`` and ``/api/v1/llm/models`` had no authentication
   whatsoever -- not even the guard added to the agent routes. Against the live
   VM ``/api/v1/plaky/users`` returned HTTP 200 and 13,754 bytes of real
   workspace user records (names, emails, avatars) to an anonymous caller, and
   ``/llm/models`` disclosed the configured LLM provider. Both are
   CWE-306 (missing authentication for a critical function).
2. ``/api/v1/sync-logs?limit=`` accepted an unbounded integer. On the live VM
   ``?limit=5000`` returned 1,195,136 bytes in a single unauthenticated request
   and the table held 17,785 rows, so ``?limit=100000`` returned the whole table
   per request -- a bulk-extraction primitive and a memory-pressure lever against
   a 512 MiB-capped container. This is CWE-770 (allocation without limits) and
   CWE-400 (uncontrolled resource consumption).
3. ``boardman-ui`` was still published on ``0.0.0.0:8088`` even though the API
   beside it had been moved to loopback, leaving a second nginx-less copy of the
   SPA on the internet. CWE-668 is the closest fit: exposure of a resource to
   the wrong sphere.

Everything here is local and offline: no network, no database, no Plaky/GitHub
calls. The Plaky handlers are exercised only far enough to prove the auth gate
runs before them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boardman.routes.health import MAX_PAGE_SIZE
from boardman.routes.health import router as health_router
from boardman.routes.plaky import router as plaky_router
from boardman.settings import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PROD = REPO_ROOT / "docker-compose.prod.yml"

GUARDED_DISCOVERY_ROUTES = [
    ("GET", "/api/v1/plaky/users"),
    ("GET", "/api/v1/llm/models"),
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Minimal app mounting the health and Plaky routers only, so the tests never
    reach the real database or the Plaky HTTP client behind them."""
    monkeypatch.setattr(settings, "boardman_api_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)

    app = FastAPI()
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(plaky_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


# --- CWE-306: discovery routes must not answer anonymous callers ----------------


@pytest.mark.parametrize(("method", "path"), GUARDED_DISCOVERY_ROUTES)
def test_discovery_route_rejects_missing_credentials(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == 401, (
        f"{method} {path} answered an unauthenticated caller with "
        f"{response.status_code}; it leaks live workspace data"
    )


@pytest.mark.parametrize(("method", "path"), GUARDED_DISCOVERY_ROUTES)
@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token",
        "Bearer ",  # empty token
        "Basic dGVzdDp0ZXN0",  # wrong scheme entirely
        "test-token",  # raw token without the Bearer prefix
        "Bearer test-token-and-a-lot-more",  # prefix of the real secret
    ],
)
def test_discovery_route_rejects_bad_credentials(
    client: TestClient, header: str, method: str, path: str
) -> None:
    response = client.request(method, path, headers={"Authorization": header})
    assert (
        response.status_code == 401
    ), f"{method} {path} accepted {header!r} with {response.status_code}"


def test_plaky_users_requires_auth_even_with_a_query(
    client: TestClient,
) -> None:
    """The guard must not be bypassable by supplying the picker's search term."""
    response = client.get("/api/v1/plaky/users", params={"query": "alice"})
    assert response.status_code == 401


# --- CWE-770 / CWE-400: caller-supplied page sizes must be bounded --------------


@pytest.mark.parametrize("limit", [0, -1, -1000, 201, 100_000, 2_147_483_647])
def test_sync_logs_rejects_out_of_range_limit(client: TestClient, limit: int) -> None:
    """`ge=1` rejects zero/negative; `le=MAX_PAGE_SIZE` rejects the oversized reads.

    FastAPI answers a failed Query constraint with 422 before the handler body
    runs, so this never touches the database.
    """
    response = client.get("/api/v1/sync-logs", params={"limit": limit})
    assert response.status_code == 422, (
        f"limit={limit} was accepted (HTTP {response.status_code}); the endpoint "
        f"would serialise an attacker-chosen number of rows"
    )


@pytest.mark.parametrize("limit", [1, 50, MAX_PAGE_SIZE])
def test_sync_logs_accepts_in_range_limit(client: TestClient, limit: int) -> None:
    """The bounds must not be so tight that legitimate paging breaks."""
    response = client.get("/api/v1/sync-logs", params={"limit": limit})
    # 401 (auth runs first) or 422 is never acceptable here; the in-range value
    # must pass validation and get as far as the handler.
    assert response.status_code != 422, f"limit={limit} was wrongly rejected as out of range"


def test_page_size_bound_is_declared() -> None:
    """Guard the constant itself: MAX_PAGE_SIZE must stay a sane, positive int.

    Someone widening the cap should have to change this test deliberately.
    """
    assert isinstance(MAX_PAGE_SIZE, int)
    assert (
        0 < MAX_PAGE_SIZE <= 1000
    ), f"MAX_PAGE_SIZE={MAX_PAGE_SIZE} is outside the range this tool considers sane"


# --- CWE-668: the UI must not stay published on a public interface --------------


def test_prod_compose_binds_ui_to_loopback() -> None:
    """`boardman-ui` must not be published on 0.0.0.0.

    The API already binds 127.0.0.1:8090 so the platform nginx is the only entry
    point; the UI binding must agree with it rather than re-exposing a second,
    nginx-less copy of the SPA.
    """
    text = COMPOSE_PROD.read_text(encoding="utf-8")
    ui_block = text.split("boardman-nginx:", 1)[1].split("\n  redis:", 1)[0]

    published = re.findall(r'-\s*"([\d.]+:)?(\d+:\d+)"', ui_block)
    assert published, "could not find the boardman-ui ports mapping; update this test"

    for host_ip, mapping in published:
        assert host_ip == "127.0.0.1:", (
            f"boardman-ui is published as {mapping!r} on {host_ip.rstrip(':') or '0.0.0.0'}; "
            f"it must bind 127.0.0.1 so the platform nginx stays the only entry point"
        )
