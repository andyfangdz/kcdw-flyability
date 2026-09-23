"""Interim cycles must retain their short horizon and never replace default discovery."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from kcdw.weathernext3_bigquery import BigQueryStore, request_body, DEFAULT_TABLE
from kcdw import wn3_hourly
import test_weathernext3_bigquery as fixtures
import test_weathernext3 as wn3


def packet_fixture():
    run = wn3.RUN+timedelta(hours=1)
    rows = fixtures.response()['rows'][:48]
    for lead,row in enumerate(rows,1):
        row.update(init_time=wn3.iso(run),valid_time=wn3.iso(run+timedelta(hours=lead)))
        row.update(wind_speed_100m_mean=7,wind_speed_100m_p10=6,wind_speed_100m_p90=8)
    provenance = wn3.fixture()['forecast']['query']
    provenance['retrieved_at'] = wn3.iso(wn3.NOW)
    return dict(version=1,init_time=wn3.iso(run),collected_at=wn3.iso(wn3.NOW),
                horizon_hours=48,rows=rows,query=provenance)


class HourlyRunTests(unittest.TestCase):
    def test_interim_fetch_requires_all_48_hours_and_reuses_cache(self):
        run = wn3.RUN + timedelta(hours=1)
        reply = fixtures.response()
        reply['rows'] = reply['rows'][:48]
        for lead, row in enumerate(reply['rows'], 1):
            row.update(init_time=wn3.iso(run), valid_time=wn3.iso(run+timedelta(hours=lead)))
        with tempfile.TemporaryDirectory() as root, patch('kcdw.weathernext3_bigquery.query', return_value=reply) as remote:
            store = BigQueryStore(session=Mock(), cache_dir=root)
            data = store.fetch(wn3.iso(run))
            self.assertEqual(len(data['rows']), 48)
            self.assertEqual(data['rows'][-1]['valid_time'], wn3.iso(run+timedelta(hours=48)))
            self.assertTrue(store.fetch(wn3.iso(run))['provenance']['local_cache_hit'])
            self.assertEqual(remote.call_count, 1)
        reply['rows'].pop(10)
        with tempfile.TemporaryDirectory() as root, patch('kcdw.weathernext3_bigquery.query', return_value=reply):
            with self.assertRaises(ValueError):
                BigQueryStore(session=Mock(), cache_dir=root).fetch(wn3.iso(run))
            self.assertEqual(list(Path(root).glob('*.json')), [])

    def test_interim_cannot_request_lead_49_but_synoptic_can(self):
        for offset, lead, valid in [(1,48,True), (1,49,False), (0,360,True), (0,361,False)]:
            run = wn3.RUN+timedelta(hours=offset)
            args = (DEFAULT_TABLE, wn3.iso(run), [wn3.iso(run+timedelta(hours=lead))], ['temperature_2m_mean'], 1024**3)
            with self.subTest(offset=offset, lead=lead):
                if valid:
                    self.assertTrue(request_body(*args)['dryRun'])
                else:
                    with self.assertRaises(ValueError): request_body(*args)
        with self.assertRaises(ValueError):
            request_body(DEFAULT_TABLE, wn3.iso(wn3.RUN+timedelta(minutes=1)),
                         [wn3.iso(wn3.RUN+timedelta(hours=1))], ['temperature_2m_mean'], 1024**3)

    def test_hourly_discovery_is_opt_in_and_validates_times(self):
        run = wn3.RUN+timedelta(hours=1)
        with patch('kcdw.weathernext3_bigquery.query', return_value={'rows':[{'init_time':wn3.iso(run)}]}) as remote:
            store = BigQueryStore(session=Mock())
            self.assertEqual(store.candidates(wn3.NOW, include_interim=True), [run])
            self.assertNotIn('MOD(', remote.call_args.args[2]['query'])
            with self.assertRaises(ValueError): store.candidates(wn3.NOW)
            for values in [[run,run], [run,run+timedelta(hours=1)], [wn3.NOW+timedelta(hours=1)], [run+timedelta(minutes=1)]]:
                remote.return_value = {'rows':[{'init_time':wn3.iso(v)} for v in values]}
                with self.assertRaises(ValueError): store.candidates(wn3.NOW, include_interim=True)


class HourlyEvidenceTests(unittest.TestCase):
    def test_collection_selects_newest_interim_and_does_not_hide_bad_data(self):
        packet = packet_fixture()
        run = wn3.RUN+timedelta(hours=1)
        store = Mock()
        store.candidates.return_value = [wn3.RUN+timedelta(hours=6),run,wn3.RUN]
        store.fetch.return_value = dict(rows=packet['rows'],provenance=packet['query'])
        result = wn3_hourly.collect(wn3.NOW,store=store)
        self.assertEqual(result['init_time'],wn3.iso(run))
        store.fetch.assert_called_once_with(wn3.iso(run),wn3_hourly.ARRAYS)
        store.fetch.reset_mock()
        store.fetch.side_effect = ValueError('bad newest run')
        with self.assertRaises(ValueError): wn3_hourly.collect(wn3.NOW,store=store)
        self.assertEqual(store.fetch.call_count,1)

    def test_validation_rejects_stale_missing_reversed_or_misbound_data(self):
        packet = packet_fixture()
        wn3_hourly.validate(packet,wn3.NOW)
        mutations = [lambda p:p['rows'].pop(),lambda p:p.update(horizon_hours=360),
                     lambda p:p['rows'][0].update(wind_speed_100m_p10=9),
                     lambda p:p['rows'][0].update(wind_speed_10m_mean=None),
                     lambda p:p['rows'][0].update(init_time=wn3.iso(wn3.RUN)),
                     lambda p:p['query'].update(retrieved_at=wn3.iso(wn3.NOW+timedelta(hours=1)))]
        for mutate in mutations:
            value = copy.deepcopy(packet);mutate(value)
            with self.assertRaises(ValueError): wn3_hourly.validate(value,wn3.NOW)
        with self.assertRaises(ValueError): wn3_hourly.validate(packet,wn3.NOW+timedelta(hours=24))

    def test_summary_preserves_precipitation_interval_and_short_horizon(self):
        packet = packet_fixture()
        start = wn3.NOW+timedelta(hours=2)
        end = start+timedelta(hours=2)
        data = wn3_hourly.summarize(packet,wn3.NOW,start,end)
        self.assertEqual(data['coverage'],'complete')
        self.assertEqual(data['instantaneous']['time'],[wn3.iso(start+timedelta(hours=h)) for h in (0,1,2)])
        self.assertEqual(data['precipitation']['time'],[wn3.iso(start+timedelta(hours=h)) for h in (1,2)])
        self.assertAlmostEqual(data['instantaneous']['fields']['wind_speed_100m']['mean'][0],13.607,places=3)
        last = wn3.RUN+timedelta(hours=49)
        self.assertEqual(wn3_hourly.summarize(packet,wn3.NOW,last-timedelta(hours=1),last+timedelta(hours=1))['coverage'],'partial')
        data = wn3_hourly.summarize(packet,wn3.NOW,last+timedelta(hours=1),last+timedelta(hours=2))
        self.assertEqual(data['coverage'],'none')
        self.assertEqual(data['instantaneous']['time'],[])

    def test_event_outside_horizon_has_no_weather_values_or_favorable_inference(self):
        import test_events
        snapshot = dict(event=test_events.EVENT.as_dict(),wn3_hourly=packet_fixture())
        data = wn3_hourly.event_evidence(snapshot,wn3.NOW)
        self.assertEqual(data['coverage'],'none')
        self.assertNotIn('instantaneous',data)
        html = wn3_hourly.render_event(snapshot,wn3.NOW)
        self.assertIn('does not yet reach',html)
        self.assertNotIn('<table>',html)

    def test_event_uses_expected_flight_and_includes_return_when_covered(self):
        import test_events
        event = test_events.EVENT
        packet = packet_fixture()
        init = datetime.combine(event.day-timedelta(days=2),datetime.min.time(),timezone.utc)+timedelta(hours=17)
        now = init+timedelta(hours=8)
        packet.update(init_time=wn3.iso(init),collected_at=wn3.iso(now))
        packet['query']['retrieved_at'] = wn3.iso(now)
        for lead,row in enumerate(packet['rows'],1):
            row.update(init_time=wn3.iso(init),valid_time=wn3.iso(init+timedelta(hours=lead)))
        timing = dict(slug=event.slug,date=event.date,timezone='America/New_York',
                      appointment_start='08:00',appointment_status='confirmed',flight_start='10:00',
                      flight_status='expected',flight_end='12:00',flight_duration_minutes=120)
        snapshot = dict(event=event.as_dict(),event_timing=timing,wn3_hourly=packet)
        data = wn3_hourly.event_evidence(snapshot,now)
        self.assertEqual(data['coverage'],'complete')
        self.assertEqual(len(data['instantaneous']['time']),3)
        self.assertEqual(len(data['precipitation']['time']),2)
        html = wn3_hourly.render_event(snapshot,now)
        self.assertIn('fully covered',html)
        for clock in ('10:00','11:00','12:00'): self.assertIn(clock,html)
        self.assertIn('100 m wind is not gust',html)

    def test_hourly_source_is_compacted_and_cannot_satisfy_readiness(self):
        import json
        from kcdw.evidence import prepare
        from kcdw.readiness import evidence_readiness
        from kcdw.validation import validate_weather_next3_source,ValidationError
        snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())
        snapshot['collected_at'] = wn3.iso(wn3.NOW)
        snapshot['sources'] = {'weather_next3_hourly':dict(ok=True,fetched_at=wn3.iso(wn3.NOW),data=packet_fixture())}
        validate_weather_next3_source(snapshot)
        data = prepare(snapshot)['sources']['weather_next3_hourly']['data']
        self.assertNotIn('rows',data)
        self.assertIn('instantaneous',data)
        self.assertEqual(evidence_readiness(snapshot),([],[]))
        snapshot['sources']['weather_next3_hourly']['data']['rows'].pop()
        with self.assertRaises(ValidationError): validate_weather_next3_source(snapshot)

    def test_daily_assessment_keeps_only_covered_day_hours_and_marks_later_days_missing(self):
        import json
        from kcdw.typesafe_assessment import day_evidence
        snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())
        snapshot['collected_at'] = wn3.iso(wn3.NOW)
        snapshot['local_date'] = '2026-09-12'
        snapshot['report_dates'] = ['2026-09-12','2026-09-13','2026-09-14','2026-09-15']
        snapshot['sources'] = {'weather_next3_hourly':dict(ok=True,fetched_at=wn3.iso(wn3.NOW),data=packet_fixture())}
        for day,coverage in [('2026-09-12','complete'),('2026-09-13','complete'),('2026-09-14','partial'),('2026-09-15','none')]:
            data = day_evidence(snapshot,day)['sources']['weather_next3_hourly']['data']
            self.assertEqual(data['coverage'],coverage)
            self.assertTrue(all(t.startswith(day) or t.startswith(str(datetime.fromisoformat(day).date()+timedelta(days=1))) for t in data['instantaneous']['time']))
            if coverage=='none': self.assertEqual(data['instantaneous']['time'],[])
