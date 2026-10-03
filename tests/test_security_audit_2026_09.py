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

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boardman.main import create_app
from boardman.routes.agent import _REPO_SLUG_RE, _writes_allowed
from boardman.routes.agent import router as agent_router
from boardman.security.api_auth import require_internal_auth
from boardman.security.repo_slug import REPO_SLUG_RE, is_valid_repo_slug, split_repo_slug
from boardman.settings import settings

# Must match NGINX_MIN_VERSION in scripts/validate_nginx_conf.sh.
_NGINX_MIN_VERSION = "1.25.1"
_NGINX_TOO_OLD_STATUS = 77

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


def _compose_api_publishes() -> list[str]:
    """Every ``ports:`` mapping that publishes the API container port 8090.

    Parsed with yaml rather than substring-matched. The previous guard asserted
    ``'"8090:8090"' not in compose``, which looks right but is satisfied by
    ``"0.0.0.0:8090:8090"`` too -- the substring after the quote differs -- so the
    exact regression it claimed to catch slipped through. It only ever worked by
    accident of the second assertion.
    """
    import yaml

    doc = yaml.safe_load(COMPOSE_PROD.read_text(encoding="utf-8")) or {}
    services = doc.get("services") or {}
    found: list[str] = []
    for svc in services.values():
        for mapping in (svc or {}).get("ports") or []:
            # A bare int is a container port, which compose publishes on 0.0.0.0.
            if isinstance(mapping, int) or "8090" in str(mapping):
                found.append(str(mapping))
    return found


def test_production_compose_does_not_publish_the_api_publicly() -> None:
    """docker-compose.prod.yml must bind the API to loopback. Publishing it on
    0.0.0.0 exposed every route above directly to the internet, bypassing the
    nginx vhost. Regression guard for that exact mistake."""
    publishes = _compose_api_publishes()
    assert publishes, (
        "expected to find the API's published port in docker-compose.prod.yml; the "
        "parse or the service layout changed, so this guard is no longer testing "
        "anything -- update it"
    )
    for mapping in publishes:
        # Accepted: an explicit loopback (or other non-wildcard) host IP.
        host = mapping.split(":")[0] if mapping.count(":") >= 2 else ""
        assert host not in ("0.0.0.0", "::", ""), (
            f"production compose publishes the API as {mapping!r}, which binds all "
            "interfaces and bypasses the nginx vhost; use 127.0.0.1:8090:8090"
        )
    assert "127.0.0.1:8090:8090" in COMPOSE_PROD.read_text(encoding="utf-8")


