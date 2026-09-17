import unittest
from datetime import datetime, timezone
from kcdw.events import local_clock

class DisplayClockTests(unittest.TestCase):
    def test_utc_day_and_dst_are_converted_not_relabelled(self):
        self.assertEqual(local_clock('2026-09-18T01:02:03.456Z',True),'Sep 17 21:02 EDT')
        self.assertEqual(local_clock('2026-01-18T01:02:03Z',True),'Jan 17 20:02 EST')
        self.assertEqual(local_clock(datetime(2026,9,18,1,2,tzinfo=timezone.utc)),'21:02')
    def test_naive_timestamp_is_not_assigned_the_host_timezone(self):
        for value in ('2026-09-18T01:02:03',datetime(2026,9,18,1,2)):
            with self.assertRaises(ValueError): local_clock(value)
