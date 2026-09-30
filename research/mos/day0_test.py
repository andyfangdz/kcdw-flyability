"""Same-day (lead day 0) calibration at KCDW, built to rule out temporal leakage (research).

Issue time is 10Z on the valid date (6 am EDT, 5 am EST), when the production update runs and
(checked on 2026-09-30) every source's 00Z run of that day is complete. Leakage rules:
  - model inputs come only from that day's 00Z runs (lead 10-25 h), all published before 10Z;
  - observation inputs are METARs only (live in production; the 1-minute archive lags weeks),
    and only those at least 45 minutes older than the issue time, matching the METAR feed's lag;
  - only hours valid 11Z or later are forecast and scored;
  - the forward year keeps the 8-day embargo; cross-validation splits by date.
Compared on identical hours: the current practice (lead day 1, the previous day's 00Z runs),
day 0 from models only, and day 0 with the METAR inputs (last wind, direction and gust, 3-hour
mean wind, and each model's current error at the last observation).
Writes var/mos/day0_test.json.
"""
import json

import numpy as np
import pandas as pd

import build_features as bf
from compare_common import ID, OBS, crps, exceedance, load
from forward_test import SPLIT, fit_predict

ISSUE_HOUR = 10
METAR_LAG = pd.Timedelta(minutes=45)
MODELS = {'gfs': 'gfs_10m_spd', 'gefs': 'gefs_speed_10m_mean', 'ifs_ens': 'ifs_ens_speed_10m_mean', 'hrrr': 'hrrr_10m_spd',
          'wn2': 'wn2_speed_10m_mean', 'wn3': 'wn3_speed_10m_mean', 'aifs': 'aifs_10m_spd'}


def day0_rows():
    """Lead-0 rows with the production row construction (build_features), written to a separate table."""
    bf.LEADS = [0]
    bf.OUT = 'var/mos/table_day0.parquet'
    bf.main()
    return pd.read_parquet(bf.OUT)


def metar_features(dates):
    m = pd.read_parquet('var/mos/metar.parquet').sort_values('valid_utc')
    m['valid_utc'] = pd.to_datetime(m.valid_utc)
    out = []
    for d in dates:
        cutoff = d + pd.Timedelta(hours=ISSUE_HOUR) - METAR_LAG
        recent = m[(m.valid_utc <= cutoff) & (m.valid_utc > cutoff - pd.Timedelta(hours=6))]
        routine = recent[recent.sknt.notna()]
        if routine.empty:
            out.append({'date': d}); continue
        last = routine.iloc[-1]
        rad = np.radians(last.drct) if last.sknt > 0 else np.nan
        out.append({'date': d, 'obs_sknt': last.sknt, 'obs_dir_sin': np.sin(rad), 'obs_dir_cos': np.cos(rad),
                    'obs_gust': last.gust if pd.notna(last.gust) else 0.0, 'obs_age_min': (cutoff + METAR_LAG - last.valid_utc).total_seconds() / 60,
                    'obs_sknt_3h': routine[routine.valid_utc > cutoff - pd.Timedelta(hours=3)].sknt.mean(),
                    'obs_gust_6h': recent.gust.max() if recent.gust.notna().any() else 0.0, 'obs_time': last.valid_utc})
    return pd.DataFrame(out)


def current_errors(d0, obs):
    """Each model's 00Z forecast for the hour of the last METAR, minus that METAR's wind (both known before issue)."""
    rows = []
    for d, g in d0.groupby('date'):
        o = obs[obs.date == d]
        if o.empty or pd.isna(o.obs_time.iloc[0]):
            continue
        valid = o.obs_time.iloc[0].round('h')
        rec = {'date': d}
        for name, col in MODELS.items():
            # The model's value at the METAR hour, from the same 00Z run; interpolated from the dense archive.
            model = DENSE.get(name)
            if model is None:
                continue
            idx, arr = model
            pos = idx.get_indexer([d])[0]
            lead = int((valid - d) / pd.Timedelta(hours=1))
            if pos >= 0 and 0 <= lead < arr.shape[1] and not np.isnan(arr[pos, lead]):
                rec[f'now_err_{name}'] = arr[pos, lead] - o.obs_sknt.iloc[0]
        rows.append(rec)
    return pd.DataFrame(rows)


DENSE = {}


def dense_speeds():
    """00Z-run 10 m speed per (init, lead hour) for each model, from the same archives the table uses."""
    for name, col in MODELS.items():
        model = bf.dense(name)
        if model is None:
            continue
        cols = {k: v for k, v in model.items()}
        u = [k for k in cols if k in ('wind_u_10m', 'wind_u_10m_mean')]
        if not u:
            continue
        uk = u[0]; vk = uk.replace('_u_', '_v_')
        idx, uu = cols[uk]; _, vv = cols[vk]
        DENSE[name] = (idx, np.hypot(uu, vv) * (3600 / 1852))


