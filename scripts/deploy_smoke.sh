#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
COMPOSE_FILE_PATH="${BOARDMAN_COMPOSE_FILE:-docker-compose.yml}"

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

compose() {
  docker compose -f "$COMPOSE_FILE_PATH" "$@"
}

require_cmd() {
  local c="$1"
  if command -v "$c" >/dev/null 2>&1; then
    pass "command available: ${c}"
  else
    fail "missing required command: ${c}"
  fi
}

check_http_json() {
  local label="$1"
  local url="$2"
  local body
  if body="$(curl -fsS "$url" 2>/dev/null)"; then
    pass "${label} reachable: ${url}"
    if [[ "$body" == *"ok"* || "$body" == *"healthy"* || "$body" == *"models"* ]]; then
      pass "${label} returned expected JSON-ish payload"
    else
      warn "${label} response did not include expected markers"
    fi
  else
    fail "${label} not reachable: ${url}"
  fi
}

check_running_services() {
  local running services
  running="$(compose ps --services --status running 2>/dev/null)"
  if [[ -z "$running" ]]; then
    fail "no running compose services detected"
    return
  fi
  pass "compose has running services"
  for services in boardman boardman-worker boardman-nginx; do
    if printf '%s\n' "$running" | grep -qx "$services"; then
      pass "service running: ${services}"
    else
      fail "service not running: ${services}"
    fi
  done
  if compose config --services 2>/dev/null | grep -qx "ollama"; then
    if printf '%s\n' "$running" | grep -qx "ollama"; then
      pass "service running: ollama"
    else
      fail "service not running: ollama"
    fi
  else
    pass "ollama service intentionally absent"
  fi
  if printf '%s\n' "$running" | grep -qx "redis"; then
    pass "optional service running: redis"
  else
    warn "optional redis service not running; expected unless --profile agent-cache is enabled"
  fi
}

check_webhook_ping() {
  local api_url="$1"
  local payload signature secret code replay_code resp_file replay_resp_file delivery_id

  payload='{"zen":"boardman smoke ping"}'
  secret="$(env_value GITHUB_WEBHOOK_SECRET)"
  resp_file="$(mktemp)"
  replay_resp_file="$(mktemp)"
  delivery_id="smoke-$(date +%s)-$$"

  if [[ -n "$secret" ]]; then
    signature="$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$secret" | awk '{print $2}')"
    code="$(
      curl -sS -o "$resp_file" -w '%{http_code}' \
        -X POST "${api_url}/api/v1/webhooks/github" \
        -H 'Content-Type: application/json' \
        -H 'X-GitHub-Event: ping' \
        -H "X-GitHub-Delivery: ${delivery_id}" \
        -H "X-Hub-Signature-256: sha256=${signature}" \
        --data "$payload"
    )"
    replay_code="$(
      curl -sS -o "$replay_resp_file" -w '%{http_code}' \
        -X POST "${api_url}/api/v1/webhooks/github" \
        -H 'Content-Type: application/json' \
        -H 'X-GitHub-Event: ping' \
        -H "X-GitHub-Delivery: ${delivery_id}" \
        -H "X-Hub-Signature-256: sha256=${signature}" \
        --data "$payload"
    )"
  else
    warn "GITHUB_WEBHOOK_SECRET not set in .env; testing webhook ping without signature"
    code="$(
      curl -sS -o "$resp_file" -w '%{http_code}' \
        -X POST "${api_url}/api/v1/webhooks/github" \
        -H 'Content-Type: application/json' \
        -H 'X-GitHub-Event: ping' \
        -H "X-GitHub-Delivery: ${delivery_id}" \
        --data "$payload"
    )"
    replay_code="$(
      curl -sS -o "$replay_resp_file" -w '%{http_code}' \
        -X POST "${api_url}/api/v1/webhooks/github" \
        -H 'Content-Type: application/json' \
        -H 'X-GitHub-Event: ping' \
        -H "X-GitHub-Delivery: ${delivery_id}" \
        --data "$payload"
    )"
  fi

  if [[ "$code" == "200" ]] && grep -q '"pong"' "$resp_file"; then
    pass "webhook ping returned 200 + pong"
  else
    fail "webhook ping failed (status=${code}); response=$(cat "$resp_file")"
  fi

  if [[ "$replay_code" == "200" ]] && grep -qi 'duplicate delivery ignored' "$replay_resp_file"; then
    pass "webhook replay returned duplicate-ignore response"
  else
    fail "webhook replay duplicate check failed (status=${replay_code}); response=$(cat "$replay_resp_file")"
  fi

  rm -f "$resp_file" "$replay_resp_file"
}

check_boardman_logs() {
  local logs
  logs="$(compose logs --tail=200 boardman 2>/dev/null || true)"
  if [[ -z "$logs" ]]; then
    warn "could not read boardman logs"
    return
  fi

  if printf '%s' "$logs" | grep -Fq "Plaky: API key present"; then
    pass "boardman logs confirm PLAKY_API_KEY is loaded"
  elif printf '%s' "$logs" | grep -Fq "PLAKY_API_KEY is empty"; then
    fail "boardman logs show PLAKY_API_KEY is empty"
  else
    warn "boardman logs did not include a PLAKY_API_KEY startup line in last 200 lines"
  fi
}

printf 'Boardman deployment smoke test\n'
printf 'Repo: %s\n\n' "$ROOT"

require_cmd docker
require_cmd curl
require_cmd awk
require_cmd openssl

if [[ -f "$COMPOSE_FILE_PATH" ]]; then
  pass "compose file found: ${COMPOSE_FILE_PATH}"
else
  fail "compose file missing: ${COMPOSE_FILE_PATH}"
fi

if [[ -f .env ]]; then
  pass ".env exists"
else
  warn ".env missing; webhook signature test may fail if server expects HMAC"
fi

check_running_services

api_url="${BOARDMAN_API_URL:-http://localhost:8090}"
nginx_url="${BOARDMAN_NGINX_URL:-http://localhost:8088}"
ollama_url="${BOARDMAN_OLLAMA_URL:-http://localhost:11434}"
expected_model="${SMOKE_OLLAMA_MODEL:-qwen2.5:0.5b}"
has_ollama=false
if compose config --services 2>/dev/null | grep -qx "ollama"; then
  has_ollama=true
fi

check_http_json "boardman health" "${api_url}/api/v1/health"
check_http_json "nginx proxy health" "${nginx_url}/api/v1/health"

if [[ "$has_ollama" == "true" ]]; then
  if tags="$(curl -fsS "${ollama_url}/api/tags" 2>/dev/null)"; then
    pass "ollama tags endpoint reachable: ${ollama_url}/api/tags"
    if printf '%s' "$tags" | grep -Fq "\"name\":\"${expected_model}\""; then
      pass "expected ollama model present: ${expected_model}"
    else
      warn "expected ollama model not found: ${expected_model}"
    fi
  else
    fail "ollama tags endpoint not reachable: ${ollama_url}/api/tags"
  fi
else
  pass "skipping ollama smoke checks; compose omits local LLM"
fi

check_webhook_ping "$api_url"
check_boardman_logs

printf '\nSmoke complete: %s failure(s), %s warning(s)\n' "$failures" "$warnings"
if [[ "$failures" -gt 0 ]]; then
  exit 1
fi
