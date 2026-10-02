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
# Newer cycles where the archives have them (GEFS and ECMWF ENS are 00Z-only; WN2's later cycles publish too late to use).
for d in gfs aifs aifs_ens hrrr; do $PY research/mos/extract_dynamical.py "$d" --cycles 6 12 18 --start "$since" --threads 8 | tail -2; done
$PY research/mos/extract_wn2.py "$since-01" | tail -2
$PY research/mos/extract_wn3.py "$since-01" | tail -2
MOS_CYCLES=6,12,18 $PY research/mos/extract_wn3.py "$since-01" | tail -2
# RRFS (NAM's replacement from 2026-10-14) is archived for all research stations but not yet a model input.
$PY research/mos/extract_rrfs.py --start "$since" --procs 6 | tail -2 || log "RRFS archive refresh failed; continuing"
$PY research/mos/build_targets.py | head -1
# Issue-time rows: each update (04/11/16/22Z) uses every source's newest run published by then (build_issue.py).
$PY research/mos/build_issue.py | tail -1
age() { [ -f "$1" ] && echo $(( $(date +%s) - $(stat -c %Y "$1") )) || echo 999999999; }
# Training runs on the desktop GPU (scripts/mos_gpu.sh). If the desktop is unreachable the previous models stay
# in use and the age checks retry at the next update.
if [ "$(age var/mos/conformal.json)" -gt $((7 * 86400)) ]; then
  log "weekly cross-validation (desktop GPU)"
  if scripts/mos_gpu.sh train | tail -3; then
    $PY research/mos/conformal.py | tail -1
    $PY research/mos/evaluate.py | tail -1
  else
    log "WARNING: cross-validation on the desktop failed; keeping the previous widths and skill"
  fi
fi
if [ "$(age var/mos/artifacts/meta.json)" -gt $((20 * 3600)) ]; then
  log "daily retrain (desktop GPU)"
  scripts/mos_gpu.sh model || log "WARNING: retrain on the desktop failed; keeping the models trained $(stat -c %y var/mos/artifacts/meta.json | cut -c1-16)"
fi
$PY research/mos/forecast.py
log done
