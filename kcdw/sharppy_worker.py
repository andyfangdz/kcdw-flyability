"""Isolated SHARPpy analysis worker for model soundings; reads stdin, never the network.

Runs in var/sharppy-venv (see requirements-sharppy.txt). Request (stdin JSON):
{"profiles": [{"id": str, "pres": [hPa], "hght": [m MSL], "tmpc": [C], "dwpc": [C],
"wdir": [deg], "wspd": [kt]}, ...]}, surface first, pressure strictly decreasing.
Profiles are resampled every 10 hPa (linear in log pressure) before analysis.
Returns, per profile, SHARPpy's surface-based and 100 hPa mixed-layer parcels,
its boundary-layer top (virtual potential temperature method), the strongest
and mean wind within that mixed layer, the 0-3 km lapse rate and precipitable
water, plus the surface parcel's virtual-temperature trace for drawing. Heights are metres above
ground level. Prints one JSON list; a profile SHARPpy cannot analyse is left out.
"""
import contextlib
import io
import json
import math
import signal
import sys
import warnings

MAX_PROFILES = 12
MAX_LEVELS = 120


def require(ok):
    if not ok:
        raise ValueError('sharppy validation failed')


def finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and abs(value) < 1e8 else None


def resample(arrays, step=10.0):
    """Add levels every ``step`` hPa, linear in log pressure (SHARPpy's own interpolation), so the
    boundary-layer top and mixed-layer winds fall between coarse model levels instead of snapping to them."""
    import numpy as np
    pres = arrays['pres']
    grid = np.arange(np.floor(pres[0] / step) * step, pres[-1], -step)
    new = np.unique(np.concatenate([pres, grid[(grid < pres[0]) & (grid > pres[-1])]]))[::-1]
    x, xs = np.log(new), np.log(pres)
    rad = np.radians(arrays['wdir'])
    u, v = -arrays['wspd'] * np.sin(rad), -arrays['wspd'] * np.cos(rad)
    at = lambda values: np.interp(x, xs[::-1], values[::-1])
    ui, vi = at(u), at(v)
    return {'pres': new, 'hght': at(arrays['hght']), 'tmpc': at(arrays['tmpc']), 'dwpc': np.minimum(at(arrays['dwpc']), at(arrays['tmpc'])),
            'wdir': np.degrees(np.arctan2(-ui, -vi)) % 360, 'wspd': np.hypot(ui, vi)}


def analyse(item):
    import numpy as np
    from sharppy.sharptab import interp, params, profile, winds
    arrays = {k: np.array(item[k], dtype=float) for k in ('pres', 'hght', 'tmpc', 'dwpc', 'wdir', 'wspd')}
    n = len(arrays['pres'])
    require(4 <= n <= MAX_LEVELS and all(len(a) == n for a in arrays.values()))
    require(bool(np.all(np.diff(arrays['pres']) < 0)) and bool(np.all(np.diff(arrays['hght']) > 0)))
    require(bool(np.all(arrays['dwpc'] <= arrays['tmpc'] + 0.01)))
    arrays = resample(arrays)
    n = len(arrays['pres'])
    prof = profile.create_profile(profile='default', missing=-9999, strictQC=False, **arrays)
    sfc_h = float(prof.hght[prof.sfc])
    agl = lambda h: None if finite(h) is None else round(float(h) - sfc_h)
    out = {'id': item['id']}
    for name, flag in (('surface', 1), ('mixed_layer', 4)):
        pcl = params.parcelx(prof, flag=flag)
        cape = round(finite(pcl.bplus) or 0.0)
        out[name] = {'lcl_m': agl(pcl.lclhght), 'lfc_m': agl(pcl.lfchght) if cape > 0 else None,
                     'cape': cape, 'cin': round(finite(pcl.bminus) or 0.0)}
        if name == 'surface':
            trace = [(finite(p), finite(t)) for p, t in zip(pcl.ptrace, pcl.ttrace)]
            # SHARPpy traces virtual temperature, as its own skew-T does.
            out['parcel_trace'] = [[round(p, 1), round(t, 1)] for p, t in trace if p is not None and t is not None]
    top = finite(params.pbl_top(prof))
    # pbl_top prints a warning and returns the profile top when no level is warm enough; that is no answer.
    require(top is not None and prof.pres[-1] < top <= prof.pres[prof.sfc])
    top_h = finite(interp.hght(prof, top))
    out['mixing_top'] = {'pres': round(top, 1), 'hght_m': agl(top_h)}
    inside = [i for i in range(n) if arrays['pres'][i] >= top]
    strongest = max(inside, key=lambda i: arrays['wspd'][i])
    # The boundary layer top is interpolated; the strongest wind can sit just below it between levels.
    at_top = (finite(interp.vec(prof, top)[0]), finite(interp.vec(prof, top)[1]))
    peak = {'wspd': round(float(arrays['wspd'][strongest]), 1), 'wdir': round(float(arrays['wdir'][strongest])),
            'hght_m': agl(arrays['hght'][strongest])}
    if at_top[1] is not None and at_top[1] > peak['wspd']:
        peak = {'wspd': round(at_top[1], 1), 'wdir': round(at_top[0]), 'hght_m': out['mixing_top']['hght_m']}
    out['mixed_layer_max_wind'] = peak
    u, v = winds.mean_wind(prof, pbot=prof.pres[prof.sfc], ptop=top)
    speed = finite(math.hypot(u, v)) if finite(u) is not None and finite(v) is not None else None
    out['mixed_layer_mean_wind'] = None if speed is None else {'wspd': round(speed, 1), 'wdir': round(math.degrees(math.atan2(-u, -v)) % 360)}
    out['lapse_rate_0_3km'] = None if finite(params.lapse_rate(prof, 0, 3000, pres=False)) is None else round(finite(params.lapse_rate(prof, 0, 3000, pres=False)), 1)
    pw = finite(params.precip_water(prof))
    out['precip_water_in'] = None if pw is None else round(pw, 2)
    return out


def main():
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(60)
    request = json.loads(sys.stdin.read(400_000))
    require(set(request) == {'profiles'})
    profiles = request['profiles']
    require(isinstance(profiles, list) and 1 <= len(profiles) <= MAX_PROFILES)
    result = []
    for item in profiles:
        # SHARPpy prints diagnostics to stdout, which carries this worker's JSON; one profile's failure drops only it.
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter('ignore')
            try:
                result.append(analyse(item))
            except Exception:
                continue
    print(json.dumps(result, allow_nan=False, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
