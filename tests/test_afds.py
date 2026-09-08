import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from kcdw.collector import AFD_OFFICES, collect_afd, collect_afds
from kcdw.renderer import source_rows

NOW = datetime(2026, 9, 8, 13, tzinfo=timezone.utc)


class AFDTests(unittest.TestCase):
    def test_selects_latest_nonfuture_and_retains_relevant_sections(self):
        client = Mock()
        client.get.side_effect = [
            {'@graph': [{'id': 'old', 'issuanceTime': '2026-09-08T10:00:00Z'},
                        {'id': 'future', 'issuanceTime': '2026-09-08T14:00:00Z'},
                        {'id': 'latest', 'issuanceTime': '2026-09-08T12:00:00Z'}]},
            {'productText': 'AFDPHI\n.WHAT HAS CHANGED...\nUpdated timing\n.DISCUSSION...\nReasoning\n&&\n.SYNOPSIS...\nOverview\n&&\n.AVIATION...\nFlight conditions\n&&\n.MARINE...\nOmitted\n&&'}]
        data = collect_afd(client, NOW, 'PHI')
        self.assertEqual(data['id'], 'latest')
        self.assertEqual(data['age_seconds'], 3600)
        self.assertEqual(data['office'], 'PHI')
        self.assertIn('Flight conditions', data['excerpt'])
        self.assertIn('Updated timing', data['excerpt'])
        self.assertIn('Reasoning', data['excerpt'])
        self.assertIn('Omitted', data['excerpt'])
        self.assertFalse(data['truncated'])
        self.assertEqual(client.get.call_args.args[0], data['source_url'])

    def test_office_outage_is_isolated_and_labels_show_issue_times(self):
        def get(url):
            office = url.rsplit('/', 1)[-1]
            if office == 'BGM':
                raise RuntimeError('offline')
            if '/locations/' in url:
                return {'@graph': [{'id': office.lower(), 'issuanceTime': '2026-09-08T12:00:00Z'}]}
            return {'productText': f'AFD{office.upper()}\n.AVIATION...\nVFR\n&&'}
        client = Mock()
        client.get.side_effect = get
        sources = collect_afds(client, NOW)
        self.assertEqual(len(sources), len(AFD_OFFICES))
        self.assertFalse(sources['bgm_afd']['ok'])
        self.assertTrue(all(source['ok'] for key, source in sources.items() if key != 'bgm_afd'))
        html = source_rows({'sources': sources})
        self.assertIn('Mount Holly (PHI)', html)
        self.assertIn('Issued 2026-09-08T12:00:00Z', html)

    def test_empty_and_wrong_office_products_rejected(self):
        for text in ('', 'AFDOKX\n.AVIATION...\nVFR'):
            client = Mock()
            client.get.side_effect = [{'@graph': [{'id': 'phi', 'issuanceTime': '2026-09-08T12:00:00Z'}]}, {'productText': text}]
            with self.assertRaises(ValueError):
                collect_afd(client, NOW, 'PHI')
