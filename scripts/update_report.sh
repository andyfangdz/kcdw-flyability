#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
VAR_DIR="${VAR_DIR:-var}"
PUBLIC_DIR="${PUBLIC_DIR:-public}"
mkdir -p "$VAR_DIR" "$PUBLIC_DIR"
exec 9>"$VAR_DIR/update.lock"
if ! flock -n 9; then
  printf '%s event=skip reason=overlap\n' "$(date -u +%FT%TZ)" >> "$VAR_DIR/update.log"
  exit 0
fi

run_id="$(date -u +%Y%m%dT%H%M%SZ).$$"
snapshot="$VAR_DIR/snapshot.${run_id}.json"
prompt="$VAR_DIR/prompt.${run_id}.txt"
analysis="$VAR_DIR/analysis.${run_id}.json"
html_tmp="$VAR_DIR/index.${run_id}.html"
health_tmp="$VAR_DIR/health.${run_id}.json"
cleanup() { rm -f "$snapshot" "$prompt" "$analysis" "$html_tmp" "$health_tmp"; }
trap cleanup EXIT
log() { printf '%s run=%s %s\n' "$(date -u +%FT%TZ)" "$run_id" "$*" >> "$VAR_DIR/update.log"; }

if [[ -n "${SNAPSHOT_FIXTURE:-}" ]]; then
  cp "$SNAPSHOT_FIXTURE" "$snapshot"
else
  python3 -m kcdw.collector --output "$snapshot"
fi
collected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["collected_at"])' "$snapshot")"
log "collector=success collected_at=$collected"
if ! python3 -c 'import json,sys; from kcdw.validation import validate_snapshot_readiness; validate_snapshot_readiness(json.load(open(sys.argv[1])))' "$snapshot"; then
  log "source_readiness=failed publication=preserved"
  exit 1
fi
log "source_readiness=success"
python3 scripts/build_prompt.py "$snapshot" "$prompt"

codex_bin="${CODEX_BIN:-codex}"
codex_version="$($codex_bin --version 2>&1 | head -n 1 | tr -cd '[:alnum:]. _/-')"
log "codex_version=${codex_version:-unknown}"
set +e
timeout "${CODEX_TIMEOUT:-12m}" "$codex_bin" exec --ephemeral --sandbox read-only --color never --output-schema schema/analysis.schema.json --output-last-message "$analysis" - < "$prompt" >>"$VAR_DIR/codex.log" 2>&1
codex_rc=$?
set -e
log "codex_exit=$codex_rc"
if (( codex_rc != 0 )); then log "validation=not_run publication=preserved"; exit "$codex_rc"; fi

if ! python3 -m kcdw.renderer "$snapshot" "$analysis" --output "$html_tmp" --health "$health_tmp"; then
  log "validation=failed publication=preserved"
  exit 1
fi
log "validation=success"
mv -f "$html_tmp" "$PUBLIC_DIR/index.html"
mv -f "$health_tmp" "$PUBLIC_DIR/health.json"
cp "$snapshot" "$VAR_DIR/latest-snapshot.json"
cp "$analysis" "$VAR_DIR/latest-analysis.json"
log "publication=success"
