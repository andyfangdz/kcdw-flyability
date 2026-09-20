# WN3 ensemble research — September 20, 2026

Saved for manual research. These analyses are not inputs to scheduled collection or published forecast reports.

Both studies use all 64 members of the **September 20, 2026 00Z run**, looking at Thursday, September 24 near KCDW. This is a fixed historical snapshot, not a current forecast.

| Analysis | Finding | Saved artifacts |
|---|---|---|
| Pressure-level profiles | A dry layer above more humid low-level air; morning wind near 3,000 ft MSL decreases substantially by afternoon. Thin cloud and the timing of the wind decrease remain unresolved. | [Analysis](profiles/analysis.md), [chart](profiles/wn3-kcdw-pressure-profiles.png), [complete profile bundle](profiles/wn3-kcdw-pressure-profiles.zip) |
| Coastal-low ensemble | The deepest detected lows are well offshore at 11 a.m. EDT, but 37 members contain multiple minima. This snapshot does not establish storm tracks or local-impact probabilities. | [Analysis](coastal-low/analysis.md), [chart](coastal-low/wn3-member-low-centers.png), [compact analysis bundle](coastal-low/wn3-low-center-summary.zip) |

The profile bundle includes the raw extracted temperature, humidity, U/V wind and geopotential values, derived member data, 3,840 source-chunk records, cached cloud-summary input, validation results and scripts. It covers six pressure levels at 8 a.m. and 2 p.m. EDT. All inputs for replaying the local profile calculations are included.

The coastal-low bundle includes the center diagnostics, all secondary minima, six hourly ensemble-mean pressure grids, KCDW pressure series for all 64 members, source manifests, validation and scripts. The larger regional member grids remain in the complete local archive:

`var/research/wn3-members-20260920/wn3-pressure-members-thursday.zip`

That 82 MB archive is preserved locally and is **not included in Git**. Its size and SHA-256, and checksums for the repository artifacts, are recorded in [manifest.json](manifest.json). The original extracted research directories also remain under `var/research/`.

## Replaying the profiles

From the repository root, extract the profile bundle into an empty working directory at the original relative location:

```sh
python -m zipfile -e research/wn3-ensemble-20260920/profiles/wn3-kcdw-pressure-profiles.zip var/research/wn3-profile-20260920
```

In a Python environment with NumPy and Matplotlib installed, run:

```sh
python var/research/wn3-profile-20260920/analyze_profiles.py
python var/research/wn3-profile-20260920/plot_profiles.py
python var/research/wn3-profile-20260920/write_report.py
```

These calculations use the saved inputs and require no cloud reads. The optional chart dependencies are listed in [requirements-charts.txt](../../requirements-charts.txt). The extraction scripts are retained for provenance; running those against GCS requires approved requester-pays access and can incur transfer charges.

For the coastal-low chart, extract its compact bundle to `var/research/wn3-members-20260920` and run `plot_members.py` with the chart dependencies and Natural Earth basemaps. Recomputing the individual member minima requires the complete local regional-pressure archive; repeating the published-mean validation also uses the original comparison caches described in `validate_members.py`.
