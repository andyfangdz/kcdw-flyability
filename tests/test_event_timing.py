"""Confirmed appointment and estimated flight time are not a flight duration."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from kcdw import event_renderer
from kcdw.event_narrative_evidence import build_event_evidence
import test_event_comparison as comparison
from test_events import EVENT, NOW

TIMING = {'slug':'commercial-checkride','date':'2026-09-24','timezone':'America/New_York',
          'appointment_start':'08:00','appointment_status':'confirmed',
          'flight_start':'10:00','flight_status':'expected','flight_end':None}

class TimingTests(unittest.TestCase):
    def snapshot(self):
        s=comparison.ComparisonTests().snapshot()
        s['event_timing']=copy.deepcopy(TIMING)
        return s

    def test_header_distinguishes_confirmed_and_expected_timing(self):
        page,_=event_renderer.render(self.snapshot(),NOW)
        header=page.split('<header class="event-header">',1)[1].split('</header>',1)[0]
        self.assertIn('08:00 EDT',header)
        self.assertIn('Confirmed checkride start',header)
        self.assertIn('Around 10:00 EDT',header)
        self.assertIn('Expected flight start',header)
        self.assertNotIn('actual time not confirmed',header)
        self.assertIn('not a confirmed flight duration',header)
        self.assertIn('Forecast context window',page)

    def test_archive_summary_uses_confirmed_timing_not_provisional_duration(self):
        text=event_renderer.summary(self.snapshot())
        self.assertIn('08:00 EDT',text)
        self.assertIn('around 10:00 EDT',text)
        self.assertIn('Forecast context',text)
        self.assertNotIn('Provisional',text)

    def test_narrative_evidence_preserves_timing_and_unknown_end(self):
        e=build_event_evidence(self.snapshot(),NOW)
        t=e['operational_timing']
        self.assertEqual(t['appointment_start_utc'],'2026-09-24T12:00:00Z')
        self.assertEqual(t['flight_start_utc'],'2026-09-24T14:00:00Z')
        self.assertEqual(t['appointment_status'],'confirmed')
        self.assertEqual(t['flight_status'],'expected')
        self.assertIsNone(t['flight_end'])
        self.assertEqual(e['event']['window'],'08-17')

    def test_expected_two_hour_flight_has_expected_end_and_utc_bounds(self):
        s=self.snapshot()
        s['event_timing'].update(flight_end='12:00',flight_duration_minutes=120)
        e=build_event_evidence(s,NOW)
        t=e['operational_timing']
        self.assertEqual(t['flight_duration_minutes'],120)
        self.assertEqual(t['flight_end_utc'],'2026-09-24T16:00:00Z')
        self.assertEqual(t['flight_end_local'],'12:00 EDT')
        self.assertEqual(t['flight_end_status'],'expected')
        self.assertEqual(e['event']['window'],'08-17')
        page,_=event_renderer.render(s,NOW)
        header=page.split('<header class="event-header">',1)[1].split('</header>',1)[0]
        self.assertIn('10:00–12:00 EDT',header)
        self.assertIn('2 hours',header)
        self.assertIn('Expected flight',header)
        self.assertIn('08:00 EDT',header)
        self.assertNotIn('end time is not confirmed',header)
        summary=event_renderer.summary(s)
        self.assertIn('12:00 EDT',summary)
        self.assertNotIn('end unknown',summary)

    def test_duration_must_match_expected_end_and_stay_within_context(self):
        from kcdw.event_timing import timing_evidence
        for minutes,end in [(0,'10:00'),(-120,'08:00'),(True,'12:00'),(120.0,'12:00'),
                            (120,'13:00'),(120,None),(120,'12:60'),(600,'20:00')]:
            with self.subTest(minutes=minutes,end=end):
                s=self.snapshot()
                s['event_timing'].update(flight_end=end,flight_duration_minutes=minutes)
                self.assertIsNone(timing_evidence(s))

    def test_old_archives_do_not_inherit_current_schedule(self):
        from kcdw.event_timing import timing_evidence
        s=self.snapshot();del s['event_timing']
        self.assertIsNone(timing_evidence(s))
        self.assertNotIn('operational_timing',build_event_evidence(s,NOW))

    def test_wrong_event_and_malformed_timing_fail_closed(self):
        from kcdw.event_timing import timing_evidence
        mutations=[('date','2026-09-25'),('slug','wrong'),('flight_start','9:00'),
                   ('flight_start','07:00'),('appointment_status','expected'),
                   ('timezone','UTC'),('flight_end','12:00')]
        for key,value in mutations:
            s=self.snapshot();s['event_timing'][key]=value
            self.assertIsNone(timing_evidence(s),(key,value))

    def test_custom_config_and_binding(self):
        from kcdw.event_timing import load_event_timing
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'events.json';p.write_text(json.dumps({'events':[EVENT.as_dict()], 'timings':{EVENT.slug:TIMING}}))
            self.assertEqual(load_event_timing(EVENT,p),TIMING)
            bad=copy.deepcopy(TIMING);bad['date']='2026-09-25'
            p.write_text(json.dumps({'timings':{EVENT.slug:bad}}))
            self.assertIsNone(load_event_timing(EVENT,p))
