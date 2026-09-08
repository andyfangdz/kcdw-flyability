import json
import tempfile
import unittest
from pathlib import Path
from kcdw.cloud_publish import bundle, Client
from kcdw.renderer import render


class CloudPublisherTests(unittest.TestCase):
    def test_bundle_requires_validated_matching_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())
            analysis = json.loads(Path('tests/fixtures/sample_analysis.json').read_text())
            html, health = render(snapshot, analysis)
            (root/'public').mkdir()
            for name, data in [('snapshot.json',snapshot),('analysis.json',analysis),('changes.json',{}),('public/health.json',health),('manifest.json',{'status':'validated','run_id':'20260903T120000Z.test'})]:
                (root/name).write_text(json.dumps(data))
            (root/'public/index.html').write_text(html)
            result = bundle(root)
            self.assertEqual(result['assessed_at'], snapshot['collected_at'])
            self.assertNotIn('snapshot', result)
            self.assertNotIn('prompt', result)
            health['generated_at']='2026-01-01T00:00:00Z'
            (root/'public/health.json').write_text(json.dumps(health))
            with self.assertRaises(ValueError):
                bundle(root)

    def test_credentials_require_private_file_and_https(self):
        with tempfile.TemporaryDirectory() as directory:
            token=Path(directory)/'token';token.write_text('test');token.chmod(0o644)
            with self.assertRaises(ValueError):
                Client({'url':'https://example.com','token_file':str(token)})
            token.chmod(0o600)
            with self.assertRaises(ValueError):
                Client({'url':'http://example.com','token_file':str(token)})
            Client({'url':'https://example.com','token_file':str(token)})
