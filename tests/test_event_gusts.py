import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from kcdw import event_gusts as gusts
from test_event_model_matrix import NOW, SNAPSHOT

UTC = timezone.utc


def response(run, *, gust=24.0, direction=40, hours=240):
    start = run.replace(hour=0)
    times = [(start + timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M') for h in range(hours)]
    covered = [run <= start + timedelta(hours=h) for h in range(hours)]
    column = lambda value: [value if ok else None for ok in covered]
    return {'latitude': 40.875, 'longitude': -74.25, 'utc_offset_seconds': 0,
            'hourly': {'time': times, 'wind_speed_10m': column(10.0), 'wind_direction_10m': column(direction),
                       'wind_gusts_10m': column(gust)}}


class FakeClient:
    def __init__(self, available, **kwargs):
        self.available, self.kwargs, self.urls = available, kwargs, []

    def get(self, url):
        self.urls.append(url)
        query = parse_qs(urlparse(url).query)
        run = datetime.fromisoformat(query['run'][0]).replace(tzinfo=UTC)
        model = query['models'][0]
        if run not in self.available.get(model, ()):
            raise OSError('HTTP Error 400: Bad Request')
        return response(run, **self.kwargs.get(model, {}))


def available(hours_back):
    return {spec[2]: [NOW.replace(minute=0) - timedelta(hours=h) for h in hours_back] for spec in gusts.MODELS}


def fake_rrfs(snapshot, run, hours, now):
    if run not in available(range(0, 30))['ncep_nbm_conus']:
        raise OSError('not published')
    series = {name: [value if t.hour >= 10 and t.day == 24 else None for t in hours]
              for name, value in (('wind', 12.0), ('gust', 22.0), ('direction', 48.0))}
    return series, f'{gusts.RRFS_ROOT}{run:%Y%m%d}/{run:%H}/', {'latitude': 40.8755, 'longitude': -74.2668}


class GustTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(gusts, '_rrfs_series', side_effect=fake_rrfs)
        self.rrfs = patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, client, cache=None, now=NOW):
        return gusts.collect_gusts(client, copy.deepcopy(SNAPSHOT), now, cache)

    def test_newest_covering_run_is_bound_and_summarized(self):
        runs = available(range(0, 30))
        packet = self.collect(FakeClient(runs, ncep_nbm_conus={'gust': 26.0, 'direction': 50}))
        gusts.validate_gusts(packet, SNAPSHOT)
        nbm = packet['models'][0]
        self.assertTrue(nbm['ok'])
        self.assertEqual(nbm['run'], '2026-09-18T21:00:00Z')
        self.assertIn('run=2026-09-18T21%3A00', nbm['source_url'])
        summary = gusts.summarize(nbm, SNAPSHOT)
        self.assertEqual([s['at'] for s in summary['samples']], ['2026-09-24T14:00:00Z', '2026-09-24T15:00:00Z', '2026-09-24T16:00:00Z'])
        self.assertEqual(summary['peak_gust_kt'], 26.0)
        self.assertEqual(summary['gust_crosswind_max_kt'], {'04': 8.9, '10': 14.2})

    def test_short_runs_are_unavailable_not_calm(self):
        client = FakeClient(available(range(0, 30)))
        original = client.get
        def short(url):
            raw = original(url)
            if 'ncep_hrrr_conus' in url:
                raw['hourly']['wind_gusts_10m'] = [None] * len(raw['hourly']['time'])
            return raw
        client.get = short
        packet = self.collect(client)
        hrrr = next(r for r in packet['models'] if r['key'] == 'hrrr')
        self.assertFalse(hrrr['ok'])
        self.assertNotIn('gust', hrrr)

    def test_cache_reuses_run_without_refetch(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'cache.json'
            first = FakeClient(available(range(0, 30)))
            self.collect(first, cache)
            second = FakeClient(available(range(0, 30)))
            packet = self.collect(second, cache)
            self.assertEqual(second.urls, [])
            self.assertTrue(all(row['ok'] for row in packet['models']))

    def test_validation_rejects_tampering_and_other_snapshots(self):
        packet = self.collect(FakeClient(available(range(0, 30))))
        for mutate in (lambda p: p['models'][0]['gust'].__setitem__(0, -1),
                       lambda p: p['models'][0].update(run='2026-09-16T21:00:00Z'),
                       lambda p: p['models'][1].update(run='2026-09-18T15:00:00Z'),
                       lambda p: p.update(snapshot_collected_at='2026-09-18T20:00:00Z'),
                       lambda p: p['models'].reverse()):
            bad = copy.deepcopy(packet)
            mutate(bad)
            with self.assertRaises(ValueError):
                gusts.validate_gusts(bad, SNAPSHOT)

    def test_rrfs_row_is_native_and_bound_to_its_run_directory(self):
        packet = self.collect(FakeClient(available(range(0, 30))))
        rrfs = next(r for r in packet['models'] if r['key'] == 'rrfs')
        self.assertTrue(rrfs['ok'])
        self.assertEqual(rrfs['run'], '2026-09-18T18:00:00Z')
        self.assertTrue(rrfs['source_url'].startswith(gusts.RRFS_ROOT + '20260918/18/'))
        self.assertEqual(gusts.summarize(rrfs, SNAPSHOT)['peak_gust_kt'], 22.0)
        bad = copy.deepcopy(packet)
        next(r for r in bad['models'] if r['key'] == 'rrfs')['source_url'] = gusts.API + '?x'
        with self.assertRaises(ValueError):
            gusts.validate_gusts(bad, SNAPSHOT)

    def test_nothing_covering_returns_none(self):
        self.rrfs.side_effect = OSError('offline')
        self.assertIsNone(self.collect(FakeClient({})))
        self.assertIsNone(self.collect(FakeClient(available(range(0, 30))), now=datetime(2026, 9, 24, 17, tzinfo=UTC)))

    def test_wind_view_lists_models_and_missing_labels(self):
        from kcdw.event_wind_view import _deterministic
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot['event_gusts'] = self.collect(FakeClient({k: v for k, v in available(range(0, 30)).items() if k != 'ncep_hrrr_conus'}))
        evidence = gusts.gust_evidence(snapshot, NOW)
        html = _deterministic(evidence, None)
        self.assertIn('NBM · NWS blend', html)
        self.assertIn('No covering run yet: HRRR.', html)
        self.assertIn('24 · 24 · 24', html)
        self.assertLess(len(json.dumps(evidence)), 4000)


if __name__ == '__main__':
    unittest.main()
