#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8794}"
case "$PORT" in (*[!0-9]*|'') echo "PORT must be numeric" >&2; exit 2;; esac
if (( PORT < 1 || PORT > 65535 )); then echo "PORT out of range" >&2; exit 2; fi
cd "$ROOT"
exec python3 -m kcdw.server --port "$PORT" --public "${PUBLIC_DIR:-$ROOT/public}" --var "${VAR_DIR:-$ROOT/var}"
