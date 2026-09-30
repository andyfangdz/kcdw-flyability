"""Shared data, folds and scoring for the method comparison (research/mos/compare_*.py).

Every method predicts the same 19 quantiles (5%..95%) for each target on the
same month-interleaved folds as train.py, so scores compare like for like.
CRPS is approximated as twice the mean pinball loss over the quantile grid.
"""
import numpy as np
import pandas as pd

LEVELS = np.round(np.arange(0.05, 0.96, 0.05), 2)
TARGETS = ('gust_peak', 'sust_mean')
ID = ['date', 'hour', 'lead_day', 'valid', 'init']
OBS = ['sust_mean', 'sust_max', 'gust_peak', 'u_obs', 'v_obs', 'spread', 'metar_sknt', 'metar_drct', 'metar_gust', 'metar_peak',
       'metar_gust_reported']
FOLDS = 5
OUT = 'var/mos/compare'


def load():
    t = pd.read_parquet('var/mos/table.parquet')
    t = t[(t.date >= '2021-06-01') & t.gust_peak.notna() & t.sust_mean.notna()].reset_index(drop=True)
    features = [c for c in t.columns if c not in ID + OBS and t[c].notna().mean() > 0.02]
    fold = ((t.date.dt.year * 12 + t.date.dt.month) % FOLDS).to_numpy()
    return t, features, fold


def pinball(y, q, alpha):
    d = y[:, None] - q
    return np.maximum(alpha * d, (alpha - 1) * d)


def crps(y, q, levels=LEVELS):
    """Twice the mean pinball loss over the grid (per hour)."""
    return 2 * pinball(y, q, levels).mean(axis=1)


def exceedance(q, threshold, levels=LEVELS):
    """P(y >= threshold) from each row's quantile function, linearly interpolated, clamped to the grid's tails."""
    out = np.empty(len(q))
    for i, row in enumerate(q):
        out[i] = 1 - np.interp(threshold, row, levels, left=0.0, right=1.0)
    return out


def save(name, t, preds):
    frame = t[['valid', 'lead_day', 'date', 'hour']].copy()
    for target, q in preds.items():
        for j, a in enumerate(LEVELS):
            frame[f'{target}_q{int(round(a * 100)):02d}'] = q[:, j].astype('float32')
    frame.to_parquet(f'{OUT}/{name}.parquet', index=False)
