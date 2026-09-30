"""Multi-station training table (research): the build_targets/build_features layout for every station.

Rows: station x local date x hour 06-21 x lead day 1-7, with the same model
features as the single-station table (station-matched grid points), plus
station latitude, longitude, elevation and an integer station code.
ICON/ECMWF HRES/GEM (Open-Meteo, daily-quota limited) exist for KCDW only and
are NaN elsewhere. Writes var/mos-multi/table/<STATION>.parquet.
"""
import glob
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import build_features as bf


ROOT = Path('var/mos-multi')
STATIONS = json.loads(Path(__file__).with_name('stations.json').read_text())
CODES = {name: i for i, name in enumerate(sorted(STATIONS))}
DATASETS = ('gfs', 'gefs', 'ifs_ens', 'aifs', 'aifs_ens', 'hrrr', 'wn2', 'wn3', 'ukmo', 'hres')
# Column prefix per dataset; Earth Engine HRES gets its own so it never collides with Open-Meteo's KCDW HRES columns.
PREFIX = {'hres': 'ifshres'}


def metars():
    con = duckdb.connect(); con.execute('INSTALL httpfs; LOAD httpfs;')
    urls = ', '.join(f"'https://data.source.coop/dynamical/asos-parquet/year={y}/data.parquet'" for y in range(2020, pd.Timestamp.now().year + 1))
    names = ', '.join(f"'{s}'" for s in STATIONS)
    return con.execute(f"""SELECT station, valid AT TIME ZONE 'UTC' AS valid_utc, drct, sknt, gust FROM read_parquet([{urls}])
                           WHERE station IN ({names})""").fetchdf()


def targets(station, metar_all):
    paths = sorted(glob.glob(str(ROOT / 'obs' / station / '*.csv')))
    frames = [pd.read_csv(p, usecols=['valid(UTC)', 'sknt', 'drct', 'gust_sknt'], na_values=['M', '']) for p in paths]
    m = pd.concat(frames, ignore_index=True).rename(columns={'valid(UTC)': 'valid'}) if frames else pd.DataFrame(columns=['valid', 'sknt', 'drct', 'gust_sknt'])
    m['valid'] = pd.to_datetime(m['valid'])
    for c in ('sknt', 'drct', 'gust_sknt'):
        m[c] = pd.to_numeric(m[c], errors='coerce')  # a few archive rows carry non-numeric codes
    m = m.dropna(subset=['sknt'])
    m = m[(m.sknt >= 0) & (m.sknt < 100) & (m.gust_sknt.isna() | ((m.gust_sknt >= m.sknt - 1) & (m.gust_sknt < 120)))]
    rad = np.radians(m.drct.fillna(0))
    m['u'], m['v'] = -m.sknt * np.sin(rad), -m.sknt * np.cos(rad)
    m['hour'] = m.valid.dt.floor('h')
    g = m.groupby('hour').agg(n=('sknt', 'size'), sust_mean=('sknt', 'mean'), sust_max=('sknt', 'max'),
                              gust_peak=('gust_sknt', 'max'), u_obs=('u', 'mean'), v_obs=('v', 'mean'))
    g = g[g.n >= 45].drop(columns='n')
    g['gust_peak'] = np.maximum(g.gust_peak.fillna(g.sust_max), g.sust_max)
    g['spread'] = g.gust_peak - g.sust_mean
    t = metar_all[metar_all.station == station].copy()
    t['valid'] = pd.to_datetime(t.valid_utc); t['hour'] = t.valid.dt.floor('h'); t['peak'] = np.fmax(t.sknt, t.gust)
    routine = t[t.valid.dt.minute.between(50, 56)].groupby('hour').agg(metar_sknt=('sknt', 'last'), metar_drct=('drct', 'last'))
    hourly = t.groupby('hour').agg(metar_gust=('gust', 'max'), metar_peak=('peak', 'max'))
    hourly['metar_gust_reported'] = hourly.metar_gust.notna().astype('int8')
    out = g.join(routine.join(hourly, how='outer'), how='outer')
    out.index.name = 'valid'
    return out


def dense_station(dataset, station, cache):
    if dataset not in cache:
        paths = sorted(glob.glob(str(ROOT / 'models' / dataset / '*.parquet')))
        cache[dataset] = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True) if paths else None
    frame = cache[dataset]
    if frame is None:
        return None
    frame = frame[(frame.station == station) & (frame.lead_h < bf.MAX_LEAD)].drop(columns='station')
    out = {}
    for var in frame.columns.drop(['init', 'lead_h']):
        wide = frame.pivot_table(index='init', columns='lead_h', values=var, aggfunc='first')
        wide = wide.reindex(columns=range(bf.MAX_LEAD)).interpolate(axis=1, limit_area='inside')
        out[var] = (wide.index, wide.to_numpy(dtype='float32'))
    return out


EXPOSURE = json.loads(Path(__file__).with_name('station_exposure.json').read_text())
SECTORS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
# Consensus forecast wind (u, v) for choosing the upwind sector, in preference order.
CONSENSUS = [('ifs_ens_wind_u_10m_mean', 'ifs_ens_wind_v_10m_mean'), ('gefs_wind_u_10m_mean', 'gefs_wind_v_10m_mean'),
             ('gfs_wind_u_10m', 'gfs_wind_v_10m'), ('wn2_wind_u_10m_mean', 'wn2_wind_v_10m_mean'), ('ukmo_wind_u_10m', 'ukmo_wind_v_10m'),
             ('hrrr_wind_u_10m', 'hrrr_wind_v_10m')]


