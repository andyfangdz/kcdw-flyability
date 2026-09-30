"""Issue-time training table: rows for each production update, each source's newest published run (research).

A forecast is issued at 04, 11, 16 and 22Z (the production updates). For each issue time and each
source, the input run is the newest cycle whose init plus that source's publication delay is at or
before the issue time; the same rule applies in production, so training and live forecasts see the
same runs. Rows cover every local hour 06-21 after the issue time, out to 7 days. Each source's own
lead (hours from its run to the valid hour) is an input, since sources differ in age.
Cycles: GEFS and ECMWF ENS are 00Z-only on dynamical.org; GFS, AIFS, AIFS ENS and HRRR have
00/06/12/18Z; WN3 is hourly but 00/06/12/18Z are extracted; WN2's later cycles publish too late to use. `--zero` restricts every source to 00Z
(the baseline for measuring what newer cycles add). Writes var/mos/table_issue[_00z].parquet, the
production training table; its `init` column is the issue time and `lead_day` counts local days from it.
"""
import glob
import sys

import numpy as np
import pandas as pd

import build_features as bf

SLOTS = (4, 11, 16, 22)  # the production updates; 11Z (not 10Z) so every 00Z run, WN2 included, is published
CYCLES = {'gfs': (0, 6, 12, 18), 'gefs': (0,), 'ifs_ens': (0,), 'aifs': (0, 6, 12, 18), 'aifs_ens': (0, 6, 12, 18), 'hrrr': (0, 6, 12, 18),
          'wn2': (0,), 'wn3': (0, 6, 12, 18)}
# Hours from init until the run is in our archives, with margin, from first-seen times measured on 2026-09-30
# (var/mos/latency_seen.json): HRRR ~2 h, GFS ~5.7 h, WN3 ~7.1 h, AIFS 06Z <= 7.4 h and 12Z > 6.3 h, ECMWF ENS
# 00Z <= 10.1 h, WN2 00Z <= 10.1 h but its 06Z still missing after 12.3 h (so WN2 stays 00Z-only).
# Update logs of 2026-09-30: no 00Z run of any source was in at 04:06Z; all were in, WN2 included, at 10:05Z.
LATENCY = {'gfs': 6.5, 'gefs': 10, 'ifs_ens': 10.5, 'aifs': 8.5, 'aifs_ens': 9, 'hrrr': 3, 'wn2': 11, 'wn3': 8}


def dense_multi(dataset):
    paths = sorted(glob.glob(f'var/mos/models/{dataset}/*.parquet')) + sorted(glob.glob(f'var/mos/models/{dataset}_c061218/*.parquet'))
    if not paths:
        return None
    frame = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True).drop_duplicates(['init', 'lead_h'])
    frame = frame[frame.lead_h < bf.MAX_LEAD]
    out = {}
    for var in frame.columns.drop(['init', 'lead_h']):
        wide = frame.pivot_table(index='init', columns='lead_h', values=var, aggfunc='first')
        wide = wide.reindex(columns=range(bf.MAX_LEAD)).interpolate(axis=1, limit_area='inside')
        out[var] = (wide.index, wide.to_numpy(dtype='float32'))
    return out


def chosen_init(issue, dataset, cycles, available):
    """Newest archived run of `dataset` published by `issue` (init + latency <= issue), up to two days back.

    Production applies the same rule, so a late or missing run falls back to the source's previous run in
    both; the source's own lead (`age_h`) tells the model how old the run is."""
    day = issue.dt.normalize()
    best = pd.Series(pd.NaT, index=issue.index)
    for back in (2, 1, 0):
        for c in cycles:
            cand = day - pd.Timedelta(days=back) + pd.Timedelta(hours=c)
            ok = (cand + pd.Timedelta(hours=LATENCY[dataset]) <= issue) & cand.isin(available)
            best = best.where(~ok | (best >= cand), cand)
    return best


