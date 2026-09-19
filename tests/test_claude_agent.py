import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from kcdw import claude_agent as agent


def envelope(**changes):
    value = {'type': 'result', 'is_error': False, 'modelUsage': {agent.MODEL: {}, 'claude-haiku-4-5-20251001': {}},
             'structured_output': {'ok': True}}
    value.update(changes)
    return json.dumps(value)


class ClaudeAgentTests(unittest.TestCase):
    def test_command_pins_model_effort_and_isolation(self):
        command = agent.build_command('{}', tools=agent.RESEARCH_TOOLS, add_dirs=(Path('/radar'),))
        self.assertEqual(command[command.index('--model') + 1], 'claude-fable-5-1')
        self.assertEqual(command[command.index('--effort') + 1], 'high')
        self.assertEqual(command[command.index('--tools') + 1], 'Read,WebSearch,WebFetch')
        self.assertEqual(command[command.index('--allowedTools') + 1], 'Read,WebSearch,WebFetch')
        self.assertEqual(command[command.index('--permission-prompts') + 1], 'none')
        self.assertEqual(command[command.index('--add-dir') + 1], '/radar')
        for flag in ('--print', '--restricted', '--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence'):
            self.assertIn(flag, command)
        for tool in ('Bash', 'Write', 'Edit'):
            self.assertNotIn(tool, ' '.join(command))
        bare = agent.build_command('{}')
        self.assertEqual(bare[bare.index('--tools') + 1], '')
        self.assertNotIn('--allowedTools', bare)

    def test_cli_schema_drops_only_the_dialect_reference(self):
        schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'type': 'object', '$defs': {'a': {'type': 'string'}}}
        self.assertEqual(json.loads(agent.cli_schema(schema)), {'type': 'object', '$defs': {'a': {'type': 'string'}}})

    def test_extract_requires_success_pinned_model_and_structured_output(self):
        self.assertEqual(agent.extract(envelope()), {'ok': True})
        self.assertEqual(agent.extract(json.dumps([{'type': 'system'}, json.loads(envelope())])), {'ok': True})
        for bad in (envelope(is_error=True), envelope(modelUsage={'claude-opus-5': {}}), envelope(structured_output=None),
                    envelope(type='assistant'), '[]'):
            with self.assertRaises(ValueError):
                agent.extract(bad)
        with self.assertRaises(ValueError):
            agent.extract('not json')

    def test_run_uses_an_empty_working_directory_and_lists_images_in_order(self):
        seen = {}

        def runner(command, **kwargs):
            seen.update(command=command, **kwargs)
            seen['listing'] = list(Path(kwargs['cwd']).iterdir())
            return subprocess.CompletedProcess(command, 0, stdout=envelope())

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            frames = [tmp / 'radar' / f'frame-0{i}.png' for i in (1, 2)]
            result = agent.run('PROMPT', '{}', tmp / 'analysis.json', tmp / 'agent.log', tools=('Read',), images=frames, runner=runner)
            self.assertEqual(result, {'ok': True})
            self.assertEqual(json.loads((tmp / 'analysis.json').read_text()), {'ok': True})
            self.assertIn('"structured_output"', (tmp / 'agent.log').read_text())
            self.assertEqual(seen['listing'], [])
            self.assertNotEqual(Path(seen['cwd']).resolve(), tmp.resolve())
            self.assertFalse(Path(seen['cwd']).exists())
            self.assertTrue(seen['input'].startswith('PROMPT'))
            first, second = (seen['input'].index(f'{n}. {frame.resolve()}') for n, frame in enumerate(frames, 1))
            self.assertLess(first, second)
            self.assertEqual(seen['command'][seen['command'].index('--add-dir') + 1], str((tmp / 'radar').resolve()))

    def test_run_raises_on_nonzero_exit_and_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 3, stdout='usage limit')
            with self.assertRaises(subprocess.CalledProcessError):
                agent.run('PROMPT', '{}', tmp / 'analysis.json', tmp / 'agent.log', runner=runner)
            self.assertFalse((tmp / 'analysis.json').exists())
            self.assertIn('usage limit', (tmp / 'agent.log').read_text())


if __name__ == '__main__':
    unittest.main()
