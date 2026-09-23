"""Supplement the saved checkride snapshot with native hourly GFS wind samples.

Run from the repository root with var/native-weather-venv/bin/python.
Uses the existing bounded byte-range fetcher and GRIB identity validation.
Writes research artifacts only; does not refresh or publish the app.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from kcdw.event_wind import wind_evidence
from kcdw.native_wind import native_wind_evidence
from kcdw.native_wind_worker import sample

OUT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / 'var/events/commercial-checkride/runs/20260921T042546Z.896948/snapshot.json'
KT = 3600 / 1852


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def main():
    raw = SNAPSHOT.read_bytes()
    snapshot = json.loads(raw)
    now = datetime.fromisoformat(snapshot['collected_at'].replace('Z', '+00:00'))
    native = snapshot['native_wind']['models']['gfs']
    init = datetime.fromisoformat(native['init'].replace('Z', '+00:00'))
    assert init == datetime(2026, 9, 21, tzinfo=timezone.utc)
    evidence = {
        'prepared_at': stamp(),
        'snapshot_path': str(SNAPSHOT.relative_to(ROOT)),
        'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
        'snapshot_collected_at': snapshot['collected_at'],
        'event': snapshot['event'], 'event_timing': snapshot['event_timing'],
        'wind': wind_evidence(snapshot, now),
        'native_wind': native_wind_evidence(snapshot, now),
        'native_wind_raw': snapshot['native_wind'],
        'model_matrix': snapshot['model_matrix'],
        'wind_trends': snapshot['wind_trends'],
        'nws_discussion': snapshot['event_afds']['offices']['OKX'],
    }
    assert evidence['wind'] and evidence['native_wind']
    save('evidence.json', evidence)

    # The app samples every three hours; retrieve intervening native hourly
    # steps, which GFS publishes through lead 120. Never interpolate gusts here.
    by_lead = {s['lead']: dict(s, fetched_at=native['fetched_at']) for s in native['samples']}
    missing = [h for h in range(84, 91) if h not in by_lead]
    def get(lead):
        try:
            return lead, dict(sample('gfs', init, lead), fetched_at=stamp())
        except Exception as exc:
            return lead, {'lead': lead, 'error': str(exc), 'attempted_at': stamp()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        by_lead.update(dict(pool.map(get, missing)))
    packet = {'init': native['init'], 'model': 'GFS', 'samples': [by_lead[h] for h in range(84, 91)]}
    save('gfs-hourly-native.json', packet)
    for s in packet['samples']:
        if 'error' in s:
            print('FAILED', s['lead'], s['error'], flush=True)
            continue
        f = s['fields']
        u, v = f['10u']['value'], f['10v']['value']
        gust = f.get('gust', {}).get('value')
        print(s['at'], 'wind_kt', round(math.hypot(u, v) * KT, 1),
              'from_true', round(math.degrees(math.atan2(-u, -v)) % 360, 1),
              'gust_kt', None if gust is None else round(gust * KT, 1),
              '925_kt', round(math.hypot(f['u925']['value'], f['v925']['value']) * KT, 1), flush=True)

    url = 'https://api.weather.gov/gridpoints/OKX/23,48'
    try:
        with urlopen(Request(url, headers={'User-Agent': 'KCDW flyability gust research'}), timeout=25) as response:
            current = json.load(response)
        save('nws-grid.json', {'url': url, 'fetched_at': stamp(), 'raw': current})
        print('NWS updated', current['properties']['updateTime'], flush=True)
    except Exception as exc:
        save('nws-fetch-error.json', {'url': url, 'attempted_at': stamp(), 'error': str(exc)})
        print('NWS refresh failed', str(exc), flush=True)


if __name__ == '__main__':
    main()
