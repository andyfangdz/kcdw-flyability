import unittest
from unittest.mock import patch
from kcdw import event_update, event_renderer
from kcdw.event_narrative_evidence import build_event_evidence
from test_events import EVENT, NOW
import test_gfs_comparison
from test_event_moisture_view import fixture


class MoistureWiringTests(unittest.TestCase):
    def test_collection_preserves_main_sources_and_adds_both_rh_sources(self):
        original = {'models': {'existing': {'ok': True}}}
        with patch.object(event_update, 'collect_base_event', return_value=original), \
             patch.object(event_update, 'collect_moisture', return_value={'kind':'deterministic'}) as det, \
             patch.object(event_update, 'collect_moisture_ensemble', return_value={'kind':'ensemble'}) as ens:
            result = event_update.collect_event(object(), EVENT, NOW)
        self.assertEqual(result['models'], {'existing': {'ok': True}})
        self.assertEqual(result['event_moisture']['kind'], 'deterministic')
        self.assertEqual(result['event_moisture_ensemble']['kind'], 'ensemble')
        self.assertEqual(det.call_count, 1)
        self.assertEqual(ens.call_count, 1)

    def test_one_failed_collector_does_not_discard_other_sources(self):
        with patch.object(event_update, 'collect_base_event', return_value={'models':{}}), \
             patch.object(event_update, 'collect_moisture', side_effect=RuntimeError('down')), \
             patch.object(event_update, 'collect_moisture_ensemble', return_value={'ok':True}):
            result = event_update.collect_event(object(), EVENT, NOW)
        self.assertIsNone(result['event_moisture'])
        self.assertEqual(result['event_moisture_ensemble'], {'ok':True})

    def test_rendering_and_narrative_use_moisture_sources(self):
        from kcdw import event_moisture_view
        snapshot = test_gfs_comparison.GfsComparisonTests().snapshot()
        with patch.object(event_moisture_view, 'validated', return_value=fixture()):
            text, _ = event_renderer.render(snapshot, NOW)
            packet = build_event_evidence(snapshot, NOW)
        self.assertIn('id="low-level-rh"', text)
        self.assertIn('id="rh-bands" type="checkbox" checked', text)
        from html.parser import HTMLParser
        nodes=[]
        parser=HTMLParser()
        parser.handle_starttag=lambda tag,attrs: nodes.append(dict(attrs)) if dict(attrs).get('data-sync-group')=='forecast' else None
        parser.feed(text)
        self.assertTrue(nodes)
        self.assertEqual(len({(n['data-axis-start'],n['data-axis-end']) for n in nodes}),1)
        source = next(s for s in packet['sources'] if s['id']=='low_level_rh')
        self.assertEqual(source['status'], 'available')
        self.assertIn('surface_RH_percent', str(source['evidence']))
        self.assertEqual(source['url'], 'https://kcdw-flyability.andyfang.workers.dev/events/commercial-checkride#low-level-rh')


if __name__ == '__main__':
    unittest.main()
