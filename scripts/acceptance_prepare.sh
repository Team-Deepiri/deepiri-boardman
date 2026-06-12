#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_FILE="boardman.acceptance.db"

if [[ -d "$DB_FILE" ]]; then
  rm -rf "$DB_FILE"
fi

: > "$DB_FILE"
chmod 600 "$DB_FILE"

echo "Prepared offline acceptance SQLite file: ${DB_FILE}"
