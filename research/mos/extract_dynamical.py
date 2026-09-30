"""Point forecasts at KCDW from dynamical.org Zarr archives, one 00Z run per day, resumable.

For each dataset and 00Z initialization, reads the grid point nearest KCDW for
every lead out to MAX_LEAD_H and writes one parquet shard per month to
var/mos/models/<dataset>/YYYY-MM.parquet. Winds are kept as u/v components;
ensembles are reduced per lead to member statistics (mean, std, p10, p50, p90,
plus speed statistics computed member by member before any averaging).

Usage: var/mos-venv/bin/python research/mos/extract_dynamical.py DATASET [--start YYYY-MM] [--threads N]
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

KCDW = (40.8752, -74.2814)
MAX_LEAD_H = 192
OUT = Path('var/mos/models')
DATASETS = {
    'gfs': ('noaa-gfs-forecast', ['wind_u_10m', 'wind_v_10m', 'wind_u_100m', 'wind_v_100m', 'temperature_2m', 'temperature_80m',
                                  'relative_humidity_2m', 'total_cloud_cover_atmosphere', 'downward_short_wave_radiation_flux_surface',
                                  'geopotential_height_cloud_ceiling', 'pressure_reduced_to_mean_sea_level', 'precipitation_surface']),
    'gefs': ('noaa-gefs-forecast-35-day', ['wind_u_10m', 'wind_v_10m', 'wind_gust_surface', 'wind_u_100m', 'wind_v_100m',
                                           'temperature_2m', 'temperature_80m', 'total_cloud_cover_atmosphere',
                                           'downward_short_wave_radiation_flux_surface']),
    'ifs_ens': ('ecmwf-ifs-ens-forecast-15-day-0-25-degree', ['wind_u_10m', 'wind_v_10m', 'wind_gust_10m', 'wind_u_100m', 'wind_v_100m',
                                                              'temperature_2m', 'dew_point_temperature_2m', 'temperature_925hpa',
                                                              'temperature_850hpa', 'total_cloud_cover_atmosphere',
                                                              'downward_short_wave_radiation_flux_surface', 'pressure_reduced_to_mean_sea_level']),
    'aifs': ('ecmwf-aifs-single-forecast', ['wind_u_10m', 'wind_v_10m', 'wind_u_100m', 'wind_v_100m', 'temperature_2m',
                                            'dew_point_temperature_2m', 'temperature_925hpa', 'temperature_850hpa',
                                            'total_cloud_cover_atmosphere', 'downward_short_wave_radiation_flux_surface']),
    'aifs_ens': ('ecmwf-aifs-ens-forecast', ['wind_u_10m', 'wind_v_10m', 'wind_u_100m', 'wind_v_100m', 'temperature_2m',
                                             'dew_point_temperature_2m', 'temperature_925hpa', 'temperature_850hpa',
                                             'total_cloud_cover_atmosphere']),
    'hrrr': ('noaa-hrrr-forecast-48-hour', ['wind_u_10m', 'wind_v_10m', 'wind_u_80m', 'wind_v_80m', 'wind_gust_surface', 'temperature_2m',
                                            'dew_point_temperature_2m', 'total_cloud_cover_atmosphere',
                                            'downward_short_wave_radiation_flux_surface', 'geopotential_height_cloud_ceiling']),
}
KNOTS = 3600 / 1852


def point_selector(ds):
    """Index selector for the grid point nearest KCDW (regular lat/lon or projected x/y with 2-D coordinates)."""
    if 'latitude' in ds.dims:
        lat = float(ds.latitude.sel(latitude=KCDW[0], method='nearest'))
        lon = float(ds.longitude.sel(longitude=KCDW[1], method='nearest'))
        return {'latitude': lat, 'longitude': lon}, (lat, lon), 'sel'
    lat2, lon2 = ds.latitude.values, ds.longitude.values
    iy, ix = np.unravel_index(np.argmin((lat2 - KCDW[0]) ** 2 + ((lon2 - KCDW[1]) * np.cos(np.radians(KCDW[0]))) ** 2), lat2.shape)
    return {'y': int(iy), 'x': int(ix)}, (float(lat2[iy, ix]), float(lon2[iy, ix])), 'isel'


def reduce(frame, member_dim):
    """Member statistics per lead; wind speeds are computed per member before averaging."""
    out = {}
    speeds = {}
    for u, v, name in (('wind_u_10m', 'wind_v_10m', 'speed_10m'), ('wind_u_100m', 'wind_v_100m', 'speed_100m')):
        if u in frame:
            speeds[name] = np.hypot(frame[u], frame[v]) * KNOTS
            out[u + '_mean'], out[v + '_mean'] = frame[u].mean(member_dim), frame[v].mean(member_dim)
    for name, values in list(speeds.items()) + [(k, frame[k]) for k in frame.data_vars if k not in
                                                 ('wind_u_10m', 'wind_v_10m', 'wind_u_100m', 'wind_v_100m')]:
        if name.startswith('wind_gust'):
            values = values * KNOTS
        out[name + '_mean'] = values.mean(member_dim)
        out[name + '_std'] = values.std(member_dim)
        for q in (10, 50, 90):
            out[f'{name}_p{q}'] = values.quantile(q / 100, dim=member_dim).drop_vars('quantile')
    return out


def extract(ds, variables, init, selector, how, leads, member_dim):
    sub = ds[variables].sel(init_time=init)
    sub = (sub.sel(selector) if how == 'sel' else sub.isel(selector)).isel(lead_time=leads).load()
    cols = reduce(sub, member_dim) if member_dim else {k: sub[k] for k in variables}
    frame = pd.DataFrame({k: np.asarray(v.values, dtype='float32') for k, v in cols.items()})
    frame.insert(0, 'lead_h', (sub.lead_time.values / np.timedelta64(1, 'h')).astype('int16'))
    frame.insert(0, 'init', pd.Timestamp(init))
    return frame


def station_indices(ds, stations):
    """Nearest grid indices for each station: {dim: DataArray(dims='station')} for pointwise isel."""
    import xarray as xr
    if 'latitude' in ds.dims:
        lat, lon = ds.latitude.values, ds.longitude.values
        iy = [int(np.abs(lat - s['lat']).argmin()) for s in stations.values()]
        ix = [int(np.abs(((lon - s['lon']) + 180) % 360 - 180).argmin()) for s in stations.values()]
        dims = ('latitude', 'longitude')
    else:
        lat2, lon2 = ds.latitude.values, ds.longitude.values
        flat = [int(np.argmin((lat2 - s['lat']) ** 2 + ((lon2 - s['lon']) * np.cos(np.radians(s['lat']))) ** 2)) for s in stations.values()]
        iy, ix = zip(*(np.unravel_index(f, lat2.shape) for f in flat))
        dims = ('y', 'x')
    return {dims[0]: xr.DataArray(list(iy), dims='station'), dims[1]: xr.DataArray(list(ix), dims='station')}


def extract_points(ds, variables, init, indexers, names, leads, member_dim):
    """All stations from one read: the tiles covering the region are loaded once."""
    sub = ds[variables].sel(init_time=init).isel(lead_time=leads).isel(indexers).load()
    frames = []
    for i, station in enumerate(names):
        one = sub.isel(station=i)
        cols = reduce(one, member_dim) if member_dim else {k: one[k] for k in variables}
        frame = pd.DataFrame({k: np.asarray(v.values, dtype='float32') for k, v in cols.items()})
        frame.insert(0, 'lead_h', (one.lead_time.values / np.timedelta64(1, 'h')).astype('int16'))
        frame.insert(0, 'init', pd.Timestamp(init))
        frame.insert(0, 'station', station)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def main():
    import dynamical_catalog as dc
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', choices=sorted(DATASETS))
    parser.add_argument('--start', default=None, help='first month YYYY-MM')
    parser.add_argument('--threads', type=int, default=12)
    parser.add_argument('--stations', default=None, help='stations.json for a multi-station extraction')
    parser.add_argument('--out', default=str(OUT), help='output root (default var/mos/models)')
    parser.add_argument('--cycles', type=int, nargs='+', default=[0], help='init hours; other than [0] they go to <dataset>_c<hours>')
    args = parser.parse_args()
    name, variables = DATASETS[args.dataset]
    ds = dc.open(name, chunks=None)
    variables = [v for v in variables if v in ds.data_vars]
    selector, grid, how = point_selector(ds)
    if args.stations:
        import json
        stations = json.loads(Path(args.stations).read_text())
        indexers, names = station_indices(ds, stations), list(stations)
        grid = f'{len(names)} stations'
    member_dim = 'ensemble_member' if 'ensemble_member' in ds.dims else None
    leads = np.where(ds.lead_time.values / np.timedelta64(1, 'h') <= MAX_LEAD_H)[0]
    inits = pd.DatetimeIndex(ds.init_time.values)
    inits = inits[inits.hour.isin(args.cycles)]
    if args.start:
        inits = inits[inits >= pd.Timestamp(args.start + '-01')]
    suffix = '' if args.cycles == [0] else '_c' + ''.join(f'{c:02d}' for c in args.cycles)
    out_dir = Path(args.out) / (args.dataset + suffix)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f'{args.dataset}: {name} grid point {grid}, {len(variables)} variables, {len(leads)} leads, '
          f'{len(inits)} runs (cycles {args.cycles}) {inits[0]:%Y-%m-%d}..{inits[-1]:%Y-%m-%d}', flush=True)
    current = pd.Timestamp.utcnow().tz_localize(None).strftime('%Y-%m')
    for month, group in pd.Series(inits, index=inits).groupby(inits.strftime('%Y-%m')):
        path = out_dir / f'{month}.parquet'
        if path.exists() and month != current:
            continue
        started = time.time()

        def one(init):
            for attempt in range(4):
                try:
                    if args.stations:
                        return extract_points(ds, variables, init, indexers, names, leads, member_dim)
                    return extract(ds, variables, init, selector, how, leads, member_dim)
                except Exception as exc:
                    error = exc
                    time.sleep(5 * (attempt + 1))
            print(f'  {init} failed: {type(error).__name__}: {error}', flush=True)
            return None

        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            frames = [f for f in pool.map(one, group.values) if f is not None]
        if frames:
            table = pd.concat(frames, ignore_index=True)
            table.attrs['grid'] = str(grid)
            table.to_parquet(path, index=False)
        print(f'  {month}: {len(frames)}/{len(group)} runs in {time.time() - started:.0f} s', flush=True)


if __name__ == '__main__':
    sys.exit(main())
