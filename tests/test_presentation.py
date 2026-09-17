"""Presentation cleanup preserves data and scripts, but folds boilerplate."""
import unittest
from kcdw.presentation import compact_notes


class PresentationTests(unittest.TestCase):
    def test_one_disclosure_keeps_sources_and_operational_content(self):
        document = '<main><p>Observed thunderstorms approaching KCDW.</p><p class="chart-note">Missing guidance is not benign weather. Field unavailable: GEPS.</p><p class="brief-limits">No calibrated flyability probability.</p><details class="chart-reading"><summary>Sources</summary><p>Run 2026-09-13T18:00Z · © Google · license</p></details><section class="disclaimer"><h2>Plan here.</h2><p>Brief before flying.</p></section><footer class="site-footer">History</footer></main>'
        result = compact_notes(document)
        front, notes = result.split('<details id="notes-sources"', 1)
        self.assertIn('Observed thunderstorms approaching KCDW.', front)
        self.assertIn('Field unavailable: GEPS.', front)
        self.assertNotIn('No calibrated flyability', front)
        self.assertNotIn('Missing guidance is not benign weather', front)
        self.assertNotIn('Plan here.', front)
        self.assertIn('© Google · license', notes)
        self.assertEqual(result.count('id="notes-sources"'), 1)
        self.assertIn('<summary>Notes &amp; sources</summary>', result)
        self.assertNotIn('id="notes-sources" open', result)
        self.assertEqual(result, compact_notes(result))

    def test_scripts_styles_svg_and_entities_stay_byte_exact(self):
        script = '<script>const note="Missing guidance is not benign weather.";const a=1<2;</script>'
        style = '<style>.x{content:"not a forecast"}</style>'
        svg = '<svg><path d="M1 2L3 4" stroke-width="1.8" vector-effect="non-scaling-stroke"/></svg>'
        document = '<main>'+style+svg+'<p>Wind 12 kt &amp; gusts 18 kt.</p>'+script+'<footer></footer></main>'
        result = compact_notes(document)
        for part in (script, style, svg, 'Wind 12 kt &amp; gusts 18 kt.'):
            self.assertIn(part, result)

    def test_empty_and_unavailable_states_are_not_removed(self):
        result = compact_notes('<main><p>Unavailable: stale source. No favorable inference.</p><p>No North America / western Atlantic track points in this mission window. This is not evidence of no tropical impacts.</p><footer></footer></main>')
        front = result.split('<details id="notes-sources"')[0]
        self.assertIn('Unavailable: stale source.', front)
        self.assertIn('No North America / western Atlantic track points', front)

    def test_nested_executable_and_svg_content_is_not_moved_or_changed(self):
        fragments = [
            '<p>Chart<script>const label="Missing guidance is not benign weather. ";</script></p>',
            '<section class="disclaimer"><p>Note</p><script>run();</script></section>',
            '<svg><foreignObject><p class="brief-limits">Missing guidance is not benign weather. </p></foreignObject></svg>',
            '<p><style>.x{content:"Missing guidance is not benign weather. "}</style></p>',
        ]
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                result = compact_notes('<main>'+fragment+'<footer></footer></main>')
                front, notes = result.split('<details id="notes-sources"', 1)
                self.assertIn(fragment, front)
                self.assertNotIn('<script', notes)
                self.assertNotIn('<svg', notes)
                self.assertNotIn('<style', notes)

    def test_disclosure_anchor_is_a_real_element_not_script_text(self):
        script = '<script>const footer="<footer>";const main="</main>";const id=\'id="notes-sources"\';</script>'
        result = compact_notes('<main>'+script+'<p class="brief-limits">Caveat</p><footer></footer></main>')
        self.assertIn(script, result)
        self.assertIn('</details><footer></footer>', result)
        self.assertIn('<details id="notes-sources"', result)

    def test_controls_stay_visible_even_in_recognized_caveat_containers(self):
        for container in ('<p>{}</p>', '<p class="brief-limits">Missing guidance is not benign weather. {}</p>'):
            control = '<input id="compare-gefs" type="checkbox" checked>'
            fragment = container.format(control)
            result = compact_notes('<main>'+fragment+'<footer></footer></main>')
            front, notes = result.split('<details id="notes-sources"', 1)
            self.assertIn(fragment, front)
            self.assertNotIn(control, notes)

    def test_prose_edits_leave_attributes_unchanged(self):
        opening = '<p title="Missing guidance is not benign weather. ">'
        result = compact_notes('<main>'+opening+'Missing guidance is not benign weather. Field unavailable.</p><footer></footer></main>')
        front = result.split('<details id="notes-sources"', 1)[0]
        self.assertIn(opening+'Field unavailable.</p>', front)


if __name__ == '__main__':
    unittest.main()
