"""Cross-source isolation, report wiring and bounded evidence, using synthetic data."""
import copy
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from kcdw.common import UTC, iso_z
from kcdw import synoptic_context as context

NOW = datetime(2026, 9, 13, 15, tzinfo=UTC)
START, END = NOW + timedelta(days=11), NOW + timedelta(days=11, hours=9)


class SynopticContextTests(unittest.TestCase):
    def providers(self):
        good = Mock(return_value={'ok': True, 'fetched_at': iso_z(NOW), 'data': {'track': 'synthetic'}, 'error': None})
        bad = Mock(side_effect=ValueError('sensitive upstream error'))
        renderer = Mock(return_value='<section><h3>Official test product</h3><p>Issued Sep 13; does not cover Sep 24.</p></section>')
        return [('wn3_cyclones', 'WN3 tropical guidance', good, renderer),
                ('nhc', 'NWS / NHC', bad, renderer),
                ('extended', 'CPC / WPC', good, renderer)]

    def test_collection_failure_is_isolated_and_sanitized(self):
        with patch.object(context, '_providers', return_value=self.providers()):
            data = context.collect_context(object(), NOW)
        self.assertTrue(data['wn3_cyclones']['ok'])
        self.assertTrue(data['extended']['ok'])
        self.assertFalse(data['nhc']['ok'])
        self.assertNotIn('sensitive', json.dumps(data))

    def test_render_passes_exact_mission_and_render_time(self):
        providers = self.providers()
        later = NOW + timedelta(days=2)
        source = {'wn3_cyclones': {'ok': True}}
        with patch.object(context, '_providers', return_value=providers):
            markup = context.render_context(source, START, END, later)
        providers[0][3].assert_any_call(source['wn3_cyclones'], START, END, later)
        self.assertIn('id="synoptic-context"', markup)
        self.assertIn('does not cover Sep 24', markup)
        self.assertNotIn('<script', markup)

    def test_renderer_failure_does_not_remove_other_sources(self):
        providers = self.providers()
        providers[0] = (*providers[0][:3], Mock(side_effect=ValueError('secret')))
        with patch.object(context, '_providers', return_value=providers):
            markup = context.render_context({}, START, END, NOW)
        self.assertIn('Unavailable', markup)
        self.assertIn('Official test product', markup)
        self.assertNotIn('secret', markup)

    def test_evidence_is_bounded_plain_text_not_raw_tracks(self):
        providers = self.providers()
        raw = {'wn3_cyclones': {'ok': True, 'data': {'raw_tracks': [42] * 10000}}}
        original = copy.deepcopy(raw)
        with patch.object(context, '_providers', return_value=providers):
            data = context.context_evidence(raw, START, END, NOW)
        encoded = json.dumps(data)
        self.assertIn('does not cover Sep 24', encoded)
        self.assertNotIn('raw_tracks', encoded)
        self.assertNotIn('<section', encoded)
        self.assertLess(len(encoded), 24000)
        self.assertEqual(raw, original)

    def test_evidence_prepare_replaces_raw_context_and_preserves_snapshot(self):
        from kcdw.evidence import prepare
        snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())
        snapshot['synoptic_context'] = {'raw_tracks': [12345] * 1000}
        original = copy.deepcopy(snapshot)
        with patch.object(context, 'context_evidence', return_value={'synthetic_summary': 'valid Sep 19–25'}) as summarize:
            prepared = prepare(snapshot)
        self.assertEqual(prepared['synoptic_context'], {'synthetic_summary': 'valid Sep 19–25'})
        self.assertEqual(snapshot, original)
        self.assertEqual(summarize.call_args.args[-1], datetime.fromisoformat(snapshot['collected_at'].replace('Z', '+00:00')))

    def test_event_collection_and_render_use_booked_window(self):
        from kcdw import event_ensemble
        from kcdw.event_renderer import render
        from test_events import EVENT, NOW as EVENT_NOW, FakeClient, synthetic_wn3
        sentinel = {'nhc': {'ok': False, 'error': 'synthetic'}}
        with patch.object(context, 'collect_context', return_value=sentinel) as collect, patch.object(event_ensemble, 'collect_weather_next3', return_value=synthetic_wn3()):
            snapshot = event_ensemble.collect_event(FakeClient(), EVENT, EVENT_NOW)
        self.assertEqual(snapshot['synoptic_context'], sentinel)
        collect.assert_called_once()
        with patch.object(context, 'render_context', return_value='<section id="test-context">Synthetic context</section>') as render_context:
            markup, _ = render(snapshot, EVENT_NOW)
        self.assertIn('id="test-context"', markup)
        _, start, end, at = render_context.call_args.args
        self.assertEqual((start.hour, end.hour), (EVENT.start_hour, EVENT.end_hour))
        self.assertEqual(start.date(), EVENT.day)
        self.assertEqual(at, EVENT_NOW)

    def test_main_renderer_includes_context(self):
        from kcdw.renderer import render
        snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())
        analysis = json.loads(Path('tests/fixtures/sample_analysis.json').read_text())
        at = datetime.fromisoformat(snapshot['collected_at'].replace('Z', '+00:00'))
        with patch.object(context, 'render_context', return_value='<section id="test-context">Synthetic context</section>') as render_context:
            markup, _ = render(snapshot, analysis, at)
        self.assertIn('id="test-context"', markup)
        self.assertEqual(render_context.call_args.args[-1], at)

    def test_long_nhc_narrative_cannot_erase_active_storm(self):
        from test_nhc_guidance import Client, STORM, NOW as NHC_NOW
        from kcdw.nhc_guidance import collect_nhc
        source = collect_nhc(Client([STORM]), NHC_NOW)
        source['data']['outlook']['narrative'] = 'Atlantic disturbance description. ' * 175
        evidence = context.context_evidence({'nhc': source}, NHC_NOW, NHC_NOW + timedelta(days=7), NHC_NOW)
        encoded = json.dumps(evidence)
        self.assertIn('al012026', encoded)
        self.assertIn('70', encoded)
        self.assertIn('2026-09-18T12:00:00Z', encoded)

    def test_each_extended_product_survives_evidence_compaction(self):
        from test_extended_guidance import ExtendedTests, NOW as EXT_NOW
        from kcdw.extended_guidance import collect_extended
        source = collect_extended(ExtendedTests().client(), EXT_NOW)
        for provider in source['data'].values():
            for product in provider['products']:
                product['excerpt'] = 'Long regional forecast discussion. ' * 100
        evidence = context.context_evidence({'extended': source}, EXT_NOW, EXT_NOW + timedelta(days=7), EXT_NOW)
        encoded = json.dumps(evidence)
        for provider in source['data'].values():
            for product in provider['products']:
                self.assertIn(product['id'], encoded)
        self.assertLess(len(encoded), 24000)

    def test_context_never_satisfies_aviation_readiness(self):
        from kcdw.readiness import evidence_readiness
        snapshot = {'collected_at': iso_z(NOW), 'sources': {}, 'synoptic_context': {k: {'ok': True} for k in ('nhc','wn3_cyclones','extended')}}
        self.assertEqual(evidence_readiness(snapshot), ([], []))
