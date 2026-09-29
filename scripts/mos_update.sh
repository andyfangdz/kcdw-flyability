#!/bin/bash
# Calibrated KCDW wind: refresh recent archives, rebuild the table, retrain daily, re-score weekly, forecast.
# Runs in the isolated research environment (var/mos-venv, requirements-mos.txt); see research/mos/README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=var/mos-venv/bin/python
export PYTHONPATH=research/mos PYTHONWARNINGS=ignore
log() { echo "$(date -u +%FT%TZ) $*"; }
# journald drops lines from short-lived pipeline processes; keep a plain log too.
exec > >(tee -a var/mos/update.log) 2>&1
# The month three days ago covers runs issued just before a month boundary.
since=$(date -u -d '3 days ago' +%Y-%m)
log "refresh from $since"
$PY research/mos/fetch_obs.py
$PY research/mos/fetch_metar.py
for d in gfs gefs ifs_ens aifs aifs_ens hrrr; do $PY research/mos/extract_dynamical.py "$d" --start "$since" --threads 8 | tail -2; done
$PY research/mos/extract_wn2.py "$since-01" | tail -2
$PY research/mos/extract_wn3.py "$since-01" | tail -2
$PY research/mos/fetch_openmeteo.py
$PY research/mos/build_targets.py | head -1
$PY research/mos/build_features.py | tail -1
age() { [ -f "$1" ] && echo $(( $(date +%s) - $(stat -c %Y "$1") )) || echo 999999999; }
if [ "$(age var/mos/conformal.json)" -gt $((7 * 86400)) ]; then
  log "weekly cross-validation"
  $PY research/mos/train.py --trees 500 | tail -3
  $PY research/mos/conformal.py | tail -1
  $PY research/mos/evaluate.py | tail -1
fi
if [ "$(age var/mos/artifacts/meta.json)" -gt $((20 * 3600)) ]; then
  log "daily retrain"
  $PY research/mos/model.py
fi
$PY research/mos/forecast.py
log done
