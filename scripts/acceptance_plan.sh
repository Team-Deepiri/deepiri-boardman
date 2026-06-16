#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

failures=0

pass() {
  printf 'PASS %s\n' "$1"
}

fail() {
  failures=$((failures + 1))
  printf 'FAIL %s\n' "$1"
}

printf 'Boardman meeting-plan offline acceptance\n'
printf 'Repo: %s\n\n' "$ROOT"

if command -v poetry >/dev/null 2>&1; then
  runner=(poetry run pytest)
  pass "test runner selected: poetry"
elif [[ -x "$ROOT/.venv/bin/pytest" ]]; then
  runner=("$ROOT/.venv/bin/pytest")
  pass "test runner selected: .venv/bin/pytest"
elif command -v uv >/dev/null 2>&1; then
  runner=(
    env PYTHONPATH="$ROOT"
    uv run --no-project
    --with pytest
    --with pytest-asyncio
    --with greenlet
    --with httpx
    --with pydantic
    --with pydantic-settings
    --with sqlalchemy
    --with aiosqlite
    --with fastapi
    --with langchain-core
    python -m pytest
  )
  pass "test runner selected: uv"
else
  fail "missing test runner: install poetry, create .venv, or install uv"
  exit 1
fi

planning_tests=(
  tests/test_planning_integration.py
  tests/test_planning_aggregator.py
  tests/test_planning_context_sync.py
  tests/test_planning_context_direction.py
  tests/test_planning_plan_output.py
  tests/test_planning_planner.py
  tests/test_planning_service.py
  tests/test_plans_route.py
  tests/test_planning_agent_tool.py
)

if "${runner[@]}" "${planning_tests[@]}" -q; then
  pass "planning pytest suite passed (${#planning_tests[@]} modules)"
else
  fail "planning pytest suite failed"
fi

printf '\nPlanning acceptance complete: %s failure(s)\n' "$failures"
if [[ "$failures" -gt 0 ]]; then
  exit 1
fi
printf 'Result: Meeting-plan offline acceptance PASS\n'
