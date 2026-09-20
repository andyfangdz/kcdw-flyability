"""Archived plotted history stays separate from current assessment evidence."""
from copy import deepcopy
import unittest
from kcdw.event_narrative_evidence import build_event_evidence
import test_gfs_comparison
from test_events import NOW


class ForecastHistoryWiringTests(unittest.TestCase):


    def test_plot_history_does_not_enter_current_narrative_evidence(self):
        snapshot=test_gfs_comparison.GfsComparisonTests().snapshot()
        before=build_event_evidence(snapshot,NOW)
        changed=deepcopy(snapshot)
        changed['forecast_history']={'sources':{'gfs':{'historical_only':'not current evidence'}}}
        self.assertEqual(build_event_evidence(changed,NOW),before)


if __name__=='__main__':
    unittest.main()
