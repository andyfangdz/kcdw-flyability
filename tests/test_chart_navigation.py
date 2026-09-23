"""Forecast time navigation keeps axes and actual valid-time data aligned."""
import json
import re
import unittest
from html import unescape
from pathlib import Path
from datetime import timedelta
from kcdw.event_renderer import Chart, render
import test_event_comparison as comparison
from test_events import NOW


class ChartNavigationTests(unittest.TestCase):
    def test_pointer_and_scroll_behavior(self):
        import subprocess
        result = subprocess.run(['node', '--test', str(Path(__file__).with_name('test_forecast_navigation.js'))], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fixed_y_axis_and_shared_valid_time_metadata(self):
        markup,_=render(comparison.ComparisonTests().snapshot(),NOW)
        self.assertGreater(markup.count('class="fixed-y-axis"'), 0)
        self.assertEqual(markup.count('class="fixed-y-axis"'),
                         markup.split('<script', 1)[0].count('data-sync-group="forecast"'))
        attrs=re.findall(r'data-axis-start="([^"]+)" data-axis-end="([^"]+)" data-event-center="([^"]+)"',markup)
        self.assertEqual(len(attrs), markup.count('class="fixed-y-axis"'))
        self.assertEqual(len(set(attrs)),1)
        self.assertIn('data-forecast-view="checkride"',markup)
        self.assertIn('data-forecast-view="today"',markup)
        self.assertIn('data-forecast-view="full"',markup)
        self.assertIn('data-forecast-script',markup)
        self.assertIn("script-src 'sha256-",markup)
        self.assertNotIn("'unsafe-eval'",markup)
        self.assertNotIn('onclick=',markup)

    def test_hover_payload_compaction_is_lossless_for_values_and_gaps(self):
        from kcdw.event_renderer import encode_chart_values
        values=[None,0.0,100.0,1.23456789,-2.0,None]
        encoded=encode_chart_values(values)
        self.assertEqual(json.loads(encoded),values)
        self.assertEqual(values,[None,0.0,100.0,1.23456789,-2.0,None])
        self.assertLess(len(encoded),len(json.dumps(values,separators=(',',':'))))
        with self.assertRaises(ValueError):
            encode_chart_values([float('nan')])

    def test_long_null_prefix_and_tail_compress_without_changing_values(self):
        from kcdw.event_renderer import encode_chart_values
        values=[None]*130+[0,12.3456789,None,-4]+[None]*70
        encoded=encode_chart_values(values)
        self.assertLess(len(encoded),150)
        packet=json.loads(encoded)
        restored=[None]*packet['n']
        restored[packet['s']:packet['s']+len(packet['d'])]=packet['d']
        self.assertEqual(restored,values)

    def test_dense_full_precision_values_have_lossless_binary_encoding(self):
        import base64,struct
        from kcdw.event_renderer import encode_chart_values
        values=[i/7.123456789 if i%11 else None for i in range(300)]
        encoded=encode_chart_values(values);packet=json.loads(encoded)
        self.assertEqual(packet['v'],2)
        raw=base64.b64decode(packet['b'])
        import math
        decoded=[None if math.isnan(v) else v for (v,) in struct.iter_unpack('<d',raw)]
        restored=[None]*packet['n'];restored[packet['s']:packet['s']+len(decoded)]=decoded
        self.assertEqual(restored,values)
        self.assertLess(len(encoded),len(json.dumps(values))*.8)

    def test_svg_point_padding_is_removed_without_changing_geometry(self):
        points=[(0,0),(0.123,100),(2,0),(3.123,1.234),(4,0)]
        before=' '.join(f'{x:.1f},{y:.1f}' for x,y in points)
        after=Chart._points(points)
        parse=lambda text:[tuple(float(v) for v in pair.split(',')) for pair in text.split()]
        self.assertEqual(parse(before),parse(after))
        self.assertLess(len(after),len(before))

    def test_compact_point_values_keep_precise_hover_data(self):
        markup,_=render(comparison.ComparisonTests().snapshot(),NOW)
        pressure=markup.split('data-comparison-field="pressure"',1)[1].split('</figure>',1)[0]
        match=re.search(r'<g data-model="wn3"[^>]*data-values="([^"]+)"',pressure)
        self.assertIsNotNone(match)
        assert match is not None
        values=json.loads(unescape(match.group(1)))
        self.assertTrue(any(abs(v-101234.56789/100)<.0001 for v in values if v is not None))
        self.assertNotIn('<circle',pressure)

    def test_isolated_valid_sample_remains_visible_without_bridging_gaps(self):
        import xml.etree.ElementTree as ET
        times=[NOW+timedelta(hours=i) for i in range(5)]
        c=Chart(times,1000,1040,(0,4))
        c.band([None,None,1010,None,None],[None,None,1030,None,None],'#000',.2)
        c.line([None,None,1020,None,None],'#000')
        nodes=ET.fromstring('<svg>'+''.join(c.parts)+'</svg>')
        self.assertEqual(len(nodes.findall('polyline')),0)
        self.assertEqual(len(nodes.findall('polygon')),0)
        markers=nodes.findall('circle')
        self.assertEqual(len(markers),1)
        self.assertAlmostEqual(float(markers[0].attrib['cx']),c.x(2),places=1)
        self.assertAlmostEqual(float(markers[0].attrib['cy']),c.y(1020),places=1)
        bands=nodes.findall('line')
        self.assertEqual(len(bands),1)
        self.assertEqual(bands[0].get('x1'),bands[0].get('x2'))
        self.assertAlmostEqual(float(bands[0].attrib['y1']),c.y(1010),places=1)
        self.assertAlmostEqual(float(bands[0].attrib['y2']),c.y(1030),places=1)

    def test_long_axis_has_full_dates_and_no_y_labels_in_scroller(self):
        times=[NOW+timedelta(hours=i) for i in range(300)]
        c=Chart(times,1000,1040,(276,285))
        c.line([1010]*300,'#000')
        text=c.render('Pressure','hPa','note',[],[1000,1020,1040])
        self.assertIn('2026-09-24',text)
        self.assertIn('class="fixed-y-axis"',text.split('data-sync-group=',1)[0])
        self.assertIn('class="forecast-x-axis"',text)
        self.assertIn('1020',text.split('data-sync-group=',1)[0])
        self.assertNotIn('text-anchor="end"',text.split('data-sync-group=',1)[1])


if __name__=='__main__':unittest.main()
