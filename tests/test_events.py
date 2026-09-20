from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kcdw import event_ensemble
from kcdw.cloud_publish import event_bundle
from kcdw.event_ensemble import MODELS, collect_event, share_percentages
from kcdw.event_renderer import render, summary
from kcdw.event_update import archive_run
from kcdw.events import Event, find_event, load_events, upcoming_events
from kcdw.renderer import event_nav_links

NOW = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)
EVENT = Event(slug="commercial-checkride", title="Commercial checkride", date="2026-09-24", window="08-17",
              nav_label="Checkride · Sep 24", description="Practical test.")


def synthetic_response(spec, hours=96, rain_members=(), start=None):
    """Flat members with rain at fixed event timestamps, independent of display range."""
    start = int((start or datetime(2026, 9, 22, 4, tzinfo=timezone.utc)).timestamp())
    rain_start = int(datetime(2026, 9, 24, 16, tzinfo=timezone.utc).timestamp())
    rain_end = int(datetime(2026, 9, 24, 21, tzinfo=timezone.utc).timestamp())
    times = [start + 3600 * i for i in range(hours)]
    hourly: dict = {"time": times}
    units: dict = {"time": "unixtime"}
    for variable in event_ensemble.VARIABLES:
        for member in range(spec.members):
            key = variable if member == 0 else f"{variable}_member{member:02d}"
            units[key] = event_ensemble.UNITS[variable]
            if (variable == "wind_gusts_10m" and spec.key in ("aifs_ens", "geps")) or (variable == "cloud_cover_low" and spec.key in ("gefs", "geps")):
                hourly[key] = [None] * hours
            elif variable == "precipitation":
                hourly[key] = [1.0 if member in rain_members and rain_start <= t <= rain_end else 0.0 for t in times]
            elif variable == "pressure_msl":
                hourly[key] = [1020.0 + member % 5 for _ in range(hours)]
            elif variable == "cloud_cover_low":
                hourly[key] = [80.0 if member == 1 else 10.0 for _ in range(hours)]
            else:
                hourly[key] = [5.0 + (member % 3) for _ in range(hours)]
    return {"latitude": 41.0, "longitude": -74.5, "timezone": "GMT", "utc_offset_seconds": 0, "hourly_units": units, "hourly": hourly}


