# Calibrated KCDW wind (multi-model MOS)

Statistical post-processing of many forecast models against KCDW's own record,
in the spirit of NOAA's Model Output Statistics, using gradient-boosted trees.
The event page shows the result (`kcdw/calibrated_wind.py`); everything here runs
in the isolated `var/mos-venv` (Python 3.12, `requirements-mos.txt`) on the
`kcdw-flyability-mos` timer via `scripts/mos_update.sh`.

## Targets (hourly, KCDW)

| Target | Source |
|---|---|
| Mean sustained wind, true 1-hour peak gust, spread, direction | IEM ASOS 1-minute record (`fetch_obs.py`) |
| METAR wind, reported gust, METAR peak | dynamical.org ASOS GeoParquet (`fetch_metar.py`) |

The 1-minute record carries the peak 5-second gust every minute, so gusts too
small to meet METAR reporting criteria are still measured.

## Inputs (never NBM, which is already station-calibrated; it is the benchmark)

| Model | Archive | From |
|---|---|---|
| GFS, GEFS (31), ECMWF IFS ENS (51), AIFS single, AIFS ENS, HRRR | dynamical.org Zarr (`extract_dynamical.py`) | 2021-05 / 2020-10 / 2024-04 / 2024-04 / 2025-07 / 2018-07 |
| WeatherNext 2 (64) | BigQuery, ~200-300 MB per run (`extract_wn2.py`) | 2022-01 |
| WeatherNext 3 statistics | BigQuery, ~30 MB per run (`extract_wn3.py`) | 2026-01 |

ICON, ECMWF HRES and GEM from Open-Meteo were removed: its previous-runs archive
("predicted N x 24 h before valid time") is up to ~18 h newer than the 00Z runs the
other inputs use, a look-ahead worth ~1.8% gust CRPS; aligned, they added nothing
(`leakage_audit.py`). NBM remains a benchmark only (`var/mos/benchmark_nbm.parquet`).
UKMO has no public forecast archive; CFS adds little at 1-7 days; AIGFS's archive
is too short to learn from yet.

## Pipeline

1. `build_targets.py`, `build_issue.py`: the production table (`table_issue.parquet`). One row per
   update (04, 11, 16, 22Z), local hour 06-21 after it and lead day 0-7 counted from the update, using
   each model's newest run published by then: GFS, AIFS, AIFS ENS, HRRR and WN3 at 00/06/12/18Z,
   GEFS, ECMWF ENS and WN2 at 00Z, under publication delays measured on 2026-09-30 with margins
   (`LATENCY`). A late run falls back to the previous one, in training and live alike; each model's
   run age is an input; an assertion checks every row. `build_features.py` still builds the older
   00Z lead-day table (`table.parquet`) used by the research comparisons.
2. `train.py`: cross-validation with month-interleaved folds; raw, linear and
   XGBoost compared on identical hours; saves out-of-fold predictions.
3. `conformal.py`: conformalized widening so p10-p90 covers ~80% per lead day.
4. `evaluate.py`: benchmark against NBM and raw models at the 11Z update; writes `benchmark.json`.
5. `model.py`: final models on every labelled row (`var/mos/artifacts`).
6. `forecast.py`: the latest update's rows → `var/mos/forecast.json` (read by the event page).

`mos_update.sh` runs at the four updates: it refreshes the recent months of every archive (and of
RRFS, archived but not yet an input), rebuilds the table, retrains daily and re-runs
cross-validation, conformal widths and the benchmark weekly.

Research checks behind this design (scripts in this folder, results in `var/mos`): `leakage_audit.py`,
`forward_test.py`, `learning_curve.py`, `day0_test.py` (same-day forecasts, -5% gust CRPS),
`issue_test.py` and `issue_vs_old.py` (newest published runs, -1.3%), and null results in
`features_test.py`, `pbl_test.py`, `aigfs_test.py`, `nn_arch.py`, `scaling_nn.py`, `ceiling_test.py`
(what a perfect mean-wind forecast would allow). Explainers: https://andyfangdz.github.io/kcdw-flyability/

## Cross-validated skill (Nov 2024 – Sep 2026, identical hours with NBM)

| Mean absolute error | Raw ECMWF ENS | NBM | Calibrated |
|---|---|---|---|
| Sustained wind, 1-4 p.m. | 1.96 kt | 2.02 kt | 1.64 kt |
| True 1-hour peak gust, 1-4 p.m. | 4.24 kt | 4.17 kt | 3.34 kt |
| Direction (wind ≥ 5 kt) | 27.3° | 27.7° | 21.5° |

`s_curve.py` draws the forecast-gust versus observed-probability curves that
motivated calibration.
