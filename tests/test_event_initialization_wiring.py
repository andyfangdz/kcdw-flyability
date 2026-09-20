"""Initialization provenance is visible, snapshot-bound, and non-readiness evidence."""
import copy
import unittest
from unittest.mock import patch
from kcdw.event_narrative_evidence import build_event_evidence
import test_event_comparison as comparison
from test_events import NOW


class InitializationWiringTests(unittest.TestCase):
    def test_provenance_survives_evidence_build_without_new_source_vote(self):
        s=comparison.ComparisonTests().snapshot()
        before=build_event_evidence(s,NOW)
        s['initialization_provenance_version']=1
        original=copy.deepcopy(s)
        rows=[{'model':'IFS','advertised_init':'2026-09-17T06:00:00Z',
               'likely_init':'2026-09-17T00:00:00Z','binding':'inferred; unverified'}]
        with patch('kcdw.event_narrative_evidence.initialization_evidence',return_value=rows,create=True):
            evidence=build_event_evidence(s,NOW)
        self.assertEqual(evidence['model_initializations'],rows)
        self.assertEqual([v['id'] for v in evidence['sources']],[v['id'] for v in before['sources']])
        self.assertEqual(s,original)

    def test_legacy_evidence_does_not_gain_current_initialization_claims(self):
        with patch('kcdw.event_narrative_evidence.initialization_evidence',create=True) as collect:
            evidence=build_event_evidence(comparison.ComparisonTests().snapshot(),NOW)
        self.assertNotIn('model_initializations',evidence)
        collect.assert_not_called()
