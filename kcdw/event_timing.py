"""Snapshot-bound appointment and expected flight timing.

Never read live configuration when rendering an archive. Expected duration/end
must be explicitly supplied and consistent; broad forecast statistics stay intact.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, time
from html import escape
import re

from .common import UTC, iso_z, load_json
from .events import DEFAULT_CONFIG, TZ, Event, _event

KEYS = {'slug', 'date', 'timezone', 'appointment_start', 'appointment_status',
        'flight_start', 'flight_status', 'flight_end'}
CLOCK = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')


def validate_timing(value, event: Event):
    if not isinstance(value, dict) or set(value) not in (KEYS, KEYS | {'flight_duration_minutes'}):
        return None
    if (value['slug'] != event.slug or value['date'] != event.date or
            value['timezone'] != 'America/New_York' or
            value['appointment_status'] != 'confirmed' or value['flight_status'] != 'expected'):
        return None
    if any(not isinstance(value[k], str) or not CLOCK.fullmatch(value[k]) for k in ('appointment_start', 'flight_start')):
        return None
    appointment, flight = [time.fromisoformat(value[k]) for k in ('appointment_start', 'flight_start')]
    if not event.start_hour <= appointment.hour or not appointment <= flight or flight.hour >= event.end_hour:
        return None
    if 'flight_duration_minutes' not in value:
        if value['flight_end'] is not None:
            return None
    else:
        duration, end = value['flight_duration_minutes'], value['flight_end']
        if (type(duration) is not int or not 0 < duration <= 1440 or
                not isinstance(end, str) or not CLOCK.fullmatch(end)):
            return None
        end_clock = time.fromisoformat(end)
        flight_minutes = flight.hour * 60 + flight.minute
        end_minutes = end_clock.hour * 60 + end_clock.minute
        if end_minutes - flight_minutes != duration or end_minutes > event.end_hour * 60:
            return None
    return deepcopy(value)


def load_event_timing(event, path=None):
    """Optional config metadata; invalid/missing schedules never become confirmed."""
    try:
        data = load_json(path or DEFAULT_CONFIG)
        return validate_timing(data.get('timings', {}).get(event.slug), event)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def timing_evidence(snapshot):
    try:
        event = _event(snapshot['event'])
        value = validate_timing(snapshot.get('event_timing'), event)
        if value is None:
            return None
        names = ('appointment_start', 'flight_start') + (('flight_end',) if value['flight_end'] else ())
        for name in names:
            local = datetime.combine(event.day, time.fromisoformat(value[name]), TZ)
            value[name+'_utc'] = iso_z(local.astimezone(UTC))
            value[name+'_local'] = local.strftime('%H:%M %Z')
        value['forecast_context_window'] = event.window
        value['flight_end_status'] = 'expected' if value['flight_end'] else 'unknown'
        end_note = ('Expected flight end and duration are supplied; the entire flight window is approximate. '
                    if value['flight_end'] else 'Flight end is unknown; do not invent a duration. ')
        value['interpretation'] = ('Appointment start is confirmed; flight start is approximate. ' + end_note +
            'Prioritize conditions around expected departure and through the flight; do not count late-afternoon clearing as an adequate substitute. '
            'The broader forecast-context window and its rain/cloud statistics are not flight-period statistics or assumed scheduling flexibility.')
        return value
    except (KeyError, ValueError, TypeError):
        return None


def timing_header(snapshot, event):
    value = timing_evidence(snapshot)
    if value is None:
        return (f'<p class="event-when">{escape(event.label())}</p>'
                f'<p class="event-window">Provisional {event.start_hour:02d}:00–{event.end_hour:02d}:00 Eastern · actual time not confirmed</p>')
    if value['flight_end']:
        hours = value['flight_duration_minutes'] / 60
        flight_line = (f'<p><strong>Around {escape(value["flight_start"])}–{escape(value["flight_end_local"])}</strong>'
                       f' · Expected flight · {hours:g} hours</p>')
        context_note = 'not the expected flight window. Flight timing remains approximate.'
    else:
        flight_line = f'<p><strong>Around {escape(value["flight_start_local"])}</strong> · Expected flight start</p>'
        context_note = 'not a confirmed flight duration. Flight end time is not confirmed.'
    return (f'<p class="event-when">{escape(event.day.strftime("%A, %B %-d"))}</p>'
            '<div class="event-schedule" aria-label="Appointment and expected flight timing">'
            f'<p><strong>{escape(value["appointment_start_local"])}</strong> · Confirmed checkride start</p>'
            f'{flight_line}</div>'
            f'<p class="event-window">Forecast context window: {event.start_hour:02d}:00–{event.end_hour:02d}:00 Eastern; '
            f'{context_note}</p>')