def add_exposure(feats, station):
    """Static site descriptors, plus the land cover and terrain upwind of the consensus forecast direction."""
    e = EXPOSURE[station]
    n = len(next(iter(feats.values())))
    feats['stn_sea_km'] = np.full(n, e['sea_km'], 'float32')
    feats['stn_elev_minus_area'] = np.full(n, e['elev_minus_area_m'], 'float32')
    for name in ('near_tree', 'near_built', 'far_tree', 'far_water'):
        feats[f'stn_{name}_all'] = np.full(n, np.mean([e['sectors'][k][name] for k in SECTORS]), 'float32')
    u = np.full(n, np.nan, 'float32'); v = np.full(n, np.nan, 'float32')
    for cu, cv in CONSENSUS:
        if cu in feats and cv in feats:
            fill = np.isnan(u) & ~np.isnan(feats[cu])
            u[fill], v[fill] = feats[cu][fill], feats[cv][fill]
    direction = np.degrees(np.arctan2(-u, -v)) % 360  # wind comes FROM this bearing
    position = direction / 45.0
    lo = np.floor(position).astype('int64') % 8
    hi = (lo + 1) % 8
    w = (position - np.floor(position)).astype('float32')
    keys = [k for k in e['sectors']['N']]
    for key in keys:
        table = np.array([e['sectors'][s][key] for s in SECTORS], 'float32')
        value = (1 - w) * table[np.where(np.isnan(direction), 0, lo)] + w * table[np.where(np.isnan(direction), 0, hi)]
        feats[f'upwind_{key}'] = np.where(np.isnan(direction), np.nan, value).astype('float32')
    feats['consensus_dir_sin'] = np.sin(np.radians(direction)).astype('float32')
    feats['consensus_dir_cos'] = np.cos(np.radians(direction)).astype('float32')


def main():
    (ROOT / 'table').mkdir(parents=True, exist_ok=True)
    metar_all = metars()
    cache = {}
    days = pd.date_range('2020-10-02', pd.Timestamp.now(tz=bf.TZ).normalize().tz_localize(None) + pd.Timedelta(days=8), freq='D')
    for station, meta in STATIONS.items():
        rows = pd.MultiIndex.from_product([days, bf.HOURS, bf.LEADS], names=['date', 'hour', 'lead_day']).to_frame(index=False)
        local = (rows.date + pd.to_timedelta(rows.hour, unit='h')).dt.tz_localize(bf.TZ, nonexistent='shift_forward', ambiguous=True)
        rows['valid'] = local.dt.tz_convert('UTC').dt.tz_localize(None)
        rows['init'] = rows.date - pd.to_timedelta(rows.lead_day, unit='D')
        rows['lead_h'] = ((rows.valid - rows.init) / pd.Timedelta(hours=1)).astype(int)
        feats = {'lead_h': rows.lead_h.to_numpy('float32'),
                 'hour_sin': np.sin(2 * np.pi * rows.hour / 24).astype('float32'), 'hour_cos': np.cos(2 * np.pi * rows.hour / 24).astype('float32'),
                 'doy_sin': np.sin(2 * np.pi * rows.date.dt.dayofyear / 365.25).astype('float32'),
                 'doy_cos': np.cos(2 * np.pi * rows.date.dt.dayofyear / 365.25).astype('float32'),
                 'stn_lat': np.full(len(rows), meta['lat'], 'float32'), 'stn_lon': np.full(len(rows), meta['lon'], 'float32'),
                 'stn_elev': np.full(len(rows), meta['elev_m'], 'float32'), 'stn_code': np.full(len(rows), CODES[station], 'float32')}
        inits, leads = pd.DatetimeIndex(rows.init), rows.lead_h.to_numpy()
        for dataset in DATASETS:
            model = dense_station(dataset, station, cache)
            if model is None:
                continue
            cols = bf.gather(model, inits, leads)
            for args in (('10m', 'wind_u_10m', 'wind_v_10m'), ('10m', 'wind_u_10m_mean', 'wind_v_10m_mean'), ('100m', 'wind_u_100m', 'wind_v_100m'),
                         ('100m', 'wind_u_100m_mean', 'wind_v_100m_mean'), ('80m', 'wind_u_80m', 'wind_v_80m'),
                         ('925', 'wind_u_925hpa', 'wind_v_925hpa'), ('850', 'wind_u_850hpa', 'wind_v_850hpa')):
                bf.wind(cols, *args)
            for t2, upper, name in (('temperature_2m', 'temperature_80m', 'dt_80m'), ('temperature_2m_mean', 'temperature_80m_mean', 'dt_80m'),
                                    ('temperature_2m', 'temperature_925hpa', 'dt_925'), ('temperature_2m_mean', 'temperature_925hpa_mean', 'dt_925'),
                                    ('temperature_925hpa', 'temperature_850hpa', 'dt_925_850'), ('temperature_925hpa_mean', 'temperature_850hpa_mean', 'dt_925_850'),
                                    ('temperature_2m', 'dew_point_2m', 'dewpoint_depression')):
                if t2 in cols and upper in cols:
                    cols[name] = (cols[t2] - cols[upper]).astype('float32')
            for key, value in cols.items():
                feats[f'{PREFIX.get(dataset, dataset)}_{key}'] = value
        add_exposure(feats, station)
        table = pd.concat([rows[['date', 'hour', 'lead_day', 'valid', 'init']], pd.DataFrame(feats)], axis=1)
        table.insert(0, 'station', station)
        table = table.join(targets(station, metar_all), on='valid')
        table.to_parquet(ROOT / 'table' / f'{station}.parquet', index=False)
        print(f'{station}: {len(table)} rows, 1-minute targets {table.gust_peak.notna().sum()}', flush=True)


if __name__ == '__main__':
    main()
