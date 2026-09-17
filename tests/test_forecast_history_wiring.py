"""Archived plotted history stays separate from current assessment evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kcdw import event_update as update
from kcdw.event_narrative_evidence import build_event_evidence
import test_gfs_comparison
from test_events import EVENT, NOW


class ForecastHistoryWiringTests(unittest.TestCase):
    def run_update(self, history, failure=False):
        snapshot=test_gfs_comparison.GfsComparisonTests().snapshot()
        def narrative(received, work_dir, now):
            self.assertEqual(received['forecast_history'],None if failure else history)
            return None
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(update,'upcoming_events',return_value=[EVENT]), \
             patch.object(update,'collect_event',return_value=snapshot), \
             patch.object(update, 'collect_ceiling', return_value=None), \
             patch.object(update, 'collect_layer_signals', return_value=None), \
             patch.object(update,'build_trends',return_value=None), \
             patch.object(update,'load_run_history',return_value=None), \
             patch.object(update,'build_forecast_history',side_effect=RuntimeError('bad archive') if failure else None,return_value=history) as builder, \
             patch.object(update,'generate_event_narrative',side_effect=narrative), \
             patch.object(update,'render',return_value=('<html></html>',{})) as render, \
             patch.object(update,'summary',return_value='test'), \
             patch.object(update,'archive_run',return_value=Path(tmp)):
            self.assertEqual(update.update(Path(tmp),None,NOW),0)
            self.assertEqual(builder.call_args.args[1],Path(tmp)/'events'/EVENT.slug/'runs')
            self.assertEqual(render.call_count,1)

    def test_history_is_added_before_render_and_archiving(self):
        self.run_update({'marker':'saved forecast data'})

    def test_broken_history_does_not_block_current_page(self):
        self.run_update(None,failure=True)

    def test_plot_history_does_not_enter_current_narrative_evidence(self):
        snapshot=test_gfs_comparison.GfsComparisonTests().snapshot()
        before=build_event_evidence(snapshot,NOW)
        changed=deepcopy(snapshot)
        changed['forecast_history']={'sources':{'gfs':{'historical_only':'not current evidence'}}}
        self.assertEqual(build_event_evidence(changed,NOW),before)


if __name__=='__main__':
    unittest.main()
