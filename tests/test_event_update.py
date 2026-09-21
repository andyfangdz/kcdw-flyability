"""Exercise collection → narrative → archive → publication once per outcome."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kcdw import event_update
import test_event_comparison as comparison
from test_events import EVENT, NOW
from test_event_timing import TIMING


# Every external collector is isolated here. Individual source contracts live
# beside their collectors; these tests cover orchestration and persistence.
SOURCES = {
    'collect_wn3_100m_wind': 'wn3_100m_wind',
    'collect_wind': 'event_wind',
    'collect_native_wind': 'native_wind',
    'build_wind_trends': 'wind_trends',
    'collect_afds': 'event_afds',
    'collect_ceiling': 'cloud_ceiling',
    'collect_layer_signals': 'cloud_layer_signals',
    'collect_wn2_members': 'weathernext2_members',
    'collect_model_matrix': 'model_matrix',
    'build_trends': 'ensemble_trends',
    'build_forecast_history': 'forecast_history',
    'build_event_changes': 'event_changes',
    'load_run_history': 'ensemble_run_history',
}


class EventUpdateTests(unittest.TestCase):
    def run_update(self, failed=(), narrative_failure=False, maps=None):
        snapshot = comparison.ComparisonTests().snapshot()
        expected = {key: None if name in failed else {'source': key}
                    for name, key in SOURCES.items()}
        # A failed refresh must replace old evidence rather than silently reuse it.
        snapshot.update({key: {'old': True} for key in expected})
        snapshot['event_narrative'] = {'old': True}
        narrative = {'provider': 'claude-code', 'data': {'headline': 'Current evidence'}}
        expected_narrative = None if narrative_failure else narrative

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            var = Path(tmp)
            if maps:
                path = var / 'events' / EVENT.slug / 'coastal-maps.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(maps))
            stack.enter_context(patch.object(event_update, 'upcoming_events', return_value=[EVENT]))
            stack.enter_context(patch.object(event_update, 'load_event_timing', return_value=TIMING))
            collect = stack.enter_context(patch.object(event_update, 'collect_event', return_value=snapshot))
            for name, key in SOURCES.items():
                stack.enter_context(patch.object(
                    event_update, name, return_value=expected[key],
                    side_effect=RuntimeError('private-source-detail') if name in failed else None))
            stack.enter_context(patch.object(event_update, 'Client'))
            stack.enter_context(patch.object(event_update, 'load_json', return_value={}))
            publish = stack.enter_context(patch.object(event_update, 'publish_event'))
            stack.enter_context(patch.object(event_update, 'publish_events_index'))

            def generate(value, work_dir, now):
                self.assertEqual({key: value[key] for key in expected}, expected)
                self.assertEqual(value['event_timing'], TIMING)
                self.assertEqual(value['initialization_provenance_version'], 1)
                self.assertEqual(work_dir.parent, var / 'events' / EVENT.slug / 'narratives')
                self.assertEqual(now, NOW)
                if narrative_failure:
                    raise TimeoutError('private-agent-detail')
                return narrative

            def render(value, now, events_path):
                self.assertEqual(value['event_narrative'], expected_narrative)
                return '<html>current charts</html>', {'generated_at': value['collected_at']}

            generate_mock = stack.enter_context(patch.object(event_update, 'generate_event_narrative', side_effect=generate))
            stack.enter_context(patch.object(event_update, 'render', side_effect=render))
            self.assertEqual(event_update.update(var, var / 'config.json', NOW), 0)
            self.assertTrue(collect.call_args.args[0].direct_native)
            self.assertTrue(collect.call_args.args[0].direct_ensembles)
            generate_mock.assert_called_once()
            archive = (var / 'events' / EVENT.slug / 'current').resolve()
            saved = json.loads((archive / 'snapshot.json').read_text())
            self.assertEqual({key: saved[key] for key in expected}, expected)
            self.assertEqual(saved['event_narrative'], expected_narrative)
            self.assertEqual(saved['event_timing'], TIMING)
            self.assertEqual(saved['initialization_provenance_version'], 1)
            self.assertEqual(saved['collected_at'], snapshot['collected_at'])
            self.assertEqual(saved['coastal_maps'], maps)
            self.assertIn('current charts', (archive / 'index.html').read_text())
            publish.assert_called_once()
            self.assertEqual(publish.call_args.args[1], archive)
            self.assertNotIn('private-', (var / 'events' / 'update.log').read_text())

    def test_current_evidence_reaches_narrative_archive_and_publisher(self):
        self.run_update()

    def test_saved_map_comparison_survives_scheduled_refresh(self):
        from test_coastal_maps import fixture, public_manifest
        self.run_update(maps=public_manifest(fixture()))

    def test_source_failures_preserve_other_evidence_and_publication(self):
        # Alternate failures so every optional source fails once while others
        # succeed. Backfill loading has a separate failure contract.
        optional = [name for name in SOURCES if name != 'load_run_history']
        for failed in (optional[::2], optional[1::2]):
            with self.subTest(failed=failed):
                self.run_update(failed=failed)

    def test_agent_failure_publishes_charts_without_reusing_old_narrative(self):
        self.run_update(narrative_failure=True)
