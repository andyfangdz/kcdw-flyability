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
| ICON, ECMWF HRES, GEM | Open-Meteo previous runs (`fetch_openmeteo.py`); live runs filled at forecast time | 2024-01 |

UKMO has no public forecast archive; CFS adds little at 1-7 days; AIGFS's archive
is too short to learn from yet.

## Pipeline

1. `build_targets.py`, `build_features.py`: one row per local date, hour 06-21 and
   lead day 1-7, using the 00Z runs issued `lead_day` days earlier (interpolated to the hour).
2. `train.py`: cross-validation with month-interleaved folds; raw, linear and
   XGBoost compared on identical hours; saves out-of-fold predictions.
3. `conformal.py`: conformalized widening so p10-p90 covers ~80% per lead day.
4. `evaluate.py`: benchmark against NBM and raw models; writes `benchmark.json`.
5. `model.py`: final models on every labelled hour (`var/mos/artifacts`).
6. `forecast.py`: newest runs → `var/mos/forecast.json` (read by the event page).

`mos_update.sh` refreshes the recent months every six hours, retrains daily and
re-runs cross-validation, conformal widths and the benchmark weekly.

## Cross-validated skill (Nov 2024 – Sep 2026, identical hours with NBM)

| Mean absolute error | Raw ECMWF ENS | NBM | Calibrated |
|---|---|---|---|
| Sustained wind, 1-4 p.m. | 1.96 kt | 2.02 kt | 1.64 kt |
| True 1-hour peak gust, 1-4 p.m. | 4.24 kt | 4.17 kt | 3.34 kt |
| Direction (wind ≥ 5 kt) | 27.3° | 27.7° | 21.5° |

`s_curve.py` draws the forecast-gust versus observed-probability curves that
motivated calibration.
