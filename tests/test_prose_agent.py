import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kcdw import claude_agent, codex_agent, event_narrative, prose_agent
from tests import test_event_narrative as event_helpers


def quota():
    return json.dumps({'type': 'result', 'is_error': True, 'result': "You're out of usage credits."})


def codex_events(value):
    return '\n'.join(json.dumps(v) for v in [
        {'type': 'thread.started', 'thread_id': 'test'},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(value)}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}},
    ])


class ProseAgentTests(unittest.TestCase):
    def test_only_explicit_failed_quota_envelopes_trigger_fallback(self):
        self.assertTrue(claude_agent.usage_limited(quota()))
        self.assertTrue(claude_agent.usage_limited(json.dumps([json.loads(quota())])))
        for text in ('usage limit', '{invalid',
                     json.dumps({'type': 'result', 'is_error': False, 'result': "You're out of usage credits."}),
                     json.dumps({'type': 'result', 'is_error': True, 'result': 'Authentication failed'})):
            self.assertFalse(claude_agent.usage_limited(text))

    def test_fallback_reuses_codex_for_later_passes_and_retains_inputs(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema = json.dumps({'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                                 'required': ['ok'], 'additionalProperties': False})

            def runner(command, **kwargs):
                seen.append((command, kwargs))
                if '--print' in command:
                    return subprocess.CompletedProcess(command, 1, stdout=quota())
                return subprocess.CompletedProcess(command, 0, stdout=codex_events({'ok': True}))

            session = prose_agent.Session()
            for name in ('draft', 'final'):
                result = session.run('Supplied weather', schema, root / (name + '.json'), root / 'log',
                                     tools=(), images=[root / 'radar.png'], runner=runner)
                self.assertEqual(result, {'ok': True})
            self.assertEqual(len(seen), 3)
            self.assertEqual(session.provenance['provider'], 'codex')
            self.assertIn('prose_fallback', (root / 'log').read_text())
            command, kwargs = seen[-1]
            self.assertIn('--image', command)
            self.assertIn('web_search="disabled"', command)
            self.assertIn('Supplied weather', kwargs['input'])
            self.assertNotEqual(kwargs['cwd'], root)

    def test_general_failures_do_not_trigger_provider_switch(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(claude_agent, 'run', side_effect=subprocess.CalledProcessError(42, ['claude'])), \
             patch.object(codex_agent, 'run') as fallback:
            with self.assertRaises(subprocess.CalledProcessError):
                prose_agent.Session().run('prompt', '{}', Path(tmp) / 'out', Path(tmp) / 'log')
            fallback.assert_not_called()

    def test_codex_configuration_is_isolated_and_research_is_explicit(self):
        command = codex_agent.build_command(Path('/tmp/schema'), Path('/tmp/work'))
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check'):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        for value in ('mcp_servers={}', 'agents.enabled=false', 'project_doc_max_bytes=0', 'web_search="disabled"'):
            self.assertIn(value, command)
        self.assertIn('web_search="live"', codex_agent.build_command(Path('/schema'), Path('/work'), research=True))

    def test_codex_rejects_failed_incomplete_or_unstructured_output(self):
        self.assertEqual(codex_agent.extract(codex_events({'ok': True})), {'ok': True})
        for output in ('{}', codex_events([]), codex_events({'ok': True}) + '\n{"type":"turn.failed"}',
                       codex_events({'ok': True}).splitlines()[1]):
            with self.assertRaises(ValueError):
                codex_agent.extract(output)

    def test_fixed_schema_preserves_choices_and_empty_windows(self):
        schema = {'type': 'object', 'required': ['days'], 'properties': {'days': {'type': 'array', 'items': {'oneOf': [
            {'type': 'object', 'required': ['date', 'windows'], 'properties': {'date': {'const': '2026-09-20'},
             'windows': {'type': 'array', 'maxItems': 0, 'items': False}}}]}}}}
        fixed = codex_agent.output_schema(schema)
        day = fixed['properties']['days']['items']['anyOf'][0]
        self.assertEqual(day['properties']['date']['enum'], ['2026-09-20'])
        self.assertEqual(day['properties']['windows']['maxItems'], 0)
        self.assertEqual(day['properties']['windows']['items'], {'type': 'string'})
        self.assertIn('oneOf', schema['properties']['days']['items'])

    def test_event_records_actual_codex_writer_after_quota_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = {'event': {'name': 'Checkride'}, 'collected_at': event_helpers.NOW.isoformat()}

            def runner(command, **kwargs):
                return subprocess.CompletedProcess(command, 1 if '--print' in command else 0,
                    stdout=quota() if '--print' in command else codex_events(event_helpers.output()))

            with patch.object(event_narrative, 'build_event_evidence', side_effect=event_helpers.evidence):
                result = event_narrative.generate_event_narrative(snapshot, Path(tmp), event_helpers.NOW,
                    runner=runner, clock=lambda: event_helpers.NOW, configure_typesafe=False)
            self.assertEqual(result['provider'], 'codex')
