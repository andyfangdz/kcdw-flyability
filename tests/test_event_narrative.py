"""Fail-closed narrative integration, with no live agent or authentication."""
import json
import stat
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kcdw import event_narrative as narrative

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def evidence(snapshot, now):
    return {'version': 1, 'event': snapshot['event'], 'collected_at': snapshot['collected_at'],
            'assessed_at': now.isoformat(), 'limitations': [], 'sources': [
                {'id': 'wn3_point', 'label': 'WN3 <source>', 'url': snapshot.get('url', 'https://example.org/weather?a=1&b=2'),
                 'status': 'unavailable' if now > NOW + timedelta(hours=2) else 'available',
                 'evidence': {'rain': snapshot.get('rain', 0.2)}}]}


def output():
    return {'headline': 'Rain <script>alert(1)</script>', 'lead': 'A planning scenario, not a ceiling forecast.',
            'sections': [{'heading': title, 'body': 'The supplied evidence leaves the ceiling unresolved.',
                          'source_ids': ['wn3_point']} for title in ['Pattern', 'Checkride', 'Uncertainty']],
            'next_check': {'text': 'Watch the next model cycle.', 'source_ids': ['wn3_point']}}


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'collected_at': NOW.isoformat(), 'event': {'name': 'Checkride'}}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.builder = patch.object(narrative, 'build_event_evidence', side_effect=evidence)
        self.builder.start()
        self.addCleanup(self.builder.stop)

    def runner(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        envelope = {'type': 'result', 'is_error': False, 'modelUsage': {'claude-fable-5-1': {}}, 'structured_output': output()}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(envelope))

    def generate(self):
        result = narrative.generate_event_narrative(self.snapshot, self.work, NOW, runner=self.runner, clock=lambda: NOW)
        self.snapshot['event_narrative'] = result
        return result

    def test_schema_binding_escaped_text_and_catalog_links(self):
        result = self.generate()
        html = narrative.render_event_narrative(self.snapshot, NOW + timedelta(minutes=5))
        self.assertEqual((result['provider'], result['model']), ('claude-code', 'claude-fable-5-1'))
        self.assertIn('Claude · Generated', html)
        self.assertIn('What this means for your checkride', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('WN3 &lt;source&gt;', html)
        self.assertIn('https://example.org/weather?a=1&amp;b=2', html)
        self.assertEqual(self.kwargs['timeout'], 420)
        self.assertEqual(self.command[self.command.index('--tools') + 1], '')
        self.assertEqual(self.command[self.command.index('--effort') + 1], 'high')
        for flag in ('--restricted', '--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence'):
            self.assertIn(flag, self.command)
        self.assertNotIn('--allowedTools', self.command)
        self.assertEqual(self.kwargs['input'], (self.work / 'prompt.txt').read_text())
        # The structured-output call repeatedly emitted bare prose after "next_check": without this explicit shape.
        self.assertIn('"next_check": {"text": string, "source_ids": [string, ...]}}', self.kwargs['input'])
        self.assertIn('next_check is an OBJECT', self.kwargs['input'])
        for name in ['evidence.json', 'prompt.txt', 'analysis.json', 'codex.log']:
            self.assertEqual(stat.S_IMODE((self.work / name).stat().st_mode), 0o600)

    def test_generated_schema_allows_only_current_available_citations(self):
        def limited(snapshot,now):
            packet=evidence(snapshot,now)
            packet['sources'].append({'id':'wind_surface','label':'Omitted','url':None,'status':'unavailable','evidence':{'reason':'budget'}})
            return packet
        with patch.object(narrative,'build_event_evidence',side_effect=limited):
            self.generate()
        schema=json.loads(self.command[self.command.index('--json-schema')+1])
        refs=[schema['properties']['sections']['items']['properties']['source_ids'],schema['properties']['next_check']['properties']['source_ids']]
        for field in refs:self.assertEqual(field['items']['enum'],['wn3_point'])
        self.assertEqual(stat.S_IMODE((self.work/'schema.json').stat().st_mode),0o600)

    def test_stale_mismatched_and_future_suppressed(self):
        self.generate()
        for now in [NOW + timedelta(hours=3), NOW + timedelta(hours=9), NOW - timedelta(minutes=10)]:
            self.assertIn('unavailable', narrative.render_event_narrative(self.snapshot, now))
        for key, value in [('collected_at', (NOW + timedelta(minutes=1)).isoformat()), ('rain', 99)]:
            snapshot = deepcopy(self.snapshot)
            snapshot[key] = value
            self.assertIn('unavailable', narrative.render_event_narrative(snapshot, NOW))

    def test_schema_unknown_sources_unsafe_urls_fail_closed(self):
        self.generate()
        for mutate in [lambda d: d['sections'].pop(),
                       lambda d: d['sections'][0]['source_ids'].append('evil\" onclick=\"x'),
                       lambda d: d['sections'][0]['source_ids'].append('wn3_point'),
                       lambda d: d.update(url='https://evil.test'),
                       lambda d: d.update(lead=3)]:
            snapshot = deepcopy(self.snapshot)
            mutate(snapshot['event_narrative']['data'])
            self.assertIn('unavailable', narrative.render_event_narrative(snapshot, NOW))
        self.snapshot['url'] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            self.generate()

    def test_real_evidence_binding_has_no_rerender_clock_drift(self):
        from test_ensemble_trends import NOW as actual_now, snapshot
        self.builder.stop()
        self.snapshot = snapshot()
        result = narrative.generate_event_narrative(self.snapshot, self.work, actual_now, runner=self.runner, clock=lambda: actual_now)
        self.snapshot['event_narrative'] = result
        rendered = narrative.render_event_narrative(self.snapshot, actual_now + timedelta(minutes=5))
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('https://developers.google.com/weathernext/guides/bigquery', rendered)
        self.assertNotIn('unavailable', rendered)

    def test_generation_records_completion_time_not_collection_time(self):
        completed_at = NOW + timedelta(seconds=45)
        result = narrative.generate_event_narrative(self.snapshot, self.work, NOW,
                    runner=self.runner, clock=lambda: completed_at)
        self.assertEqual(result['generated_at'], completed_at.isoformat())
        self.snapshot['event_narrative'] = result
        self.assertNotIn('unavailable', narrative.render_event_narrative(self.snapshot, completed_at))

    def test_failure_does_not_reuse_old_analysis(self):
        self.generate()
        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 240)
        with self.assertRaises(subprocess.TimeoutExpired):
            narrative.generate_event_narrative(self.snapshot, self.work, NOW, runner=fail)
        self.assertEqual((self.work / 'analysis.json').read_text(), '')
        self.assertIn('unavailable', narrative.render_event_narrative({}, NOW))


if __name__ == '__main__':
    unittest.main()
