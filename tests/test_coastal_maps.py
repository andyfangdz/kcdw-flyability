import copy
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from kcdw.coastal_maps import load, render, validate
from kcdw.coastal_publish import public_manifest, upload_frame


def fixture():
    runs = ['2026-09-19T12:00:00Z', '2026-09-20T12:00:00Z']
    times = ['2026-09-24T12:00:00Z', '2026-09-24T18:00:00Z']
    frames = [{'model': 'wn3', 'run': r, 'valid': t, 'lead': 120-24*i+6*j,
        'sha256': 'a'*64, 'width': 1800, 'height': 1725, 'file': '/private/local/map.png'}
        for i,r in enumerate(runs) for j,t in enumerate(times)]
    return {'version': 1, 'event_slug': 'commercial-checkride', 'event_date': '2026-09-24',
        'prepared_at': '2026-09-21T00:00:00Z', 'runs': runs, 'times': times,
        'models': [{'id': 'wn3', 'name': 'WeatherNext 3', 'resolution': '0.1°', 'statistic': 'Ensemble mean',
            'source_url': 'https://developers.google.com/weathernext/guides/earth-engine'}],
        'palette': ['ffffff']*13, 'frames': frames}


EVENT = {'slug': 'commercial-checkride', 'date': '2026-09-24'}


class CoastalMapsTests(unittest.TestCase):
    def test_public_manifest_removes_local_paths_and_round_trips(self):
        data = public_manifest(fixture())
        self.assertNotIn('/private/', json.dumps(data))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'maps.json'
            self.assertIsNone(load(path, EVENT))
            path.write_text(json.dumps(data))
            self.assertEqual(load(path, EVENT), data)

    def test_reject_incomplete_duplicate_misaligned_and_cross_event_maps(self):
        data = public_manifest(fixture())
        mutations = [lambda d:d['frames'].pop(),
            lambda d:d['frames'].append(copy.deepcopy(d['frames'][0])),
            lambda d:d['frames'][0].update(lead=1),
            lambda d:d['frames'][0].update(width=1900),
            lambda d:d.update(event_date='2026-09-25'),
            lambda d:d['frames'][0].update(url='https://elsewhere.example/map.png')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(data)
                mutate(bad)
                with self.assertRaises(ValueError):
                    validate(bad, EVENT)
                self.assertEqual(render(bad, EVENT), '')

    def test_server_render_has_real_fallback_accessible_controls_and_source_times(self):
        html = render(public_manifest(fixture()), EVENT)
        self.assertIn('Valid Thu Sep 24 · 8 AM EDT', html)
        self.assertIn('F096', html)
        self.assertIn('loading="lazy"', html)
        self.assertIn('data-map-run', html)
        self.assertIn('data-map-time', html)
        self.assertIn('<noscript>', html)
        self.assertNotIn('/private/', html)
        self.assertIn('saved comparison', html)

    def test_corrupt_png_never_reaches_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'map.png'
            path.write_bytes(b'corrupt')
            frame = fixture()['frames'][0] | {'file': str(path)}
            with self.assertRaises(ValueError):
                upload_frame(None, EVENT['slug'], frame)

    def test_four_model_layout_discloses_gfs_pressure_import_and_preserves_controls(self):
        data = fixture()
        for model_id, name in (('wn2','WeatherNext 2'),('ifs','ECMWF IFS'),('gfs','NOAA GFS')):
            data['models'].append(data['models'][0] | {'id':model_id,'name':name})
            data['frames'].extend(f | {'model':model_id} for f in list(data['frames']) if f['model']=='wn3')
        html = render(public_manifest(data),EVENT)
        self.assertIn('4 models · 2 runs · 2 times',html)
        self.assertIn('coastal-grid-four',html)
        self.assertIn('Expand NOAA GFS map',html)
        self.assertIn('matching NOAA sea-level pressure imported without resampling',html)
        self.assertIn('https://www.nco.ncep.noaa.gov/pmb/products/gfs/',html)
        self.assertEqual(html.count('class="coastal-card"'),4)

    def test_binary_publisher_preserves_production_headers_and_verifies_readback(self):
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6VLsAAAAASUVORK5CYII=')
        digest = hashlib.sha256(png).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'map.png'
            path.write_bytes(png)
            frame = {'file':str(path), 'width':1, 'height':1, 'sha256':digest}
            confirmation = json.dumps({'stored':True, 'sha256':digest, 'width':1, 'height':1}).encode()
            client = SimpleNamespace(url='https://weather.example', token='test-token', opener=Mock())
            client.opener.open.side_effect = [BytesIO(confirmation), BytesIO(png)]
            self.assertEqual(upload_frame(client, EVENT['slug'], frame), f"/events/{EVENT['slug']}/maps/{digest}.png")
            for call in client.opener.open.call_args_list:
                request = call.args[0]
                self.assertEqual(request.get_header('User-agent'), 'KCDW-Flyability-Publisher/1.0')
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')
            client.opener.open.side_effect = [BytesIO(confirmation), BytesIO(b'wrong pixels')]
            with self.assertRaisesRegex(ValueError, 'Published PNG checksum mismatch'):
                upload_frame(client, EVENT['slug'], frame)
