"""AFDs supplement the event narrative without changing flight readiness."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from kcdw import event_renderer, event_update
from kcdw.event_narrative_evidence import build_event_evidence
import test_event_comparison as comparison
from test_events import EVENT, NOW


def packets() -> dict:
    return {office:{'office':office,'name':name,'issued_at':NOW.isoformat(),
        'fetched_at':NOW.isoformat(),
        'product_url':'https://api.weather.gov/products/12345678-1234-1234-1234-123456789012',
        'discussion_excerpt':'A front may stall Monday through Wednesday. Position is uncertain.',
        'aviation_excerpt':'Monday: MVFR possible in showers.',
        'discussion_truncated':True,'aviation_truncated':True,
        'aviation_heading':'AVIATION /THROUGH MONDAY/'}
        for office,name in [('OKX','New York/Upton'),('PHI','Mount Holly'),('ALY','Albany')]}


class AfdWiringTests(unittest.TestCase):
    def snapshot(self):
        s=comparison.ComparisonTests().snapshot()
        s['event_afds']={'collector_marker':True}
        return s

    def test_three_independently_citable_offices_and_immutable_weather(self):
        s=self.snapshot();original=copy.deepcopy(s)
        with patch('kcdw.event_narrative_evidence.validated_afds',return_value=packets(),create=True):
            e=build_event_evidence(s,NOW)
        sources={x['id']:x for x in e['sources']}
        for office in ('okx','phi','aly'):
            self.assertEqual(sources['afd_'+office]['status'],'available')
            self.assertEqual(sources['afd_'+office]['url'],packets()[office.upper()]['product_url'])
            self.assertIn('Monday through Wednesday',sources['afd_'+office]['evidence']['discussion_excerpt'])
        self.assertEqual(s,original)
        self.assertLessEqual(len(json.dumps(e).encode()),60000)

    def test_budget_preserves_afds_and_comparison_before_long_background_text(self):
        s=self.snapshot()
        comparison={'historical':True,'bounded_fixture':'x'*12000}
        official={'products':[{'status':'current','text':'Background. '*63} for _ in range(3)]}
        with patch('kcdw.event_narrative_evidence.validated_afds',return_value=packets()), \
             patch('kcdw.event_narrative_evidence.validated_event_changes',return_value=comparison), \
             patch('kcdw.event_narrative_evidence._official',return_value=copy.deepcopy(official)), \
             patch('kcdw.event_narrative_evidence.MAX_BYTES',1000000):
            complete=build_event_evidence(s,NOW)
        for source in complete['sources']:
            for key in ('event_samples','rain_intervals'):
                if key in source['evidence']:
                    del source['evidence'][key]
                    source['evidence']['hourly_detail_omitted']='Window totals and fixed-time summaries retained; hourly detail omitted for size.'
            for product in source['evidence'].get('products',[]):
                product['text']=product['text'][:700]
                product['truncated']=True
        cap=len(json.dumps(complete).encode())-800
        with patch('kcdw.event_narrative_evidence.validated_afds',return_value=packets()), \
             patch('kcdw.event_narrative_evidence.validated_event_changes',return_value=comparison), \
             patch('kcdw.event_narrative_evidence._official',side_effect=lambda *_:copy.deepcopy(official)), \
             patch('kcdw.event_narrative_evidence.MAX_BYTES',cap):
            result=build_event_evidence(s,NOW)
        sources={x['id']:x for x in result['sources']}
        for key in ('afd_okx','afd_phi','afd_aly','snapshot_changes'):
            self.assertEqual(sources[key]['status'],'available',key)
        self.assertLessEqual(len(json.dumps(result).encode()),cap)

    def test_archives_without_afds_do_not_inherit_live_readings(self):
        s=self.snapshot();del s['event_afds']
        with patch('kcdw.event_narrative_evidence.validated_afds',return_value=packets(),create=True) as get:
            e=build_event_evidence(s,NOW)
        self.assertFalse(any(x['id'].startswith('afd_') for x in e['sources']))
        get.assert_not_called()

    def test_one_missing_office_leaves_others_and_weather_available(self):
        p=packets();p['PHI']=None
        with patch('kcdw.event_narrative_evidence.validated_afds',return_value=p,create=True):
            sources={x['id']:x for x in build_event_evidence(self.snapshot(),NOW)['sources']}
        self.assertEqual(sources['afd_phi']['status'],'unavailable')
        self.assertEqual(sources['afd_okx']['status'],'available')
        self.assertEqual(sources['gefs']['status'],'available')

    def test_page_escapes_discussion_and_preserves_office_times(self):
        from kcdw.event_afd_view import render_afds
        p=packets();p['OKX']['discussion_excerpt']='Front <script>alert(1)</script> remains nearby.'
        with patch('kcdw.event_afd_view.validated_afds',return_value=p):
            page=render_afds(self.snapshot(),NOW)
        self.assertIn('id="forecaster-discussion"',page)
        self.assertIn('New York/Upton',page)
        self.assertIn('EDT',page)
        self.assertIn('THROUGH MONDAY',page)
        self.assertNotIn('<script>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('Thursday will',page)

    def test_display_excerpt_retains_late_period_instead_of_near_term_prefix(self):
        from kcdw.event_afd_view import render_afds
        p=packets()
        p['OKX']['discussion_excerpt']=('.DISCUSSION...\n\n.KEY MESSAGE 1...\n\n'+
            'Friday is dry. '*90+'\n\n.KEY MESSAGE 3...\n\n'+
            'The front may stall Monday through Wednesday. Its position is uncertain.')
        with patch('kcdw.event_afd_view.validated_afds',return_value=p):
            page=render_afds(self.snapshot(),NOW)
        local=page.split('OKX ·',1)[1].split('</article>',1)[0]
        self.assertIn('The front may stall Monday through Wednesday.',local)
        self.assertIn('KEY MESSAGE 3',local)

    def test_page_failure_is_explicit_and_does_not_change_readiness(self):
        from kcdw.event_afd_view import render_afds
        with patch('kcdw.event_afd_view.validated_afds',return_value={}):
            page=render_afds(self.snapshot(),NOW)
        self.assertIn('unavailable',page.lower())
        with patch('kcdw.event_afd_view.validated_afds',return_value=packets()):
            new_page,new_health=event_renderer.render(self.snapshot(),NOW)
        original=comparison.ComparisonTests().snapshot()
        _,old_health=event_renderer.render(original,NOW)
        self.assertEqual(new_health,old_health)
        self.assertIn('href="#forecaster-discussion"',new_page)

    def test_collector_runs_before_narration_and_failure_is_optional(self):
        for fail in (False,True):
            s=self.snapshot();s.pop('event_afds')
            def narrative(snapshot,*_):
                self.assertEqual(snapshot['event_afds'],None if fail else {'marker':'new'})
                return None
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(event_update,'upcoming_events',return_value=[EVENT]), \
                 patch.object(event_update,'collect_event',return_value=s), \
                 patch.object(event_update,'collect_wind',return_value=None), \
                 patch.object(event_update,'collect_native_wind',return_value=None), \
                 patch.object(event_update,'build_wind_trends',return_value=None), \
                 patch.object(event_update,'collect_afds',return_value={'marker':'new'},side_effect=RuntimeError('down') if fail else None,create=True), \
                 patch.object(event_update,'collect_ceiling',return_value=None), \
                 patch.object(event_update,'collect_layer_signals',return_value=None), \
                 patch.object(event_update,'generate_event_narrative',side_effect=narrative), \
                 patch.object(event_update,'render',return_value=('<html>ok</html>',{})):
                self.assertEqual(event_update.update(Path(tmp),None,NOW),0)
                archived=json.loads((Path(tmp)/'events'/EVENT.slug/'current/snapshot.json').read_text())
                self.assertIn('event_afds',archived)
