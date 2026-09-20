"""Offline contract and pipeline tests; no credentials, network, or live model required."""
import copy
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kcdw import assessment_agent, event_narrative, typesafe_assessment as assessment
from kcdw import typesafe_client as api, typesafe_review as review
from kcdw.typesafe_packing import compact_state
from kcdw.common import parse_time
from kcdw.renderer import render
from kcdw.runs import copy_typesafe, finish
from kcdw.scoring import planning_windows, score_label
from kcdw.validation import ValidationError, validate_analysis
from tests import test_event_narrative as event_helpers

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests' / 'fixtures'


def fixture():
    snapshot = json.loads((FIX / 'sample_snapshot.json').read_text())
    draft = json.loads((FIX / 'sample_analysis.json').read_text())
    for day in draft['days']:
        day['outlook'] = score_label(max(w['score'] for w in day['windows']))
        day['windows'] = [w for w in day['windows'] if w['window'] in planning_windows(snapshot, day['date'])]
        for window in day['windows']:
            window.pop('label', None)
    return snapshot, draft


def response_for(request):
    answers = {}
    for key, question in request['questions'].items():
        kind = question['type']
        if kind == 'choice':
            options = question['criteria']
            selected = next((c for c in ('probable', 'no_change_claim', 'unknown', 'supported') if c in options), next(iter(options)))
            probabilities = {c: (1.0 if c == selected else 0.0) for c in options}
            answers[key] = {'type': kind, 'choice': selected, 'probabilities': probabilities, 'confidence': 0.9}
        elif kind == 'score':
            n = len(question['criteria'])
            probabilities = {str(i): 0.0 for i in range(n)}
            probabilities[str(n - 2)] = 0.3
            probabilities[str(n - 1)] = 0.7
            answers[key] = {'type': kind, 'score': n - 1.3, 'confidence': 0.4,
                            'probabilities': probabilities,
                            'legend': {str(i): level for i, level in enumerate(question['criteria'])}}
        else:
            answers[key] = {'type': 'noul', 'noul': 0.8}
    return {'model': request['model'], 'answers': answers, 'usage': {'input_tokens': 123, 'output_tokens': 45}}


class Response:
    def __init__(self, value=None, status=200):
        self.value, self.status_code = value, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield json.dumps(self.value).encode()


class TypeSafeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = []

        def transport(url, **kwargs):
            request = json.loads(kwargs['data'])
            self.requests.append(request)
            self.transport_args = (url, kwargs)
            return Response(response_for(request))

        self.client = api.Client('private-test-key', self.root / 'typesafe', transport=transport, sleep=lambda _: None)
        self.snapshot, self.draft = fixture()

    def test_http_contract_private_artifacts_and_exact_model(self):
        result = self.client.evaluate('contract', {'text': 'evidence'}, {'q': {'type': 'choice', 'instructions': 'Question?',
                                                                            'criteria': {'yes': 'Yes', 'no': 'No'}}})
        url, kwargs = self.transport_args
        self.assertEqual(url, 'https://api.typesafe.ai/v1/systemone')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (10, 60))
        self.assertEqual(result['model'], api.MODEL)
        self.assertEqual(result['usage']['input_tokens'], 123)
        for path in self.client.artifacts.glob('*.json'):
            self.assertNotIn('private-test-key', path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_bad_responses_cannot_be_used_as_judgments(self):
        question = {'q': {'type': 'choice', 'criteria': {'a': 'A', 'b': 'B'}}}
        good = response_for({'model': api.MODEL, 'questions': question})
        changes = [lambda r: r.update(model='jev-latest'), lambda r: r.update(answers={}),
                   lambda r: r['answers']['q'].update(type='score'),
                   lambda r: r['answers']['q'].update(confidence=float('nan')),
                   lambda r: r['answers']['q'].update(choice='absent'),
                   lambda r: r['answers']['q'].update(choice='b'),
                   lambda r: r['answers']['q'].update(probabilities={'a': 0.1, 'b': 0.2}),
                   lambda r: r['answers']['q'].update(probabilities={'a': True, 'b': 0}),
                   lambda r: r.update(usage={'input_tokens': -1})]
        for change in changes:
            value = copy.deepcopy(good)
            change(value)
            with self.subTest(value=value), self.assertRaises(api.TypeSafeError):
                api.validate_response(value, question, api.MODEL)

    def test_score_must_match_legend_and_weighted_distribution(self):
        questions = {'q': {'type': 'score', 'criteria': ['Low', 'Medium', 'High']}}
        good = response_for({'model': api.MODEL, 'questions': questions})
        for changes in ({'score': 0.1}, {'legend': {'0': 'Wrong'}}, {'score': float('inf')}):
            value = copy.deepcopy(good)
            value['answers']['q'].update(changes)
            with self.assertRaises(api.TypeSafeError):
                api.validate_response(value, questions, api.MODEL)

    def test_rounded_probabilities_allow_only_their_rounding_error(self):
        questions = {'q': {'type': 'score', 'criteria': ['Low', 'Medium', 'High']}}
        value = response_for({'model': api.MODEL, 'questions': questions})
        answer = value['answers']['q']
        answer.update(score=1.0, probabilities={'0': 0.33, '1': 0.33, '2': 0.33})
        validated = api.validate_response(value, questions, api.MODEL)
        self.assertEqual(validated['answers']['q']['probabilities'], answer['probabilities'])
        for probabilities in ({'0': 0.32, '1': 0.32, '2': 0.32}, {'0': 0.3301, '1': 0.3301, '2': 0.3301}):
            answer['probabilities'] = probabilities
            with self.assertRaises(api.TypeSafeError):
                api.validate_response(value, questions, api.MODEL)

    def test_rate_limit_retries_once_and_transport_errors_are_sanitized(self):
        attempts = []

        def transport(url, **kwargs):
            attempts.append(1)
            return Response(status=429) if len(attempts) == 1 else Response(response_for(json.loads(kwargs['data'])))

        self.client.transport = transport
        self.client.evaluate('retry', {}, {'q': {'type': 'noul', 'instructions': 'Yes?'}})
        self.assertEqual(len(attempts), 2)
        self.client.transport = lambda *a, **k: (_ for _ in ()).throw(OSError('private-test-key echoed by transport'))
        with self.assertRaisesRegex(api.TypeSafeError, 'transport or JSON'):
            self.client.evaluate('failure', {}, {'q': {'type': 'noul', 'instructions': 'Yes?'}})
        self.assertNotIn('private-test-key', (self.client.artifacts / '002-failure.error.json').read_text())

    def test_configuration_is_explicit_and_key_file_permissions_are_checked(self):
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'unused-key'}, clear=True):
            self.assertIsNone(api.configured_client(self.root / 'default-off', var=self.root))
        with patch.dict(os.environ, {'KCDW_TYPESAFE': 'auto'}, clear=True):
            self.assertIsNone(api.configured_client(self.root / 'out', var=self.root))
            os.environ['KCDW_TYPESAFE'] = 'required'
            with self.assertRaises(api.TypeSafeError):
                api.configured_client(self.root / 'out', var=self.root)
            path = self.root / 'typesafe-api-key'
            path.write_text('private-test-key')
            path.chmod(0o644)
            with self.assertRaises(api.TypeSafeError):
                api.configured_client(self.root / 'out', var=self.root)
            path.chmod(0o600)
            self.assertEqual(api.configured_client(self.root / 'out', var=self.root).model, api.MODEL)
            os.environ['KCDW_TYPESAFE'] = 'off'
            self.assertIsNone(api.configured_client(self.root / 'out', var=self.root))

    def test_transient_contract_error_retries_without_accepting_invalid_choice(self):
        attempts = []

        def transport(url, **kwargs):
            response = response_for(json.loads(kwargs['data']))
            attempts.append(1)
            if len(attempts) == 1:
                response['answers']['q']['choice'] = 'b'
            return Response(response)

        self.client.transport = transport
        questions = {'q': {'type': 'choice', 'instructions': 'Choose', 'criteria': {'a': 'A', 'b': 'B'}}}
        result = self.client.evaluate('contract-retry', {}, questions)
        self.assertEqual(result['answers']['q']['choice'], 'a')
        self.assertEqual(len(attempts), 2)
        self.assertIn('disagrees', (self.client.artifacts / '001-contract-retry.retry.json').read_text())
        attempts.clear()
        self.client.transport = lambda url, **kwargs: Response({**response_for(json.loads(kwargs['data'])), 'model': 'wrong'})
        with self.assertRaises(api.TypeSafeError):
            self.client.evaluate('contract-still-invalid', {}, questions)
    def test_weather_encoding_round_trips_missing_fields_times_and_extrema(self):
        passage = 'Original forecast paragraph with valid times and important uncertainty. ' * 4
        records = {f'field{i}': {'hourly_p90_max': 40 + i, 'mean_min': 0, 'sample_count': 12,
                               'valid_at': '2026-09-20T16:00:00Z', 'unit': 'kt'} for i in range(12)}
        records['field1']['missing_value'] = None
        state = {'weather': {'fields': records, 'first': 'First bulletin\n\n' + passage,
                             'second': 'Second bulletin\n\n' + passage,
                             'rows': [{'timestamp': f'2026-09-20T{h:02d}:00:00Z', 'wind_gust_knots': h} for h in range(12)]},
                 'claims': [{'claim': 'Test claim', 'context': passage}], 'paragraphs': [passage]}
        packed = compact_state(state)

        def decode(value):
            if isinstance(value, list):
                return [decode(v) for v in value]
            if not isinstance(value, dict):
                return value
            if set(value) == {'text_ref'}:
                return packed['text_dictionary'][value['text_ref']]
            if set(value) == {'text_parts'}:
                return ''.join(decode(v) for v in value['text_parts'])
            if 'table_columns' in value and 'table_rows' in value:
                rows = [{k: decode(v) for k, v in zip(value['table_columns'], row) if v != {'absent_field': True}}
                        for row in value['table_rows']]
                return dict(zip(value['record_keys'], rows)) if 'record_keys' in value else rows
            return {k: decode(v) for k, v in value.items()}

        self.assertEqual(decode(packed['weather']), state['weather'])
        self.assertEqual(packed['claims'], state['claims'])
        self.assertEqual(packed['paragraphs'], state['paragraphs'])
        self.assertLess(len(api.encoded(packed)), len(api.encoded(state)))

    def test_large_state_uses_compact_text_without_dropping_evidence(self):
        state = {'evidence': {'original_text': 'Weather evidence. ' * 3500}}
        self.client.evaluate('large-text', state, {'q': {'type': 'noul', 'instructions': 'Is weather mentioned?'}})
        self.assertIsInstance(self.requests[0]['state'], str)
        self.assertEqual(json.loads(self.requests[0]['state']), state)

    def test_ranked_passages_retain_verbatim_text_context_and_timestamps(self):
        source = self.snapshot['sources']['okx_afd']['data']
        source['excerpt'] = '.AVIATION...\n\nClouds persist through Monday morning.\n\nClearing follows late Monday.'
        ranking = review.rank_afds(self.client, self.snapshot)
        selected = ranking['sources'][0]['selected']
        self.assertIn('Clouds persist through Monday morning.', [p['text'] for p in selected])
        self.assertEqual(ranking['sources'][0]['metadata']['issuanceTime'], source['issuanceTime'])
        self.assertEqual(self.requests[0]['state']['product_text'], source['excerpt'])
        self.assertIn('paragraphs[1]', self.requests[0]['questions']['p1']['instructions']['question'])
        self.assertEqual(ranking['evidence_sha256'], api.digest(self.snapshot))

    def test_review_covers_headline_lead_next_check_and_change_meaning(self):
        evidence = event_helpers.evidence({'event': {'name': 'Test'}, 'collected_at': event_helpers.NOW.isoformat()}, event_helpers.NOW)
        result = review.review_briefing(self.client, evidence, event_helpers.output(), purpose='event')
        paths = {r['path'] for r in result['claims']}
        self.assertTrue({'headline', 'lead', 'next_check.text', 'sections[0].body'} <= paths)
        self.assertEqual(result['changes']['change_support']['choice'], 'no_change_claim')
        self.assertEqual(result['changes']['change_materiality']['choice'], 'unknown')
        self.assertEqual(result['mode'], 'observe')
        self.assertEqual(result['narrative_sha256'], api.digest(event_helpers.output()))
        self.assertIn('wn3_point', self.requests[0]['state']['sources'])

    def test_failed_review_is_unavailable_not_a_pass(self):
        result = review.optional_feature(self.client, 'test-review', lambda: (_ for _ in ()).throw(TimeoutError('private')))
        self.assertIsNone(result)
        saved = json.loads((self.client.artifacts / 'test-review-unavailable.json').read_text())
        self.assertEqual(saved['status'], 'unavailable')
        self.assertNotIn('private', json.dumps(saved))

    def test_week_has_exact_windows_and_weather_confidence_is_not_model_confidence(self):
        decisions, _ = assessment.assess_week(self.client, self.snapshot, self.draft)
        self.assertEqual(len(decisions['days']), 7)
        self.assertEqual(len(self.requests), 7)
        for day in decisions['days']:
            self.assertEqual([w['window'] for w in day['windows']], planning_windows(self.snapshot, day['date']))
            self.assertEqual(day['confidence_score'], 85.0)
            self.assertEqual(day['confidence'], 'high')
            self.assertEqual(day['score'], 70)
        saved = json.loads((self.client.artifacts / '001-weekly-2026-09-03.response.json').read_text())
        self.assertEqual(saved['answers']['weather_confidence']['confidence'], 0.4)
        self.assertNotEqual(decisions['best_day'], decisions['backup_day'])

    def test_day_slicing_keeps_naive_eastern_times_extrema_and_full_intervals(self):
        source = self.snapshot['sources']['open_meteo']['data']
        source['hourly'] = {'time': ['2026-09-03T09:00', '2026-09-03T10:00', '2026-09-05T09:00'],
                            'wind_gusts_10m': [4, 40, 5]}
        evidence = assessment.day_evidence(self.snapshot, '2026-09-03')
        self.assertEqual(evidence['sources']['open_meteo']['data']['hourly']['wind_gusts_10m'], [4, 40])
        self.assertEqual(evidence['assessment_period']['timezone'], 'America/New_York')
        self.assertEqual(evidence['sources']['open_meteo']['data']['daily']['time'], ['2026-09-03'])
        self.assertEqual(self.snapshot['sources']['open_meteo']['data']['hourly']['wind_gusts_10m'], [4, 40, 5])

    def test_summary_scope_explicitly_omits_hourly_precision(self):
        evidence = assessment.overview_evidence(self.snapshot)
        for key in ('nws_grid', 'nws_hourly', 'open_meteo'):
            if evidence['sources'].get(key, {}).get('ok'):
                self.assertIn('review_detail_omitted', evidence['sources'][key]['data'])
        self.assertEqual(evidence['report_dates'], self.snapshot['report_dates'])

    def test_future_day_preserves_unknown_advisories_without_treating_expiry_as_clear(self):
        self.snapshot['sources']['awc_metars']['data'] = [
            {'icaoId': 'KCDW', 'obsTime': 1, 'rawOb': 'older'},
            {'icaoId': 'KCDW', 'obsTime': 2, 'rawOb': 'latest'},
            {'icaoId': 'KMMU', 'rawOb': 'unknown time'},
        ]
        expired = {'properties': {'hazard': 'CONVECTIVE', 'validTimeFrom': '2026-09-03T10:00:00Z',
                                   'validTimeTo': '2026-09-03T14:00:00Z', 'rawAirSigmet': 'Old advisory'}}
        unknown = {'properties': {'hazard': 'CONVECTIVE', 'rawAirSigmet': 'Unknown validity'}}
        self.snapshot['sources']['awc_convective_sigmets']['data'] = [expired, unknown]
        evidence = assessment.day_evidence(self.snapshot, '2026-09-05')
        metars = evidence['sources']['awc_metars']['data']
        self.assertEqual([v['rawOb'] for v in metars['latest_by_station']], ['latest'])
        self.assertEqual(metars['unknown_time_or_station'][0]['rawOb'], 'unknown time')
        sigmets = evidence['sources']['awc_convective_sigmets']['data']
        self.assertEqual(sigmets['overlapping_or_unknown_validity'], [unknown])
        self.assertEqual(sigmets['outside_period'][0]['validTimeTo'], expired['properties']['validTimeTo'])
        self.assertIn('not a forecast of no convection', sigmets['selection_note'])

    def written(self, decisions):
        value = copy.deepcopy(self.draft)
        value.update(best_day=decisions['best_day'], backup_day=decisions['backup_day'])
        for day, fixed in zip(value['days'], decisions['days']):
            day.update(outlook=fixed['outlook'], confidence=fixed['confidence'])
            for window, selected in zip(day['windows'], fixed['windows']):
                window['score'] = selected['score']
        return value

    def test_writer_cannot_change_decisions_or_reuse_another_snapshot(self):
        decisions, _ = assessment.assess_week(self.client, self.snapshot, self.draft)
        written = self.written(decisions)
        final = assessment.apply_decisions(written, decisions, self.snapshot)
        validate_analysis(final, self.snapshot)
        self.assertEqual(final['assessment']['provider'], 'typesafe')
        for change in (lambda v: v.update(best_day=v['backup_day']),
                       lambda v: v['days'][0].update(confidence='low'),
                       lambda v: v['days'][0]['windows'][0].update(score=10)):
            changed = copy.deepcopy(written)
            change(changed)
            with self.assertRaises(ValueError):
                assessment.apply_decisions(changed, decisions, self.snapshot)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['extra_evidence'] = 'different'
        with self.assertRaises(api.TypeSafeError):
            assessment.apply_decisions(written, decisions, snapshot)

    def test_metadata_and_indices_are_validated_and_render_as_ordinal(self):
        decisions, _ = assessment.assess_week(self.client, self.snapshot, self.draft)
        final = assessment.apply_decisions(self.written(decisions), decisions, self.snapshot)
        html, _ = render(self.snapshot, final, parse_time(self.snapshot['collected_at']))
        self.assertIn('Weather confidence index: 85/100', html)
        self.assertIn('Ordinal planning indices, not probabilities.', html)
        for mutation in (lambda a: a['days'][0].update(confidence_score=float('nan')),
                         lambda a: a['days'][0].update(score=10),
                         lambda a: a['assessment'].update(snapshot_sha256='0' * 64),
                         lambda a: a['assessment'].update(rubric_version=True),
                         lambda a: a.update(assessment=None),
                         lambda a: a.pop('assessment')):
            changed = copy.deepcopy(final)
            mutation(changed)
            with self.assertRaises(ValidationError):
                validate_analysis(changed, self.snapshot)

    def test_full_orchestrator_uses_two_passes_and_fixed_schema(self):
        seen = []

        def runner(command, **kwargs):
            seen.append((command, kwargs))
            value = self.draft if len(seen) == 1 else self.written(json.loads((self.client.artifacts / 'weekly-decisions.json').read_text()))
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                'type': 'result', 'is_error': False, 'modelUsage': {'claude-fable-5-1': {}}, 'structured_output': value}))

        result = assessment_agent.generate(self.snapshot, 'Weather prompt', json.loads((ROOT / 'schema/analysis.schema.json').read_text()),
                                           self.root / 'analysis.json', self.root / 'log', self.client.artifacts,
                                           runner=runner, client=self.client, configure=False)
        self.assertEqual(len(seen), 2)
        self.assertIn('WebSearch', seen[0][0][seen[0][0].index('--tools') + 1])
        self.assertEqual(seen[1][0][seen[1][0].index('--tools') + 1], '')
        schema = json.loads(seen[1][0][seen[1][0].index('--json-schema') + 1])
        self.assertEqual(schema['properties']['best_day']['const'], result['best_day'])
        self.assertEqual(schema['properties']['days']['items']['oneOf'][0]['properties']['outlook']['const'], 'Probably flyable')
        self.assertNotIn('assessment', schema['properties'])
        self.assertEqual(json.loads((self.root / 'analysis.json').read_text()), result)
        self.assertEqual(json.loads((self.client.artifacts / 'status.json').read_text())['status'], 'complete')

    def test_assessment_failure_does_not_write_output_or_call_final_writer(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(1)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                'type': 'result', 'is_error': False, 'modelUsage': {'claude-fable-5-1': {}}, 'structured_output': self.draft}))

        with patch.object(assessment_agent, 'assess_week', side_effect=api.TypeSafeError('unavailable')):
            with self.assertRaises(api.TypeSafeError):
                assessment_agent.generate(self.snapshot, 'Weather prompt', json.loads((ROOT / 'schema/analysis.schema.json').read_text()),
                                           self.root / 'analysis.json', self.root / 'log', self.client.artifacts,
                                           runner=runner, client=self.client, configure=False)
        self.assertEqual(len(calls), 1)
        self.assertFalse((self.root / 'analysis.json').exists())

    def test_event_reviewer_failure_preserves_valid_narrative(self):
        snapshot = {'event': {'name': 'Checkride'}, 'collected_at': event_helpers.NOW.isoformat()}

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                'type': 'result', 'is_error': False, 'modelUsage': {'claude-fable-5-1': {}}, 'structured_output': event_helpers.output()}))

        with patch.object(event_narrative, 'build_event_evidence', side_effect=event_helpers.evidence), \
             patch.object(review, 'review_briefing', side_effect=api.TypeSafeError('unavailable')):
            result = event_narrative.generate_event_narrative(snapshot, self.root / 'event', event_helpers.NOW,
                runner=runner, clock=lambda: event_helpers.NOW, typesafe_client=self.client, configure_typesafe=False)
        self.assertEqual(result['data'], event_helpers.output())
        self.assertEqual(json.loads((self.client.artifacts / 'status.json').read_text())['status'], 'unavailable')

    def test_failed_runs_archive_diagnostics_without_following_links(self):
        source = self.root / 'typesafe'
        api.private_json(source / 'failure.json', {'status': 'failed'})
        secret = self.root / 'secret'
        secret.write_text('private-secret')
        (source / 'linked.json').symlink_to(secret)
        dest = self.root / 'archive'
        dest.mkdir()
        copy_typesafe(source, dest)
        self.assertTrue((dest / 'typesafe' / 'failure.json').exists())
        self.assertFalse((dest / 'typesafe' / 'linked.json').exists())
        missing = self.root / 'missing'
        finish(self.root, 'failed-run', missing, missing, missing, missing, missing, 1, source)
        self.assertTrue((self.root / 'runs' / 'failed-run' / 'typesafe' / 'failure.json').exists())


if __name__ == '__main__':
    unittest.main()
