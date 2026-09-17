"""Continuous run-history refresh triggers publication only for pending changes."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kcdw import history_update
from test_events import EVENT, NOW
from test_run_history import fixture


class HistoryUpdateTests(unittest.TestCase):
    def test_new_cycles_publish_once_and_unchanged_poll_is_noop(self):
        history = fixture()
        refresh = Mock(return_value=history)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(history_update, 'upcoming_events', return_value=[EVENT]), \
             patch.object(history_update, 'update_forecasts', return_value=0) as publish:
            var = Path(tmp)
            config = var/'config.json'
            self.assertEqual(history_update.update(var, config, NOW, refresh=refresh), 0)
            self.assertEqual(history_update.update(var, config, NOW, refresh=refresh), 0)
            self.assertEqual(publish.call_count, 1)
            self.assertEqual(refresh.call_count, 2)
            refresh.assert_called_with(var/'events'/EVENT.slug, EVENT.as_dict(), NOW)
            new = copy.deepcopy(history)
            new['points'][0]['metrics']['pressure']['center'] += 1
            refresh.return_value = new
            self.assertEqual(history_update.update(var, config, NOW, refresh=refresh), 0)
            self.assertEqual(publish.call_count, 2)

    def test_failed_publication_is_retried_without_new_cycles(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(history_update, 'upcoming_events', return_value=[EVENT]), \
             patch.object(history_update, 'update_forecasts', side_effect=[1, 0, 0]) as publish:
            var = Path(tmp)
            refresh = Mock(return_value=fixture())
            for expected in (1, 0, 0):
                self.assertEqual(history_update.update(var, var/'config.json', NOW, refresh=refresh), expected)
            self.assertEqual(publish.call_count, 2)

    def test_source_failure_retains_state_and_has_sanitized_log(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(history_update, 'upcoming_events', return_value=[EVENT]), \
             patch.object(history_update, 'update_forecasts') as publish:
            var = Path(tmp)
            refresh = Mock(side_effect=OSError('private-credential-material'))
            self.assertEqual(history_update.update(var, None, NOW, refresh=refresh), 1)
            publish.assert_not_called()
            self.assertNotIn('private-credential-material', (var/'events'/'run-history-update.log').read_text())

    def test_local_success_does_not_suppress_later_cloud_publication(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(history_update, 'upcoming_events', return_value=[EVENT]), \
             patch.object(history_update, 'update_forecasts', return_value=0) as publish:
            var = Path(tmp)
            refresh = Mock(return_value=fixture())
            self.assertEqual(history_update.update(var, None, NOW, refresh=refresh), 0)
            self.assertEqual(history_update.update(var, var/'config.json', NOW, refresh=refresh), 0)
            self.assertEqual(publish.call_count, 2)

    def test_missing_history_and_past_events_do_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(history_update, 'upcoming_events', return_value=[EVENT]), \
             patch.object(history_update, 'update_forecasts') as publish:
            refresh = Mock(return_value=None)
            self.assertEqual(history_update.update(Path(tmp), None, NOW, refresh=refresh), 0)
            publish.assert_not_called()
        past = Mock()
        past.days_out.return_value = -1
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(history_update, 'upcoming_events', return_value=[past]):
            refresh = Mock()
            self.assertEqual(history_update.update(Path(tmp), None, NOW, refresh=refresh), 0)
            refresh.assert_not_called()