READY = 'var/mos/table_day0_ready.parquet'


def build():
    """Stage 1 (needs the model archives): day-0 rows with METAR inputs, hours after issue only."""
    d0 = day0_rows()
    d0 = d0[(d0.date >= '2021-06-01') & d0.gust_peak.notna() & d0.sust_mean.notna()].reset_index(drop=True)
    dense_speeds()
    obs = metar_features(sorted(d0.date.unique()))
    errs = current_errors(d0, obs)
    d0 = d0.merge(obs.drop(columns='obs_time'), on='date', how='left').merge(errs, on='date', how='left')
    d0 = d0[d0.valid.dt.hour.isin(range(11, 24)) | (d0.valid.dt.hour < 2)].reset_index(drop=True)  # valid 11Z-01Z: after issue
    d0.to_parquet(READY, index=False)
    print(f'wrote {READY}: {len(d0)} rows', flush=True)


def main():
    import sys
    if '--build' in sys.argv:
        return build()
    t, features, fold = load()
    d0 = pd.read_parquet(READY)
    obs_cols = [c for c in d0.columns if c.startswith(('obs_', 'now_err_'))]
    both = pd.concat([t, d0], ignore_index=True)
    both_fold = ((both.date.dt.year * 12 + both.date.dt.month) % 5).to_numpy()
    is0 = (both.lead_day == 0).to_numpy()
    # Current practice for the same valid hours: the lead-1 rows.
    key = d0[['valid']].assign(lead_day=1)
    lead1_rows = both.reset_index().merge(key, on=['valid', 'lead_day'])['index'].to_numpy()
    same = d0.valid.isin(both.loc[lead1_rows, 'valid']).to_numpy()
    print(f'day-0 rows (valid 11Z+): {len(d0)}; with METAR inputs {int(d0.obs_sknt.notna().sum())}; paired with a lead-1 row {int(same.sum())}', flush=True)
    variants = {'lead day 1 (current practice)': (features, 1), 'day 0, models only': (features, 0), 'day 0 + METAR inputs': (features + obs_cols, 0)}
    test_date = (both.date >= SPLIT).to_numpy()
    train = (both.date < SPLIT - pd.Timedelta(days=8)).to_numpy()
    out = {'issue': f'{ISSUE_HOUR:02d}Z, METARs at least {int(METAR_LAG.total_seconds() / 60)} min old', 'forward': {}, 'cv': {}}
    aft = both.hour.between(13, 16).to_numpy()

    def score(q_rows, rows, target):
        y = both.loc[rows, target].to_numpy()
        c = crps(y, q_rows)
        a = aft[rows]
        return {'crps': round(float(c.mean()), 4), 'crps_1_4pm': round(float(c[a].mean()), 4), 'mae': round(float(np.abs(q_rows[:, 9] - y).mean()), 3),
                'hours': int(len(rows))}

    # Identical hours: day-0 rows with a matching lead-1 row, scored by valid time.
    d0_idx = np.flatnonzero(is0)
    pair = pd.DataFrame({'i0': d0_idx, 'valid': both.valid.to_numpy()[d0_idx]}).merge(
        pd.DataFrame({'i1': lead1_rows, 'valid': both.valid.to_numpy()[lead1_rows]}), on='valid')
    for mode in ('forward', 'cv'):
        for name, (cols, lead) in variants.items():
            res = {}
            for target in ('gust_peak', 'sust_mean'):
                rows = pair.i1.to_numpy() if lead == 1 else pair.i0.to_numpy()
                q = np.full((len(both), 19), np.nan)
                usable = ~is0 if lead == 1 else np.ones(len(both), bool)  # current practice trains exactly as production: lead days 1-7
                if mode == 'forward':
                    te = np.zeros(len(both), bool); te[rows] = True; te &= test_date
                    q[te] = fit_predict(both, cols, train & usable, te, target)
                    rows = rows[test_date[rows]]
                else:
                    for k in range(5):
                        te = np.zeros(len(both), bool); te[rows] = True; te &= both_fold == k
                        q[te] = fit_predict(both, cols, (both_fold != k) & usable, te, target)
                res[target] = score(q[rows], rows, target)
                if target == 'gust_peak':
                    windy = both.loc[rows, target].to_numpy() >= 20
                    p = exceedance(q[rows], 20.0)
                    res[target]['brier_20kt'] = round(float(((p - windy) ** 2).mean()), 4)
            out[mode][name] = res
            print(mode, name, res, flush=True)
    json.dump(out, open('var/mos/day0_test.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
