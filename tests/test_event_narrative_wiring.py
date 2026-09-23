"""Live Codex narrative reaches event rendering without blocking weather data."""
import unittest
from unittest.mock import patch

from kcdw import event_renderer
from test_events import NOW
import test_event_comparison as comparison


class EventNarrativeWiringTests(unittest.TestCase):


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

    def test_narrative_is_included_and_renderer_failure_is_isolated(self):
        snapshot = comparison.ComparisonTests().snapshot()
        fragment = '<section id="event-narrative"><h2>What this means</h2><p>Evidence explanation</p></section>'
        with patch.object(event_renderer, 'render_event_narrative', return_value=fragment, create=True) as narrative:
            markup, _ = event_renderer.render(snapshot, NOW)
        narrative.assert_called_once_with(snapshot, NOW)
        self.assertIn(fragment, markup)
        with patch.object(event_renderer, 'render_event_narrative', side_effect=ValueError('bad output'), create=True):
            markup, _ = event_renderer.render(snapshot, NOW)
        self.assertIn('id="multimodel-comparison"', markup)
        self.assertNotIn('bad output', markup)
