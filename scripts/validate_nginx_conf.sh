#!/usr/bin/env bash
# Validate the Boardman nginx vhosts with a real nginx, no root required.
#
# Why this exists: deploy/nginx/*.conf are include fragments for the VPS's own
# nginx. They are never loaded locally, so a syntax or semantic error only shows
# up during a production deploy -- at the worst possible moment. This harness
# builds a throwaway nginx prefix, substitutes ONLY environment-specific values
# (the Let's Encrypt cert paths, the privileged listen ports, and the
# docker-internal upstream name), and runs the real `nginx -t` on the result.
#
# Everything else -- location blocks, proxy_pass targets, ssl_protocols,
# http2 on, add_header, client_max_body_size, the catch-all 403 -- is validated
# verbatim as written.
#
# Two levels of check:
#
#   (default)  `nginx -t` -- does nginx accept this config at all?
#   --live     additionally boot nginx behind a stub backend and make real
#              requests through it, proving the public vhost's allowlist actually
#              routes the way it reads. `nginx -t` only proves the file parses:
#              it cannot catch `location = /api/v1/webhooks/github` quietly losing
#              precedence to the catch-all, which would 403 every GitHub delivery
#              while still passing a config test.
#
# Usage:
#   scripts/validate_nginx_conf.sh [--live] [file ...]
#   scripts/validate_nginx_conf.sh --render <dir> [file ...]
#
#   --render <dir>  write the fully-substituted main config + self-signed cert
#                   to <dir> and exit, without running nginx. Used by CI to run
#                   the check inside the production nginx image.
#   --live          also verify routing end to end (implies a real nginx).
#   KEEP_WORK=1     keep the generated config for debugging.
#   NGINX_BIN=...   use this nginx binary instead of PATH or the auto-build.
#   NGINX_MIN_VERSION=...  refuse an nginx older than this (default 1.25.1).
#
# Exit status: 0 passed, 1 a vhost failed validation, 2 usage/environment error,
# 77 skipped because the available nginx is too old to parse these vhosts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NGINX_BIN="${NGINX_BIN:-}"
LIVE=0
RENDER_DIR=""

# Lowest nginx that can parse deploy/nginx at all (`http2 on` landed in 1.25.1).
# Production runs nginx:1.27-alpine, so anything below this is simply not the
# nginx this repo is written for.
NGINX_MIN_VERSION="${NGINX_MIN_VERSION:-1.25.1}"