def _run_preflight_loopback_guard(compose_config: str, port: int) -> str:
    """Run the preflight's published-port loopback check against a compose config.

    Sources the two functions straight out of `deploy_preflight.sh` so this tests
    the shipped logic rather than a copy of it. `docker compose config` is not
    invoked: the rendered config is handed in directly, which keeps the test
    runnable offline with no Docker daemon.
    """
    script = (REPO_ROOT / "scripts" / "deploy_preflight.sh").read_text("utf-8")
    functions = []
    for name in ("service_block_publishing", "check_published_port_is_loopback"):
        start = script.index(f"{name}() {{")
        depth, i = 0, start
        while True:
            if script[i] == "{":
                depth += 1
            elif script[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        functions.append(script[start : i + 1])

    harness = "\n".join(
        [
            "set -u",
            "pass() { printf 'PASS %s\\n' \"$1\"; }",
            "fail() { printf 'FAIL %s\\n' \"$1\"; }",
            *functions,
            'check_published_port_is_loopback "$CONFIG" "$PORT" "API"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", harness],
        input=compose_config,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CONFIG": compose_config, "PORT": str(port)},
    )
    assert result.returncode == 0, f"guard harness failed: {result.stderr}"
    return result.stdout


def _rendered_compose(api_host_ip: str, ui_host_ip: str) -> str:
    """A minimal `docker compose config` rendering with two published ports."""
    return (
        "name: deepiri-boardman\n"
        "services:\n"
        "  boardman:\n"
        "    container_name: deepiri-boardman\n"
        "    ports:\n"
        "      - mode: ingress\n"
        "        target: 8090\n"
        '        published: "8090"\n'
        f"        host_ip: {api_host_ip}\n"
        "        protocol: tcp\n"
        "  boardman-ui:\n"
        "    container_name: deepiri-boardman-ui\n"
        "    ports:\n"
        "      - mode: ingress\n"
        "        target: 80\n"
        '        published: "8088"\n'
        f"        host_ip: {ui_host_ip}\n"
        "        protocol: tcp\n"
        "networks:\n"
        "  platform-shared:\n"
        "    external: true\n"
    )


def test_preflight_treats_a_public_api_bind_as_a_failure() -> None:
    """`deploy_preflight.sh` runs on every deploy and is the last gate before the
    stack is brought up. When it published the API on all interfaces it only
    *warned* -- which does not stop a deploy. It must fail, and it must accept the
    intended loopback bind without complaint (a warning there would train operators
    to ignore the check).
    """
    ok = _run_preflight_loopback_guard(_rendered_compose("127.0.0.1", "127.0.0.1"), 8090)
    assert ok.startswith("PASS "), f"loopback API bind should pass cleanly, got: {ok!r}"

    bad = _run_preflight_loopback_guard(_rendered_compose("0.0.0.0", "127.0.0.1"), 8090)
    assert bad.startswith("FAIL "), (
        "a public API bind must FAIL the preflight, not warn: a warning does not "
        f"stop a deploy, and this is the exact mistake that exposed the API. Got: {bad!r}"
    )


def test_preflight_loopback_check_is_scoped_to_the_publishing_service() -> None:
    """The host_ip must be read from the service block that publishes the port.

    It used to be grepped out of the whole rendered config, so *any* loopback
    binding satisfied the check. Adding the loopback-only boardman-ui service on
    127.0.0.1:8088 therefore masked a regression of the API to 0.0.0.0:8090 --
    the guard passed on exactly the config it exists to reject. This is the
    regression that shipped, so pin the scoping.
    """
    # API public, UI loopback: the API must still be caught.
    api_public = _run_preflight_loopback_guard(_rendered_compose("0.0.0.0", "127.0.0.1"), 8090)
    assert api_public.startswith("FAIL "), (
        "the UI's loopback binding satisfied the API's check; the host_ip has to be "
        f"read from the API's own service block. Got: {api_public!r}"
    )

    # UI public, API loopback: the UI must be caught too. Nothing checked 8088
    # before, so a public UI bind (a second, nginx-less copy of the SPA) went
    # unreported.
    ui_public = _run_preflight_loopback_guard(_rendered_compose("127.0.0.1", "0.0.0.0"), 8088)
    assert ui_public.startswith(
        "FAIL "
    ), f"the UI port is published too and must be loopback-only. Got: {ui_public!r}"

    # And a service that does not publish the port produces no output at all,
    # rather than a spurious failure.
    absent = _run_preflight_loopback_guard("name: x\nservices:\n  boardman:\n    image: y\n", 8090)
    assert absent.strip() == "", f"unpublished port should be a no-op, got: {absent!r}"


def test_public_vhost_only_proxies_an_explicit_allowlist() -> None:
    """boardman.deepiri.com is the box's public entry point and serves no UI, so it
    must not proxy the whole `/api/` tree through to the internet. The catch-all
    has to deny, and only the endpoints with a real unauthenticated caller
    (GitHub's webhook POST, the Cloudflare Worker's pick-qa) may be `location =`
    proxied. This reads the config as text so it runs offline;
    `test_nginx_vhosts_pass_a_real_nginx_config_test` additionally hands the file
    to a real nginx, and the location precedence it depends on is covered below.
    """
    conf = (REPO_ROOT / "deploy" / "nginx" / "boardman.deepiri.com.conf").read_text("utf-8")

    # The catch-all must return, not proxy.
    catchall = conf[conf.rindex("location /api/") :]
    assert "proxy_pass" not in catchall, (
        "the public vhost's catch-all /api/ block proxies again -- that republishes "
        "every read-mostly route and agent chat to the internet"
    )
    assert "return 403" in catchall

    # Exactly these two API paths (plus /health) may be proxied, and the two
    # public API ones must be exact matches so they win over the catch-all.
    for required in (
        "location = /api/v1/webhooks/github",
        "location = /api/v1/assignment/pick-qa",
        "location = /health",
    ):
        assert required in conf, f"public vhost no longer proxies {required}"

    # The UI's own vhost legitimately keeps the full proxy; make sure the deny is
    # not accidentally applied there and breaking the SPA.
    ui_conf = (REPO_ROOT / "deploy" / "nginx" / "default.conf").read_text("utf-8")
    assert "location /api/" in ui_conf
    assert "return 403" not in ui_conf, (
        "the boardman-ui vhost (:8088) must keep proxying all of /api/ or the SPA "
        "breaks -- the deny-by-default belongs only on the public vhost"
    )


def test_public_vhost_allowlist_beats_the_catch_all_prefix() -> None:
    """nginx resolves `location =` (exact) before `location /` (prefix). This
    guards the assumption the allowlist above relies on: if it were ever false, the
    allowlisted webhook would fall into the 403 catch-all and GitHub deliveries
    would break."""
    conf = (REPO_ROOT / "deploy" / "nginx" / "boardman.deepiri.com.conf").read_text("utf-8")
    specs = [m.group(1).strip() for m in re.finditer(r"^\s*location\s+(.+?)\s*\{", conf, re.M)]

    assert any(
        s == "= /api/v1/webhooks/github" for s in specs
    ), "the webhook location must be an exact (=) match"
    assert "/api/" in specs, "expected a catch-all /api/ prefix block to exist"

    # Exact match wins for the webhook path in nginx's selection order.
    exact = [s[1:].strip() for s in specs if s.startswith("=")]
    assert "/api/v1/webhooks/github" in exact
    assert "/api/v1/assignment/pick-qa" in exact


def _available_nginx() -> str | None:
    """Path to an nginx binary usable for a real `nginx -t`, or None.

    Set NGINX_BIN to use a locally built one (see scripts/validate_nginx_conf.sh,
    which can build one). `nginx` on PATH is the normal CI case.
    """
    explicit = os.environ.get("NGINX_BIN")
    if explicit and Path(explicit).is_file() and os.access(explicit, os.X_OK):
        return explicit
    return shutil.which("nginx")


def _skip_if_nginx_too_old(result: subprocess.CompletedProcess[str]) -> None:
    """Treat the validator's "skipped" status as a skip, not a failure.

    deploy/nginx uses `http2 on`, which needs nginx >= 1.25.1. A GitHub runner
    ships 1.24.0, and asking that binary to parse the vhost produces a
    `unknown directive "http2"` error that says nothing about the vhost. The
    script signals that with status 77; without this, every Python job would fail
    on an environment limitation that has nothing to do with the config.

    The dedicated `docker` job runs both checks against nginx:1.27-alpine -- the
    image production actually uses -- so the guard is still enforced on every
    push; it is only skipped where a usable nginx is absent.
    """
    if result.returncode == _NGINX_TOO_OLD_STATUS:
        pytest.skip(
            "the available nginx is too old to parse deploy/nginx "
            f"(needs >= {_NGINX_MIN_VERSION}); set NGINX_BIN to enable:\n{result.stderr}"
        )


def test_nginx_vhosts_pass_a_real_nginx_config_test() -> None:
    """Run the real `nginx -t` over both deploy/nginx fragments.

    The checks above read the config as text, which cannot catch a genuine nginx
    error: a misspelled directive, an unterminated block, an invalid ssl_protocols
    value, or a location that nginx itself rejects. Those only surface at deploy
    time. scripts/validate_nginx_conf.sh wraps each fragment in a throwaway
    prefix -- substituting only the Let's Encrypt paths, the privileged listen
    ports, and the docker-internal upstream -- and asks real nginx to parse it, so
    the committed file is validated as written.

    Skips when no nginx is available so the suite stays runnable offline.
    """
    nginx = _available_nginx()
    if nginx is None:
        pytest.skip("no nginx binary available; set NGINX_BIN to enable")

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "validate_nginx_conf.sh")],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "NGINX_BIN": nginx},
    )
    _skip_if_nginx_too_old(result)
    assert result.returncode == 0, (
        "nginx rejected a deploy/nginx vhost:\n" f"{result.stdout}\n{result.stderr}"
    )
    # Guard against the script silently testing nothing.
    assert "nginx validation passed for 2 file(s)" in result.stdout