class FakeClient:
    def __init__(self, broken=(), rain=()):
        self.broken, self.rain, self.urls = set(broken), rain, []

    def get(self, url):
        self.urls.append(url)
        if "static/meta.json" in url:
            return {"last_run_initialisation_time": 1789214400, "last_run_availability_time": 1789239194,
                    "temporal_resolution_seconds": 10800, "update_interval_seconds": 21600, "data_end_time": 1790607600}
        model_id = url.split("models=")[1].split("&")[0]
        spec = next(s for s in MODELS if s.model_id == model_id)
        if spec.key in self.broken:
            raise RuntimeError("upstream down")
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(url).query)
        start = datetime.fromisoformat(query['start_hour'][0]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(query['end_hour'][0]).replace(tzinfo=timezone.utc)
        hours = int((end-start).total_seconds()/3600) + 1
        return synthetic_response(spec, hours=hours, start=start, rain_members=self.rain)


class EventConfigTests(unittest.TestCase):
    def test_repository_events_file_loads_and_navigates(self):
        events = load_events()
        self.assertTrue(events)
        checkride = find_event("commercial-checkride")
        self.assertEqual(checkride.date, "2026-09-24")
        self.assertEqual(checkride.path(), "/events/commercial-checkride")
        self.assertEqual(checkride.days_out(NOW), 12)
        self.assertIn("Checkride", event_nav_links(NOW))
        far_future = datetime(2027, 6, 1, tzinfo=timezone.utc)
        self.assertEqual(upcoming_events(far_future), [])
        self.assertEqual(event_nav_links(far_future), "")

    def test_invalid_events_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            for bad in ({"slug": "Bad Slug"}, {"window": "17-08"}, {"date": "2026-13-01"}, {"title": "x" * 81}):
                path.write_text(json.dumps({"events": [EVENT.as_dict() | bad]}))
                with self.assertRaises(ValueError):
                    load_events(path)
            path.write_text(json.dumps({"events": [EVENT.as_dict(), EVENT.as_dict()]}))
            with self.assertRaises(ValueError):
                load_events(path)
            path.write_text("[]")
            self.assertEqual(event_nav_links(NOW, path), "")


class EventEnsembleTests(unittest.TestCase):
    def test_collects_validates_and_reduces_members(self):
        client = FakeClient(rain=(0, 2, 3))  # member 1 is the cloudy member; rain must not mask it
        snapshot = collect_event(client, EVENT, NOW)
        self.assertEqual(snapshot["days_out"], 12)
        self.assertTrue(all(model["ok"] for model in snapshot["models"].values()))
        gefs = snapshot["models"]["gefs"]["data"]
        self.assertEqual(gefs["window"]["members_complete"], 31)
        self.assertEqual(gefs["window"]["counts"]["rain"], 3)
        self.assertEqual(sum(gefs["window"]["percent"].values()), 100)
        self.assertFalse(gefs["window"]["has_low_cloud_field"])
        self.assertEqual(gefs["window"]["low_cloud_mean_percent"], None)
        ecmwf = snapshot["models"]["ecmwf_ens"]["data"]
        self.assertTrue(ecmwf["window"]["has_low_cloud_field"])
        self.assertEqual(ecmwf["window"]["counts"]["dry_low_cloud"], 1)
        self.assertEqual(ecmwf["hourly"]["wind_gusts_10m"]["members_with_data"], 51)
        aifs = snapshot["models"]["aifs_ens"]["data"]
        self.assertEqual(aifs["hourly"]["wind_gusts_10m"]["members_with_data"], 0)
        self.assertEqual(len(gefs["hourly"]["time"]), 336)
        # 31 members cycle 1020..1024 (7,6,6,6,6); the 16th sorted value is 1022.
        self.assertEqual(gefs["hourly"]["pressure_msl"]["p50"][0], 1022.0)
        self.assertEqual(gefs["hourly"]["pressure_msl"]["p10"][0], 1020.0)
        self.assertEqual(gefs["hourly"]["pressure_msl"]["p90"][0], 1024.0)
        self.assertTrue(all("start_hour=2026-09-12T04%3A00" in url for url in client.urls if "v1/ensemble" in url))

    def test_hourly_rain_fans_preserve_amounts_and_missing_samples(self):
        spec = MODELS[0]
        raw = synthetic_response(spec, rain_members=range(16))
        raw['hourly']['precipitation'] = [None] * 96
        times, members = event_ensemble._validate(raw, spec, 96)
        hourly = event_ensemble._hourly_stats(times, members)
        rain = hourly['precipitation']
        self.assertEqual(rain['sample_counts'][60], 30)
        self.assertEqual(rain['p10'][60], 0)
        self.assertEqual(rain['p50'][60], .5)
        self.assertEqual(rain['p90'][60], 1)
        self.assertEqual(rain['p50'][61], .5)
        self.assertEqual(rain['p90'][59], 0)
        self.assertEqual(rain['sample_counts'], hourly['rain_sample_counts'])

    def test_persisted_rain_fans_validate_but_older_archives_work(self):
        from copy import deepcopy
        snapshot = collect_event(FakeClient(rain=range(16)), EVENT, NOW)
        self.assertIn('precipitation', snapshot['models']['gefs']['data']['hourly'])
        event_ensemble.validate_snapshot(snapshot)
        for value in (-1, float('nan'), 501):
            broken = deepcopy(snapshot)
            broken['models']['gefs']['data']['hourly']['precipitation']['p90'][60] = value
            with self.assertRaises(ValueError):
                event_ensemble.validate_snapshot(broken)
        broken = deepcopy(snapshot)
        broken['models']['gefs']['data']['hourly']['precipitation']['p50'].pop()
        with self.assertRaises(ValueError):
            event_ensemble.validate_snapshot(broken)
        for source in snapshot['models'].values():
            source['data']['hourly'].pop('precipitation')
        event_ensemble.validate_snapshot(snapshot)

    def test_one_broken_model_does_not_fail_the_event(self):
        snapshot = collect_event(FakeClient(broken={"geps"}), EVENT, NOW)
        self.assertFalse(snapshot["models"]["geps"]["ok"])
        self.assertIn("upstream down", snapshot["models"]["geps"]["error"])
        self.assertTrue(snapshot["models"]["gefs"]["ok"])
        with self.assertRaises(RuntimeError):
            collect_event(FakeClient(broken={s.key for s in MODELS}), EVENT, NOW)

    def test_validation_rejects_bad_contracts(self):
        spec = MODELS[0]
        for mutate in (
            lambda r: r["hourly"].__setitem__("pressure_msl", r["hourly"]["pressure_msl"][:-1]),
            lambda r: r["hourly"]["pressure_msl"].__setitem__(3, 2000.0),
            lambda r: r["hourly"]["pressure_msl"].__setitem__(3, float("nan")),
            lambda r: r["hourly_units"].__setitem__("precipitation", "inch"),
            lambda r: r["hourly"].__setitem__("unexpected_field", [0.0] * 96),
            lambda r: r.__setitem__("latitude", 20.0),
            lambda r: r.__setitem__("timezone", "America/New_York"),
            lambda r: r["hourly"].pop("pressure_msl_member30"),
        ):
            raw = synthetic_response(spec)
            mutate(raw)
            with self.assertRaises(ValueError):
                event_ensemble._validate(raw, spec, 96)

    def test_share_percentages_sum_to_100(self):
        for counts, total in (({"a": 38, "b": 8, "c": 5}, 51), ({"a": 1, "b": 1, "c": 1}, 3), ({"a": 0, "b": 0}, 0)):
            shares = share_percentages(counts, total)
            self.assertEqual(sum(shares.values()), 100 if total else 0)
        self.assertEqual(share_percentages({"a": 23, "b": 8}, 31), {"a": 74, "b": 26})


class EventRendererTests(unittest.TestCase):
    def test_renders_page_with_charts_and_scenarios(self):
        snapshot = collect_event(FakeClient(rain=(0,)), EVENT, NOW)
        html, health = render(snapshot, NOW)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertEqual(html.count("<svg"), 6)
        self.assertIn('data-page="event"', html)
        self.assertIn("Commercial checkride", html)
        self.assertIn('aria-current="page">Checkride · Sep 24', html)
        self.assertIn("CMC GEPS", html)
        self.assertIn("Missing low cloud is unavailable", html)
        self.assertIn('id="official-title">Before departure', html)
        self.assertEqual(health["generated_at"], snapshot["collected_at"])
        self.assertEqual(health["event"], "commercial-checkride")
        self.assertFalse(health["stale"])
        self.assertIn("no calibrated flyability probability", summary(snapshot))
        self.assertNotIn("Leaning favorable", html)
        self.assertNotIn("All members", html)
        self.assertEqual(health["status"], "provenance_unverified")

    def test_stale_fetch_and_stale_source_are_not_current(self):
        snapshot = collect_event(FakeClient(), EVENT, NOW)
        snapshot["models"]["gefs"]["data"]["metadata"]["fresh"] = False
        html, health = render(snapshot, NOW + timedelta(hours=9))
        self.assertTrue(health["stale"])
        self.assertIn("stale or unknown", html)
        self.assertNotIn("CURRENT", html)

    def test_interval_boundary_and_missing_cloud(self):
        spec = MODELS[0]
        raw = synthetic_response(spec)
        # 08 Eastern endpoint is previous hour: exclude; 17 endpoint include.
        raw["hourly"]["precipitation"][56] = 100
        raw["hourly"]["precipitation"][65] = 1
        times, members = event_ensemble._validate(raw, spec, 96)
        window = event_ensemble._window_scenarios(EVENT, times, members)
        self.assertEqual(window["window_hours"], 9)
        self.assertEqual(window["rain_total_mm"]["max"], 1)
        self.assertEqual(window["counts"]["dry_light_wind"], 0)
        self.assertEqual(window["counts"]["cloud_unknown"], 31)

    def test_axis_grid_member_and_gaps(self):
        spec = MODELS[0]
        for mutate in (
            lambda r: r.__setitem__("latitude", float("nan")),
            lambda r: r["hourly_units"].__setitem__("time", "iso8601"),
            lambda r: r["hourly"].__setitem__("pressure_msl_member00", r["hourly"]["pressure_msl"]),
            lambda r: r["hourly"].__setitem__("pressure_msl_member99", r["hourly"].pop("pressure_msl_member30")),
            lambda r: r["hourly"]["precipitation"].__setitem__(1, None),
            lambda r: r["hourly"].__setitem__("time", [x+3600 for x in r["hourly"]["time"]]),
        ):
            raw = synthetic_response(spec); mutate(raw)
            with self.assertRaises(ValueError):
                event_ensemble._validate(raw, spec, 96, datetime(2026,9,22,4,tzinfo=timezone.utc))

    def test_failed_model_is_disclosed_and_escaped(self):
        snapshot = collect_event(FakeClient(broken={"geps"}), EVENT, NOW)
        snapshot["models"]["geps"]["error"] = "<script>alert(1)</script>"
        html, _ = render(snapshot, NOW)
        self.assertIn("CMC GEPS</strong> unavailable", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("3 of 4 systems", html)


class EventPublishTests(unittest.TestCase):
    def test_archive_and_bundle_round_trip(self):
        snapshot = collect_event(FakeClient(), EVENT, NOW)
        html, health = render(snapshot, NOW)
        with tempfile.TemporaryDirectory() as directory:
            var = Path(directory)
            archive = archive_run(var, EVENT.slug, "20260912T200000Z.test", snapshot, html, health, summary(snapshot))
            self.assertTrue((var / "events" / EVENT.slug / "current").is_symlink())
            bundle = event_bundle(archive)
            self.assertEqual(bundle["slug"], EVENT.slug)
            self.assertEqual(bundle["assessed_at"], snapshot["collected_at"])
            self.assertEqual(set(bundle["event"]), {"slug", "title", "date", "window", "nav_label"})
            self.assertNotIn("description", bundle["event"])
            self.assertLess(len(json.dumps(bundle)), 800_000)
            with self.assertRaises(FileExistsError):
                archive_run(var, EVENT.slug, "20260912T200000Z.test", snapshot, html, health, "x")
            manifest = json.loads((archive / "manifest.json").read_text())
            manifest["kind"] = "report"
            (archive / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                event_bundle(archive)


def setUpModule():
    from unittest.mock import patch
    global _wn3_offline
    _wn3_offline = patch.object(event_ensemble, "collect_weather_next3", side_effect=RuntimeError("offline test"), create=True)
    _wn3_offline.start()


def tearDownModule():
    _wn3_offline.stop()


def synthetic_wn3():
    from kcdw.common import iso_z
    from test_weathernext3 import fixture
    data = fixture()
    data['status']['fetched_at'] = iso_z(NOW)
    for name, value in (("temperature_2m", 17.123456789), ("precipitation_1h", 0.123456789),
                        ("sea_level_pressure", 101234.56789), ("wind_speed_10m", 4.123456789)):
        field = data['forecast']['fields'][name]
        field.update(mean=[value]*360, p10=[value*.99]*360, p90=[value*1.01]*360)
    return data


class WeatherNext3EventTests(unittest.TestCase):
    def test_real_adapter_validation_and_fallback_provenance(self):
        from unittest.mock import patch
        from kcdw.common import iso_z
        envelope = synthetic_wn3()
        status = envelope["status"]
        status.update(requested_init_utc=iso_z(NOW.replace(hour=18)),
                      fetched_at=iso_z(NOW-timedelta(minutes=10)), fallback=True, state="degraded")
        envelope['forecast']['requested_init_utc'] = iso_z(NOW.replace(hour=18))
        with patch.object(event_ensemble, "collect_weather_next3", return_value=envelope):
            snapshot = collect_event(FakeClient(), EVENT, NOW)
        markup, health = render(snapshot, NOW)
        self.assertTrue(health["weathernext3"]["available"])
        self.assertEqual(health["weathernext3"]["fallback"], "earlier run than requested")
        self.assertIn("2026-09-12T19:50:00Z", markup)
        self.assertIn("2026-09-12T18:00:00Z", markup)
        envelope["explicit_last_good"] = True
        _, health = render(snapshot, NOW)
        self.assertFalse(health["weathernext3"]["available"])

    def test_near_term_not_preferred_and_no_raw_bundle(self):
        from unittest.mock import patch
        envelope = synthetic_wn3()
        with patch.object(event_ensemble, "collect_weather_next3", return_value=envelope):
            snapshot = collect_event(FakeClient(), EVENT, NOW)
        markup, health = render(snapshot, NOW)
        with tempfile.TemporaryDirectory() as directory:
            archive = archive_run(Path(directory), EVENT.slug, "wn3-test", snapshot, markup, health, summary(snapshot))
            public = json.dumps(event_bundle(archive))
            self.assertNotIn("101234.56789", public)
            self.assertNotIn("valid_time_utc", public)
            self.assertNotIn("precipitation_1h", public)
        _, health = render(snapshot, datetime(2026, 9, 23, tzinfo=timezone.utc))
        self.assertEqual(health["preferred_source"], "official near-term aviation guidance")

    def test_aifs_fallback_precedes_legacy_weather_next2(self):
        from kcdw.event_renderer import _planning_panel
        snapshot = collect_event(FakeClient(), EVENT, NOW)
        snapshot['weathernext2'] = {'ok': True}
        self.assertEqual(_planning_panel(snapshot, NOW)[2], 'ECMWF AIFS-ENS')

    def test_outage_is_independent_and_disclosed(self):
        snapshot = collect_event(FakeClient(), EVENT, NOW)
        self.assertFalse(snapshot["weathernext3"]["ok"])
        markup, health = render(snapshot, NOW)
        self.assertIn("WeatherNext 3 unavailable", markup)
        self.assertEqual(health["preferred_source"], "ECMWF AIFS-ENS")
        self.assertTrue(snapshot["models"]["aifs_ens"]["ok"])

    def test_derived_diagnostic_provenance_and_no_raw_publication(self):
        from unittest.mock import patch
        envelope = synthetic_wn3()
        with patch.object(event_ensemble, "collect_weather_next3", return_value=envelope), patch.object(event_ensemble, "validate_weather_next3", create=True) as validate:
            snapshot = collect_event(FakeClient(), EVENT, NOW)
            markup, health = render(snapshot, NOW)
            validate.assert_called_with(envelope, NOW)
        self.assertEqual(health["preferred_source"], "Google WeatherNext 3")
        self.assertIn('id="wn3-diagnostic"', markup)
        self.assertIn("Checkride planning caution", markup)
        self.assertIn("Model mean", markup)
        self.assertIn("Percentile signal", markup)
        self.assertIn("2026-09-12T12:00:00Z", markup)
        self.assertIn("https://", markup.split("Google attribution")[1])
        self.assertIn("not an event probability", markup)
        for raw in ("17.123456789", "101234.56789", "4.123456789", "valid_time_utc", "precipitation_1h", '"fields"'):
            self.assertNotIn(raw, markup + json.dumps(health))
        self.assertIn('data-comparison-field="rain"', markup)

    def test_render_validation_failure_falls_back_not_benign(self):
        from unittest.mock import patch
        snapshot = collect_event(FakeClient(), EVENT, NOW)
        snapshot["weathernext3"] = {"ok": True, "data": synthetic_wn3(), "error": None}
        with patch.object(event_ensemble, "validate_weather_next3", side_effect=ValueError("raw secret series"), create=True):
            markup, health = render(snapshot, NOW)
        self.assertFalse(health["weathernext3"]["available"])
        self.assertEqual(health["preferred_source"], "ECMWF AIFS-ENS")
        self.assertIn("validation failed", markup)
        self.assertNotIn("raw secret series", markup + json.dumps(health))

    def test_aged_snapshot_revalidates_wn3_at_render_time(self):
        from unittest.mock import patch
        envelope = synthetic_wn3()
        with patch.object(event_ensemble, 'collect_weather_next3', return_value=envelope):
            snapshot = collect_event(FakeClient(), EVENT, NOW)
        markup, health = render(snapshot, NOW)
        self.assertTrue(health['weathernext3']['available'])
        self.assertEqual(health['preferred_source'], 'Google WeatherNext 3')
        self.assertIn('id="wn3-numbers"', markup)
        markup, health = render(snapshot, NOW + timedelta(hours=72))
        self.assertTrue(health['stale'])
        self.assertFalse(health['weathernext3']['available'])
        self.assertEqual(health['preferred_source'], 'ECMWF AIFS-ENS')
        self.assertNotIn('id="wn3-numbers"', markup)
        self.assertNotIn('<g data-model="wn3"', markup)
        self.assertIn('WeatherNext 3 unavailable', markup)

    def test_numerical_wn3_charts_and_event_statistics(self):
        from unittest.mock import patch
        envelope = synthetic_wn3()
        with patch.object(event_ensemble, 'collect_weather_next3', return_value=envelope):
            snapshot = collect_event(FakeClient(), EVENT, NOW)
        markup, health = render(snapshot, NOW)
        self.assertIn('id="wn3-numbers"', markup)
        self.assertEqual(markup.count('data-comparison-field='), 6)
        comparison = markup.split('id="multimodel-comparison"', 1)[1].split('</section>', 1)[0]
        dedicated = markup.split('id="wn3-numbers"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(comparison.count('<g data-model="wn3"'), 5)
        self.assertEqual(dedicated.count('<g data-model="wn3"'), 2)
        self.assertIn('aria-label="WN3 / Dew point"', markup)
        self.assertIn('aria-label="Low-cloud fraction', comparison)
        self.assertIn('aria-label="Temperature"', markup)
        self.assertIn('Mean event rainfall', markup)
        self.assertIn('Hourly p10–p90', markup)
        self.assertIn('not event-total percentiles', markup)
        self.assertLess(markup.index('id="wn3-numbers"'), markup.index('Forecast context / per-model distributions'))
        envelope['forecast']['fields']['wind_speed_10m']['mean'][0] = None
        markup, health = render(snapshot, NOW)
        self.assertNotIn('id="wn3-numbers"', markup)
        self.assertFalse(health['weathernext3']['available'])

    def test_wind_includes_opening_and_excludes_closing_instant(self):
        from unittest.mock import patch
        envelope = synthetic_wn3()
        with patch.object(event_ensemble, 'collect_weather_next3', return_value=envelope):
            snapshot = collect_event(FakeClient(), EVENT, NOW)
        axis = envelope['forecast']['valid_time_utc']
        opening = axis.index('2026-09-24T12:00:00Z')
        closing = axis.index('2026-09-24T21:00:00Z')
        field = envelope['forecast']['fields']['wind_speed_10m']
        for stat in ('mean', 'p10', 'p90'): field[stat][opening] = 20
        _, health = render(snapshot, NOW)
        self.assertEqual(health['weathernext3']['wind_mean'], 'planning trigger reached')
        self.assertEqual(health['weathernext3']['wind_signal'], 'lower and upper percentiles reach trigger')
        for stat in ('mean', 'p10', 'p90'):
            field[stat][opening] = 1
            field[stat][closing] = 20
        _, health = render(snapshot, NOW)
        self.assertEqual(health['weathernext3']['wind_mean'], 'below planning trigger')
        self.assertEqual(health['weathernext3']['wind_signal'], 'percentile range below trigger')

    def test_complete_window_and_preceding_hour_semantics(self):
        from unittest.mock import patch
        envelope = synthetic_wn3()
        snapshot = collect_event(FakeClient(), EVENT, NOW)
        snapshot["weathernext3"] = {"ok": True, "data": envelope, "error": None}
        axis = envelope["forecast"]["valid_time_utc"]
        rain = envelope["forecast"]["fields"]["precipitation_1h"]
        boundary = axis.index("2026-09-24T12:00:00Z")
        rain["mean"][boundary] = 99
        with patch.object(event_ensemble, "validate_weather_next3", create=True):
            _, health = render(snapshot, NOW)
            self.assertEqual(health["weathernext3"]["precipitation_mean"], "below planning trigger")
            rain["mean"][axis.index("2026-09-24T21:00:00Z")] = 3
            _, health = render(snapshot, NOW)
            self.assertEqual(health["weathernext3"]["precipitation_mean"], "planning trigger reached")
            axis.remove("2026-09-24T21:00:00Z")
            markup, health = render(snapshot, NOW)
            self.assertFalse(health["weathernext3"]["available"])
            self.assertIn("incomplete event window", markup)


if __name__ == "__main__":
    unittest.main()
