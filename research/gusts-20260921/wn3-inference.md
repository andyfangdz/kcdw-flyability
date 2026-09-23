# What WN3 adds to the Thursday gust assessment

WN3 provides useful evidence about stronger winds above the surface and their evolution. It does not supply enough information here to convert that evidence into a reliable gust magnitude or exceedance probability.

The latest saved surface/100 m evidence is the **September 20 18Z run**, retrieved before the September 21 00:26 EDT snapshot. Both heights use the same run and grid point, 40.9°N, 74.3°W. The separately extracted pressure profiles are from **September 20 00Z**, 18 hours older; they must not be presented as a vertical extension of the newer surface forecast.

## Same-run wind structure during the morning

| September 24, EDT | 10 m ensemble mean | 100 m ensemble mean | 100 m p10–p90 |
|---|---:|---:|---:|
| 8 a.m. | 7.7 kt | 14.5 kt | 12.7–16.1 kt |
| 9 a.m. | 8.2 kt | 13.6 kt | 11.8–15.3 kt |
| **10 a.m.** | **9.1 kt** | **13.4 kt** | **11.5–15.4 kt** |
| **11 a.m.** | **9.3 kt** | **13.4 kt** | **11.4–15.3 kt** |
| **Noon** | **8.9 kt** | **12.7 kt** | **10.8–15.3 kt** |
| 1 p.m. | 8.1 kt | 11.7 kt | 9.2–14.3 kt |
| 2 p.m. | 7.3 kt | 10.5 kt | 7.8–13.3 kt |

These are wind speeds at height, not gusts. The p10–p90 range describes ensemble forecast spread, not second-to-second variability. Neither 100 m mean nor p90 is a gust ceiling. All values were converted from m/s with 3600/1852; the validated source packet and supporting fields are retained in [wn3-context.json](wn3-context.json). [Google field definitions](https://developers.google.com/weathernext/guides/models).

Three inferences are useful:

1. **Faster air exists immediately above the surface.** During the flight, ensemble-mean wind at 100 m (about 330 ft AGL) exceeds the 10 m mean by about 4 kt. Downward momentum transfer could contribute to gusts. This difference of scalar means is not a memberwise vector-shear distribution, gust increment, or diagnosed mixing rate.
2. **Morning mixing could offset part of the weakening aloft.** Between 8 and 11 a.m., 100 m mean speed falls from 14.5 to 13.4 kt while 10 m mean speed rises from 7.7 to 9.3 kt. Mean surface temperature rises from 10.5°C to 15.5°C. This combination is consistent with increasing daytime coupling of surface air to faster air above it. It does not prove the process or establish the timing of individual gusts. Surface heating, stability, friction and convection all affect gust generation. [ECMWF surface-wind guidance](https://confluence.ecmwf.int/pages/viewpage.action?pageId=202166701).
3. **The easing signal is clearer after the flight.** The 100 m mean changes only modestly from 13.4 to 12.7 kt during 10 a.m.–noon, then reaches 10.5 kt at 2 p.m. Surface mean wind peaks at the 11 a.m. sample before weakening. WN3 supports a weakening background flow later, but it does not independently validate GFS's larger decline in surface gust magnitude through the flight.

## Stronger winds farther above the airport

The older September 20 00Z WN3 profile extraction has a **28 kt median wind near 3,000 ft MSL at 8 a.m.**, with p10–p90 of 24–31 kt. That falls to **12 kt at 2 p.m.**, with p10–p90 of 10–15 kt. These are all-member native six-hour samples, not interpolated flight-hour winds. [Profile analysis](../wn3-ensemble-20260920/profiles/analysis.md).

This earlier run provides a physically plausible source of stronger momentum if sufficiently deep mixing occurs. It does not demonstrate that 25–30 kt reaches the surface. Conversely, the newer forecast's relatively modest 100 m winds cannot rule out a gust fed by higher layers or local turbulence. The older profiles cannot prove that the same stronger layer persists in the newer cycle.

The older run also has warming between 925 and 850 hPa in 45 of 64 members at 8 a.m. That cap lies above the 925-hPa wind sample; it should not be treated as proof that the faster 925-hPa air is isolated from the ground. The sparse pressure levels do not resolve the surface layer or its mixing depth.

## How this changes the gust interpretation

WN3 gives physical support for considering momentum transfer and supports eventual easing. It adds little confidence to a specific statement that Thursday's gusts remain below 20 kt, or that they reach 25–28 kt. The 100 m information is modestly reassuring about the strength of the immediate near-surface flow, while the older profiles preserve a reason to investigate stronger morning gust potential.

A numerical WN3-derived gust product would require an independently validated relationship to observed KCDW gusts, ideally conditioned on wind direction, winds at several heights, stability, heating and forecast lead. Multiplying sustained wind by a universal factor, substituting 100 m p90 for gust, or averaging WN3 wind with IFS gust would not provide that validation.

No new WN3 query or pressure-field download was required for this assessment. No application code or published forecast was changed.