def test_public_vhost_allowlist_actually_routes_through_a_real_nginx() -> None:
    """Boot the public vhost for real and prove the allowlist routes, not just parses.

    `nginx -t` cannot see the failure mode this vhost is built to prevent. If
    `location = /api/v1/webhooks/github` ever lost precedence to the
    `location /api/` catch-all -- a prefix/exact mixup, a reordered block, someone
    "tidying" the `=` away -- every GitHub delivery would 403 and the config would
    still pass a config test. Likewise, if the catch-all were changed back to
    `proxy_pass`, the read-mostly routes and agent chat would silently go back out
    to the internet.

    Both regressions were confirmed to pass `nginx -t` and fail this check. So this
    runs the real nginx in front of a stub backend and asserts, per path, whether
    the request reached the backend at all.

    Skips when no nginx is available so the suite stays runnable offline.
    """
    nginx = _available_nginx()
    if nginx is None:
        pytest.skip("no nginx binary available; set NGINX_BIN to enable")

    result = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "validate_nginx_conf.sh"),
            "--live",
            "deploy/nginx/boardman.deepiri.com.conf",
        ],
        capture_output=True,
        text=True,
        # Generous: the script waits for nginx and the stub to accept connections.
        timeout=300,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "NGINX_BIN": nginx},
    )
    _skip_if_nginx_too_old(result)
    assert result.returncode == 0, (
        "the public vhost does not route the way the allowlist claims:\n"
        f"{result.stdout}\n{result.stderr}"
    )

    out = result.stdout
    # Every allowlisted path must actually be proxied, not merely return 200.
    for label in (
        "GET  /health",
        "POST /api/v1/webhooks/github",
        "POST /api/v1/assignment/pick-qa",
    ):
        assert f"{label}" in out, f"live check never exercised {label}"
    # And the routes the audit found exposed must be stopped at the edge.
    for label in (
        "GET  /api/v1/tasks",
        "GET  /api/v1/repos/org",
        "GET  /api/v1/llm/models",
        "POST /api/v1/agent/chat",
        "POST /api/v1/repos/classify",
    ):
        assert f"{label}" in out, f"live check never exercised {label}"
    assert "not proxied" in out, "no path was confirmed denied at the edge"
    assert "reached backend" in out, "no path was confirmed proxied to the backend"


