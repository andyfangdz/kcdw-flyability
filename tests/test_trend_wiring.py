"""Trend history is a supplemental, persisted part of the event update."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kcdw import event_update, event_renderer
from test_events import EVENT, NOW
import test_event_comparison as comparison


class TrendWiringTests(unittest.TestCase):
    def test_collected_trends_reach_renderer_archive_and_publisher(self):
        snapshot = comparison.ComparisonTests().snapshot()
        trends = {'version': 1, 'as_of': snapshot['collected_at'], 'models': {}}
        with tempfile.TemporaryDirectory() as tmp:
            var = Path(tmp)
            def rendered(s, now, events_path):
                self.assertEqual(s['ensemble_trends'], trends)
                return '<!doctype html><body>trend fixture</body>', {'generated_at': s['collected_at']}
            with patch.object(event_update, 'upcoming_events', return_value=[EVENT]), \
                 patch.object(event_update, 'collect_event', return_value=snapshot), \
                 patch.object(event_update,'collect_wind',return_value=None), \
                 patch.object(event_update,'collect_native_wind',return_value=None), \
                 patch.object(event_update,'build_wind_trends',return_value=None), \
                 patch.object(event_update, 'collect_afds', return_value=None), \
                 patch.object(event_update, 'collect_ceiling', return_value=None), \
                 patch.object(event_update, 'collect_layer_signals', return_value=None), \
                 patch.object(event_update, 'generate_event_narrative', return_value=None), \
                 patch.object(event_update, 'build_trends', return_value=trends, create=True) as build, \
                 patch.object(event_update, 'render', side_effect=rendered), \
                 patch.object(event_update, 'Client'), \
                 patch.object(event_update, 'load_json', return_value={}), \
                 patch.object(event_update, 'publish_event') as publish, \
                 patch.object(event_update, 'publish_events_index'):
                self.assertEqual(event_update.update(var, var/'fake-config.json', NOW), 0)
            build.assert_called_once_with(snapshot, var/'events'/EVENT.slug/'runs', NOW)
            archive = (var/'events'/EVENT.slug/'current').resolve()
            self.assertEqual(json.loads((archive/'snapshot.json').read_text())['ensemble_trends'], trends)
            self.assertEqual(publish.call_args.args[1], archive)

    def test_failed_history_does_not_block_current_forecast(self):
        snapshot = comparison.ComparisonTests().snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(event_update, 'upcoming_events', return_value=[EVENT]), \
                 patch.object(event_update, 'collect_event', return_value=snapshot), \
                 patch.object(event_update,'collect_wind',return_value=None), \
                 patch.object(event_update,'collect_native_wind',return_value=None), \
                 patch.object(event_update,'build_wind_trends',return_value=None), \
                 patch.object(event_update, 'collect_afds', return_value=None), \
                 patch.object(event_update, 'collect_ceiling', return_value=None), \
                 patch.object(event_update, 'collect_layer_signals', return_value=None), \
                 patch.object(event_update, 'generate_event_narrative', return_value=None), \
                 patch.object(event_update, 'build_trends', side_effect=OSError('private-path-secret'), create=True), \
                 patch.object(event_update, 'render', return_value=('<!doctype html>', {'generated_at': snapshot['collected_at']})):
                self.assertEqual(event_update.update(Path(tmp), None, NOW), 0)
            saved = json.loads((Path(tmp)/'events'/EVENT.slug/'current'/'snapshot.json').read_text())
            self.assertIn('ensemble_trends', saved)
            self.assertIsNone(saved['ensemble_trends'])
            self.assertNotIn('private-path-secret', json.dumps(saved))

    def test_page_integrates_section_and_navigation_with_render_time(self):
        snapshot = comparison.ComparisonTests().snapshot()
        snapshot['ensemble_trends'] = {'fixture': True}
        with patch.object(event_renderer, 'render_trends', return_value='<section id="ensemble-trends">fixture</section>', create=True) as render:
            markup, _ = event_renderer.render(snapshot, NOW)
        render.assert_called_once_with(snapshot['ensemble_trends'], snapshot['event'], NOW)
        self.assertIn('href="#ensemble-trends"', markup)
        self.assertIn('<section id="ensemble-trends">fixture</section>', markup)
