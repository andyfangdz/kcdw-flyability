"""Isolated Earth Engine worker: archived ECMWF IFS runs at KCDW in one server-side query.

Runs in the Earth Engine environment (var/gfs-earth-engine-venv). Request (stdin
JSON): {"creation_hour": 0|12, "lead": int, "since": "YYYY-MM-DD"}. Returns, for
every run with that creation hour since the date, 10 m u/v (m/s) at ``lead``
and 10fg (m/s) at ``lead + 6``.

Earth Engine's ECMWF/NRT_FORECAST/IFS/OPER rows are georeferenced one 0.25-degree
row north of the GRIB grid (verified 2026-09-24 against ecCodes and the land-sea
mask along 73W). Sampling at ROW_SHIFT north of the true point therefore reads
the true 41.0N/74.25W cell. The caller cross-checks overlapping dates against
native GRIB before using any value, so a future fix upstream cannot pass silently.
"""
import json
import signal
import sys

COLLECTION = 'ECMWF/NRT_FORECAST/IFS/OPER'
TRUE_POINT = (-74.25, 41.0)
ROW_SHIFT = 0.25
TRANSFORM = [0.25, 0, -180.125, 0, -0.25, 90.125]
PROJECT = 'aviation-486817'
WIND = ['u_component_of_wind_10m_sfc', 'v_component_of_wind_10m_sfc']
GUST = 'max_10m_wind_gust_since_last_post_processing_sfc'


def require(ok):
    if not ok:
        raise ValueError('earth engine ecmwf validation failed')


def main():
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(300)
    request = json.loads(sys.stdin.read(4096))
    require(set(request) == {'creation_hour', 'lead', 'since'})
    hour, lead, since = request['creation_hour'], request['lead'], request['since']
    require(hour in (0, 12) and type(lead) is int and 0 <= lead <= 354 and lead % 6 == 0 and len(since) == 10)
    import ee
    import google.auth
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform',
                                                 'https://www.googleapis.com/auth/earthengine'])
    ee.Initialize(credentials=credentials, project=PROJECT)
    base = ee.ImageCollection(COLLECTION).filter(ee.Filter.eq('creation_hour', hour)).filterDate(since, '2100-01-01')
    point = ee.Geometry.Point([TRUE_POINT[0], TRUE_POINT[1] + ROW_SHIFT])

    def sample(step, bands):
        collection = base.filter(ee.Filter.eq('forecast_hours', step)).select(bands)
        first = collection.first().select(bands[0]).projection().getInfo()
        require(first['transform'] == TRANSFORM)
        feature = lambda img: ee.Feature(None, img.reduceRegion(ee.Reducer.first(), point, crs=img.select(bands[0]).projection())
                                         ).set('created', img.get('creation_time'))
        return {f['properties']['created']: f['properties'] for f in collection.map(feature).getInfo()['features']}

    wind, gust = sample(lead, WIND), sample(lead + 6, [GUST])
    out = []
    for created in sorted(set(wind) & set(gust)):
        w, g = wind[created], gust[created]
        if all(w.get(b) is not None for b in WIND) and g.get(GUST) is not None:
            out.append({'created_ms': created, 'u': w[WIND[0]], 'v': w[WIND[1]], 'gust': g[GUST]})
    print(json.dumps(out, allow_nan=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
