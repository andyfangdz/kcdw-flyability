import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kcdw import event_climatology as clim
from kcdw.event_personal import validate_personal
from kcdw.events import Event
from test_event_model_matrix import NOW, SNAPSHOT

UTC = timezone.utc
HEADER = 'station,valid,sknt,gust,drct,vsby,skyc1,skyc2,skyc3,skyl1,skyl2,skyl3,wxcodes\n'


def metar_csv(days=400):
    lines = []
    start = datetime(2024, 1, 1)
    for d in range(days):
        day = start + timedelta(days=d)
        wind = d % 20
        for hm in ('09:53', '10:53', '11:53'):
            gust = 'null' if wind < 10 else str(wind + 9)
            sky = ('OVC', '800.00') if d % 10 == 0 else ('CLR', 'null')
            lines.append(f'CDW,{day:%Y-%m-%d} {hm},{wind}.00,{gust},210.00,10.00,{sky[0]},null,null,{sky[1]},null,null,null')
    return HEADER + '\n'.join(lines) + '\n'


def hourly(days=400, suffix='_previous_day7', gust=True, start='2024-09-01'):
    t0 = datetime.fromisoformat(start)
    times = [(t0 + timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M') for h in range(24 * days)]
    speed = [float((i // 24) % 15) for i in range(len(times))]
    return {'time': times, 'wind_speed_10m' + suffix: speed, 'wind_direction_10m' + suffix: [220.0] * len(times),
            'wind_gusts_10m' + suffix: [s + 8 if gust else None for s in speed], 'precipitation' + suffix: [0.0] * len(times)}


class Client:
    def __init__(self):
        self.urls = []

    def get_text(self, url, maximum):
        self.urls.append(url)
        return metar_csv()

    def get(self, url):
        self.urls.append(url)
        if 'previous-runs' in url:
            return {'hourly': hourly(gust='ecmwf' not in url)}
        h = {'time': [f'2026-09-24T{h:02d}:00' for h in range(24)]}
        for _, _, model in clim.ARCHIVE_MODELS:
            h |= {k.replace('_x', '_' + model): v for k, v in hourly(1, '_x', start='2026-09-24').items() if k != 'time'}
        return {'hourly': h}


class ClimatologyTests(unittest.TestCase):
    def snapshot(self):
        s = copy.deepcopy(SNAPSHOT)
        s['event_personal'] = {'slug': 'commercial-checkride', 'gust_limit_kt': 20,
                               'references': [{'label': 'Practice', 'date': '2026-09-17', 'sustained_kt': 13, 'gust_kt': 26, 'note': 'Landings hard.'}]}
        return s

    def collect(self, s, cache):
        return clim.collect_climatology(Client(), s, cache, NOW)

    def test_window_rows_and_flags(self):
        win = clim.window(self.snapshot())
        self.assertEqual(win['hours'], [10, 11, 12])
        text = HEADER + 'CDW,2024-05-01 09:53,5.00,null,210.00,10.00,CLR,null,null,null,null,null,null\n' \
            'CDW,2024-05-01 10:53,12.00,24.00,50.00,10.00,BKN,null,null,2500.00,null,null,RA\n' \
            'CDW,2024-05-01 11:53,8.00,null,40.00,4.00,CLR,null,null,null,null,null,null\n'
        [row] = clim.observed_rows(text, win)
        self.assertEqual(row[:4], ['2024-05-01', 12, 24, 12])
        self.assertFalse(row[5])  # ceiling, visibility and rain all fail

    def test_cache_is_reused_and_current_matches_archive_gusts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'c.json'
            s = self.snapshot()
            packet = self.collect(s, cache)
            self.assertEqual(set(packet['current']['rows']), {'nbm', 'gfs', 'ifs'})
            self.assertIsNone(packet['current']['rows']['ifs'][1])  # IFS archive has no gust
            client = Client()
            clim.collect_climatology(client, s, cache, NOW + timedelta(hours=2))
            self.assertEqual([u for u in client.urls if 'previous-runs' in u or 'mesonet' in u], [])

    def test_percentiles_tiers_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.snapshot()
            s['event_climatology'] = self.collect(s, Path(tmp) / 'c.json')
            a = clim.analysis(s, NOW)
            self.assertEqual(sum(a['tiers']['year'][k] for k in 'ABCD'), 100)
            self.assertEqual(clim._pct([1, 2, 3, 4], lambda v: v, 2), 62)
            html = clim.render_climatology(s, NOW)
            self.assertIn('20-kt gust limit', html)
            self.assertIn('Practice', html)
            self.assertEqual(html.count('class="clim-panel"'), 3)
            self.assertIsNotNone(clim.climatology_evidence(s, NOW))

    def test_wn3_low_cloud_ranked_without_gust_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.snapshot()
            packet = self.collect(s, Path(tmp) / 'c.json')
            packet['models']['wn3'] = {'label': 'WN3 mean', 'model': 'weathernext-3-bigquery', 'source_url': 'x', 'period': ['2026-01-08', '2026-09-23'],
                                       'rows': [[f'2026-{1 + i // 28:02d}-{1 + i % 28:02d}', 5.0 + i % 10, None, 2.0, 0.0, float(i % 100), 80.0] for i in range(200)]}
            packet['current']['rows']['wn3'] = [6.0, None, 2.0, 0.0, 20.0, 70.0]
            s['event_climatology'] = clim.validate_climatology(packet, s)
            m = clim.analysis(s, NOW)['models']['wn3']
            self.assertIsNone(m['gust_year'])
            self.assertIsNone(m['within_year'])
            self.assertEqual(m['low_cloud_year'], 80)
            self.assertIn('20% · 80', clim.render_climatology(s, NOW))

    def test_validation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.snapshot()
            packet = self.collect(s, Path(tmp) / 'c.json')
            for mutate in (lambda p: p.update(snapshot_collected_at='x'), lambda p: p['observed']['rows'][0].__setitem__(1, -3),
                           lambda p: p['window'].update(hours=[1])):
                bad = copy.deepcopy(packet)
                mutate(bad)
                with self.assertRaises(ValueError):
                    clim.validate_climatology(bad, s)
            s['event_climatology'] = {'version': 1}
            self.assertEqual(clim.render_climatology(s, NOW), '')

    def test_personal_settings_validation(self):
        event = Event(**SNAPSHOT['event'])
        good = self.snapshot()['event_personal']
        self.assertEqual(validate_personal(good, event)['gust_limit_kt'], 20)
        for bad in ({**good, 'gust_limit_kt': 200}, {**good, 'slug': 'other'},
                    {**good, 'references': [{**good['references'][0], 'gust_kt': 5}]}):
            with self.assertRaises(ValueError):
                validate_personal(bad, event)


if __name__ == '__main__':
    unittest.main()
