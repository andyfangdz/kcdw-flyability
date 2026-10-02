#!/bin/bash
# Run a calibrated-wind training step on the desktop GPU (andydesktop: Windows + WSL Ubuntu, RTX 5090) and
# bring its outputs back; the homeserver only forecasts. The local outputs are replaced only after the remote
# run succeeds, so any failure (desktop asleep, off the network) leaves the previous models in place.
# Usage: scripts/mos_gpu.sh model|train
# One-time desktop setup: ~/mos/venv with requirements-mos.txt and a CUDA build of XGBoost (same version as var/mos-venv).
set -euo pipefail
cd "$(dirname "$0")/.."
step=${1:-}
case $step in
  model) outputs='var/mos/artifacts' ;;
  train) outputs='var/mos/oof.parquet var/mos/cv-results.json' ;;
  *) echo "usage: $0 model|train" >&2; exit 2 ;;
esac
SSH=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=60 -o LogLevel=ERROR)
HOST=andydesktop
DROP=/mnt/c/Users/andyf/mos_transfer/prod  # Windows-side transfer folder (scp's mos_transfer/prod), seen from WSL
job=var/mos/gpu  # not /tmp: the service has a private /tmp and may write only under var/
rm -rf "$job" && mkdir -p "$job/out"
tar czf "$job/code.tgz" research/mos/*.py
ssh "${SSH[@]}" $HOST "wsl -d Ubuntu -- mkdir -p $DROP"
scp -q "${SSH[@]}" "$job/code.tgz" var/mos/table_issue.parquet "$HOST:mos_transfer/prod/"
# WSL lives only as long as this ssh session, so the job runs in the foreground.
ssh "${SSH[@]}" $HOST "wsl -d Ubuntu -- bash -s" <<EOF
set -euo pipefail
mkdir -p ~/mos/prod && cd ~/mos/prod
rm -rf research var/mos && mkdir -p var/mos
tar xzf $DROP/code.tgz && mv $DROP/table_issue.parquet var/mos/
PYTHONPATH=research/mos PYTHONWARNINGS=ignore MOS_DEVICE=cuda ~/mos/venv/bin/python research/mos/$step.py
tar czf $DROP/out.tgz $outputs
EOF
scp -q "${SSH[@]}" "$HOST:mos_transfer/prod/out.tgz" "$job/"
tar xzf "$job/out.tgz" -C "$job/out"
for path in $outputs; do
  if [ -d "$job/out/$path" ]; then
    rm -rf "$path.old"; [ -e "$path" ] && mv "$path" "$path.old"
    mv "$job/out/$path" "$path" && rm -rf "$path.old"
  else
    mv "$job/out/$path" "$path"
  fi
done
rm -rf "$job"