# Unprivileged stand-ins for the deployment's 80/443 and the docker-published 8090.
# Overridable mainly so the live check can run twice concurrently in tests.
HTTP_PORT="${NGINX_TEST_HTTP_PORT:-8080}"
TLS_PORT="${NGINX_TEST_TLS_PORT:-8443}"
STUB_PORT="${NGINX_TEST_STUB_PORT:-8090}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) LIVE=1; shift ;;
    --render)
      [[ $# -ge 2 ]] || { echo "--render needs a directory" >&2; exit 2; }
      RENDER_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) break ;;
  esac
done

if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  mapfile -t FILES < <(find "$ROOT/deploy/nginx" -maxdepth 1 -name '*.conf' | sort)
fi

if [[ -n "$RENDER_DIR" ]]; then
  WORK="$RENDER_DIR"
  mkdir -p "$WORK"
  # Deliberately no EXIT trap: --render's whole job is to leave the rendered
  # config on disk for the caller (CI) to run nginx against. The caller cleans up.
elif [[ "${KEEP_WORK:-0}" == "1" ]]; then
  WORK="$(mktemp -d)"
  echo "KEEP_WORK=1 -> generated configs left in $WORK"
else
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
fi
mkdir -p "$WORK/conf" "$WORK/logs" "$WORK/tmp"

# --- 1. obtain an nginx binary ------------------------------------------------
# In --render mode we only emit configs; the caller (CI) supplies the nginx, so
# nothing is downloaded or built here.
if [[ -z "$RENDER_DIR" && -z "$NGINX_BIN" ]]; then
  if command -v nginx >/dev/null 2>&1; then
    NGINX_BIN="$(command -v nginx)"
  else
    echo "no nginx found; building one into $WORK/nginx (needs gcc, make, openssl headers)"
    tarball="$WORK/nginx.tar.gz"
    curl -sSLo "$tarball" "https://nginx.org/download/${NGINX_VERSION:-nginx-1.31.6}.tar.gz"
    tar xzf "$tarball" -C "$WORK"
    # `return` comes from the rewrite module (so PCRE is required), and the
    # boardman vhost uses listen 443 ssl + http2 on -- so this needs ssl and v2
    # too, plus OpenSSL dev headers. No sudo assumed.
    (
      cd "$WORK/${NGINX_VERSION:-nginx-1.31.6}"
      ./configure --prefix="$WORK/nginx" \
        --with-http_ssl_module --with-http_v2_module \
        --with-http_realip_module --with-http_stub_status_module \
        >/dev/null
      make -j"$(nproc 2>/dev/null || echo 2)" >/dev/null
      make install >/dev/null
    )
    NGINX_BIN="$WORK/nginx/sbin/nginx"
  fi
fi

if [[ -z "$RENDER_DIR" ]]; then
  echo "using nginx: $NGINX_BIN"
  "$NGINX_BIN" -v 2>&1 | sed 's/^/  /'

  # The vhost uses `http2 on`, which only exists from nginx 1.25.1; production
  # runs nginx:1.27-alpine. An older binary rejects it as an unknown directive,
  # which reads like a broken vhost when it is really an nginx that is too old to
  # judge one. Exit 77 (the conventional "skipped" status) to keep those two cases
  # distinct, so a stale nginx can never masquerade as a passing or failing check.
  ver="$("$NGINX_BIN" -v 2>&1 | sed -n 's|.*nginx/\([0-9][0-9.]*\).*|\1|p')"
  if [[ -z "$ver" ]]; then
    echo "could not parse an nginx version out of '$NGINX_BIN -v'" >&2
    exit 2
  fi
  if [[ "$(printf '%s\n%s\n' "$NGINX_MIN_VERSION" "$ver" | sort -V | head -1)" != "$NGINX_MIN_VERSION" ]]; then
    cat >&2 <<EOF
nginx $ver is older than the $NGINX_MIN_VERSION this vhost needs.

  deploy/nginx uses \`http2 on\`, added in nginx 1.25.1; production runs
  nginx:1.27-alpine. An older binary cannot parse the committed vhost at all, so
  this run cannot judge it either way. That is a limitation of this nginx, not a
  defect in the config, so exiting 77 (skipped) rather than reporting a failure.
  Callers should treat 77 as "not run" -- pytest skips, CI is expected to fail.

  Point NGINX_BIN at nginx >= $NGINX_MIN_VERSION to run the check for real.
EOF
    exit 77
  fi
fi

# --- 2. prerequisites nginx needs but the repo doesn't ship -------------------
# self-signed cert so the ssl_* directives resolve, and a minimal mime.types for
# the wrapper's `include`.
mkdir -p "$WORK/conf"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$WORK/conf/key.pem" -out "$WORK/conf/cert.pem" \
  -days 2 -subj "/CN=boardman.deepiri.com" >/dev/null 2>&1
cat > "$WORK/conf/mime.types" <<'MIME'
types {
    text/html                             html htm;
    text/css                              css;
    application/javascript                js;
    application/json                      json;
    image/png                             png;
    image/svg+svg                         svg svgz;
    image/x-icon                          ico;
    text/plain                            txt;
}
MIME

failures=0
PUB_WORK=""
FIRST_WORK=""
declare -a RESULTS=()

for file in "${FILES[@]}"; do
  name="$(basename "$file")"
  work="$WORK/$name"
  mkdir -p "$work"

  # Substitute ONLY deployment-environment specifics. Everything else is verbatim:
  # every location block, proxy_pass target, ssl_* setting, add_header,
  # client_max_body_size, the catch-all 403, and the `http2 on` directive are all
  # validated as written. The binary must be built with rewrite+ssl+http_v2
  # (the auto-build below does this); point NGINX_BIN at a stock nginx if unsure.
  sed -E \
    -e "s|/etc/letsencrypt/live/[^/]+/fullchain\.pem|$WORK/conf/cert.pem|g" \
    -e "s|/etc/letsencrypt/live/[^/]+/privkey\.pem|$WORK/conf/key.pem|g" \
    -e "s|^([[:space:]]*)listen 80;|\1listen $HTTP_PORT;|" \
    -e "s|^([[:space:]]*)listen 443 ssl;|\1listen $TLS_PORT ssl;|" \
    "$file" > "$work/fragment.conf"

  # default.conf needs a document root so `try_files`/root directives resolve.
  extra_http=""
  if [[ "$name" == "default.conf" ]]; then
    mkdir -p "$work/html"
    echo '<!doctype html><title>ok</title>' > "$work/html/index.html"
    # $'...' so the \n escapes are real newlines; a single-quoted segment here
    # would leave a literal "\n" in the config, which nginx reads as a directive
    # named `"\n"`.
    extra_http=$'\n    root '"$work"$'/html;\n    index index.html;'
  fi

  # The vhost's upstream is the docker-internal name `boardman`. nginx resolves
  # upstream names at config-parse time, so `nginx -t` needs it to resolve. Without
  # root we cannot add a hosts alias, so rewrite ONLY the upstream server line to
  # loopback (the stub backend in --live, otherwise just something that resolves).
  # Every location, proxy_pass, and header stays verbatim.
  sed -i -E "s|^([[:space:]]*)server boardman:8090;|\1server 127.0.0.1:$STUB_PORT;|" \
    "$work/fragment.conf"
  sed -i -E "s|http://boardman:8090|http://127.0.0.1:$STUB_PORT|g" "$work/fragment.conf"

  # default.conf deliberately points `resolver` at Docker's embedded DNS
  # (127.0.0.11), which does not exist off a Docker network. nginx refuses to start
  # a server that uses a variable in proxy_pass without a resolver, so repoint it at
  # a real one for the duration of the test. The variable-in-proxy_pass behaviour
  # this comment warns about is exactly what the test is checking still parses.
  sed -i -E 's|^([[:space:]]*)resolver 127\.0\.0\.11[^;]*;|\1resolver 127.0.0.53 valid=10s;|' \
    "$work/fragment.conf"

  mkdir -p "$work/logs"

  # Remember where the public vhost was rendered, for the live check below. Keyed
  # on the file name so `--live` works on a copy under any name.
  [[ -n "$FIRST_WORK" ]] || FIRST_WORK="$work"
  if [[ "$name" == "boardman.deepiri.com.conf" ]]; then
    PUB_WORK="$work"
  fi

  # `daemon off` would block the script, so the live run removes it and starts
  # nginx in the background. -t does not care either way.
  daemon_line="daemon off;"
  [[ "$LIVE" == "1" ]] && daemon_line=""

  cat > "$work/nginx.conf" <<EOF
worker_processes 1;
$daemon_line
pid $work/nginx.pid;
error_log $work/error.log warn;
events { worker_connections 64; }
http {
    include $WORK/conf/mime.types;
    default_type application/octet-stream;
    access_log off;
    client_body_temp_path $WORK/tmp/client_body;
    proxy_temp_path $WORK/tmp/proxy;
    fastcgi_temp_path $WORK/tmp/fastcgi;
    uwsgi_temp_path $WORK/tmp/uwsgi;
    scgi_temp_path $WORK/tmp/scgi;$extra_http
$(cat "$work/fragment.conf")
}
EOF

  # In --render mode we only write the config; the caller runs nginx against it
  # (CI does this inside the production nginx image). Absolute paths inside the
  # generated config all live under $WORK, so mounting $WORK at the same path in a
  # container makes them resolve unchanged.
  if [[ -n "$RENDER_DIR" ]]; then
    RESULTS+=("RENDER $name")
    continue
  fi

  if "$NGINX_BIN" -t -c "$work/nginx.conf" -p "$work" 2> "$work/test.log"; then
    RESULTS+=("PASS  $name")
  else
    RESULTS+=("FAIL  $name")
    failures=$((failures + 1))
    sed 's/^/        /' "$work/test.log"
  fi
done

# --- 4. live routing check ----------------------------------------------------
# `nginx -t` above proves the file parses. It cannot prove the public vhost routes
# the way it reads: if `location = /api/v1/webhooks/github` ever lost precedence to
# the `location /api/` catch-all, every GitHub delivery would 403 and the config
# would still pass. So boot the real nginx in front of a stub backend and make
# actual requests.
if [[ "$LIVE" == "1" && "$failures" -eq 0 ]]; then
  echo
  echo "live routing check (real nginx, stub backend on 127.0.0.1:$STUB_PORT)"

  STUB_LOG="$WORK/stub.log"
  cat > "$WORK/stub.py" <<STUB
import http.server, socketserver, sys

LOG = sys.argv[1]

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _handle(self):
        # Drain the request body before responding. nginx holds upstream
        # connections alive, so a body left unread in the socket would be parsed as
        # the start of the *next* request on that connection ("Unsupported method
        # ('{}GET')").
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        with open(LOG, "a") as fh:
            fh.write(f"{self.command} {self.path}\n")
        body = b'{"stub":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_PATCH = do_DELETE = _handle
    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingTCPServer):
    # Threaded on purpose: nginx keeps upstream connections alive, so a
    # single-threaded stub would stay blocked serving the first kept-alive
    # connection and every later request would hang.
    allow_reuse_address = True
    daemon_threads = True

with Server(("127.0.0.1", $STUB_PORT), Handler) as httpd:
    httpd.serve_forever()
STUB

  # Prefer the public vhost; fall back to whichever single file was checked so
  # `--live` also works on a copy saved under another name.
  PUB="${PUB_WORK:-${FIRST_WORK:-$WORK/boardman.deepiri.com.conf}}"

  # Check the ports BEFORE starting the stub, or the stub's own listener looks busy.
  # This is a real bind() attempt rather than an HTTP probe: a listener that
  # accepts but never answers would pass a curl probe and then hang the readiness
  # loop below.
  port_busy() {
    python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)   # busy
finally:
    s.close()
sys.exit(1)       # free
PY
  }
  for p in "$HTTP_PORT" "$TLS_PORT" "$STUB_PORT"; do
    if port_busy "$p"; then
      echo "  FAIL  port $p is already in use; set NGINX_TEST_*_PORT to move the test"
      exit 1
    fi
  done

  # </dev/null and a redirected stdout: this is a background child, so leaving it
  # attached to the script's stdout would keep the pipe open and hang any caller
  # that captures output (e.g. `... | tail`).
  python3 "$WORK/stub.py" "$STUB_LOG" </dev/null >"$WORK/stub.out" 2>&1 &
  STUB_PID=$!

  # nginx daemonizes: it detaches but would still hold this script's stdout/stderr
  # open, which deadlocks a caller that captures output. Redirect them, then stop
  # it later via its pid file -- $! would be the short-lived parent.
  if ! "$NGINX_BIN" -c "$PUB/nginx.conf" -p "$PUB" >"$WORK/nginx.out" 2>&1; then
    echo "  FAIL  nginx refused to start"
    sed 's/^/        /' "$WORK/nginx.out"
    sed 's/^/        /' "$PUB/error.log" 2>/dev/null || true
    kill "$STUB_PID" 2>/dev/null || true
    exit 1
  fi
  # nginx writes the pid file as the master comes up, and removes it on exit, so
  # read it once here rather than re-reading it in cleanup (where it can vanish
  # between the -f test and the read).
  for _ in $(seq 1 50); do
    [[ -s "$PUB/nginx.pid" ]] && break
    sleep 0.1
  done
  NGINX_MASTER="$(cat "$PUB/nginx.pid" 2>/dev/null || echo 0)"

  cleanup() {
    # `nginx -s stop` is the graceful path; the recorded pid is the fallback so a
    # failed signal cannot leave the ports bound for the next run.
    "$NGINX_BIN" -c "$PUB/nginx.conf" -p "$PUB" -s stop >/dev/null 2>&1 || true
    if [[ "$NGINX_MASTER" != "0" ]]; then
      kill "$NGINX_MASTER" 2>/dev/null || true
    fi
    kill "$STUB_PID" 2>/dev/null || true
    wait "$STUB_PID" 2>/dev/null || true
  }
  trap 'cleanup; rm -rf "$WORK"' EXIT

  # Wait for both to accept connections.
  ready=0
  for _ in $(seq 1 50); do
    if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$STUB_PORT/" \
       && curl -sk -o /dev/null --max-time 1 -H 'Host: boardman.deepiri.com' \
            "https://127.0.0.1:$TLS_PORT/health"; then
      ready=1
      break
    fi
    sleep 0.2
  done
  if [[ "$ready" != "1" ]]; then
    echo "  FAIL  nginx or the stub backend never came up"
    sed 's/^/        /' "$PUB/error.log" 2>/dev/null || true
    exit 1
  fi

  # Did this specific request reach the backend? Count stub log lines before and
  # after, rather than grepping for the client-facing path: `proxy_pass
  # http://boardman_api/api/v1/health` rewrites the upstream path, so the stub sees
  # /api/v1/health for a request to /health. A delta is the only reliable signal.
  #
  # `$want` is a space-separated list of acceptable status codes. Paths that are not
  # under /api/ legitimately 404 rather than 403 -- what matters is that they never
  # reach the backend.
  live_check() {
    local label="$1" path="$2" method="$3" want="$4" proxied="$5"
    local before after got hit
    before=$(wc -l < "$STUB_LOG" 2>/dev/null || echo 0)

    if [[ "$method" == "GET" ]]; then
      got=$(curl -sk -o "$WORK/body" -w '%{http_code}' --max-time 5 \
        -H 'Host: boardman.deepiri.com' "https://127.0.0.1:$TLS_PORT$path")
    else
      got=$(curl -sk -o "$WORK/body" -w '%{http_code}' --max-time 5 \
        -X "$method" -H 'Host: boardman.deepiri.com' -d '{}' \
        "https://127.0.0.1:$TLS_PORT$path")
    fi

    sleep 0.1
    after=$(wc -l < "$STUB_LOG" 2>/dev/null || echo 0)
    if (( after > before )); then hit=true; else hit=false; fi

    if [[ " $want " != *" $got "* ]]; then
      printf '  FAIL  %-42s expected [%s], got %s\n' "$label" "$want" "$got"
      sed 's/^/          body: /' "$WORK/body"
      failures=$((failures + 1))
      return
    fi
    if [[ "$proxied" == "true" && "$hit" != "true" ]]; then
      printf '  FAIL  %-42s got %s but never reached the backend\n' "$label" "$got"
      failures=$((failures + 1))
      return
    fi
    if [[ "$proxied" == "false" && "$hit" == "true" ]]; then
      printf '  FAIL  %-42s was proxied but must be denied at the edge\n' "$label"
      failures=$((failures + 1))
      return
    fi
    printf '  ok    %-42s %s %s\n' "$label" "$got" \
      "$([[ "$proxied" == "true" ]] && echo '(reached backend)' || echo '(not proxied)')"
  }

  # The allowlist: these three have a real unauthenticated caller.
  live_check "GET  /health"                    "/health"                    GET  200 true
  live_check "POST /api/v1/webhooks/github"    "/api/v1/webhooks/github"    POST 200 true
  live_check "POST /api/v1/assignment/pick-qa" "/api/v1/assignment/pick-qa" POST 200 true

  # Everything else must be stopped at the edge, not proxied. These are the routes
  # the audit found reachable: read-mostly listings that leak org/repo/roster data,
  # and agent chat, which spends the server's LLM budget.
  live_check "GET  /api/v1/tasks"               "/api/v1/tasks"               GET  403 false
  live_check "GET  /api/v1/repos/org"          "/api/v1/repos/org"          GET  403 false
  live_check "GET  /api/v1/llm/models"         "/api/v1/llm/models"         GET  403 false
  live_check "POST /api/v1/tasks"              "/api/v1/tasks"               POST 403 false
  live_check "POST /api/v1/agent/chat"         "/api/v1/agent/chat"          POST 403 false
  live_check "POST /api/v1/repos/classify"     "/api/v1/repos/classify"      POST 403 false
  # Near-misses must not slip through the exact-match rule. GET on a POST-only
  # allowlisted path is proxied by `location =` (nginx matches on path, not
  # method) and is rejected by the app, so a 5xx is the acceptable outcome -- what
  # matters is only that it is not a 403, which would mean the exact match lost.
  live_check "GET  /api/v1/webhooks/github"    "/api/v1/webhooks/github"    GET  "200 400 405 501" true
  live_check "GET  /api/v1/assignment/pick-qa" "/api/v1/assignment/pick-qa" GET  "200 400 405 501" true
  # A near-miss on /health (not an exact match) must NOT be proxied. It 404s
  # because the TLS server has no `location /`.
  live_check "GET  /healthz"                   "/healthz"                   GET  "403 404" false

  # The deny response must be the documented JSON, and must not enumerate routes.
  dbody=$(curl -sk --max-time 5 -H 'Host: boardman.deepiri.com' \
    "https://127.0.0.1:$TLS_PORT/api/v1/tasks")
  if grep -q 'not exposed on the public vhost' <<<"$dbody"; then
    printf '  ok    %-42s %s\n' "403 body is the documented JSON" ""
  else
    printf '  FAIL  %-42s got %s\n' "403 body is the documented JSON" "$dbody"
    failures=$((failures + 1))
  fi

  # Port 80 must redirect to https rather than serve anything.
  redir=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -H 'Host: boardman.deepiri.com' "http://127.0.0.1:$HTTP_PORT/api/v1/tasks")
  if [[ "$redir" == "301" ]]; then
    printf '  ok    %-42s %s (http -> https)\n' "GET  /api/v1/tasks over http" "$redir"
  else
    printf '  FAIL  %-42s expected 301, got %s\n' "GET  /api/v1/tasks over http" "$redir"
    failures=$((failures + 1))
  fi

  # Deliberately NOT resetting the trap here: doing so would drop the
  # `nginx -s stop` from cleanup() and leave the ports bound for the next run.
fi

printf '\n'
for r in "${RESULTS[@]}"; do printf '%s\n' "$r"; done

if [[ "$failures" -gt 0 ]]; then
  printf '\nnginx validation FAILED for %d problem(s)\n' "$failures"
  exit 1
fi
if [[ -n "$RENDER_DIR" ]]; then
  printf '\nrendered %d file(s) to %s\n' "${#FILES[@]}" "$RENDER_DIR"
  exit 0
fi
printf '\nnginx validation passed for %d file(s)\n' "${#FILES[@]}"
