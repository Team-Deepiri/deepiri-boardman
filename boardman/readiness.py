"""Offline deployment readiness checks for the standalone Boardman repo."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PASS = "pass"
WARN = "warn"
FAIL = "fail"
PENDING = "pending"

REQUIRED_ENV_KEYS = (
    "PLAKY_API_KEY",
    "GITHUB_PAT",
    "GITHUB_WEBHOOK_SECRET",
)

SECURITY_ENV_KEYS = (
    "WORKER_INTERNAL_SECRET",
    "ROUTE_SECRET",
)

DEPLOYMENT_DECISION_KEYS = (
    "BOARDMAN_TARGET_ENV",
    "GITHUB_AUTH_MODE",
    "BOARDMAN_PUBLIC_URL",
)

WEBHOOK_EVENTS = {
    "issues",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
}

VALID_GITHUB_AUTH_MODES = {"pat", "github_app", "both"}

PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "change_me",
    "replace_me",
    "todo",
    "tbd",
    "none",
    "null",
    "your-token",
    "your-secret",
}

TEAM_ASSIGNMENT_PLACEHOLDERS = {
    "person-1",
    "person-2",
    "tag-1",
    "tag-2",
    "your-github-login",
}


@dataclass(frozen=True)
class ReadinessCheck:
    area: str
    name: str
    status: str
    detail: str
    next_step: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "area": self.area,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "next_step": self.next_step,
        }


@dataclass(frozen=True)
class ReadinessReport:
    repo_root: str
    env_file: str
    compose_file: str
    checks: list[ReadinessCheck]

    @property
    def passed(self) -> int:
        return sum(1 for check in self.checks if check.status == PASS)

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == WARN)

    @property
    def failures(self) -> int:
        return sum(1 for check in self.checks if check.status == FAIL)

    @property
    def pending(self) -> int:
        return sum(1 for check in self.checks if check.status == PENDING)

    @property
    def ready_for_go_live(self) -> bool:
        return self.failures == 0 and self.pending == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "env_file": self.env_file,
            "compose_file": self.compose_file,
            "summary": {
                "pass": self.passed,
                "warn": self.warnings,
                "fail": self.failures,
                "pending": self.pending,
                "ready_for_go_live": self.ready_for_go_live,
            },
            "checks": [check.to_dict() for check in self.checks],
        }


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-like file without exporting or logging secret values."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _clean_env_value(value.strip())
        if key:
            out[key] = value.strip()
    return out


def build_readiness_report(
    repo_root: Path | str,
    env_file: Path | str = ".env",
    compose_file: Path | str = "docker-compose.prod.yml",
    repos_file: Path | str = "repos.yml",
    team_assignments_file: Path | str = "team_assignments.yml",
    database_file: Path | str = "boardman.db",
) -> ReadinessReport:
    root = Path(repo_root).resolve()
    env_path = _resolve(root, env_file)
    compose_path = _resolve(root, compose_file)
    repos_path = _resolve(root, repos_file)
    team_path = _resolve(root, team_assignments_file)
    db_path = _resolve(root, database_file)
    env = load_env_file(env_path)

    checks: list[ReadinessCheck] = []
    checks.extend(_check_repo_layout(root, env_path, compose_path))
    checks.extend(_check_env(env_path, env))
    checks.extend(_check_deployment_decisions(env))
    checks.extend(_check_compose(compose_path))
    checks.extend(_check_repos_yml(repos_path))
    checks.extend(_check_team_assignments(team_path))
    checks.extend(_check_database(db_path))
    checks.extend(_check_runtime_smoke_guidance())

    return ReadinessReport(
        repo_root=str(root),
        env_file=str(env_path),
        compose_file=str(compose_path),
        checks=checks,
    )


def _resolve(root: Path, path: Path | str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else root / p


def _strip_inline_comment(value: str) -> str:
    marker = " #"
    idx = value.find(marker)
    if idx >= 0:
        return value[:idx].rstrip()
    return value


def _clean_env_value(value: str) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        end = value.find(quote, 1)
        if end >= 0:
            return value[1:end]
        return value[1:]
    return _strip_inline_comment(value)


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.strip()
    low = normalized.lower()
    return (
        low in PLACEHOLDER_VALUES
        or low.startswith("your_")
        or low.startswith("your-")
        or "_here" in low
        or low.startswith("<")
        or low.endswith(">")
    )


def _boolish_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "done", "rotated"}


def _check_repo_layout(root: Path, env_path: Path, compose_path: Path) -> list[ReadinessCheck]:
    checks = [
        _file_check("repo", "Dockerfile", root / "Dockerfile"),
        _file_check("repo", "production compose", compose_path),
        _file_check("repo", "preflight script", root / "scripts" / "deploy_preflight.sh"),
        _file_check("repo", "smoke script", root / "scripts" / "deploy_smoke.sh"),
    ]
    if env_path.is_file():
        checks.append(
            ReadinessCheck("env", "runtime env file", PASS, f"{env_path.name} exists")
        )
    else:
        checks.append(
            ReadinessCheck(
                "env",
                "runtime env file",
                FAIL,
                f"{env_path.name} is missing",
                "Copy .env.production.example to .env on the target host and fill it there.",
            )
        )
    return checks


def _file_check(area: str, name: str, path: Path) -> ReadinessCheck:
    if path.is_file():
        return ReadinessCheck(area, name, PASS, f"{path.name} found")
    return ReadinessCheck(area, name, FAIL, f"{path.name} missing", f"Restore {path}")


def _check_env(env_path: Path, env: dict[str, str]) -> list[ReadinessCheck]:
    checks: list[ReadinessCheck] = []
    if not env_path.is_file():
        return checks

    for key in REQUIRED_ENV_KEYS:
        checks.append(_env_key_check("env", key, env.get(key), required=True))
    for key in SECURITY_ENV_KEYS:
        checks.append(_env_key_check("security", key, env.get(key), required=False))

    if _boolish_true(env.get("BOARDMAN_SECRETS_ROTATED")):
        checks.append(
            ReadinessCheck(
                "security",
                "credential rotation gate",
                PASS,
                "BOARDMAN_SECRETS_ROTATED is true",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                "security",
                "credential rotation gate",
                PENDING,
                "exposed/shared credentials must be rotated before deployment",
                "Rotate Plaky, GitHub, webhook, worker, route, and cloud tokens; then "
                "set BOARDMAN_SECRETS_ROTATED=true.",
            )
        )

    events = _split_csv(env.get("GITHUB_WEBHOOK_EVENTS", ""))
    missing_events = sorted(WEBHOOK_EVENTS - set(events))
    if not events:
        checks.append(
            ReadinessCheck(
                "github",
                "webhook events",
                PENDING,
                "GITHUB_WEBHOOK_EVENTS is not documented in the env file",
                "Confirm GitHub webhook events: issues, pull_request, pull_request_review, "
                "pull_request_review_comment, issue_comment.",
            )
        )
    elif missing_events:
        checks.append(
            ReadinessCheck(
                "github",
                "webhook events",
                WARN,
                f"missing event marker(s): {', '.join(missing_events)}",
                "Update GITHUB_WEBHOOK_EVENTS after the GitHub webhook is configured.",
            )
        )
    else:
        checks.append(
            ReadinessCheck("github", "webhook events", PASS, "required webhook events are listed")
        )
    return checks


def _env_key_check(area: str, key: str, value: str | None, required: bool) -> ReadinessCheck:
    if _is_placeholder(value):
        status = FAIL if required else PENDING
        return ReadinessCheck(
            area,
            key,
            status,
            "missing or placeholder value",
            f"Set {key} in the target environment; do not commit the real value.",
        )
    return ReadinessCheck(area, key, PASS, "set without exposing value")


def _check_deployment_decisions(env: dict[str, str]) -> list[ReadinessCheck]:
    checks: list[ReadinessCheck] = []
    for key in DEPLOYMENT_DECISION_KEYS:
        value = (env.get(key) or "").strip()
        if value:
            checks.append(ReadinessCheck("deployment", key, PASS, f"{key} is recorded"))
        else:
            checks.append(
                ReadinessCheck(
                    "deployment",
                    key,
                    PENDING,
                    f"{key} is not recorded",
                    "Confirm this with Joe/Kyle before go-live.",
                )
            )

    auth_mode = (env.get("GITHUB_AUTH_MODE") or "").strip().lower()
    if auth_mode and auth_mode not in VALID_GITHUB_AUTH_MODES:
        checks.append(
            ReadinessCheck(
                "github",
                "auth mode value",
                FAIL,
                f"unsupported GITHUB_AUTH_MODE={auth_mode!r}",
                "Use one of: pat, github_app, both.",
            )
        )
    elif auth_mode in {"github_app", "both"}:
        checks.append(
            ReadinessCheck(
                "github",
                "auth mode implementation",
                WARN,
                "current runtime path is PAT-first; GitHub App mode needs explicit "
                "implementation confirmation",
            )
        )
    elif auth_mode == "pat":
        checks.append(
            ReadinessCheck(
                "github",
                "auth mode implementation",
                PASS,
                "PAT mode matches current runtime",
            )
        )

    return checks


def _check_compose(compose_path: Path) -> list[ReadinessCheck]:
    if not compose_path.is_file():
        return []
    data = _load_yaml(compose_path)
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return [
            ReadinessCheck(
                "docker",
                "compose services",
                FAIL,
                "compose file does not define a services map",
            )
        ]

    checks: list[ReadinessCheck] = []
    for service in ("boardman", "boardman-worker", "boardman-nginx"):
        if service in services:
            checks.append(ReadinessCheck("docker", f"service {service}", PASS, "present"))
        else:
            checks.append(
                ReadinessCheck(
                    "docker",
                    f"service {service}",
                    FAIL,
                    "missing from production compose",
                    f"Add or restore the {service} compose service.",
                )
            )

    checks.extend(_check_queue_services(services))

    if "ollama" in services:
        checks.append(
            ReadinessCheck(
                "docker",
                "production ollama",
                FAIL,
                "production compose includes local Ollama",
                "Use docker-compose.prod.yml without local model inference for wave one.",
            )
        )
    else:
        checks.append(
            ReadinessCheck("docker", "production ollama", PASS, "local Ollama is absent")
        )

    boardman_ports = services.get("boardman", {}).get("ports", [])
    nginx_ports = services.get("boardman-nginx", {}).get("ports", [])
    if boardman_ports:
        checks.append(
            ReadinessCheck(
                "docker",
                "api exposure",
                WARN,
                "boardman API port is published directly",
                "Prefer nginx/TLS as the public entrypoint; firewall 8090 on the VPS.",
            )
        )
    else:
        checks.append(ReadinessCheck("docker", "api exposure", PASS, "API is internal-only"))

    if nginx_ports:
        checks.append(
            ReadinessCheck("docker", "nginx exposure", PASS, "nginx publishes a public port")
        )
    else:
        checks.append(
            ReadinessCheck(
                "docker",
                "nginx exposure",
                PENDING,
                "nginx has no published port",
                "Expose nginx to the host or document the external reverse proxy.",
            )
        )
    return checks


def _check_queue_services(services: dict[str, Any]) -> list[ReadinessCheck]:
    checks: list[ReadinessCheck] = []
    if "boardman-worker" in services:
        worker_command = services.get("boardman-worker", {}).get("command", "")
        worker_command_text = (
            " ".join(worker_command) if isinstance(worker_command, list) else str(worker_command)
        )
        if "boardman.sqlite_worker" in worker_command_text:
            checks.append(
                ReadinessCheck(
                    "queue",
                    "worker queue backend",
                    PASS,
                    "boardman-worker consumes SQLite background_jobs from boardman.db",
                    "Kafka/Redpanda is not part of the wave-one Boardman compose path.",
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    "queue",
                    "worker queue backend",
                    WARN,
                    "boardman-worker command is not the expected SQLite worker",
                    "Confirm the worker queue backend before go-live.",
                )
            )

    if any(service in services for service in ("kafka", "redpanda")):
        return checks + [
            ReadinessCheck(
                "queue",
                "kafka-compatible service",
                WARN,
                "Kafka-compatible service is present in compose",
                "Confirm this is intentional; Boardman wave one does not require Kafka.",
            )
        ]
    return checks + [
        ReadinessCheck(
            "queue",
            "kafka-compatible service",
            PASS,
            "no Kafka/Redpanda service in Boardman wave-one compose",
        )
    ]


def _check_repos_yml(path: Path) -> list[ReadinessCheck]:
    if not path.is_file():
        return [
            ReadinessCheck(
                "plaky",
                "repos.yml",
                PENDING,
                "repos.yml is missing",
                "Create repos.yml with repo names and Plaky board/group IDs.",
            )
        ]
    data = _load_yaml(path)
    if not isinstance(data, dict):
        return [ReadinessCheck("plaky", "repos.yml", FAIL, "repos.yml is not a YAML map")]

    defaults = data.get("defaults")
    repos = data.get("repos") or {}
    if not isinstance(repos, dict):
        return [ReadinessCheck("plaky", "repos.yml repos", FAIL, "repos must be a YAML map")]

    default_has_board_group = isinstance(defaults, dict) and bool(
        str(defaults.get("plaky_board_id") or "").strip()
        and str(defaults.get("plaky_group_id") or "").strip()
    )
    if not repos and not default_has_board_group:
        return [
            ReadinessCheck(
                "plaky",
                "repo placement",
                PENDING,
                "repos.yml has no repo entries and no default board/group IDs",
                "Fill repo names with Plaky board/group IDs after the target Plaky board "
                "is confirmed.",
            )
        ]

    missing: list[str] = []
    for repo, entry in repos.items():
        if not isinstance(entry, dict):
            missing.append(str(repo))
            continue
        has_board = bool(str(entry.get("plaky_board_id") or "").strip()) or default_has_board_group
        has_group = bool(str(entry.get("plaky_group_id") or "").strip()) or default_has_board_group
        if not (has_board and has_group):
            missing.append(str(repo))

    if missing:
        sample = ", ".join(missing[:5])
        suffix = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        return [
            ReadinessCheck(
                "plaky",
                "repo placement",
                PENDING,
                f"{len(missing)} repo(s) missing board/group IDs: {sample}{suffix}",
                "Fill repo -> Plaky board/group IDs.",
            )
        ]
    return [ReadinessCheck("plaky", "repo placement", PASS, "repo board/group routing is filled")]


def _check_team_assignments(path: Path) -> list[ReadinessCheck]:
    if not path.is_file():
        return [
            ReadinessCheck(
                "plaky",
                "field inventory",
                PENDING,
                "team_assignments.yml is missing",
                "Copy team_assignments.yml.example and fill Plaky field IDs.",
            )
        ]
    data = _load_yaml(path)
    if not isinstance(data, dict):
        return [
            ReadinessCheck(
                "plaky",
                "field inventory",
                FAIL,
                "team_assignments.yml is not a YAML map",
            )
        ]

    field_keys = data.get("plaky_field_keys") or {}
    if not isinstance(field_keys, dict):
        return [ReadinessCheck("plaky", "field inventory", PENDING, "plaky_field_keys is missing")]

    required_fields = ("engineer", "qa", "repo", "github_repos")
    missing_fields = [
        key for key in required_fields if _is_placeholder(str(field_keys.get(key) or ""))
    ]
    placeholder_fields = [
        key
        for key in required_fields
        if str(field_keys.get(key) or "").strip() in TEAM_ASSIGNMENT_PLACEHOLDERS
    ]
    if missing_fields:
        return [
            ReadinessCheck(
                "plaky",
                "field inventory",
                PENDING,
                f"missing field key(s): {', '.join(missing_fields)}",
                "Fill Plaky field keys in team_assignments.yml.",
            )
        ]
    if placeholder_fields:
        return [
            ReadinessCheck(
                "plaky",
                "field inventory",
                WARN,
                f"field key(s) look like template placeholders: {', '.join(placeholder_fields)}",
                "Replace sample field IDs with real Plaky field IDs.",
            )
        ]

    member_overrides = data.get("member_overrides") or {}
    if isinstance(member_overrides, dict):
        placeholder_members = [
            str(login)
            for login, entry in member_overrides.items()
            if str(login).strip() in TEAM_ASSIGNMENT_PLACEHOLDERS
            or (isinstance(entry, dict) and _is_placeholder(str(entry.get("id") or "")))
        ]
        if placeholder_members:
            return [
                ReadinessCheck(
                    "plaky",
                    "member inventory",
                    PENDING,
                    f"member override(s) need real Plaky IDs: {', '.join(placeholder_members[:5])}",
                    "Fill member_overrides or rely on GitHub support-team auto matching.",
                )
            ]

    return [
        ReadinessCheck(
            "plaky",
            "field inventory",
            PASS,
            "Plaky field/member inventory is filled",
        )
    ]


def _check_database(path: Path) -> list[ReadinessCheck]:
    if path.is_dir():
        return [
            ReadinessCheck(
                "runtime",
                "sqlite database file",
                FAIL,
                "boardman.db is a directory",
                "Remove the directory and create an empty file with "
                "': > boardman.db && chmod 600 boardman.db'.",
            )
        ]
    if path.is_file():
        return [ReadinessCheck("runtime", "sqlite database file", PASS, "boardman.db file exists")]
    return [
        ReadinessCheck(
            "runtime",
            "sqlite database file",
            PENDING,
            "boardman.db has not been pre-created",
            "Before compose up: ': > boardman.db && chmod 600 boardman.db'.",
        )
    ]


def _check_runtime_smoke_guidance() -> list[ReadinessCheck]:
    return [
        ReadinessCheck(
            "runtime",
            "smoke test",
            WARN,
            "runtime smoke is not proven by offline readiness",
            "After compose is up, run BOARDMAN_COMPOSE_FILE=docker-compose.prod.yml "
            "bash scripts/deploy_smoke.sh.",
        )
    ]


def _split_csv(value: str) -> list[str]:
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {"__yaml_error__": str(exc)}


def checks_by_status(checks: Iterable[ReadinessCheck], status: str) -> list[ReadinessCheck]:
    return [check for check in checks if check.status == status]
