"""Threshold probabilities from a quantile forecast, shared by production (model.py, forecast.py) and research.

A 23-level quantile model (1st-99th percentile) gives the forecast distribution; P(value >= T) is read
from it, linear between levels and with exponential tails beyond the 1st and 99th fitted to the 95-99
spread. Out of sample this beat dedicated per-threshold classifiers and an isotonic recalibration
(research/mos/reliability_test.py; https://andyfangdz.github.io/kcdw-flyability/reliability/).
"""
import numpy as np

LEVELS = np.array([0.01, 0.02] + list(np.round(np.arange(0.05, 0.96, 0.05), 2)) + [0.98, 0.99])


def exceed(q, threshold):
    """P(y >= T) from quantiles sorted along axis 1 at LEVELS."""
    p = np.empty(len(q))
    for i, row in enumerate(q):
        if threshold <= row[0]:
            p[i] = 1 - LEVELS[0] * max(threshold, 0) / max(row[0], 1e-6)
        elif threshold >= row[-1]:
            scale = max((row[-1] - row[-5]) / np.log(5), 0.3)  # 95th-99th spread sets the tail decay
            p[i] = (1 - LEVELS[-1]) * np.exp(-(threshold - row[-1]) / scale)
        else:
            p[i] = 1 - np.interp(threshold, row, LEVELS)
    return np.clip(p, 0, 1)
