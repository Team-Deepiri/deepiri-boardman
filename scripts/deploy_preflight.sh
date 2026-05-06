#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

failures=0
warnings=0

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

env_value() {
  local key="$1"
  awk -F= -v k="$key" '
    $0 !~ /^[[:space:]]*#/ && $1 == k {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' .env 2>/dev/null
}

check_env_key() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  if [[ -z "$value" ]]; then
    fail ".env is missing required ${key}"
    return
  fi
  if [[ "$value" == your_* || "$value" == *"_here" || "$value" == "<"*">" ]]; then
    fail ".env ${key} still looks like a placeholder"
    return
  fi
  pass ".env has ${key} set"
}

printf 'Boardman deployment preflight\n'
printf 'Repo: %s\n\n' "$ROOT"

if [[ -f docker-compose.yml && -f .env.example ]]; then
  pass "running from repo root"
else
  fail "run this script from the deepiri-boardman repo root"
fi

if [[ -f .env ]]; then
  pass ".env exists"
  if git check-ignore -q .env 2>/dev/null; then
    pass ".env is ignored by git"
  else
    fail ".env is not ignored by git"
  fi
else
  fail ".env missing; copy .env.example to .env and fill service credentials"
fi

if [[ -f .env ]]; then
  check_env_key "PLAKY_API_KEY"
  check_env_key "GITHUB_PAT"
  check_env_key "GITHUB_WEBHOOK_SECRET"
  if [[ -z "$(env_value WORKER_INTERNAL_SECRET)" ]]; then
    warn ".env missing WORKER_INTERNAL_SECRET; internal QA worker API will be disabled"
  else
    pass ".env has WORKER_INTERNAL_SECRET set"
  fi
fi

if [[ -d boardman.db ]]; then
  fail "boardman.db is a directory; remove it and create a file with ': > boardman.db && chmod 600 boardman.db'"
elif [[ -f boardman.db ]]; then
  pass "boardman.db exists as a file"
else
  fail "boardman.db missing; create it before compose with ': > boardman.db && chmod 600 boardman.db'"
fi

if command -v docker >/dev/null 2>&1; then
  pass "docker CLI is installed"
else
  fail "docker CLI is not installed"
fi

if docker info >/dev/null 2>&1; then
  pass "docker daemon is reachable"
else
  fail "docker daemon is not reachable"
fi

if docker compose version >/dev/null 2>&1; then
  pass "docker compose plugin is installed"
else
  fail "docker compose plugin is not installed"
fi

compose_config=""
if compose_config="$(docker compose config 2>/dev/null)"; then
  pass "docker compose config renders"
  services="$(printf '%s\n' "$compose_config" | docker compose config --services 2>/dev/null)"
  for service in boardman boardman-worker boardman-nginx redis ollama; do
    if printf '%s\n' "$services" | grep -qx "$service"; then
      pass "compose service ${service} is present"
    else
      fail "compose service ${service} is missing"
    fi
  done
  if printf '%s\n' "$compose_config" | grep -Eq 'published: "?11434"?'; then
    warn "compose publishes Ollama port 11434; keep it firewalled/private on VPS"
  fi
  if printf '%s\n' "$compose_config" | grep -Eq 'published: "?8090"?'; then
    warn "compose publishes API port 8090; prefer nginx/TLS as the public entrypoint"
  fi
else
  fail "docker compose config failed"
fi

printf '\nPreflight complete: %s failure(s), %s warning(s)\n' "$failures" "$warnings"
if [[ "$failures" -gt 0 ]]; then
  exit 1
fi
