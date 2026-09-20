import copy
import unittest
from datetime import datetime, timedelta, timezone

from kcdw import event_wn2_members as wn2
from kcdw.event_wn2_members_view import headline, render_wn2_members
from test_event_model_matrix import SNAPSHOT

UTC = timezone.utc
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
SNAP = dict(SNAPSHOT, collected_at='2026-09-19T12:00:00Z')


def response(*, members=64, cloudy=5, northeast=50, wet=2, drop=0):
    start = datetime(2026, 9, 24, 12, tzinfo=UTC)
    hourly, units = {'time': [int((start + timedelta(hours=h)).timestamp()) for h in range(7)]}, {'time': 'unixtime'}
    for m in range(members):
        suffix = '' if m == 0 else f'_member{m:02d}'
        column = lambda value: [None if m < drop else value] * 7
        for name, value in (('wind_speed_10m', 6.0 + m / 16), ('wind_speed_100m', 12.0 + m / 16), ('pressure_msl', 1025.0 + m / 16),
                            ('cloud_cover_low', 90.0 if m < cloudy else 5.0), ('wind_direction_10m', 40.0 if m < northeast else 350.0),
                            ('precipitation', 0.6 if m < wet else 0.0)):
            hourly[name + suffix], units[name + suffix] = column(value), wn2.VARIABLES[name][0]
    return {'latitude': 41.0, 'longitude': -74.25, 'utc_offset_seconds': 0, 'hourly': hourly, 'hourly_units': units}


class FakeClient:
    direct_native = False

    def __init__(self, payload, init=datetime(2026, 9, 19, 0, tzinfo=UTC)):
        self.payload, self.init, self.urls = payload, init, []

    def get(self, url):
        self.urls.append(url)
        if url == wn2.METADATA:
            return {'last_run_initialisation_time': int(self.init.timestamp()), 'temporal_resolution_seconds': 21600}
        return self.payload


class Wn2MemberTests(unittest.TestCase):
    def packet(self, **kwargs):
        return wn2.collect_legacy_members(FakeClient(response(**kwargs)), SNAP, NOW)

    def test_samples_are_native_steps_bracketing_the_flight(self):
        start, end = datetime(2026, 9, 24, 14, tzinfo=UTC), datetime(2026, 9, 24, 16, tzinfo=UTC)
        self.assertEqual([t.hour for t in wn2.sample_times(start, end)], [12, 18])
        self.assertEqual([t.hour for t in wn2.sample_times(start.replace(hour=12), end.replace(hour=21))], [12, 18, 0])

    def test_collect_reduces_members_to_counts_and_quantiles(self):
        fake = FakeClient(response())
        packet = wn2.collect_legacy_members(fake, SNAP, NOW)
        wn2.validate_members(packet, SNAP)
        self.assertIn('models=google_weathernext2_ensemble', fake.urls[0])
        self.assertIn('start_hour=2026-09-24T12%3A00', fake.urls[0])
        self.assertEqual([s['at'] for s in packet['samples']], ['2026-09-24T12:00:00Z', '2026-09-24T18:00:00Z'])
        first = packet['samples'][0]
        self.assertEqual((first['cloudy'], first['sector']), ({'count': 5, 'n': 64}, {'count': 50, 'n': 64}))
        self.assertEqual(first['wind_10m_kt']['n'], 64)
        self.assertLess(first['wind_10m_kt']['p10'], first['wind_10m_kt']['p90'])
        # Two hours end inside the 10:00-12:00 window: 2 x 0.6 mm reaches the 1 mm screen for the two wet members.
        self.assertEqual(packet['rain'], {'count': 2, 'n': 64, 'p90_mm': 0.0})
        self.assertEqual((packet['advertised_init'], packet['run_binding']), ('2026-09-19T00:00:00Z', 'latest-advertised; not response-bound'))
        self.assertNotIn('hourly', packet)

    def test_collect_fails_closed_on_bad_contracts(self):
        for payload in (response(members=65), response(drop=20), dict(response(), utc_offset_seconds=-14400), dict(response(), latitude=45.0)):
            with self.assertRaises(ValueError):
                wn2.collect_legacy_members(FakeClient(payload), SNAP, NOW)
        bad = response()
        bad['hourly_units']['wind_speed_10m_member03'] = 'km/h'
        with self.assertRaises(ValueError):
            wn2.collect_legacy_members(FakeClient(bad), SNAP, NOW)
        shifted = response()
        shifted['hourly']['time'] = [t + 3600 for t in shifted['hourly']['time']]
        with self.assertRaises(ValueError):
            wn2.collect_legacy_members(FakeClient(shifted), SNAP, NOW)
        with self.assertRaises(ValueError):
            wn2.collect_legacy_members(FakeClient(response(), init=NOW + timedelta(hours=6)), SNAP, NOW)
        self.assertIsNone(wn2.collect_legacy_members(FakeClient(response()), SNAP, datetime(2026, 9, 24, 17, tzinfo=UTC)))
        self.assertEqual(self.packet(drop=10)['samples'][0]['cloudy']['n'], 54)

    def test_validation_rejects_tampering(self):
        packet = self.packet()
        for mutate in (lambda p: p.update(version=2), lambda p: p.update(members=51), lambda p: p['thresholds'].update(cloudy_pct=50),
                       lambda p: p['samples'].pop(), lambda p: p['samples'][0]['cloudy'].update(count=70),
                       lambda p: p['samples'][0]['wind_10m_kt'].update(p10=99.0), lambda p: p['rain'].update(n=10),
                       lambda p: p.update(run_binding='response-bound'), lambda p: p.update(snapshot_collected_at='x')):
            bad = copy.deepcopy(packet)
            mutate(bad)
            with self.assertRaises((ValueError, KeyError, TypeError)):
                wn2.validate_members(bad, SNAP)

    def test_view_reports_counts_and_fails_closed(self):
        snapshot = dict(SNAP, weathernext2_members=self.packet())
        markup = render_wn2_members(snapshot, NOW)
        self.assertEqual(headline(snapshot['weathernext2_members']), '5 of 64 members keep a low deck; 50 from the northeast; rain in 2 of 64')
        for text in ('id="wn2-members"', 'Thu 08:00', 'Thu 14:00', '5 of 64', '50 of 64', 'From 020–070°',
                     'no gust field; 100 m wind is context, not a gust forecast', 'not bound to that run'):
            self.assertIn(text, markup)
        tampered = copy.deepcopy(snapshot)
        tampered['weathernext2_members']['model_id'] = '<script>'
        self.assertEqual(render_wn2_members(tampered, NOW), '')
        self.assertEqual(render_wn2_members(SNAP, NOW), '')


if __name__ == '__main__':
    unittest.main()
