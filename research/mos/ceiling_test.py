"""How much gust error could better wind forecasts ever remove? An oracle experiment (research).

Deliberately uses OBSERVED values of the same hour as inputs, so these are ceilings, not
forecasts. Month-interleaved cross-validation of the 1-minute peak gust at KCDW:
  production inputs
  + observed hourly mean wind           a perfect sustained-wind forecast
  + observed mean wind vector           perfect speed and direction
  observed wind + time only             the true wind alone, no model inputs
The gap between the first two is what better wind forecasts could win; what remains with a
perfect wind is gust-factor and turbulence uncertainty. Writes var/mos/ceiling_test.json.
"""
import json

import numpy as np

from compare_common import crps, exceedance, load
from forward_test import fit_predict

TIME = ['hour_sin', 'hour_cos', 'doy_sin', 'doy_cos', 'lead_h']


def main():
    t, features, fold = load()
    t['obs_sust'] = t.sust_mean
    t['obs_u'], t['obs_v'] = t.u_obs, t.v_obs
    variants = {'production inputs': features, '+ observed mean wind (perfect sustained forecast)': features + ['obs_sust'],
                '+ observed wind vector (perfect speed and direction)': features + ['obs_sust', 'obs_u', 'obs_v'],
                'observed wind + time only (no models)': TIME + ['obs_sust', 'obs_u', 'obs_v']}
    y = t.gust_peak.to_numpy()
    aft = t.hour.between(13, 16).to_numpy()
    windy = y >= 20
    out = {}
    base_q = None
    for name, cols in variants.items():
        q = np.full((len(t), 19), np.nan)
        for k in np.unique(fold):
            q[fold == k] = fit_predict(t, cols, fold != k, fold == k, 'gust_peak')
        c = crps(y, q)
        p20 = exceedance(q, 20.0)
        clim = windy.mean()
        row = {'crps': round(float(c.mean()), 4), 'crps_1_4pm': round(float(c[aft].mean()), 4), 'mae': round(float(np.abs(q[:, 9] - y).mean()), 3),
               'brier_skill_20kt': round(float(1 - ((p20 - windy) ** 2).mean() / ((clim - windy) ** 2).mean()), 3),
               'crps_by_lead': {int(l): round(float(c[(t.lead_day == l).to_numpy()].mean()), 4) for l in sorted(t.lead_day.unique())}}
        out[name] = row
        print(name, row, flush=True)
    json.dump(out, open('var/mos/ceiling_test.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
