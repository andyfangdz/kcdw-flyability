"""EMOS and XGBoost (19 quantiles) out-of-fold predictions for the method comparison. Runs in var/mos-venv (CPU).

EMOS: a normal distribution truncated at zero whose mean is linear in the ensemble
mean (ECMWF ENS where available, else GEFS; gust or 10 m speed) plus hour-of-day
and lead terms, and whose log standard deviation is linear in the log ensemble
spread; fitted per fold by minimizing CRPS, the standard statistical baseline.
"""
import math
from pathlib import Path

import numpy as np
import xgboost as xgb
from scipy.optimize import minimize
from scipy.stats import norm

from compare_common import LEVELS, OUT, TARGETS, crps, load, save

import os
DEVICE = os.environ.get('MOS_DEVICE', 'cpu')  # 'cuda' on the RTX 5090 desktop

ENSEMBLE = {'gust_peak': [('ifs_ens_wind_gust_10m_mean', 'ifs_ens_wind_gust_10m_std'), ('gefs_wind_gust_surface_mean', 'gefs_wind_gust_surface_std')],
            'sust_mean': [('ifs_ens_speed_10m_mean', 'ifs_ens_speed_10m_std'), ('gefs_speed_10m_mean', 'gefs_speed_10m_std')]}


def ensemble(t, target):
    mean = np.full(len(t), np.nan); spread = np.full(len(t), np.nan)
    for m, s in ENSEMBLE[target]:
        fill = np.isnan(mean) & t[m].notna().to_numpy() & t[s].notna().to_numpy()
        mean[fill], spread[fill] = t[m].to_numpy()[fill], t[s].to_numpy()[fill]
    source = np.where(t[ENSEMBLE[target][0][0]].notna(), 1.0, 0.0)  # 1 when ECMWF ENS supplied it
    return mean, spread, source


def emos_design(t, mean, spread, source):
    h = t.hour.to_numpy()
    mu_x = np.c_[np.ones(len(t)), mean, mean * source, source, np.sin(2 * np.pi * h / 24), np.cos(2 * np.pi * h / 24),
                 t.lead_h.to_numpy() / 100, t.doy_sin, t.doy_cos]
    sd_x = np.c_[np.ones(len(t)), np.log(spread + 0.3), source, t.lead_h.to_numpy() / 100]
    return mu_x, sd_x


def crps_normal(y, mu, sigma):
    z = (y - mu) / sigma
    return sigma * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / math.sqrt(math.pi))


def emos(t, fold, target):
    y = t[target].to_numpy()
    mean, spread, source = ensemble(t, target)
    ok = ~np.isnan(mean)
    mu_x, sd_x = emos_design(t, np.nan_to_num(mean), np.nan_to_num(spread, nan=1.0), source)
    q = np.full((len(t), len(LEVELS)), np.nan)
    for k in np.unique(fold):
        tr, te = ok & (fold != k), ok & (fold == k)
        a, b = mu_x.shape[1], sd_x.shape[1]
        loss = lambda w: crps_normal(y[tr], mu_x[tr] @ w[:a], np.exp(sd_x[tr] @ w[a:])).mean()
        w0 = np.r_[np.zeros(a), np.log(2.0), np.zeros(b - 1)]
        w0[1] = 1.0
        w = minimize(loss, w0, method='L-BFGS-B').x
        mu, sigma = mu_x[te] @ w[:a], np.exp(sd_x[te] @ w[a:])
        # Truncate at zero: quantiles of N(mu, sigma) conditioned on y >= 0.
        p0 = norm.cdf(-mu / sigma)
        q[te] = mu[:, None] + sigma[:, None] * norm.ppf(p0[:, None] + LEVELS[None, :] * (1 - p0[:, None]))
    return q


def xgb_quantiles(t, features, fold, target):
    X, y = t[features].to_numpy('float32'), t[target].to_numpy('float32')
    q = np.full((len(t), len(LEVELS)), np.nan)
    for k in np.unique(fold):
        tr, te = fold != k, fold == k
        model = xgb.XGBRegressor(objective='reg:quantileerror', quantile_alpha=LEVELS, tree_method='hist', device=DEVICE, n_estimators=500,
                                 learning_rate=0.05, max_depth=6, min_child_weight=50, subsample=0.8, colsample_bytree=0.6, reg_lambda=5.0)
        model.fit(X[tr], y[tr]); model.set_params(device='cpu')  # predict on host data without a device mismatch
        q[te] = np.sort(model.predict(X[te]), axis=1)
        print(f'  xgb {target} fold {k} done', flush=True)
    return q


def main():
    Path(OUT).mkdir(parents=True, exist_ok=True)
    t, features, fold = load()
    emos_preds = {target: np.maximum(emos(t, fold, target), 0) for target in TARGETS}
    save('emos', t, emos_preds)
    for target in TARGETS:
        y = t[target].to_numpy(); ok = ~np.isnan(emos_preds[target][:, 0])
        print(f'EMOS {target}: CRPS {crps(y[ok], emos_preds[target][ok]).mean():.3f} on {ok.sum()} hours', flush=True)
    save('xgb', t, {target: np.maximum(xgb_quantiles(t, features, fold, target), 0) for target in TARGETS})
    print('xgb done', flush=True)


if __name__ == '__main__':
    main()
