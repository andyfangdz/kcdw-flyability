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
codex_log="$VAR_DIR/codex.${run_id}.log"
typesafe_dir="$VAR_DIR/typesafe.${run_id}"
managed_radar=0
if [[ -n "${RADAR_DIR:-}" ]]; then
  radar_dir="$RADAR_DIR"
else
  radar_dir="$VAR_DIR/radar.${run_id}"
  managed_radar=1
fi
cleanup() {
  run_rc=$?
  if ! python3 -m kcdw.feedback record "$VAR_DIR/agent-feedback.jsonl" "$run_id" "$snapshot" "$analysis" "$run_rc"; then
    log "feedback=failed"
  fi
  if ! python3 -m kcdw.runs finish "$VAR_DIR" "$run_id" "$snapshot" "$analysis" "$prompt" "$radar_dir" "$codex_log" --typesafe "$typesafe_dir" --exit-code "$run_rc"; then
    log "archive=failed"
  elif [[ -d "$typesafe_dir" ]]; then
    rm -rf -- "$typesafe_dir"
  fi
  if [[ -f "$codex_log" ]]; then cat "$codex_log" >> "$VAR_DIR/codex.log"; fi
  rm -f "$codex_log" "$snapshot" "$prompt" "$analysis" "$html_tmp" "$health_tmp"
  if (( managed_radar )); then rm -rf -- "$radar_dir"; fi
}
trap cleanup EXIT
log() { printf '%s run=%s %s\n' "$(date -u +%FT%TZ)" "$run_id" "$*" >> "$VAR_DIR/update.log"; }

if [[ -n "${SNAPSHOT_FIXTURE:-}" ]]; then
  cp "$SNAPSHOT_FIXTURE" "$snapshot"
else
  python3 -m kcdw.collector --output "$snapshot" --radar-dir "$radar_dir" --cache-dir "$VAR_DIR/cache/nbm"
fi
collected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["collected_at"])' "$snapshot")"
log "collector=success collected_at=$collected"
if ! python3 -c 'import json,sys; from kcdw.validation import validate_snapshot_readiness; validate_snapshot_readiness(json.load(open(sys.argv[1])))' "$snapshot"; then
  log "source_readiness=failed publication=preserved"
  exit 1
fi
log "source_readiness=success"
python3 scripts/build_prompt.py "$snapshot" "$prompt"

if ! radar_validation="$(python3 -m kcdw.radar_evidence validate "$snapshot" "$radar_dir")"; then
  log "radar_attachments=failed publication=preserved"
  exit 1
fi
read -r radar_state radar_count extra <<< "$radar_validation"
if [[ -n "${extra:-}" || ! "$radar_state" =~ ^(ok|failed|absent)$ || ! "$radar_count" =~ ^[0-6]$ ]]; then
  log "radar_attachments=failed reason=invalid_validator_output publication=preserved"
  exit 1
fi
radar_files=()
for (( attachment=1; attachment<=radar_count; attachment++ )); do
  printf -v frame_name 'frame-%02d.png' "$attachment"
  radar_files+=("$radar_dir/$frame_name")
done
radar_args=()
for frame in "${radar_files[@]}"; do radar_args+=(--image "$frame"); done
log "radar_attachments=$radar_count radar_source=$radar_state"

agent_version="$(python3 -m kcdw.claude_agent --version-only 2>/dev/null | head -n 1)"
log "agent=claude-code model=claude-fable-5-1 effort=high fallback=codex fallback_model=gpt-6-astra agent_version=${agent_version:-unknown}"
set +e
timeout "${AGENT_TIMEOUT:-25m}" python3 -m kcdw.assessment_agent --research --snapshot "$snapshot" --var "$VAR_DIR" --artifacts "$typesafe_dir" --prompt "$prompt" --schema schema/analysis.schema.json --output "$analysis" --log "$codex_log" "${radar_args[@]}" >>"$codex_log" 2>&1
codex_rc=$?
set -e
log "agent_exit=$codex_rc"
if (( codex_rc != 0 )); then log "validation=not_run publication=preserved"; exit "$codex_rc"; fi

previous_args=()
cloud_config="${CLOUD_PUBLISH_CONFIG:-$VAR_DIR/cloudflare.json}"
if [[ -f "$cloud_config" ]]; then
  if python3 -m kcdw.cloud_publish --config "$cloud_config" previous "$VAR_DIR/cloud-previous.json"; then
    previous_args=(--previous "$VAR_DIR/cloud-previous.json")
    log "previous_assessment=cloudflare"
  else
    log "previous_assessment=cloudflare_unavailable fallback=local"
  fi
fi
if (( ${#previous_args[@]} )); then
  :
elif [[ -f "$VAR_DIR/current/analysis.json" ]]; then
  previous_args=(--previous "$VAR_DIR/current/analysis.json")
elif [[ -f "$VAR_DIR/latest-analysis.json" ]]; then
  previous_args=(--previous "$VAR_DIR/latest-analysis.json")
fi
if ! python3 -m kcdw.renderer "$snapshot" "$analysis" --output "$html_tmp" --health "$health_tmp" "${previous_args[@]}"; then
  log "validation=failed publication=preserved"
  exit 1
fi
log "validation=success"
if ! python3 -m kcdw.runs publish "$VAR_DIR" "$run_id" "$snapshot" "$analysis" "$prompt" "$radar_dir" "$codex_log" --typesafe "$typesafe_dir" --public "$PUBLIC_DIR" --html "$html_tmp" --health "$health_tmp" "${previous_args[@]}"; then
  log "publication=failed"
  exit 1
fi
log "publication=success"

if [[ -f "$cloud_config" ]]; then
  if python3 -m kcdw.cloud_publish --config "$cloud_config" publish "$VAR_DIR/runs/$run_id"; then
    log "cloud_publication=success"
  else
    log "cloud_publication=failed remote_report=preserved retry=backfill"
    exit 1
  fi
fi