def rows_frame():
    issue_days = pd.date_range('2020-10-02', pd.Timestamp.utcnow().tz_localize(None).normalize(), freq='D')
    issues = pd.DatetimeIndex([d + pd.Timedelta(hours=s) for d in issue_days for s in SLOTS])
    issues = issues[issues <= pd.Timestamp.utcnow().tz_localize(None)]
    local_issue_date = issues.tz_localize('UTC').tz_convert(bf.TZ).normalize().tz_localize(None)
    parts = []
    for ahead in range(0, 8):
        for h in bf.HOURS:
            date = local_issue_date + pd.Timedelta(days=ahead)
            local = (date + pd.Timedelta(hours=h)).tz_localize(bf.TZ, nonexistent='shift_forward', ambiguous=True)
            valid = local.tz_convert('UTC').tz_localize(None)
            parts.append(pd.DataFrame({'issue': issues, 'date': date, 'hour': h, 'lead_day': ahead, 'valid': valid}))
    rows = pd.concat(parts, ignore_index=True)
    rows = rows[rows.valid > rows.issue].reset_index(drop=True)
    rows['lead_h'] = ((rows.valid - rows.issue) / pd.Timedelta(hours=1)).astype('float32')
    rows['slot'] = rows.issue.dt.hour.astype('float32')
    return rows


def main():
    zero = '--zero' in sys.argv
    rows = rows_frame()
    feats = {'lead_h': rows.lead_h.to_numpy('float32'), 'slot': rows.slot.to_numpy('float32'),
             'hour_sin': np.sin(2 * np.pi * rows.hour / 24).astype('float32'), 'hour_cos': np.cos(2 * np.pi * rows.hour / 24).astype('float32'),
             'doy_sin': np.sin(2 * np.pi * rows.date.dt.dayofyear / 365.25).astype('float32'),
             'doy_cos': np.cos(2 * np.pi * rows.date.dt.dayofyear / 365.25).astype('float32')}
    for dataset, cycles in CYCLES.items():
        model = dense_multi(dataset)
        if model is None:
            continue
        available = pd.DatetimeIndex(model[next(iter(model))][0])
        init = chosen_init(rows.issue, dataset, (0,) if zero else cycles, available)
        lead = ((rows.valid - init) / pd.Timedelta(hours=1)).to_numpy()
        lead = np.where(np.isnan(lead), -1, lead).astype(int)
        cols = bf.gather(model, pd.DatetimeIndex(init), lead)
        for args in (('10m', 'wind_u_10m', 'wind_v_10m'), ('10m', 'wind_u_10m_mean', 'wind_v_10m_mean'), ('100m', 'wind_u_100m', 'wind_v_100m'),
                     ('100m', 'wind_u_100m_mean', 'wind_v_100m_mean'), ('80m', 'wind_u_80m', 'wind_v_80m')):
            bf.wind(cols, *args)
        for t2, upper, name in (('temperature_2m', 'temperature_80m', 'dt_80m'), ('temperature_2m_mean', 'temperature_80m_mean', 'dt_80m'),
                                ('temperature_2m', 'temperature_925hpa', 'dt_925'), ('temperature_2m_mean', 'temperature_925hpa_mean', 'dt_925'),
                                ('temperature_925hpa', 'temperature_850hpa', 'dt_925_850'), ('temperature_925hpa_mean', 'temperature_850hpa_mean', 'dt_925_850')):
            if t2 in cols and upper in cols:
                cols[name] = (cols[t2] - cols[upper]).astype('float32')
        present = ~np.isnan(cols[next(iter(cols))])
        used = pd.DatetimeIndex(init)[present]
        assert ((used + pd.Timedelta(hours=LATENCY[dataset])) <= pd.DatetimeIndex(rows.issue)[present]).all(), f'{dataset}: run used before publication'
        assert (pd.DatetimeIndex(rows.valid)[present] > used).all(), f'{dataset}: valid hour not after its run'
        cols['age_h'] = np.where(present, lead, np.nan).astype('float32')  # this source's own lead to the valid hour
        for key, value in cols.items():
            feats[f'{dataset}_{key}'] = value
        print(f'{dataset}: coverage {present.mean():.0%}, cycles used {sorted(set(pd.DatetimeIndex(init[present]).hour))}', flush=True)
    table = pd.concat([rows[['issue', 'date', 'hour', 'lead_day', 'valid']].rename(columns={'issue': 'init'}), pd.DataFrame(feats)], axis=1)
    table = table.join(pd.read_parquet('var/mos/targets.parquet').set_index('valid'), on='valid')
    out = 'var/mos/table_issue_00z.parquet' if zero else 'var/mos/table_issue.parquet'
    table.to_parquet(out, index=False)
    print(f'{out}: {len(table)} rows, {table.shape[1]} columns, labelled {table.gust_peak.notna().sum()}', flush=True)


if __name__ == '__main__':
    main()
