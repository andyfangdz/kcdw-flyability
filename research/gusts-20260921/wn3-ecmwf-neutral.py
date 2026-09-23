"""Conditional gust calculation; not a native or calibrated WN3 gust forecast.

Run from the repository root. The source is frozen deliberately so subsequent
site refreshes do not silently change this research result.
"""

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path(
    "var/events/commercial-checkride/runs/20260921T131049Z.996210/snapshot.json"
)
OUTPUT = Path(__file__).with_suffix(".json")
KNOTS_PER_MPS = 3600 / 1852
KAPPA = 0.4
C_UGN = 7.2

raw = SOURCE.read_bytes()
snapshot = json.loads(raw)
forecast = snapshot["weathernext3"]["data"]["forecast"]
aloft = snapshot["wn3_100m_wind"]
surface = forecast["fields"]["wind_speed_10m"]
assert forecast["response_init_utc"] == aloft["init_time"]
assert forecast["grid_point"] == aloft["grid_point"]
assert surface["unit"] == aloft["unit"] == "m/s"
winds = dict(zip(forecast["valid_time_utc"], surface["mean"], strict=True))

rows = []
for hour in aloft["hours"]:
    at = hour["at"]
    u10, u100 = winds[at], hour["mean"]
    assert math.isfinite(u10) and math.isfinite(u100) and 0 <= u10 <= u100
    friction = KAPPA * (u100 - u10) / math.log(100 / 10)
    gust = u10 + C_UGN * friction
    # Independent algebraic evaluation also checks unit conversion.
    gust_kt = u10 * KNOTS_PER_MPS + (C_UGN * KAPPA / math.log(10)) * (
        (u100 - u10) * KNOTS_PER_MPS
    )
    assert math.isclose(gust * KNOTS_PER_MPS, gust_kt, abs_tol=1e-12)
    rows.append({
        "at_utc": at,
        "at_local": datetime.fromisoformat(at).astimezone(
            ZoneInfo("America/New_York")
        ).isoformat(),
        "wn3_mean_10m_mps": u10,
        "wn3_mean_100m_mps": u100,
        "wn3_mean_10m_kt": u10 * KNOTS_PER_MPS,
        "wn3_mean_100m_kt": u100 * KNOTS_PER_MPS,
        "inferred_neutral_friction_velocity_mps": friction,
        "conditional_neutral_gust_kt": gust_kt,
    })

result = {
    "label": "WN3-derived ECMWF-style neutral-profile gust approximation",
    "native_wn3_gust_forecast": False,
    "calibrated": False,
    "source_snapshot": str(SOURCE),
    "source_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
    "snapshot_collected_at": snapshot["collected_at"],
    "wn3_initialization_utc": forecast["response_init_utc"],
    "grid_point": forecast["grid_point"],
    "constants": {"kappa": KAPPA, "C_ugn": C_UGN, "knots_per_mps": KNOTS_PER_MPS},
    "method": {
        "documented_turbulent_gust_formula": "G = U10 + 7.2 u_star f(zi/L)",
        "assumed_stability_factor": 1,
        "inferred_friction_velocity": "u_star = 0.4 (U100 - U10) / ln(100/10)",
        "combined_formula": "G = U10 + (2.88 / ln(10)) (U100 - U10)",
    },
    "assumptions": [
        "Neutral stratification; no stability enhancement.",
        "No deep-convective gust contribution.",
        "Both heights follow one horizontally homogeneous logarithmic surface-layer profile.",
        "Zero displacement height, common effective roughness, aligned wind directions.",
        "Uses ensemble-mean scalar speeds; does not diagnose member gust probabilities.",
    ],
    "limitations": [
        "WN3 published variables omit surface stress/friction velocity and Monin-Obukhov length.",
        "Two model height winds need not obey the assumed profile or common roughness.",
        "Hourly samples are not maxima over every model timestep or the full flight interval.",
        "This is neither an upper/lower bound nor a validated forecast of observed airport gusts.",
    ],
    "references": [
        "https://www.ecmwf.int/sites/default/files/2023-06/Part-IV-Physical-Processes.pdf",
        "https://www.ecmwf.int/en/newsletter/171/news/wind-gust-predictions-storm-eunice",
        "https://developers.google.com/weathernext/guides/models",
    ],
    "hours": rows,
}
OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
for row in rows:
    print(
        row["at_local"],
        f"10m {row['wn3_mean_10m_kt']:.1f} kt; 100m {row['wn3_mean_100m_kt']:.1f} kt; "
        f"conditional gust {row['conditional_neutral_gust_kt']:.1f} kt",
    )
