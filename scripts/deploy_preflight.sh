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

# Print the `docker compose config` service block that publishes $1, or nothing.
#
# `docker compose config` emits services as two-space-indented keys under
# `services:`, with each port as a `published:`/`host_ip:` pair. awk has no YAML
# parser available here, so this tracks the current service by indentation and
# emits it only if that block is the one carrying the port. Scoping matters: a
# whole-file grep for `host_ip: 127.0.0.1` matches whichever service happens to
# be bound to loopback, which is how the API check came to be satisfied by the UI.
service_block_publishing() {
  local port="$1"
  awk -v want="$port" '
    /^services:[[:space:]]*$/ { in_services = 1; next }
    # Any top-level key ends the services mapping.
    in_services && /^[^[:space:]#]/ { in_services = 0 }
    in_services && /^  [^[:space:]#][^:]*:[[:space:]]*$/ {
      if (block != "" && published(block)) { print block }
      block = $0 "\n"
      next
    }
    in_services && /^  / { block = block $0 "\n" }
    END { if (block != "" && published(block)) { print block } }

    function published(text,   pattern) {
      pattern = "published: \"?" want "\"?([[:space:]]|$)"
      return text ~ pattern
    }
  '
}

check_published_port_is_loopback() {
  local config="$1"
  local port="$2"
  local label="$3"
  local block
  block="$(printf '%s\n' "$config" | service_block_publishing "$port")"
  if [[ -z "$block" ]]; then
    return 0
  fi
  if printf '%s\n' "$block" | grep -Eq 'host_ip: "?(127\.0\.0\.1|::1)"?'; then
    pass "compose publishes ${label} port ${port} on loopback only"
  else
    fail "compose publishes ${label} port ${port} on a non-loopback host IP; this exposes it directly to the internet and bypasses nginx. Bind 127.0.0.1:${port}:${port}"
  fi
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

has_nvidia_gpu() {
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    return 0
  fi
  if command -v lspci >/dev/null 2>&1 && lspci 2>/dev/null | grep -qi nvidia; then
    return 0
  fi
  return 1
}

check_ollama_gpu_runtime() {
  local docker_runtime runtime_override

  if ! has_nvidia_gpu; then
    pass "no NVIDIA GPU detected; Ollama will run on CPU"
    return
  fi

  pass "NVIDIA GPU detected on host"
  runtime_override="$(env_value OLLAMA_DOCKER_RUNTIME)"
  docker_runtime="$(docker info --format '{{.DefaultRuntime}}' 2>/dev/null || true)"

  if [[ "$docker_runtime" == "nvidia" ]]; then
    pass "Docker default runtime is nvidia; Ollama will use GPU by default"
  elif [[ "$runtime_override" == "nvidia" ]]; then
    pass ".env sets OLLAMA_DOCKER_RUNTIME=nvidia for the Ollama service"
  else
    warn "NVIDIA GPU detected but Docker default runtime is not nvidia; run nvidia-ctk --set-as-default or set OLLAMA_DOCKER_RUNTIME=nvidia"
  fi
}

printf 'Boardman deployment preflight\n'
printf 'Repo: %s\n\n' "$ROOT"

if [[ -f "$COMPOSE_FILE_PATH" && -f .env.example ]]; then
  pass "running from repo root"
else
  fail "run this script from the deepiri-boardman repo root with a valid compose file"
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
  if [[ -z "$(env_value ROUTE_SECRET)" ]]; then
    warn ".env missing ROUTE_SECRET; Cloudflare Worker /assign-qa route will not be secured"
  else
    pass ".env has ROUTE_SECRET set"
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
if compose_config="$(compose config 2>/dev/null)"; then
  pass "docker compose config renders (${COMPOSE_FILE_PATH})"
  services="$(compose config --services 2>/dev/null)"
  for service in boardman boardman-worker boardman-nginx; do
    if printf '%s\n' "$services" | grep -qx "$service"; then
      pass "compose service ${service} is present"
    else
      fail "compose service ${service} is missing"
    fi
  done
  if printf '%s\n' "$services" | grep -qx "ollama"; then
    pass "compose service ollama is present for local/dev LLM"
    check_ollama_gpu_runtime
  else
    pass "compose omits ollama for cloud production"
    if printf '%s\n' "$compose_config" | grep -Eq 'LLM_PROVIDER: "?ollama"?'; then
      fail "production compose omits ollama but renders LLM_PROVIDER=ollama; use a hosted provider or disable LLM-dependent features"
    fi
  fi
  if printf '%s\n' "$services" | grep -qx "redis"; then
    pass "optional compose service redis is present"
  else
    warn "optional redis service is profile-gated; enable with --profile agent-cache only if AGENT_REDIS_URL is set"
  fi
  if printf '%s\n' "$compose_config" | grep -Eq 'published: "?11434"?'; then
    warn "compose publishes Ollama port 11434; keep it firewalled/private on VPS"
  fi
  # The API and UI ports are intentionally published (nginx reaches the API over
  # the host, operators curl both), so their host IP is the whole question. A bare
  # "published: 8090" or a 0.0.0.0/:: host binds every interface and bypasses the
  # nginx vhost -- that is a failure, not a warning, because it puts the
  # unauthenticated read-mostly routes on the public internet. Loopback
  # (127.0.0.1 / ::1) is the intended configuration.
  #
  # The host_ip has to be read from the SAME service block that publishes the
  # port, not from the config as a whole. A whole-file grep is satisfied by any
  # loopback binding anywhere, so the boardman-ui service publishing
  # 127.0.0.1:8088 masked a regression of the API to 0.0.0.0 -- the guard passed
  # on the exact config it exists to reject.
  check_published_port_is_loopback "$compose_config" 8090 "API"
  check_published_port_is_loopback "$compose_config" 8088 "UI"
else
  fail "docker compose config failed"
fi

printf '\nPreflight complete: %s failure(s), %s warning(s)\n' "$failures" "$warnings"
if [[ "$failures" -gt 0 ]]; then
  exit 1
fi