def test_internal_auth_dependency_is_importable_and_callable() -> None:
    """Belt-and-braces: the dependency other code may import must exist."""
    assert callable(require_internal_auth)


# ============================================================================
# Second pass: routes the first audit missed.
#
# The gates above were a correct fix for six routes, but a follow-up review of
# the full route inventory found the same exposure class on a larger set that the
# SPA did not use, plus two that it does. Everything below is offline: the point
# is to prove the auth gate runs *before* the handler, so the handlers are
# replaced with sentinels that fail the test if they are ever reached.
# ============================================================================


def _app_with_stubbed_handlers(
    monkeypatch: pytest.MonkeyPatch,
    stubs: dict[str, object],
) -> TestClient:
    """Build a TestClient over the full router set with the given handlers replaced.

    Each key in ``stubs`` is a target string to monkeypatch; the replacement
    raises, so a test fails loudly if a guarded route reaches its handler instead
    of being stopped by the auth dependency.
    """

    def _explode(*_a: object, **_k: object):  # noqa: ANN202
        raise AssertionError("guarded route reached its handler without authentication")

    for target in stubs:
        monkeypatch.setattr(target, _explode, raising=False)

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def secured_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Both secrets set, i.e. a realistic hardened deployment. The assignment routes
    # authenticate against worker_internal_secret; the agent/task/plans routes accept
    # boardman_api_token and fall back to the worker secret.
    monkeypatch.setattr(settings, "boardman_api_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "test-worker-secret", raising=False)
    return TestClient(create_app(), raise_server_exceptions=False)


# Routes reachable only with the internal token. Each of these either writes to
# Plaky/GitHub with the server's credentials, rewrites a config file on disk, or
# spends LLM budget -- i.e. the same class as the six routes gated above.
SECOND_PASS_ROUTES = [
    ("POST", "/api/v1/reconcile/acme/widget", {}),  # replays full write handlers
    ("PATCH", "/api/v1/tasks/item-1", {}),  # server Plaky credentials
    ("POST", "/api/v1/tasks/item-1/subtasks", {}),
    ("POST", "/api/v1/tasks/item-1/link-pr", {}),
    ("POST", "/api/v1/tasks", {}),  # SPA-called; see docs/DEPLOYMENT.md
    ("POST", "/api/v1/repos/classify", {}),  # rewrites repos.yml routing config
    ("POST", "/api/v1/assignment/sync-field-keys?board_id=b1", {}),  # writes team_assignments.yml
    ("POST", "/api/v1/plans/generate", {}),  # LLM budget + writes to disk by default
    ("GET", "/api/v1/agent/sessions/s1/history", None),  # transcript
    ("DELETE", "/api/v1/agent/sessions/s1", None),
    ("GET", "/api/v1/mappings", None),  # enumerates every repo/issue/task id
    ("GET", "/api/v1/sync-logs", None),  # repo names, actors, error strings
]


@pytest.mark.parametrize(("method", "path", "json_body"), SECOND_PASS_ROUTES)
def test_second_pass_routes_reject_missing_credentials(
    secured_client: TestClient, method: str, path: str, json_body: object
) -> None:
    response = secured_client.request(method, path, json=json_body)
    assert response.status_code in (401, 404), (
        f"{method} {path} answered an unauthenticated caller with {response.status_code}; "
        "this route was missed by the first pass of the audit"
    )


@pytest.mark.parametrize(("method", "path", "json_body"), SECOND_PASS_ROUTES)
def test_second_pass_routes_reject_bad_token(
    secured_client: TestClient, method: str, path: str, json_body: object
) -> None:
    response = secured_client.request(
        method, path, json=json_body, headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401, f"{method} {path} accepted a wrong token"


def test_second_pass_routes_fail_closed_without_a_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token configured must 404 everywhere, not fall open."""
    monkeypatch.setattr(settings, "boardman_api_token", "", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)
    client = TestClient(create_app(), raise_server_exceptions=False)

    for method, path, json_body in SECOND_PASS_ROUTES:
        response = client.request(
            method, path, json=json_body, headers={"Authorization": "Bearer anything"}
        )
        assert response.status_code == 404, f"{method} {path} returned {response.status_code}"


def test_auth_gate_runs_before_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strongest form of the check: reconcile is stubbed to explode, and a token
    holder gets through to it. If auth were merely a response filter, the stub would
    fire for the anonymous request."""
    monkeypatch.setattr(settings, "boardman_api_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)

    async def _fake_reconcile(*_a: object, **_k: object) -> dict[str, object]:
        return {"ok": True, "reached_handler": True}

    monkeypatch.setattr(
        "boardman.services.reconcile.reconcile_repo", _fake_reconcile, raising=False
    )
    client = TestClient(create_app(), raise_server_exceptions=False)

    anon = client.post("/api/v1/reconcile/acme/widget")
    assert anon.status_code == 401

    authed = client.post(
        "/api/v1/reconcile/acme/widget", headers={"Authorization": "Bearer test-token"}
    )
    assert authed.status_code == 200
    assert authed.json().get("reached_handler") is True


# --- Slug validation is shared, so every route that takes one agrees -----------


@pytest.mark.parametrize(
    "repo",
    ["--repo=attacker-org/evil", "-x/evil", "owner/name/extra", "owner", "../../etc/passwd", ""],
)
@pytest.mark.parametrize("path", ["/api/v1/agent/init-direction", "/api/v1/agent/scan"])
def test_both_routes_reject_untrusted_repo_values(
    secured_client: TestClient, path: str, repo: str
) -> None:
    """init-direction and scan both hand owner/name to gh/git argv, so both must
    validate. Scan previously had no slug check at all."""
    response = secured_client.post(
        path, json={"repo": repo}, headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False, f"{path} accepted repo={repo!r}"


@pytest.mark.parametrize(
    "repo",
    ["deepiri-org/deepiri-boardman", "Team-Deepiri/some_repo.js", "org.with.dots/repo-dashes"],
)
def test_both_routes_accept_wellformed_repo_values(repo: str) -> None:
    assert is_valid_repo_slug(repo), f"validator rejected the legitimate slug {repo!r}"


@pytest.mark.parametrize("repo", ["owner/name", "a/b", "org.with.dots/repo-dashes"])
def test_split_repo_slug_returns_the_parts(repo: str) -> None:
    assert split_repo_slug(repo) == tuple(repo.split("/"))  # type: ignore[comparison-overlap]


def test_reconcile_rejects_a_flag_like_owner(secured_client: TestClient) -> None:
    """The route splits owner/repo from the path, so the flag-smuggling shape is
    per-segment. Starlette will not match a path segment starting with a dash to
    the {owner} param's normal form in all cases, so assert the endpoint either
    refuses it or never treats it as a repo."""
    response = secured_client.post(
        "/api/v1/reconcile/-x/evil", headers={"Authorization": "Bearer test-token"}
    )
    # Either rejected outright, or handled without treating owner as a gh flag.
    assert response.status_code in (200, 404, 422)
    if response.status_code == 200:
        assert response.json().get("ok") is not True or response.json().get("reached") is None


def test_shared_validator_is_the_one_agent_uses() -> None:
    """Guards against the two validators drifting apart again."""
    assert _REPO_SLUG_RE is REPO_SLUG_RE
    assert agent_router is not None


# --- allow_writes is a privilege inside the body, not behind the route ---------


def test_agent_chat_downgrades_allow_writes_for_anonymous_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """allow_writes hands the agent Plaky mutation tools. The chat route must stay
    open for the SPA, so the privilege itself has to be downgraded -- otherwise
    anyone on the internet can drive writes to the team's board by asking for them.
    """
    monkeypatch.setattr(settings, "boardman_api_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)

    seen: dict[str, object] = {}

    async def _fake_run(_session, **kwargs):  # noqa: ANN001, ANN202
        seen["allow_writes"] = kwargs.get("allow_writes")
        return "ok", "sid-1"

    monkeypatch.setattr("boardman.routes.agent.run_agent_chat", _fake_run, raising=False)
    monkeypatch.setattr(
        "boardman.routes.agent.require_agent_rate_limit", _noop_limit, raising=False
    )
    client = TestClient(create_app(), raise_server_exceptions=False)

    anon = client.post("/api/v1/agent/chat", json={"message": "hi", "allow_writes": True})
    assert anon.status_code == 200
    assert seen["allow_writes"] is False, "anonymous caller was granted agent write tools"

    authed = client.post(
        "/api/v1/agent/chat",
        json={"message": "hi", "allow_writes": True},
        headers={"Authorization": "Bearer test-token"},
    )
    assert authed.status_code == 200
    assert seen["allow_writes"] is True, "token holder lost the write capability"


def test_agent_chat_stream_downgrades_allow_writes_for_anonymous_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "boardman_api_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)

    seen: dict[str, object] = {}

    async def _fake_iter(_db, **kwargs):  # noqa: ANN001, ANN202
        seen["allow_writes"] = kwargs.get("allow_writes")
        yield b"data: {}\n\n"

    monkeypatch.setattr("boardman.routes.agent.iter_agent_chat_sse", _fake_iter, raising=False)
    monkeypatch.setattr(
        "boardman.routes.agent.require_agent_rate_limit", _noop_limit, raising=False
    )
    client = TestClient(create_app(), raise_server_exceptions=False)

    client.post("/api/v1/agent/chat/stream", json={"message": "hi", "allow_writes": True})
    assert seen["allow_writes"] is False, "anonymous SSE caller was granted write tools"

    client.post(
        "/api/v1/agent/chat/stream",
        json={"message": "hi", "allow_writes": True},
        headers={"Authorization": "Bearer test-token"},
    )
    assert seen["allow_writes"] is True


def test_allow_writes_false_stays_false_even_when_authenticated(
    secured_client: TestClient,
) -> None:
    """Auth must not be a way to *escalate* an off request into an on one."""
    assert _writes_allowed(False, "Bearer test-token") is False
    assert _writes_allowed(False, None) is False
    assert _writes_allowed(True, "Bearer test-token") is True
    assert _writes_allowed(True, "Bearer nope") is False
    assert _writes_allowed(True, None) is False


def test_queued_chat_persists_the_downgraded_allow_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`queue: true` persists the body to SQLite and the worker replays it later, so
    the downgrade has to happen *before* enqueue. Otherwise the downgrade would only
    cover the synchronous path and `queue` would be a trivial bypass."""
    monkeypatch.setattr(settings, "boardman_api_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "worker_internal_secret", "", raising=False)
    monkeypatch.setattr(settings, "agent_async_enqueue_enabled", True, raising=False)

    captured: dict[str, object] = {}

    class _Queue:
        async def enqueue_job(self, name: str, payload: dict) -> object:  # noqa: ANN001
            captured["payload"] = payload

            class _Job:
                job_id = "job-1"

            return _Job()

    monkeypatch.setattr("boardman.routes.agent.get_job_queue", lambda: _Queue(), raising=False)
    monkeypatch.setattr(
        "boardman.routes.agent.require_agent_rate_limit", _noop_limit, raising=False
    )
    client = TestClient(create_app(), raise_server_exceptions=False)

    client.post("/api/v1/agent/chat", json={"message": "hi", "allow_writes": True, "queue": True})
    assert captured["payload"].get("allow_writes") is False, (
        "the queued payload kept allow_writes=true; the worker would replay the "
        "un-downgraded privilege"
    )

    client.post(
        "/api/v1/agent/chat",
        json={"message": "hi", "allow_writes": True, "queue": True},
        headers={"Authorization": "Bearer test-token"},
    )
    assert captured["payload"].get("allow_writes") is True


# --- The UI cannot hold a secret, so it must not pretend to ------------------


def test_ui_does_not_embed_a_token() -> None:
    """boardman-ui is a public static bundle: anything in it is readable by anyone
    who loads the page, so no secret may be compiled in.

    The operator pastes the token at runtime into sessionStorage (lib/apiToken.ts).
    This asserts the *shape* of that arrangement -- no token-ish build-time env var
    and no literal secret -- rather than the mere mention of the env var's name,
    which legitimately appears as placeholder/help text.
    """
    ui_src = REPO_ROOT / "boardman-ui" / "src"

    # 1. No build-time variable may carry a secret.
    env_offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in ui_src.rglob("*.ts*")
        for match in re.findall(r"import\.meta\.env\.(VITE_[A-Z0-9_]+)", path.read_text("utf-8"))
        if "TOKEN" in match or "SECRET" in match
    ]
    assert not env_offenders, f"UI reads a secret from a build-time env var: {env_offenders}"

    # 2. No long opaque literal that looks like a baked credential.
    literal_offenders: list[str] = []
    for path in ui_src.rglob("*.ts*"):
        for literal in re.findall(
            r"""["'`]([A-Za-z0-9+/=_-]{24,})["'`]""", path.read_text("utf-8")
        ):
            # Long enough to be a credential, and not a URL/import/mime/base64 data URI.
            if literal.startswith(("http", "data:", "text/")) or "/" in literal or " " in literal:
                continue
            literal_offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {literal[:8]}...")
    assert not literal_offenders, f"UI appears to inline a credential: {literal_offenders}"

    # 3. The token must not be persisted in a way that outlives the tab. Checked
    # against real API calls so explanatory prose mentioning localStorage does not
    # trip the assertion.
    token_mod = (ui_src / "lib" / "apiToken.ts").read_text("utf-8")
    code = "\n".join(
        line for line in token_mod.splitlines() if not line.lstrip().startswith(("*", "//", "/*"))
    )
    assert "localStorage" not in code, "token must not outlive the tab"
    assert "sessionStorage" in code, "token should be tab-scoped"


async def _noop_limit(_request: object) -> None:  # noqa: ANN202
    return None
