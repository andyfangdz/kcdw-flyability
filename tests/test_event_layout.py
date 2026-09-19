"""Operational hierarchy, native disclosures, and chart visibility."""
import re
import unittest
from unittest.mock import patch

from kcdw import event_ensemble
from kcdw.event_renderer import render
from test_events import EVENT, NOW, FakeClient, synthetic_wn3


class EventLayoutTests(unittest.TestCase):
    def markup(self):
        with patch.object(event_ensemble, 'collect_weather_next3', return_value=synthetic_wn3()):
            snapshot = event_ensemble.collect_event(FakeClient(), EVENT, NOW)
        return render(snapshot, NOW)[0]

    def test_operational_landmarks_and_chart_priority(self):
        markup = self.markup()
        self.assertIn('<header class="event-header">', markup)
        self.assertIn('aria-label="Briefing sections"', markup)
        for anchor in ('briefing', 'multimodel-comparison', 'wn3-numbers', 'sources-methods'):
            self.assertIn('id="' + anchor + '"', markup)
        self.assertLess(markup.index('id="briefing"'), markup.index('id="multimodel-comparison"'))
        self.assertLess(markup.index('id="multimodel-comparison"'), markup.index('id="wn3-diagnostic"'))
        self.assertEqual(re.findall(r'data-comparison-field="([^"]+)"', markup),
                         ['rain', 'wind', 'cloud', 'pressure', 'temperature', 'gust'])
        self.assertEqual(markup.count('<svg'), 10)
        self.assertNotIn('<h2>No flyability probability</h2>', markup)
        self.assertIn('class="next-check"', markup)
        self.assertIn('class="freshness"', markup)

    def test_technical_prose_is_disclosed_not_a_hero(self):
        markup = self.markup()
        self.assertIn('<details id="wn3-diagnostic"', markup)
        self.assertIn('<details id="sources-methods"', markup)
        self.assertIn('<details id="window-distributions"', markup)
        self.assertNotRegex(markup, r'<details id="(?:wn3-diagnostic|sources-methods|window-distributions)"[^>]*\bopen\b')
        self.assertIn('id="compare-bands" type="checkbox" checked', markup)
        self.assertIn('tabindex="0" role="region"', markup)
        self.assertEqual(markup.count('<script data-forecast-script>'), 1)
        self.assertNotIn('src="http', markup)
        self.assertNotIn('onclick=', markup)


class PageBudgetTests(unittest.TestCase):
    def test_oversized_page_drops_only_the_legacy_fetch_history(self):
        from kcdw.event_renderer import PAGE_BUDGET, fit_page_budget
        block = '<details><summary>Earlier page-fetch history</summary>' + 'x' * 500 + '</details>'
        small = '<!doctype html><main><p>keep</p>' + block + '</main>'
        self.assertIn(block, fit_page_budget(small, block))
        big = '<!doctype html><main><p>keep</p>' + 'y' * PAGE_BUDGET + block + '</main>'
        fitted = fit_page_budget(big, block)
        self.assertNotIn(block, fitted)
        self.assertIn('Earlier page-fetch history omitted', fitted)
        self.assertIn('<p>keep</p>', fitted)
        self.assertLess(PAGE_BUDGET, 800_000)
        self.assertEqual(fit_page_budget(big, ''), fit_page_budget(big, 'absent block'))
