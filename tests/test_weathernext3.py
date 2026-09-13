"""Synthetic contract tests; live smoke is opt-in and prints metadata only."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from kcdw.weathernext3 import collect_weather_next3, validate_weather_next3, summarize_weather_next3

RUN = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
NOW = RUN + timedelta(hours=2)

def iso(t):
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")

def fixture(fallback=False):
    requested = RUN + timedelta(hours=6) if fallback else RUN
    fetched = requested + timedelta(minutes=30)
    fields = {}
    for name, unit, value in (("temperature_2m", "degC", 20), ("precipitation_1h", "mm", 1),
                              ("sea_level_pressure", "Pa", 101000), ("wind_speed_10m", "m/s", 5)):
        fields[name] = {"unit": unit, "mean": [value] * 360, "p10": [value / 2 if name != "sea_level_pressure" else 99000] * 360,
                        "p90": [value * 1.5 if name != "sea_level_pressure" else 103000] * 360}
    return {"explicit_last_good": False, "status": {
        "state": "degraded" if fallback else "ready", "available": True, "freshness": "fresh",
        "authentication": "last_refresh_succeeded", "error_code": None,
        "station": "KCDW", "model": "WeatherNext 3", "model_id": 12, "latitude": 40.8752, "longitude": -74.2814,
        "actual_run_utc": iso(RUN), "requested_init_utc": iso(requested), "attempted_init_utc": iso(RUN),
        "last_good_requested_init_utc": iso(requested), "fetched_at": iso(fetched), "fallback": fallback},
        "forecast": {"source": "Weather Lab GetForecastStatistics", "model": "WeatherNext 3", "model_id": 12,
        "latitude": 40.8752, "longitude": -74.2814, "requested_init_utc": iso(RUN), "response_init_utc": iso(RUN),
        "valid_time_utc": [iso(RUN + timedelta(hours=i)) for i in range(1, 361)], "fields": fields}}

class WeatherNext3Tests(unittest.TestCase):
    def test_valid_and_explicit_fallback_and_mean_outside_quantiles(self):
        data = fixture()
        data['status']['fetched_at'] = iso(RUN + timedelta(minutes=30, milliseconds=123))
        data["forecast"]["fields"]["precipitation_1h"]["mean"][0] = 10
        self.assertIsNone(validate_weather_next3(data, NOW))
        self.assertIsNone(validate_weather_next3(fixture(True), RUN + timedelta(hours=7)))

    def test_rejects_bad_metadata_and_times(self):
        cases = [(('explicit_last_good',), True), (('status','available'), 1),
                 (('status','station'), 'KTEB'), (('status','model_id'), True),
                 (('forecast','model'), 'WeatherNext 2'), (('forecast','latitude'), 40),
                 (('status','freshness'), 'stale'), (('status','error_code'), 'auth_required'),
                 (('status','fallback'), True), (('status','attempted_init_utc'), iso(RUN-timedelta(hours=6))),
                 (('forecast','requested_init_utc'), iso(RUN-timedelta(hours=6))),
                 (('status','fetched_at'), iso(NOW+timedelta(minutes=6))),
                 (('forecast','valid_time_utc',0), iso(RUN)),
                 (('forecast','valid_time_utc'), []), (('status','requested_init_utc'), iso(RUN+timedelta(hours=6)))]
        for path, value in cases:
            with self.subTest(path=path):
                data = fixture(); node = data
                for key in path[:-1]: node = node[key]
                node[path[-1]] = value
                with self.assertRaises(ValueError): validate_weather_next3(data, NOW)
        for now in (RUN-timedelta(seconds=1), RUN+timedelta(hours=18, seconds=1), NOW.replace(tzinfo=None)):
            with self.assertRaises(ValueError): validate_weather_next3(fixture(), now)

    def test_rejects_bad_fields(self):
        for value in (True, float('nan'), float('inf'), -1, 1501, '1'):
            data = fixture(); data['forecast']['fields']['precipitation_1h']['mean'][0] = value
            with self.subTest(value=str(value)), self.assertRaises(ValueError): validate_weather_next3(data, NOW)
        for mutate in (lambda f: f.pop('temperature_2m'), lambda f: f['wind_speed_10m'].update(unit='kt'),
                       lambda f: f['wind_speed_10m'].update(mean=[1]), lambda f: f['wind_speed_10m']['p10'].__setitem__(0, 20)):
            data = fixture(); mutate(data['forecast']['fields'])
            with self.assertRaises(ValueError): validate_weather_next3(data, NOW)

    def test_summary_all_days_coverage_and_quantile_semantics(self):
        summary = summarize_weather_next3(fixture(), NOW)
        self.assertEqual(len(summary['days']), 15)
        self.assertEqual(summary['timezone'], 'America/New_York')
        self.assertIn('cannot', summary['semantics']['quantiles'])
        self.assertIn('visibility', summary['missing_fields'])
        day = summary['days'][1]
        self.assertEqual(day['coverage']['hours'], 12)
        self.assertEqual(day['fields']['precipitation_1h']['mean_sum_mm'], 12)
        self.assertNotIn('p90_sum', json.dumps(summary))
        self.assertLess(summary['days'][0]['coverage']['hours'], 12)
        self.assertEqual(summary['days'][-1]['date'], '2026-09-26')

    def test_precipitation_uses_endpoints_after_opening_through_closing(self):
        data = fixture()
        axis = data['forecast']['valid_time_utc']
        opening = axis.index('2026-09-13T12:00:00.000Z')  # 08 Eastern
        closing = axis.index('2026-09-14T00:00:00.000Z')  # 20 Eastern
        field = data['forecast']['fields']['precipitation_1h']
        field['mean'][opening] = 100
        field['mean'][closing] = 9
        day = summarize_weather_next3(data, NOW)['days'][1]
        self.assertEqual(day['fields']['precipitation_1h']['mean_sum_mm'], 20)
        self.assertEqual(day['fields']['precipitation_1h']['sample_count'], 12)

    def test_private_collect_and_token_security(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp)/'token'; token.write_text('synthetic-test-token'); token.chmod(0o600)
            response = MagicMock(); response.__enter__.return_value = response
            response.status = 200; response.read.return_value = json.dumps(fixture()).encode()
            opener = MagicMock(); opener.open.return_value = response
            with patch.dict(os.environ, {'WEATHERLAB_TOKEN_FILE': str(token)}), patch('kcdw.weathernext3.build_opener', return_value=opener) as build:
                self.assertEqual(collect_weather_next3(NOW), fixture())
                req = opener.open.call_args.args[0]
                self.assertEqual(req.full_url, 'http://127.0.0.1:8796/v1/forecast/KCDW')
                self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
                self.assertEqual(response.read.call_args.args[0], 3*1024*1024+1)
                self.assertEqual(build.call_args.args[0].proxies, {})
                token.chmod(0o644)
                with self.assertRaisesRegex(ValueError, '^weathernext3_token_error$'): collect_weather_next3(NOW)
                token.chmod(0o600); link = Path(tmp)/'link'; link.symlink_to(token)
                with patch.dict(os.environ, {'WEATHERLAB_TOKEN_FILE': str(link)}), self.assertRaises(ValueError): collect_weather_next3(NOW)
                opener.open.side_effect = RuntimeError('synthetic-test-token MUST NOT LEAK')
                with self.assertRaisesRegex(ValueError, '^weathernext3_request_error$'): collect_weather_next3(NOW)

    def test_oversized_and_redirect_response_rejected(self):
        with patch('kcdw.weathernext3._read_token', return_value='synthetic'):
            for status, body in ((302,b'{}'), (200,b'x'*(3*1024*1024+1)), (200,b'{bad')):
                response = MagicMock(); response.__enter__.return_value=response
                response.status=status; response.read.return_value=body
                opener=MagicMock(); opener.open.return_value=response
                with patch('kcdw.weathernext3.build_opener', return_value=opener), self.assertRaises(ValueError): collect_weather_next3(NOW)

    @unittest.skipUnless(os.environ.get('WEATHERLAB_LIVE_SMOKE') == '1', 'opt-in private live feed')
    def test_live_metadata_only(self):
        now = datetime.now(timezone.utc)
        data = collect_weather_next3(now)
        summary = summarize_weather_next3(data, now)
        print(json.dumps({'model': summary['model'], 'actual_run_utc': summary['actual_run_utc'], 'days': len(summary['days'])}))

if __name__ == '__main__': unittest.main()
