"""Live Codex narrative reaches event rendering without blocking weather data."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kcdw import event_update, event_renderer
from test_events import EVENT, NOW
import test_event_comparison as comparison


class EventNarrativeWiringTests(unittest.TestCase):
    def test_live_collected_snapshot_and_history_reach_generator_then_archive(self):
        snapshot = comparison.ComparisonTests().snapshot()
        narrative = {'provider': 'codex', 'data': {'headline': 'Live evidence explanation'}}
        with tempfile.TemporaryDirectory() as tmp:
            var = Path(tmp)
            def generated(value, work_dir, now):
                self.assertIs(value, snapshot)
                self.assertIn('ensemble_run_history', value)
                self.assertIn('ensemble_trends', value)
                self.assertEqual(value['event_changes'], {'historical': True})
                self.assertEqual(now, NOW)
                self.assertEqual(work_dir.parent, var/'events'/EVENT.slug/'narratives')
                work_dir.mkdir(parents=True)
                (work_dir/'analysis.json').write_text(json.dumps(narrative))
                return narrative
            def collected(client, event, now):
                self.assertTrue(client.direct_native)
                self.assertTrue(client.direct_ensembles)
                return snapshot
            def rendered(value, now, events_path):
                self.assertEqual(value['event_narrative'], narrative)
                return '<html>narrative</html>', {}
            with patch.object(event_update, 'upcoming_events', return_value=[EVENT]), \
                 patch.object(event_update, 'collect_event', side_effect=collected), \
                 patch.object(event_update,'collect_wind',return_value=None), \
                 patch.object(event_update,'collect_native_wind',return_value=None), \
                 patch.object(event_update,'build_wind_trends',return_value=None), \
                 patch.object(event_update, 'collect_afds', return_value=None), \
                 patch.object(event_update, 'collect_ceiling', return_value=None), \
                 patch.object(event_update, 'collect_layer_signals', return_value=None), \
                 patch.object(event_update, 'generate_event_narrative', side_effect=generated, create=True) as generate, \
                 patch.object(event_update, 'build_event_changes', return_value={'historical': True}, create=True) as changes, \
                 patch.object(event_update, 'render', side_effect=rendered):
                self.assertEqual(event_update.update(var, None, NOW), 0)
            self.assertEqual(generate.call_count, 1)
            changes.assert_called_once_with(snapshot, var/'events'/EVENT.slug/'runs', NOW)
            saved = json.loads((var/'events'/EVENT.slug/'current/snapshot.json').read_text())
            self.assertEqual(saved['event_narrative'], narrative)

    def test_comparison_failure_does_not_block_current_generation(self):
        snapshot = comparison.ComparisonTests().snapshot()
        snapshot['event_changes'] = {'old': 'must not reuse'}
        def generated(value, work_dir, now):
            self.assertIsNone(value['event_changes'])
            return {'marker': 'current narrative'}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(event_update, 'upcoming_events', return_value=[EVENT]), \
             patch.object(event_update, 'collect_event', return_value=snapshot), \
             patch.object(event_update,'collect_wind',return_value=None), \
             patch.object(event_update,'collect_native_wind',return_value=None), \
             patch.object(event_update,'build_wind_trends',return_value=None), \
             patch.object(event_update, 'collect_afds', return_value=None), \
             patch.object(event_update, 'collect_ceiling', return_value=None), \
             patch.object(event_update, 'collect_layer_signals', return_value=None), \
             patch.object(event_update, 'build_event_changes', side_effect=ValueError('private archive path')), \
             patch.object(event_update, 'generate_event_narrative', side_effect=generated) as generate, \
             patch.object(event_update, 'render', return_value=('<html>charts</html>', {})):
            self.assertEqual(event_update.update(Path(tmp), None, NOW), 0)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(snapshot['event_narrative'], {'marker': 'current narrative'})
            self.assertNotIn('private archive path', (Path(tmp)/'events/update.log').read_text())

    def test_generation_failure_keeps_fresh_charts_without_old_narrative(self):
        snapshot = comparison.ComparisonTests().snapshot()
        snapshot['event_narrative'] = {'old': 'do not reuse'}
        with tempfile.TemporaryDirectory() as tmp:
            var = Path(tmp)
            with patch.object(event_update, 'upcoming_events', return_value=[EVENT]), \
                 patch.object(event_update, 'collect_event', return_value=snapshot), \
                 patch.object(event_update,'collect_wind',return_value=None), \
                 patch.object(event_update,'collect_native_wind',return_value=None), \
                 patch.object(event_update,'build_wind_trends',return_value=None), \
                 patch.object(event_update, 'collect_afds', return_value=None), \
                 patch.object(event_update, 'collect_ceiling', return_value=None), \
                 patch.object(event_update, 'collect_layer_signals', return_value=None), \
                 patch.object(event_update, 'generate_event_narrative', side_effect=TimeoutError('private-token'), create=True), \
                 patch.object(event_update, 'render', return_value=('<html>charts</html>', {})):
                self.assertEqual(event_update.update(var, None, NOW), 0)
            saved = json.loads((var/'events'/EVENT.slug/'current/snapshot.json').read_text())
            self.assertIsNone(saved['event_narrative'])
            self.assertEqual(saved['collected_at'], snapshot['collected_at'])
            log = (var/'events/update.log').read_text()
            self.assertIn('narrative=unavailable', log)
            self.assertNotIn('private-token', log)

    def test_default_chart_lines_compact_collinear_hours_without_bridging_gaps(self):
        from datetime import timedelta
        from kcdw.event_renderer import Chart
        from xml.etree import ElementTree
        times = [NOW + timedelta(hours=i) for i in range(9)]
        chart = Chart(times, 0, 10, None)
        values = [1, 2, 3, 4, None, 5, 6, 7, 8]
        chart.line(values, '#000')
        segments = [ElementTree.fromstring(part).attrib['points'].split() for part in chart.parts]
        self.assertEqual([len(segment) for segment in segments], [2, 2])
        expected = [[0, 3], [5, 8]]
        for segment, indices in zip(segments, expected):
            self.assertEqual([tuple(map(float, point.split(','))) for point in segment],
                             [(round(chart.x(i),1),round(chart.y(values[i]),1)) for i in indices])

    def test_narrative_is_prominent_and_renderer_failure_is_isolated(self):
        snapshot = comparison.ComparisonTests().snapshot()
        fragment = '<section id="event-narrative"><h2>What this means</h2><p>Evidence explanation</p></section>'
        with patch.object(event_renderer, 'render_event_narrative', return_value=fragment, create=True) as narrative:
            markup, _ = event_renderer.render(snapshot, NOW)
        narrative.assert_called_once_with(snapshot, NOW)
        self.assertIn(fragment, markup)
        self.assertLess(markup.index(fragment), markup.index('<section id="multimodel-comparison"'))
        with patch.object(event_renderer, 'render_event_narrative', side_effect=ValueError('bad output'), create=True):
            markup, _ = event_renderer.render(snapshot, NOW)
        self.assertIn('id="multimodel-comparison"', markup)
        self.assertNotIn('bad output', markup)
