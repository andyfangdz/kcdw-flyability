"""Synthetic AFD contracts; fixtures are not actual weather evidence."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from kcdw.event_afd import collect_afds, validate_afds, afd_evidence

UTC = timezone.utc
NOW = datetime(2026, 9, 17, 3, tzinfo=UTC)
IDS = {office: f'00000000-0000-4000-8000-00000000000{i}'
       for i, office in enumerate(('OKX', 'PHI', 'ALY'))}


def stamp(at):
    return at.isoformat().replace('+00:00', 'Z')


def snapshot():
    return {'collected_at': stamp(NOW), 'event': {
        'slug': 'custom-flight', 'date': '2026-09-24', 'window': '08-16',
        'title': 'Custom flight', 'nav_label': 'Flight', 'description': 'Planning'}}


def text(office):
    return (f'000\nFXUS61 K{office} 170100\nAFD{office}\n\n'
            'Area Forecast Discussion\nNational Weather Service\n\n'
            '.DISCUSSION...\n.KEY MESSAGE 1...\n\nTonight is dry.\n\n'
            '.KEY MESSAGE 2...\n\nNext week remains uncertain. The front may stall.\n\n&&\n\n'
            '.AVIATION /00Z THURSDAY THROUGH MONDAY/...\nVFR tonight.\n\n'
            '.OUTLOOK FOR FRIDAY THROUGH MONDAY...\n\nMonday: Showers possible.\n\n&&\n\n'
            '.MARINE...\nWaves remain low.\n&&\n$$')


def product(office, issued=None):
    return {'id': IDS[office], '@id': 'https://evil.invalid/do-not-fetch',
            'productCode': 'AFD', 'issuingOffice': 'K' + office,
            'issuanceTime': stamp(issued or (NOW - timedelta(hours=2))),
            'productText': text(office)}


class FakeClient:
    def __init__(self, listing=None, detail=None, fail=None):
        self.urls = []
        self.listing, self.detail, self.fail = listing, detail, fail

    def get(self, url):
        self.urls.append(url)
        if self.fail and self.fail in url:
            raise RuntimeError('secret=DO_NOT_LEAK https://evil.invalid/token')
        if '/locations/' in url:
            office = url.rsplit('/', 1)[-1]
            entries = [{k: v for k, v in product(office).items() if k != 'productText'}]
            if self.listing:
                self.listing(office, entries)
            return {'@graph': entries}
        office = next(o for o, ident in IDS.items() if url.endswith(ident))
        result = product(office)
        if self.detail:
            self.detail(office, result)
        return result


class AFDTests(unittest.TestCase):
    def good(self, client=None):
        snap = snapshot()
        env = collect_afds(client or FakeClient(), snap, NOW)
        self.assertTrue(all(validate_afds(env, snap, NOW)['offices'].values()))
        return snap, env

    def test_pinned_six_requests_and_bound_detached_envelope(self):
        client = FakeClient()
        snap, env = self.good(client)
        self.assertEqual(len(client.urls), 6)
        self.assertTrue(all(u.startswith('https://api.weather.gov/products/') for u in client.urls))
        self.assertNotIn('evil', json.dumps(env))
        self.assertEqual(env['version'], 1)
        self.assertEqual(env['event'], {k: snap['event'][k] for k in ('slug', 'date', 'window')})
        snap['event']['date'] = '2026-10-12'
        self.assertEqual(env['event']['date'], '2026-09-24')

    def test_nested_heading_and_aviation_qualifier(self):
        snap, env = self.good()
        snap['event_afds'] = env
        item = afd_evidence(snap, NOW)['OKX']
        self.assertIn('Next week remains uncertain.', item['discussion_excerpt'])
        self.assertIn('.KEY MESSAGE 2...', item['discussion_excerpt'])
        self.assertNotIn('Waves', item['discussion_excerpt'])
        self.assertIn('THROUGH MONDAY', item['aviation_heading'])
        self.assertIn('Monday: Showers possible.', item['aviation_excerpt'])
        self.assertFalse(item['discussion_truncated'])
        self.assertNotIn('valid_through', item)

    def test_per_office_fetch_failure_safe_error_and_no_carry_forward(self):
        snap, previous = self.good()
        snap['event_afds'] = previous
        env = collect_afds(FakeClient(fail='locations/PHI'), snap, NOW)
        self.assertIsNone(env['offices']['PHI'])
        self.assertTrue(env['offices']['OKX'])
        self.assertTrue(env['offices']['ALY'])
        self.assertNotIn('DO_NOT_LEAK', json.dumps(env))

    def test_office_identity_code_and_header_all_required(self):
        for key, value in [('issuingOffice', 'KPHI'), ('productCode', 'TAF'),
                           ('productText', text('PHI'))]:
            def mutate(office, raw):
                if office == 'OKX':
                    raw[key] = value
            with self.subTest(key=key):
                env = collect_afds(FakeClient(detail=mutate), snapshot(), NOW)
                self.assertIsNone(env['offices']['OKX'])
                self.assertTrue(env['offices']['PHI'])

    def test_list_detail_metadata_binding(self):
        for field, bad in [('id', IDS['PHI']), ('issuanceTime', stamp(NOW - timedelta(hours=1)))]:
            def mutate(office, raw):
                if office == 'OKX':
                    raw[field] = bad
            env = collect_afds(FakeClient(detail=mutate), snapshot(), NOW)
            self.assertIsNone(env['offices']['OKX'])

    def test_malformed_uuid_never_repaired_or_requested(self):
        for invalid in ['../../private', 'https://evil.invalid/a', IDS['OKX'] + '?secret=x',
                        '{' + IDS['OKX'] + '}', 'ABCDEFAB-0000-4000-8000-000000000000']:
            def mutate(office, entries):
                if office == 'OKX':
                    entries[0]['id'] = invalid
            client = FakeClient(listing=mutate)
            env = collect_afds(client, snapshot(), NOW)
            self.assertIsNone(env['offices']['OKX'])
            self.assertFalse(any(invalid in url for url in client.urls))

    def test_latest_valid_listing_selected_not_first(self):
        def mutate(office, entries):
            old = dict(entries[0], id='11111111-0000-4000-8000-000000000000',
                       issuanceTime=stamp(NOW - timedelta(hours=3)))
            future = dict(entries[0], id='../../bad', issuanceTime=stamp(NOW))
            entries[:] = [old, future, entries[0]]
        self.good(FakeClient(listing=mutate))

    def test_invalid_list_metadata_skipped_without_details(self):
        for field, bad in [('issuingOffice', 'KBOX'), ('productCode', 'TAF'),
                           ('issuanceTime', '2026-09-17T01:00:00')]:
            def mutate(office, entries):
                if office == 'OKX':
                    entries[0][field] = bad
            client = FakeClient(listing=mutate)
            env = collect_afds(client, snapshot(), NOW)
            self.assertIsNone(env['offices']['OKX'])
            self.assertEqual(len(client.urls), 5)

    def test_product_fetch_ttl_and_future_clock_limits(self):
        snap, env = self.good()
        self.assertTrue(validate_afds(env, snap, NOW + timedelta(hours=12))['offices']['OKX'])
        self.assertIsNone(validate_afds(env, snap, NOW + timedelta(hours=12, seconds=1))['offices']['OKX'])
        for age, accepted in [(18, True), (18.001, False), (-5 / 60, True), (-6 / 60, False)]:
            def mutate(office, raw):
                raw['issuanceTime'] = stamp(NOW - timedelta(hours=age))
            def listing(office, entries):
                mutate(office, entries[0])
            current = collect_afds(FakeClient(listing=listing, detail=mutate), snapshot(), NOW)
            self.assertEqual(bool(current['offices']['OKX']), accepted)
        current = copy.deepcopy(env)
        current['offices']['OKX']['fetched_at'] = stamp(NOW + timedelta(minutes=6))
        self.assertIsNone(validate_afds(current, snap, NOW)['offices']['OKX'])

    def test_revalidation_original_clocks_and_tampered_excerpt(self):
        snap, env = self.good()
        item = env['offices']['OKX']
        item['discussion_excerpt'] = 'Invented exact event forecast'
        item['aviation_excerpt'] = 'Invented flight clearance'
        snap['event_afds'] = env
        evidence = afd_evidence(snap, NOW + timedelta(hours=1))['OKX']
        self.assertEqual(evidence['fetched_at'], stamp(NOW))
        self.assertEqual(evidence['issued_at'], stamp(NOW - timedelta(hours=2)))
        self.assertNotIn('Invented', json.dumps(evidence))
        self.assertEqual(item['discussion_excerpt'], 'Invented exact event forecast')

    def test_bad_office_cache_does_not_discard_neighbors(self):
        snap, env = self.good()
        env['offices']['OKX']['productText'] = text('ALY')
        checked = validate_afds(env, snap, NOW)
        self.assertIsNone(checked['offices']['OKX'])
        self.assertTrue(checked['offices']['PHI'])
        self.assertTrue(checked['offices']['ALY'])

    def test_parent_snapshot_and_custom_event_binding(self):
        snap = snapshot()
        snap['event'].update(date='2027-01-04', window='09-13', slug='winter-flight')
        env = collect_afds(FakeClient(), snap, NOW)
        self.assertTrue(validate_afds(env, snap, NOW)['offices']['OKX'])
        for field, value in [('date', '2027-01-05'), ('slug', 'other'), ('window', '08-15')]:
            changed = copy.deepcopy(snap)
            changed['event'][field] = value
            self.assertIsNone(validate_afds(env, changed, NOW))
        changed = dict(snap, collected_at=stamp(NOW + timedelta(seconds=1)))
        self.assertIsNone(validate_afds(env, changed, NOW))
        env['version'] = True
        self.assertIsNone(validate_afds(env, snap, NOW))

    def test_old_format_periods_retained(self):
        def mutate(office, raw):
            raw['productText'] = text(office).replace(
                '.DISCUSSION...\n.KEY MESSAGE 1...\n\nTonight is dry.\n\n'
                '.KEY MESSAGE 2...\n\nNext week remains uncertain. The front may stall.',
                '.SYNOPSIS...\nHigh pressure.\n&&\n.NEAR TERM /THROUGH TONIGHT/...\nDry tonight.\n&&\n'
                '.SHORT TERM /FRIDAY THROUGH SATURDAY/...\nCool Friday.\n&&\n'
                '.LONG TERM /SUNDAY THROUGH WEDNESDAY/...\nA front may stall next week.')
        snap, env = self.good(FakeClient(detail=mutate))
        snap['event_afds'] = env
        excerpt = afd_evidence(snap, NOW)['OKX']['discussion_excerpt']
        self.assertIn('SUNDAY THROUGH WEDNESDAY', excerpt)
        self.assertIn('A front may stall next week.', excerpt)
        self.assertIn('Dry tonight.', excerpt)

    def test_excerpt_budgets_tail_context_and_verbatim_paragraphs(self):
        def mutate(office, raw):
            near = '\n\n'.join(f'Near-term paragraph {i}. ' + 'Weather is fair. ' * 25 for i in range(8))
            raw['productText'] = text(office).replace('Tonight is dry.', near).replace(
                'Next week remains uncertain. The front may stall.',
                'In the extended period a front may stall north or south of the area.\n\n'
                'This boundary location remains uncertain, so showers remain possible.').replace(
                'VFR tonight.', 'Tonight conditions vary. ' * 100)
        snap, env = self.good(FakeClient(detail=mutate))
        snap['event_afds'] = env
        evidence = afd_evidence(snap, NOW)
        for office, item in evidence.items():
            original = ' '.join(env['offices'][office]['productText'].split())
            for section, limit in [('discussion', 1900), ('aviation', 700)]:
                excerpt = item[section + '_excerpt']
                self.assertLessEqual(len(excerpt), limit)
                self.assertTrue(item[section + '_truncated'])
                self.assertIn('[... omitted ...]', excerpt)
                for part in excerpt.split('\n\n'):
                    if part != '[... omitted ...]':
                        self.assertIn(' '.join(part.split()), original)
            self.assertIn('This boundary location remains uncertain', item['discussion_excerpt'])
            self.assertIn('.KEY MESSAGE 2...', item['discussion_excerpt'])
            self.assertIn('Monday: Showers possible.', item['aviation_excerpt'])
            self.assertIn(item['aviation_heading'], item['aviation_excerpt'])

    def test_standalone_period_heading_survives_body_truncation(self):
        from kcdw.event_afd import discussion_quote
        for separator in ('\n\n','\n'):
            def mutate(office, raw):
                raw['productText'] = text(office).replace(
                    'Next week remains uncertain. The front may stall.',
                    'Monday through Wednesday...'+separator+
                    'Forecast confidence remains limited by uncertainty in the position of the front. '*30)
            snap,env=self.good(FakeClient(detail=mutate))
            snap['event_afds']=env
            for item in afd_evidence(snap,NOW).values():
                excerpt=item['discussion_excerpt']
                self.assertIn('Monday through Wednesday...',excerpt)
                self.assertIn('Forecast confidence remains limited',excerpt)
                self.assertLessEqual(len(excerpt),1900)
                display=discussion_quote(excerpt)
                self.assertIn('Monday through Wednesday...',display)
                self.assertIn('Forecast confidence remains limited',display)
                self.assertLessEqual(len(display),1250)

    def test_oversized_text_fails_closed_not_silently_clipped(self):
        def mutate(office, raw):
            if office == 'OKX':
                raw['productText'] += 'x' * 32001
        env = collect_afds(FakeClient(detail=mutate), snapshot(), NOW)
        self.assertIsNone(env['offices']['OKX'])
        self.assertTrue(env['offices']['PHI'])

    def test_missing_sections_do_not_invent_coverage(self):
        def mutate(office, raw):
            raw['productText'] = f'FXUS61 K{office} 170100\nAFD{office}\n\n.MARINE...\nCalm.\n&&'
        snap, env = self.good(FakeClient(detail=mutate))
        snap['event_afds'] = env
        item = afd_evidence(snap, NOW)['OKX']
        self.assertEqual(item['discussion_excerpt'], '')
        self.assertEqual(item['aviation_excerpt'], '')
        self.assertEqual(item['aviation_heading'], '')

    def test_heading_only_key_message_is_not_silently_discarded(self):
        def mutate(office, raw):
            raw['productText'] = text(office).replace(
                '.KEY MESSAGE 1...\n\nTonight is dry.\n\n'
                '.KEY MESSAGE 2...\n\nNext week remains uncertain. The front may stall.',
                'KEY MESSAGE 1...Dry through Friday.\n\nKEY MESSAGE 2...Wet next week.')
        snap, env = self.good(FakeClient(detail=mutate))
        snap['event_afds'] = env
        item = afd_evidence(snap, NOW)['OKX']
        self.assertIn('Dry through Friday.', item['discussion_excerpt'])
        self.assertIn('Wet next week.', item['discussion_excerpt'])
        self.assertFalse(item['discussion_truncated'])

    def test_long_paragraph_uses_whole_opening_sentences_and_marks_cut(self):
        def mutate(office, raw):
            raw['productText'] = text(office).replace(
                'Next week remains uncertain. The front may stall.',
                'The extended period begins wet. ' + 'Uncertainty persists. ' * 180)
        snap, env = self.good(FakeClient(detail=mutate))
        snap['event_afds'] = env
        item = afd_evidence(snap, NOW)['OKX']
        self.assertLessEqual(len(item['discussion_excerpt']), 1900)
        self.assertIn('The extended period begins wet.', item['discussion_excerpt'])
        self.assertTrue(item['discussion_excerpt'].endswith('[... omitted ...]'))

    def test_major_section_fallback_and_multiline_period_heading(self):
        def mutate(office, raw):
            raw['productText'] = text(office).replace('&&\n\n', '').replace(
                '.AVIATION /00Z THURSDAY THROUGH MONDAY/...',
                '.AVIATION /00Z THURSDAY\nTHROUGH MONDAY/...')
        snap, env = self.good(FakeClient(detail=mutate))
        snap['event_afds'] = env
        item = afd_evidence(snap, NOW)['OKX']
        self.assertEqual(item['aviation_heading'], '.AVIATION /00Z THURSDAY THROUGH MONDAY/...')
        self.assertNotIn('VFR tonight', item['discussion_excerpt'])
        self.assertNotIn('Waves', item['aviation_excerpt'])

    def test_product_stales_independently_before_fetch_expires(self):
        def mutate(office, raw):
            if office == 'OKX':
                raw['issuanceTime'] = stamp(NOW - timedelta(hours=17))
        def listing(office, entries):
            mutate(office, entries[0])
        snap, env = self.good(FakeClient(listing=listing, detail=mutate))
        checked = validate_afds(env, snap, NOW + timedelta(hours=2))
        self.assertIsNone(checked['offices']['OKX'])
        self.assertTrue(checked['offices']['PHI'])
        self.assertTrue(checked['offices']['ALY'])

    def test_wmo_identity_and_cached_url_are_revalidated(self):
        snap, env = self.good()
        for field, value in [('productText', text('OKX').replace('FXUS61 KOKX', 'FXUS61 KPHI')),
                             ('product_url', 'https://evil.invalid/detail'),
                             ('listing', dict(product('OKX'), issuanceTime=stamp(NOW)))]:
            changed = copy.deepcopy(env)
            changed['offices']['OKX'][field] = value
            self.assertIsNone(validate_afds(changed, snap, NOW)['offices']['OKX'])
            self.assertTrue(validate_afds(changed, snap, NOW)['offices']['ALY'])

    def test_bounded_listing_scan_and_no_fallback_detail_loop(self):
        def listing(office, entries):
            entries[:] = [{}] * 100 + entries
        client = FakeClient(listing=listing)
        env = collect_afds(client, snapshot(), NOW)
        self.assertTrue(all(v is None for v in env['offices'].values()))
        self.assertEqual(len(client.urls), 3)

    def test_missing_envelope_and_malformed_inputs_fail_closed(self):
        self.assertEqual(afd_evidence(snapshot(), NOW), dict.fromkeys(IDS))
        for env in (None, [], {}, {'version': 1, 'offices': []}):
            self.assertIsNone(validate_afds(env, snapshot(), NOW))
        self.assertIsNone(validate_afds({}, snapshot(), NOW.replace(tzinfo=None)))


if __name__ == '__main__':
    unittest.main()
