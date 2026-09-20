#!/usr/bin/env bash
# Run project commands with the same runtime/auth paths as homeserver units.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$ROOT/var/runtime-venv/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/kcdw-flyability/google-adc.json"
export CLAUDE_CONFIG_DIR="$HOME/.config/kcdw-flyability/claude"
export CLAUDE_BIN="$HOME/.local/bin/claude"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
umask 077
cd "$ROOT"
if (( $# == 0 )); then
  printf 'Usage: scripts/with-runtime.sh COMMAND [ARGS...]\n' >&2
  exit 2
fi
exec "$@"
