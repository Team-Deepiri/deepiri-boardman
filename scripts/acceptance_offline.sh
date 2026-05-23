#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE_PATH="${BOARDMAN_COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${BOARDMAN_ACCEPTANCE_ENV_FILE:-.env.acceptance.example}"
REPOS_FILE="${BOARDMAN_ACCEPTANCE_REPOS_FILE:-repos.acceptance.yml}"
TEAM_FILE="${BOARDMAN_ACCEPTANCE_TEAM_FILE:-team_assignments.acceptance.yml}"
DB_FILE="${BOARDMAN_ACCEPTANCE_DB_FILE:-boardman.acceptance.db}"

failures=0
warnings=0
created_temp_env=0
created_temp_db=0
readiness_runner=""

pass() {
  printf 'PASS %s\n' "$1"
}

warn() {
  warnings=$((warnings + 1))
  printf 'WARN %s\n' "$1"
}

fail() {
  failures=$((failures + 1))
  printf 'FAIL %s\n' "$1"
}

require_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "command available: ${cmd}"
  else
    fail "missing required command: ${cmd}"
  fi
}

cleanup() {
  if [[ "$created_temp_env" -eq 1 && -f .env ]]; then
    rm -f .env
  fi
  if [[ "$created_temp_db" -eq 1 && -f "$DB_FILE" ]]; then
    rm -f "$DB_FILE"
  fi
}
trap cleanup EXIT

printf 'Boardman wave-one offline acceptance\n'
printf 'Repo: %s\n\n' "$ROOT"

for cmd in docker awk grep sed git poetry; do
  if [[ "$cmd" == "poetry" ]]; then
    continue
  fi
  require_cmd "$cmd"
done

if command -v poetry >/dev/null 2>&1; then
  readiness_runner="poetry"
  pass "readiness runner selected: poetry"
elif command -v uv >/dev/null 2>&1; then
  readiness_runner="uv"
  pass "readiness runner selected: uv"
else
  fail "missing readiness runner: install poetry or uv"
fi

if [[ "$failures" -gt 0 ]]; then
  printf '\nAcceptance halted: missing required command(s)\n'
  exit 1
fi

if [[ -f "$COMPOSE_FILE_PATH" ]]; then
  pass "compose file found: ${COMPOSE_FILE_PATH}"
else
  fail "compose file missing: ${COMPOSE_FILE_PATH}"
fi

if [[ -f "$ENV_FILE" ]]; then
  pass "acceptance env fixture found: ${ENV_FILE}"
else
  fail "acceptance env fixture missing: ${ENV_FILE}"
fi

if [[ -f "$REPOS_FILE" ]]; then
  pass "acceptance repos fixture found: ${REPOS_FILE}"
else
  fail "acceptance repos fixture missing: ${REPOS_FILE}"
fi

if [[ -f "$TEAM_FILE" ]]; then
  pass "acceptance team fixture found: ${TEAM_FILE}"
else
  fail "acceptance team fixture missing: ${TEAM_FILE}"
fi

if [[ "$failures" -gt 0 ]]; then
  printf '\nAcceptance halted: fixture prerequisites missing\n'
  exit 1
fi

# docker compose config reads env_file=.env from compose; supply a temporary one if absent.
if [[ ! -f .env ]]; then
  cp "$ENV_FILE" .env
  created_temp_env=1
  pass "temporary .env created from acceptance fixture"
else
  warn ".env already exists; compose render will use existing .env"
fi

if [[ ! -f "$DB_FILE" ]]; then
  bash scripts/acceptance_prepare.sh >/dev/null
  created_temp_db=1
  pass "temporary acceptance db created: ${DB_FILE}"
else
  warn "acceptance db already exists: ${DB_FILE}"
fi

compose_config=""
if compose_config="$(docker compose -f "$COMPOSE_FILE_PATH" config 2>/dev/null)"; then
  pass "docker compose config renders (${COMPOSE_FILE_PATH})"
else
  fail "docker compose config failed (${COMPOSE_FILE_PATH})"
  printf '\nAcceptance complete: %s failure(s), %s warning(s)\n' "$failures" "$warnings"
  exit 1
fi

services="$(docker compose -f "$COMPOSE_FILE_PATH" config --services 2>/dev/null)"
for service in boardman boardman-worker boardman-nginx; do
  if printf '%s\n' "$services" | grep -qx "$service"; then
    pass "compose service present: ${service}"
  else
    fail "compose service missing: ${service}"
  fi
done

for forbidden in ollama kafka redpanda; do
  if printf '%s\n' "$services" | grep -qx "$forbidden"; then
    fail "forbidden service present in wave-one compose: ${forbidden}"
  else
    pass "forbidden service absent: ${forbidden}"
  fi
done

llm_provider="$(
  awk -F= '
    $0 !~ /^[[:space:]]*#/ && $1 == "LLM_PROVIDER" {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "$ENV_FILE" 2>/dev/null
)"
llm_provider="$(printf '%s' "$llm_provider" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
if [[ "$llm_provider" == "ollama" ]]; then
  fail "acceptance env must not set LLM_PROVIDER=ollama"
else
  pass "acceptance env uses hosted-LLM mode (LLM_PROVIDER=${llm_provider:-unset})"
fi

if [[ "$readiness_runner" == "poetry" ]]; then
  readiness_cmd=(
    poetry run boardman readiness
    --env-file "$ENV_FILE"
    --compose-file "$COMPOSE_FILE_PATH"
    --repos-file "$REPOS_FILE"
    --team-assignments-file "$TEAM_FILE"
    --database-file "$DB_FILE"
    --strict-pending
    --format table
  )
else
  readiness_cmd=(
    uv run
    --no-project
    --with PyYAML
    --with rich
    --with typer
    --with httpx
    --with pydantic
    --with pydantic-settings
    --with sqlalchemy
    --with fastapi
    --with aiosqlite
    --with redis
    --with langchain
    --with langchain-core
    --with langchain-ollama
    --with langchain-openai
    --with langchain-anthropic
    --with langchain-google-genai
    python -m boardman.cli.commands readiness
    --env-file "$ENV_FILE"
    --compose-file "$COMPOSE_FILE_PATH"
    --repos-file "$REPOS_FILE"
    --team-assignments-file "$TEAM_FILE"
    --database-file "$DB_FILE"
    --strict-pending
    --format table
  )
fi

if "${readiness_cmd[@]}" >/tmp/boardman_acceptance_readiness.out 2>&1; then
  pass "readiness strict mode passed with acceptance fixtures"
else
  fail "readiness strict mode failed with acceptance fixtures"
  sed -n '1,200p' /tmp/boardman_acceptance_readiness.out
fi
rm -f /tmp/boardman_acceptance_readiness.out

printf '\nAcceptance complete: %s failure(s), %s warning(s)\n' "$failures" "$warnings"
if [[ "$failures" -gt 0 ]]; then
  printf 'Result: NOT READY for engineering-complete handoff\n'
  exit 1
fi
printf 'Result: Engineering-complete offline acceptance PASS\n'
