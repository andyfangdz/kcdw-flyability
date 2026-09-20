"""Keep the current forecast when trimming oversized published pages."""
import unittest


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
