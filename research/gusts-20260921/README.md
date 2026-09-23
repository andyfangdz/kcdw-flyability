# KCDW gust investigation — September 24, 10 a.m.–noon EDT

Evidence checked September 21, 2026, at approximately 12:33 a.m. EDT. The expected flight is Thursday, September 24, 10 a.m.–noon; the appointment is at 8 a.m.

Follow-up: [What WN3's 10 m, 100 m and pressure-level winds imply about gust potential](wn3-inference.md).

**Gusts remain a meaningful independent concern.** The sources support two different outcomes: NWS and the latest GFS favor upper-teen/near-20 kt gusts during the flight, while IFS and ICON retain mid-20s gusts, with native IFS reaching the upper 20s in intervals overlapping the flight. A 20–25 kt gust scenario is a reasonable planning consideration, with upper-20s gusts still supported by some guidance. This is a synthesis of conflicting forecasts, not a calibrated interval or an aircraft/pilot limit.

## What the additional hourly extraction shows

The app's native GFS profile ordinarily samples every three hours. This investigation retrieved the four intervening native hourly steps from NOAA, preserving the same September 21 00Z initialization and grid point. These are actual hourly model outputs, not values interpolated from the app's chart.

| Thursday EDT | GFS sustained, kt | GFS direction, ° true | GFS gust, kt | GFS 925-hPa wind, kt | NWS gust, kt |
|---|---:|---:|---:|---:|---:|
| 8 a.m. | 9.2 | 25 | 25.9 | 32.5 | 15 |
| 9 a.m. | 10.5 | 37 | 23.7 | 32.7 | 16 |
| **10 a.m.** | **11.2** | **45** | **20.2** | **25.2** | **17** |
| **11 a.m.** | **10.8** | **54** | **17.9** | **23.4** | **17** |
| **Noon** | **10.7** | **62** | **15.8** | **20.8** | **16** |
| 1 p.m. | 10.2 | 57 | 14.8 | 19.8 | 14 |
| 2 p.m. | 9.2 | 48 | 14.2 | 18.8 | 13 |

GFS therefore places its stronger gusts before the flight and weakens through the expected departure/return window. Its 10 a.m. and noon values are now resolved without temporal interpolation. These remain instantaneous gust diagnostics at sampled times, not guaranteed maxima over each intervening hour. The nearest native grid point is 41.0°N, 74.25°W, approximately 14 km north of KCDW.

The NWS grid was independently fetched again at 12:32 a.m. EDT and agreed with the saved briefing. It specifies 050° true at 12 kt, gusting 17 kt through the flight, with 16 kt at the noon sample. Its issue time remains **Sunday 2:08 p.m. EDT**; the later fetch did not make it a newer forecast. [NWS grid](https://api.weather.gov/gridpoints/OKX/23,48), [saved response](nws-grid.json), [native GFS fields and byte-range proofs](gfs-hourly-native.json).

## The stronger IFS gusts are not confined to the early morning

The existing briefing displays native IFS's largest gust over 8 a.m.–2 p.m. Splitting its two retained source fields gives:

| Native IFS interval, EDT | Maximum gust |
|---|---:|
| 8–11 a.m. | 27.4 kt |
| 11 a.m.–2 p.m. | 28.3 kt |

Both intervals overlap the expected flight. The stronger signal persists into the second interval, so it cannot be dismissed as only an early-morning peak. Neither interval establishes the precise time of its maximum; both maxima could still fall outside the two-hour flight.

These fields come from September 20 18Z IFS at the same native grid point as GFS. Their identities explicitly encode three-hour maxima (`10fg3`); the app correctly combines them into a six-hour maximum. **An IFS interval maximum and a GFS instantaneous diagnostic measure different periods.** The amount of their disagreement attributable to that difference is unknown. [ECMWF open-data field definitions](https://www.ecmwf.int/en/forecasts/datasets/open-data), [saved native identities](evidence.json).

## Other model comparisons and ensemble spread

The saved run-pinned model matrix evaluates the 10, 11 and noon samples at each provider's selected point:

| Guidance | Initialization | Peak reported gust in sampled window | Previous covering run |
|---|---|---:|---:|
| IFS deterministic through Open-Meteo | Sep 20 18Z | 25.2 kt | 25.7 kt, Sep 20 12Z |
| ICON through Open-Meteo | Sep 21 00Z | 25.1 kt | 25.2 kt, Sep 20 18Z |
| GFS through Open-Meteo | Sep 20 18Z | 21.2 kt | 19.6 kt, Sep 20 12Z |
| UKMO through Open-Meteo | Sep 20 12Z | 17.7 kt | 20.2 kt, Sep 20 00Z |

IFS and ICON's approximately 25 kt forecasts survived their latest run changes. The older, differently gridded Open-Meteo GFS entry should not be relabeled as the newer native 00Z GFS extraction above. Exact requests and grid coordinates are retained in [evidence.json](evidence.json).

The separate rolling Open-Meteo ensemble packet supplies paired gust/direction samples at the flight's closing hourly endpoints:

| Ensemble | Median member sampled gust maximum | 90th percentile | Members ≥20 kt | Members ≥25 kt | Members ≥30 kt |
|---|---:|---:|---:|---:|---:|
| GEFS | 17.5 kt | 20.8 kt | 5/31 | 2/31 | 0/31 |
| IFS ENS | 22.9 kt | 27.6 kt | 40/51 | 17/51 | 0/51 |

This is a model-family disagreement, not just one high deterministic forecast. The fractions are descriptive member counts, **not calibrated chances of an airport gust**. The rolling API's advertised initialization is not bound to individual returned values, and temporal interpolation can affect the samples. These 51 IFS members are also a different collection from the app's 50 direct-native IFS perturbations. Do not combine the collections or pool their counts. No sampled member reaching 30 kt does not establish a 30 kt ceiling.

For an additional check on persistence, the response-bound native IFS ensemble's noon display median stayed between 22.1 and 24.6 kt across the saved recent cycles; the latest is 22.9 kt. These are interpolated display values from interval gust samples, not verified noon maxima. WN3 and AIFS do not supply gust forecasts in these packets; their lower sustained winds cannot resolve the gust disagreement.

## Physical and operational meaning

All three native deterministic profile sources have approximately 30–33 kt wind at 925 hPa at 8 a.m. GFS still has 25 kt there at 10 a.m., easing to 21 kt by noon. That supports examining how the morning boundary layer mixes stronger momentum toward the surface. It does not establish a surface gust equal to the 925-hPa wind, a precise height above ground, or a turbulence/LLWS diagnosis. Gust magnitude depends on surface friction, stability and mixing as well as mean wind. [ECMWF surface-wind guidance](https://confluence.ecmwf.int/spaces/FUG/pages/673551686/Section%2B9.3%2BSurface%2Bwind).

The newer **Sunday 11:11 p.m. EDT OKX discussion** highlights a prolonged northeast pressure-gradient flow and a regional Wednesday/Thursday aviation gust range of 20–30 kt, strongest at coastal terminals. That is consistent with retaining the stronger scenario while recognizing inland KCDW's lower point forecast. The regional range is not a KCDW-specific forecast. Dry conditions alone would not eliminate this wind mechanism. [NWS discussion](https://api.weather.gov/products/03ddb9b6-98f5-4753-9417-4f07fa2f36c8); its complete retrieved text and issue/fetch times are preserved in [evidence.json](evidence.json).

For the runway geometry already used by the app (04: 030° true; 10: 083° true), a **hypothetical 25 kt gust from 050° true** has approximately **8.6 kt crosswind on 04** and **13.6 kt on 10**. At 060° true those components become 12.5 and 9.8 kt respectively, so a modest direction change matters. These are magnitude calculations using `gust × abs(sin(direction − runway heading))`, not runway assignments or operational limits. Actual gust direction can differ from the model's mean wind direction; runway availability is unchecked.

The most useful next evidence is convergence between updated NWS point gusts and IFS/ICON, plus closer-range profiles and observations showing whether the stronger morning winds mix down. Present evidence supports carrying gust strength and direction as a separate checkride constraint even if the cloud/rain outlook improves.

## Reproduction and checks

- [collect.py](collect.py) freezes evidence from the identified archived snapshot, extracts missing GFS hours with the existing bounded NOAA byte-range reader and ecCodes decoder, and refreshes the public NWS grid. Run from the repository root with `var/native-weather-venv/bin/python research/gusts-20260921/collect.py`.
- [evidence.json](evidence.json) records the original snapshot path and SHA-256, validated wind summaries, native field identities, model request URLs, forecast evolution, and the NWS discussion. Three of the seven GFS hours come from this existing archived extraction; the four additional hours carry their own retrieval clocks.
- All seven GFS hours have all seven required fields: 49 decoded fields in total. Run/valid times, instantaneous step bounds, common grid coordinates and byte-range sizes were checked; the decoder also checks GRIB identity and source-message structure. NWS intervals cover every displayed hour and match the original flight-window values.
- Research artifacts only; no application code or published forecast was changed. ECMWF data: CC BY 4.0; Open-Meteo data: CC BY 4.0; NOAA/NWS data: public domain.
