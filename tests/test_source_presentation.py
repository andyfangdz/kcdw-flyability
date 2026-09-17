"""Source labels operate on already validated packets, never infer a run."""
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from kcdw.event_initialization import initialization_evidence, render_initializations

NOW = datetime(2026, 9, 17, 20, tzinfo=timezone.utc)


def direct(provider='ECMWF'):
    return {'fetched_at': '2026-09-17T19:30:00Z', 'metadata': {
        'provenance': 'direct-native', 'model_init_is_response_bound': True,
        'initialization_time': '2026-09-17T12:00:00Z', 'source_provider': provider,
        'sampling': 'Native 6-hour steps; linearly interpolated display.'}}


class SourcePresentationTests(unittest.TestCase):
    def test_native_client_capability_is_explicit(self):
        from kcdw.collector import Client
        self.assertFalse(Client().direct_native)
        self.assertTrue(Client(direct_native=True).direct_native)

    def test_archive_keeps_native_proof_within_compact_storage_budget(self):
        import json
        import tempfile
        from pathlib import Path
        from kcdw.event_update import archive_run
        # Native proof shape has many small nested records; indentation must not
        # consume the bounded archive reader's budget or discard any fields.
        snapshot = {'collected_at': '2026-09-17T20:00:00Z',
                    'proof': [{'run': '2026-09-17T12:00:00Z', 'fields': {'value': 91.2345, 'unit': '%'}} for _ in range(1000)]}
        compact_bytes = len(json.dumps(snapshot, separators=(',', ':'), sort_keys=True).encode())
        with tempfile.TemporaryDirectory() as folder:
            target = archive_run(Path(folder), 'test', 'native-test', snapshot, '<p>test</p>', {}, 'test')
            raw = (target/'snapshot.json').read_bytes()
            self.assertEqual(json.loads(raw), snapshot)
            self.assertLessEqual(len(raw), compact_bytes+1)

    def test_sampling_dictionary_is_lossless_and_keeps_numbers(self):
        from kcdw.event_evidence_compact import compact_sampling
        import copy,json
        text='native cadence and source semantics; '*30
        original={'sources':[{'sampling':text,'rh':102.4},{'nested':{'sampling':text},'rh':None}]}
        packed=compact_sampling(original)
        self.assertLess(len(json.dumps(packed)),len(json.dumps(original)))
        definitions=packed.pop('sampling_definitions')
        def expand(value):
            if isinstance(value,dict):return {k:(definitions[v.removeprefix('@sampling:')] if k=='sampling' and isinstance(v,str) and v.startswith('@sampling:') else expand(v)) for k,v in value.items()}
            if isinstance(value,list):return [expand(v) for v in value]
            return value
        self.assertEqual(expand(packed),original)

    def test_native_snapshot_clock_includes_collection_completion(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from tests.test_events import EVENT
        from kcdw import event_update
        started = '2026-09-17T20:00:00Z'
        source = {'models': {}, 'collected_at': started}
        finished = NOW + timedelta(minutes=6)
        with patch.object(event_update, 'collect_base_event', return_value=source), \
             patch.object(event_update, 'collect_moisture', return_value={}), \
             patch.object(event_update, 'collect_moisture_ensemble', return_value={}), \
             patch.object(event_update, '_collection_clock', return_value=finished, create=True):
            result = event_update.collect_event(SimpleNamespace(direct_native=True), EVENT, NOW)
        self.assertEqual(result['collection_started_at'], started)
        self.assertEqual(result['collected_at'], '2026-09-17T20:06:00Z')
        self.assertEqual(result['direct_native_version'], 1)

    def test_direct_rh_reports_exact_run_without_advertised_metadata(self):
        status = {'models': {'ifs': {'available': True, 'data': direct()},
                             'aifs_single': {'available': True, 'data': direct()}}}
        snapshot = {'collected_at': '2026-09-17T19:30:00Z'}
        with patch('kcdw.event_initialization.validate_moisture', return_value=status):
            rows = initialization_evidence(snapshot, NOW)
            html = render_initializations(snapshot, NOW)
        row = next(r for r in rows if r['model'] == 'ifs')
        self.assertEqual(row['binding'], 'response-bound')
        self.assertEqual(row['init'], '2026-09-17T12:00:00Z')
        self.assertNotIn('likely_init', row)
        self.assertNotIn('available_at', row)
        self.assertIn('ECMWF direct', row['scope'])
        self.assertNotIn('Rolling IFS', html)

    def test_presentation_does_not_rehabilitate_future_collection_run(self):
        data = direct(); data['metadata']['initialization_time'] = '2026-09-17T18:00:00Z'
        data['fetched_at'] = '2026-09-17T17:59:00Z'
        status = {'models': {'ifs': {'available': True, 'data': data}}}
        with patch('kcdw.event_initialization.validate_moisture', return_value=status):
            rows = initialization_evidence({'collected_at': data['fetched_at']}, NOW)
        self.assertEqual(next(r for r in rows if r['model'] == 'ifs')['binding'], 'unavailable')

    def test_source_description_differentiates_direct_fallback_and_legacy(self):
        from kcdw.source_presentation import source_description
        self.assertIn('ECMWF direct', source_description(direct()))
        self.assertIn('2026-09-17T12:00:00Z', source_description(direct()))
        legacy = {'metadata': {}, 'fetched_at': '2026-09-17T19:30:00Z'}
        self.assertIn('Rolling', source_description(legacy))
        self.assertNotIn('fallback', source_description(legacy))
        legacy['metadata']['direct_fallback_reason'] = 'Native source unavailable'
        self.assertIn('Open-Meteo fallback', source_description(legacy))

    def test_invalid_direct_marker_does_not_claim_verified_cycle(self):
        from kcdw.source_presentation import is_direct
        d = direct(); d['metadata']['model_init_is_response_bound'] = 1
        self.assertFalse(is_direct(d))
        d = direct(); d['metadata']['source_provider'] = 'invented'
        self.assertFalse(is_direct(d))
