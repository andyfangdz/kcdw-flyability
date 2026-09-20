"""Rolling report wiring: new supplementary sources must not gate aviation."""
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock
from kcdw import renderer, collector, synoptic_context

NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
FIX = Path(__file__).parent / 'fixtures'


class WeeklyWiringTests(unittest.TestCase):
    def test_rolling_page_includes_weekly_chart_fragment_and_navigation(self):
        snapshot = json.loads((FIX / 'sample_snapshot.json').read_text())
        analysis = json.loads((FIX / 'sample_analysis.json').read_text())
        weekly = types.SimpleNamespace()
        weekly.render_weekly = Mock(return_value='<section id="weekly-ensembles">WEEKLY CHARTS</section>')
        weekly.chart_css = Mock(return_value='/* weekly chart CSS */')
        with patch.dict(sys.modules, {'kcdw.weekly_guidance': weekly}):
            html, health = renderer.render(snapshot, analysis, NOW)
        weekly.render_weekly.assert_called_once_with(snapshot, NOW)
        self.assertIn('href="#weekly-ensembles"', html)
        self.assertIn('href="#regional-guidance"', html)
        self.assertIn('id="daily-outlook"', html)
        self.assertIn('WEEKLY CHARTS', html)
        self.assertIn('data-forecast-script', html)
        import re
        import hashlib
        import base64
        from html import unescape
        match = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html)
        assert match is not None
        policy = unescape(match.group(1))
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertIn("'sha256-" + digest + "'", policy)
        self.assertFalse(health['stale'])

    def test_context_spc_is_optional_and_collected_only_when_requested(self):
        spc = types.SimpleNamespace()
        spc.collect_spc = Mock(return_value={'ok': True, 'fetched_at': NOW.isoformat(), 'data': {'fixture': True}, 'error': None})
        spc.render_spc = Mock(return_value='<section><h3>SPC fixture</h3><p>VALID SPC TEXT</p></section>')
        # Existing providers remain untouched and independently available.
        providers = synoptic_context._providers()
        with patch.dict(sys.modules, {'kcdw.spc_guidance': spc}):
            all_providers = synoptic_context._providers(include_spc=True)
            self.assertEqual({p[0] for p in all_providers}, {p[0] for p in providers} | {'spc'})
            context = {'spc': spc.collect_spc(None, NOW)}
            html = synoptic_context.render_context(context, NOW, NOW.replace(day=9), NOW)
            evidence = synoptic_context.context_evidence(context, NOW, NOW.replace(day=9), NOW)
        self.assertIn('data-context-source="spc"', html)
        self.assertIn('VALID SPC TEXT', html)
        self.assertTrue(any(p['source'] == 'spc' and 'VALID SPC TEXT' in p['text'] for p in evidence['products']))

    def test_chart_failure_preserves_core_weekly_report(self):
        snapshot = json.loads((FIX / 'sample_snapshot.json').read_text())
        analysis = json.loads((FIX / 'sample_analysis.json').read_text())
        weekly = types.SimpleNamespace(render_weekly=Mock(side_effect=ValueError('private malformed source')),
                                       chart_css=Mock(return_value=''))
        with patch.dict(sys.modules, {'kcdw.weekly_guidance': weekly}):
            html, health = renderer.render(snapshot, analysis, NOW)
        self.assertIn('Weekly model charts unavailable', html)
        self.assertIn('id="daily-outlook"', html)
        self.assertNotIn('private malformed source', html)
        self.assertFalse(health['stale'])

    def test_evidence_uses_validated_text_not_raw_weekly_arrays(self):
        from kcdw.evidence import prepare
        snapshot = json.loads((FIX / 'sample_snapshot.json').read_text())
        snapshot['weekly_guidance'] = {'raw_arrays': [987654] * 1000}
        weekly = types.SimpleNamespace(render_weekly=Mock(return_value='<section><p>GFS deterministic; WN3 mean/p10–p90</p><svg><text>HIDDEN PLOT</text></svg></section>'))
        with patch.dict(sys.modules, {'kcdw.weekly_guidance': weekly}):
            prepared = prepare(snapshot)
        self.assertNotIn('raw_arrays', json.dumps(prepared))
        self.assertNotIn('HIDDEN PLOT', json.dumps(prepared))
        self.assertIn('GFS deterministic', prepared['weekly_guidance']['text'])
        self.assertIn('raw_arrays', snapshot['weekly_guidance'])

    def test_collector_persists_weekly_and_requests_spc(self):
        weekly = types.SimpleNamespace()
        weekly.collect_weekly = Mock(return_value={'version': 1, 'fixture': True})
        client = Mock()
        client.get.side_effect = RuntimeError('offline')
        client.get_text.side_effect = RuntimeError('offline')
        with patch.dict(sys.modules, {'kcdw.weekly_guidance': weekly}), \
             patch.object(collector, 'collect_weather_next3', side_effect=RuntimeError('offline')), \
             patch.object(collector, 'collect_radar_loop', side_effect=RuntimeError('offline')), \
             patch.object(synoptic_context, 'collect_context', return_value={'spc': {'ok': False}}) as context:
            snapshot = collector.collect(NOW, client)
        weekly.collect_weekly.assert_called_once_with(client, NOW)
        self.assertEqual(snapshot['weekly_guidance'], {'version': 1, 'fixture': True})
        context.assert_called_once_with(client, NOW, include_spc=True)
        self.assertNotIn('weekly_guidance', snapshot['sources'])
        self.assertEqual(snapshot['report_dates'][0], '2026-09-03')


if __name__ == '__main__':
    unittest.main()
