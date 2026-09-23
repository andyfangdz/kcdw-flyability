#!/usr/bin/env python3
"""Collect recent interim WN3 runs at KCDW without publishing a report.

Uses the bounded BigQuery transport and cache. Each run retains its own
initialization, 48-hour horizon and original query provenance.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kcdw.weathernext3 import FIELD_SPECS, OPTIONAL_FIELD_SPECS, STATISTICS
from kcdw.weathernext3_bigquery import BigQueryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    store = BigQueryStore()
    runs = [r for r in store.candidates(now, include_interim=True) if r.hour % 6][:args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    specs = FIELD_SPECS | OPTIONAL_FIELD_SPECS
    arrays = [f'{spec.array}_{stat}' for spec in specs.values() for stat in STATISTICS]
    manifest = dict(version=1, checked_at=now.isoformat(), grid_point={'latitude':40.9, 'longitude':-74.3},
                    horizon_hours=48, fields={k:dict(unit=v.unit, source_unit=v.source_unit,
                    source_array=v.array) for k,v in specs.items()}, runs=[])
    for run in runs:
        name = run.strftime('%Y%m%dT%H%M%SZ')
        try:
            result = store.fetch(run.isoformat(), arrays)
            path = args.output / (name+'.json')
            path.write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
            entry = dict(init_time=run.isoformat(), file=path.name, status='available',
                         first_valid=result['rows'][0]['valid_time'], last_valid=result['rows'][-1]['valid_time'],
                         hours=len(result['rows']), query=result['provenance'])
        except LookupError:
            entry = dict(init_time=run.isoformat(), status='unavailable', reason='run no longer published')
        manifest['runs'].append(entry)
        print(json.dumps(entry), flush=True)
        # Preserve successfully gathered runs even if a later query fails.
        (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False)+'\n')
    if not runs:
        (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    main()
