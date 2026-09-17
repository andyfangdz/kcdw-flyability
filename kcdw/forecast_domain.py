"""Choose the shared hourly display domain from current plotted forecast data.

This changes only the axis: callers must timestamp-align each source and retain
missing values. No archived runs or historical values are read or synthesized.
"""
from datetime import datetime, timedelta
import math

from .common import UTC, parse_time
from . import event_ensemble, event_moisture_view, gfs_guidance

PLOTTED_FIELDS = (
    'precipitation', 'wind_speed_10m', 'cloud_cover_low',
    'pressure_msl', 'temperature_2m', 'wind_gusts_10m',
)
WN3_FIELDS = ('precipitation_1h', 'wind_speed_10m',
              'sea_level_pressure', 'temperature_2m')
FAN_STATS = ('p10', 'p50', 'p90')
HOUR = timedelta(hours=1)


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _utc(value):
    moment = value if isinstance(value, datetime) else parse_time(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError('forecast domain timestamps must be timezone-aware')
    return moment.astimezone(UTC)


def _series(hourly, fields, stats=FAN_STATS):
    """Yield just plotted numeric arrays, never counts or other metadata."""
    for name in fields:
        raw = hourly.get(name)
        if isinstance(raw, dict):
            for stat in stats:
                values = raw.get(stat)
                if isinstance(values, (list, tuple)):
                    yield values
        elif isinstance(raw, (list, tuple)):
            yield raw


def forecast_times(snapshot, models, original_times, now, window_start):
    """Return a new hourly UTC axis ending at the original last timestamp.

    Main models are already validated normalized records. Supplementary sources
    use the same availability gates as their renderers. The first usable plotted
    sample chooses the start (even outside the old start); the mission start is
    always retained. With no evidence, retain the old start instead. The axis is
    anchored at its unchanged end; a fractional mission start rounds outward.
    """
    if not original_times:
        return []
    end = _utc(original_times[-1])
    earliest = None

    def consider(times, series):
        nonlocal earliest
        arrays = list(series)
        for i, stamp in enumerate(times):
            if not any(i < len(values) and _number(values[i]) for values in arrays):
                continue
            moment = _utc(stamp)
            if moment <= end and (earliest is None or moment < earliest):
                earliest = moment

    for model in models:
        hourly = model.get('hourly', {})
        # The comparison renderer omits fans with no available members.
        fields = [name for name in PLOTTED_FIELDS
                  if isinstance(hourly.get(name), dict)
                  and hourly[name].get('members_with_data')]
        consider(hourly.get('time', []), _series(hourly, fields))

    wn2 = snapshot.get('weathernext2', {})
    if wn2.get('ok'):
        hourly = wn2['data']['hourly']
        # SD alone is not a plotted value: both band endpoints need a mean.
        means = [hourly[name] for name in PLOTTED_FIELDS
                 if isinstance(hourly.get(name), (list, tuple))
                 and isinstance(hourly.get(name + '_spread'), (list, tuple))]
        consider(hourly.get('time', []), means)

    if event_ensemble.weathernext3_diagnostic(snapshot, now).get('available'):
        forecast = snapshot['weathernext3']['data']['forecast']
        consider(forecast.get('valid_time_utc', []),
                 _series(forecast.get('fields', {}), WN3_FIELDS, ('mean', 'p10', 'p90')))

    if gfs_guidance.validate_gfs(snapshot.get('gfs'), now).get('available'):
        hourly = snapshot['gfs']['data']['hourly']
        consider(hourly.get('time', []), _series(hourly, PLOTTED_FIELDS))

    moisture = event_moisture_view.validated(snapshot, now)['models']
    for key in event_moisture_view.MODELS:
        source = moisture.get(key, {})
        if source.get('available'):
            hourly = source['data']['hourly']
            consider(hourly.get('time', []),
                     _series(hourly, [field for field, _ in event_moisture_view.FIELDS]))

    start = min(earliest if earliest is not None else _utc(original_times[0]),
                _utc(window_start))
    hours = math.ceil((end - start) / HOUR)
    return [end - HOUR * i for i in range(hours, -1, -1)]
