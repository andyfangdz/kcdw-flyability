import json
import tempfile
import unittest
from pathlib import Path
from kcdw.replay import prepare_replay


class ReplayTests(unittest.TestCase):
    def test_ablation_preserves_archive_and_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'archive'
            archive.mkdir()
            raw = Path('tests/fixtures/sample_snapshot.json').read_text()
            (archive / 'snapshot.json').write_text(raw)
            original = json.loads(raw)
            key = next(iter(original['sources']))
            result = prepare_replay(archive, root / 'replay', [key])
            self.assertEqual(result['collected_at'], original['collected_at'])
            self.assertEqual(result['sources'][key]['status'], 'failed')
            self.assertEqual((archive / 'snapshot.json').read_text(), raw)
            self.assertIn('REPLAY MODE', (root / 'replay/prompt.txt').read_text())
            with self.assertRaises(ValueError):
                prepare_replay(archive, root / 'invalid', ['unknown'])
            self.assertFalse((root / 'invalid').exists())
