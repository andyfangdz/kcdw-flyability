"""Native packet boundaries; raw fixture smoke is optional, never fabricated."""
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import unittest
import tempfile
from pathlib import Path
from kcdw import native_wind as nw
from kcdw import native_wind_worker as worker

NOW = datetime(2026, 9, 17, 16, tzinfo=timezone.utc)


def snapshot():
    s = {'collected_at': '2026-09-17T16:00:00Z',
         'event': {'slug':'test','date':'2026-09-24','window':'08-17','title':'Test','nav_label':'Test','description':'Test'},
         'event_timing': {'slug':'test','date':'2026-09-24','timezone':'America/New_York',
                          'flight_start':'10:00','flight_end':'12:00','flight_duration_minutes':120,
                          'appointment_start':'08:00','appointment_status':'confirmed','flight_status':'expected'},
         'event_moisture': {'models': {}}}
    for key, dataset in nw.DATASETS.items():
        meta = {'latest_advertised_init':'2026-09-17T06:00:00Z',
                'latest_advertised_available_at':'2026-09-17T14:00:00Z',
                'fetched_at':s['collected_at'],
                'endpoint':f'https://api.open-meteo.com/data/{dataset}/static/meta.json',
                'data_end_time':'2026-09-23T09:00:00Z' if key=='ifs' else '2026-10-02T12:00:00Z'}
        source={'ok':True,'data':{'fetched_at':s['collected_at'],'metadata':{'datasets':{dataset:meta}}}}
        if key=='gfs': s['gfs']=source
        else: s['event_moisture']['models'][key]=source
    return s


def samples(request):
    init=nw._time(request['init']); model=request['model']; out=[]
    for lead in request['leads']:
        fields={}
        for name in worker.CORE:
            identity=worker.identity(model,name,init,lead)
            fields[name]={'identity':identity,'value':-3.,'latitude':41.,'longitude':-74.25,
                          'proof':{'start':0,'end':99,'total':1000,'bytes':100,'sha256':'a'*64}}
        out.append({'at':nw.iso_z(init+timedelta(hours=lead)), 'lead':lead,
                    'url':worker.url_for(model,init,lead),'fields':fields})
    return out


def discovery(model, start, end, now, kind='wind'):
    """Upstream run newer than the deliberately stale Open-Meteo fixture."""
    import math
    init = NOW.replace(hour=12)
    cadence = 3 if model == 'gfs' else 6
    lo = math.floor((start-init).total_seconds()/3600/cadence)*cadence
    hi = math.ceil((end-init).total_seconds()/3600/cadence)*cadence
    return {'init': nw.iso_z(init), 'leads': list(range(lo, hi+1, cadence))}


def packet(tmp_path):
    s=snapshot()
    with patch.object(nw,'discover_run',side_effect=discovery,create=True), patch.object(nw,'_run_worker',side_effect=samples):
        p=nw.collect_native_wind(s,tmp_path,NOW)
    assert all(p['models'].values())
    return s,p


def legacy_packet():
    """Construct the persisted v1 format, without using the new collector."""
    s = snapshot()
    models = {}
    for key in nw.DATASETS:
        init, leads = nw._selection(s, key, NOW)
        rows = samples(dict(model=key, init=nw.iso_z(init), leads=leads))
        for row in rows:
            row['url'] = worker.url_for(key, init, row['lead'], legacy=True)
            for name, field in row['fields'].items():
                field['identity'] = worker.identity(key, name, init, row['lead'], legacy=True)
        models[key] = dict(model=key, init=nw.iso_z(init), fetched_at=s['collected_at'], samples=rows)
    return s, dict(version=1, snapshot_collected_at=s['collected_at'], window=nw._window(s, NOW), models=models)


class NativeWindTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_cache_retains_original_collection_validation(self):
        s,p=packet(self.tmp_path)
        for path in self.tmp_path.glob('wind-v2-*.json'):
            stored=json.loads(path.read_text())
            source=stored.get('source',stored)
            if source['model']=='gfs':
                source['fetched_at']='2026-09-17T16:01:00Z'
                path.write_text(json.dumps(stored))
        s['collected_at']='2026-09-17T17:00:00Z'
        with patch.object(nw,'discover_run',side_effect=discovery),patch.object(nw,'_run_worker',side_effect=samples) as run:
            fresh=nw.collect_native_wind(s,self.tmp_path,NOW+timedelta(hours=1))
        self.assertEqual(run.call_count,1)
        self.assertEqual(fresh['models']['gfs']['fetched_at'],s['collected_at'])
        self.assertEqual(fresh['models']['ifs']['fetched_at'],p['models']['ifs']['fetched_at'])

    def test_direct_discovery_ignores_open_meteo_and_uses_newer_run(self):
        for state in ('older', 'absent', 'unavailable', 'malformed'):
            s = snapshot()
            if state == 'absent':
                del s['gfs']
                del s['event_moisture']
            elif state == 'unavailable':
                s['gfs'] = {'ok': False, 'data': None}
                s['event_moisture'] = {'ok': False, 'models': {}}
            elif state == 'malformed':
                s['gfs']['data']['metadata'] = None
                s['event_moisture']['models']['ifs']['data']['fetched_at'] = '2026-01-01T00:00:00Z'
            with patch.object(nw, 'discover_run', side_effect=discovery) as discover, patch.object(nw, '_run_worker', side_effect=samples):
                p = nw.collect_native_wind(s, self.tmp_path, NOW)
            self.assertEqual(p['version'], 2)
            self.assertEqual(discover.call_count, 3)
            for call in discover.call_args_list:
                self.assertEqual(call.args[1:3], (nw._time('2026-09-24T14:00:00Z'), nw._time('2026-09-24T16:00:00Z')))
                self.assertEqual(call.kwargs, {'kind': 'wind'})
            self.assertTrue(all(m and m['init'] == '2026-09-17T12:00:00Z' for m in p['models'].values()))

    def test_v2_validation_and_evidence_are_network_free(self):
        s, p = packet(self.tmp_path)
        del s['gfs']
        del s['event_moisture']
        s['native_wind'] = p
        with patch.object(nw, 'discover_run', side_effect=AssertionError('no discovery'), create=True) as discover, patch.object(nw, '_run_worker', side_effect=AssertionError('no worker')) as run, patch.object(worker, 'fetch', side_effect=AssertionError('no network')) as fetch:
            self.assertTrue(all(nw.validate_native_wind(p, s, NOW)['models'].values()))
            self.assertTrue(all(nw.native_wind_evidence(s, NOW)['models'].values()))
        discover.assert_not_called()
        run.assert_not_called()
        fetch.assert_not_called()

    def test_v1_legacy_packets_keep_metadata_and_scda_semantics(self):
        s, p = legacy_packet()
        self.assertTrue(all(nw.validate_native_wind(p, s, NOW)['models'].values()))
        # A nearer mission keeps the archived IFS short cycle, including scda.
        s['event']['date'] = s['event_timing']['date'] = '2026-09-18'
        p['window'] = nw._window(s, NOW)
        for key in nw.DATASETS:
            init, leads = nw._selection(s, key, NOW)
            rows = samples(dict(model=key, init=nw.iso_z(init), leads=leads))
            for row in rows:
                row['url'] = worker.url_for(key, init, row['lead'], legacy=True)
                for name, field in row['fields'].items():
                    field['identity'] = worker.identity(key, name, init, row['lead'], legacy=True)
            p['models'][key].update(init=nw.iso_z(init), samples=rows)
        self.assertIn('/scda/', p['models']['ifs']['samples'][0]['url'])
        self.assertTrue(all(nw.validate_native_wind(p, s, NOW)['models'].values()))

    def test_new_ifs_oper_stream_all_cycles(self):
        for hour in (0, 6, 12, 18):
            init = NOW.replace(hour=hour)
            self.assertIn('/oper/', worker.url_for('ifs', init, 6))
            self.assertEqual(worker.identity('ifs', '10u', init, 6)['marsStream'], 'oper')
            rows = []
            for i, name in enumerate(worker.CORE):
                rows.append(dict(param=name if name.startswith('10') else name[0], levelist=None if name.startswith('10') else name[1:], date=init.strftime('%Y%m%d'), time=init.strftime('%H%M'), step='6', type='fc', domain='g', stream='oper', **{'class': 'od'}, levtype='sfc' if name.startswith('10') else 'pl', _offset=i*100, _length=100))
            self.assertEqual(set(worker.indexed_ranges('ifs', '\n'.join(json.dumps(r) for r in rows), init, 6)), set(worker.CORE))
            rows[0]['stream'] = 'scda'
            with self.assertRaises(ValueError):
                worker.indexed_ranges('ifs', '\n'.join(json.dumps(r) for r in rows), init, 6)
            if hour in (6, 18):
                for row in rows: row['stream'] = 'scda'
                self.assertEqual(set(worker.indexed_ranges('ifs', '\n'.join(json.dumps(r) for r in rows), init, 6, legacy=True)), set(worker.CORE))

    def test_v2_future_init_and_fetch_never_become_valid_later(self):
        s, original = packet(self.tmp_path)
        for mutation in ('fetch', 'init'):
            p = copy.deepcopy(original)
            m = p['models']['gfs']
            if mutation == 'fetch':
                m['fetched_at'] = '2026-09-17T16:01:00Z'
            else:
                m['init'] = '2026-09-17T18:00:00Z'
                m['fetched_at'] = m['init']
                m['samples'] = samples(dict(model='gfs', init=m['init'], leads=[168, 171, 174]))
            for when in (NOW, NOW+timedelta(hours=3)):
                checked = nw.validate_native_wind(p, s, when)
                self.assertIsNone(checked['models']['gfs'])
                self.assertIsNotNone(checked['models']['ifs'])

    def test_v2_short_ifs_cycle_does_not_claim_long_horizon(self):
        s, p = packet(self.tmp_path)
        init = NOW.replace(hour=6)
        p['models']['ifs'].update(init=nw.iso_z(init), samples=samples(dict(model='ifs', init=nw.iso_z(init), leads=[174, 180])))
        checked = nw.validate_native_wind(p, s, NOW)
        self.assertIsNone(checked['models']['ifs'])
        self.assertIsNotNone(checked['models']['gfs'])

    def test_v2_persisted_run_and_sample_axis_are_validated(self):
        s, original = packet(self.tmp_path)
        for stamp in ('2026-09-16T06:00:00Z', '2026-09-17T12:01:00Z', '2026-09-17T12:00:00+00:00'):
            p = copy.deepcopy(original)
            p['models']['gfs']['init'] = stamp
            self.assertIsNone(nw.validate_native_wind(p, s, NOW)['models']['gfs'])
        for mutation in ('missing', 'duplicate', 'mixed', 'url', 'hash', 'extra'):
            p = copy.deepcopy(original)
            m = p['models']['gfs']
            if mutation == 'missing': m['samples'].pop()
            elif mutation == 'duplicate': m['samples'][1] = m['samples'][0]
            elif mutation == 'mixed': m['samples'][1] = samples(dict(model='gfs', init='2026-09-17T06:00:00Z', leads=[177]))[0]
            elif mutation == 'url': m['samples'][0]['url'] += '?other=run'
            elif mutation == 'hash': m['samples'][0]['fields']['10u']['proof']['sha256'] = 'bad'
            else: m['samples'][0]['fields']['unknown'] = m['samples'][0]['fields']['10u']
            checked = nw.validate_native_wind(p, s, NOW)
            self.assertIsNone(checked['models']['gfs'], mutation)
            self.assertIsNotNone(checked['models']['ifs'], mutation)

    def test_discovery_precedes_cache_and_failure_never_reuses_old_run(self):
        s, original = packet(self.tmp_path)
        later = NOW+timedelta(hours=9)
        s['collected_at'] = nw.iso_z(later)
        def newest(model, start, end, now, kind='wind'):
            old = discovery(model, start, end, now, kind)
            return dict(init='2026-09-18T00:00:00Z', leads=[h-12 for h in old['leads']])
        with patch.object(nw, 'discover_run', side_effect=newest) as discover, patch.object(nw, '_run_worker', side_effect=samples) as run:
            p = nw.collect_native_wind(s, self.tmp_path, later)
        self.assertEqual(discover.call_count, 3)
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(m and m['init'] == '2026-09-18T00:00:00Z' for m in p['models'].values()))
        for failure in (None, RuntimeError('index offline')):
            def isolated(model, *args, **kwargs):
                if model == 'ifs':
                    if isinstance(failure, Exception): raise failure
                    return failure
                return newest(model, *args, **kwargs)
            with patch.object(nw, 'discover_run', side_effect=isolated), patch.object(nw, '_run_worker', side_effect=AssertionError('cache expected')) as run:
                failed = nw.collect_native_wind(s, self.tmp_path, later)
            run.assert_not_called()
            self.assertIsNone(failed['models']['ifs'])
            self.assertIsNotNone(failed['models']['gfs'])
            self.assertIsNotNone(failed['models']['aifs_single'])

    def test_ifs_gust_requires_real_six_hour_interval(self):
        import sys
        from unittest.mock import MagicMock
        init = NOW.replace(hour=12)
        # A three-hour maximum at lead 6 is not a six-hour maximum.
        meta = worker.identity('ifs', 'gust', init, 6)
        meta['startStep'] = 3
        ec = MagicMock()
        ec.codes_get.side_effect = lambda g, k: meta[k]
        raw = b'GRIB' + b'\x00'*4 + (20).to_bytes(8, 'big') + b'7777'
        with patch.dict(sys.modules, {'eccodes': ec}), self.assertRaises(ValueError):
            worker.decode(raw, 'ifs', 'gust', init, 6)
        # Lead zero cannot have a six-hour post-initialization maximum.
        with self.assertRaises(ValueError):
            worker.identity('ifs', 'gust', init, 0)

    def test_ifs_three_hour_gusts_inside_144h_build_a_verified_six_hour_maximum(self):
        init = NOW.replace(hour=12)
        now_field = worker.identity('ifs', 'gust3', init, 126)
        before = worker.identity('ifs', 'gust3_prev', init, 126)
        self.assertEqual((now_field['paramId'], now_field['startStep'], now_field['endStep'], now_field['stepType']), (228028, 123, 126, 'max'))
        self.assertEqual((before['startStep'], before['endStep']), (120, 123))
        self.assertEqual(before['validityTime'], int((init + timedelta(hours=123)).strftime('%H%M')))
        for bad in (('gfs', 'gust3', 126), ('aifs_single', 'gust3', 126), ('ifs', 'gust3', 3)):
            with self.assertRaises(ValueError):
                worker.identity(bad[0], bad[1], init, bad[2])
        with self.assertRaises(ValueError):
            worker.identity('ifs', 'gust3', init, 126, legacy=True)

        def with_gusts(request):
            rows = samples(request)
            if request['model'] == 'ifs':
                start = nw._time(request['init'])
                for row in rows:
                    for name, value in (('gust3', 12.2), ('gust3_prev', 10.5)):
                        row['fields'][name] = {'identity': worker.identity('ifs', name, start, row['lead']), 'value': value, 'latitude': 41.,
                                               'longitude': -74.25, 'proof': {'start': 0, 'end': 99, 'total': 1000, 'bytes': 100, 'sha256': 'b'*64}}
                    row['fields']['gust3_prev']['url'] = worker.url_for('ifs', start, row['lead'] - 3)
            return rows

        s = snapshot()
        with patch.object(nw, 'discover_run', side_effect=discovery, create=True), patch.object(nw, '_run_worker', side_effect=with_gusts):
            p = nw.collect_native_wind(s, self.tmp_path, NOW)
        s['native_wind'] = p
        self.assertIsNotNone(nw.validate_native_wind(p, s, NOW)['models']['ifs'])
        gust = nw.native_wind_evidence(s, NOW)['models']['ifs']['samples'][0]['gust']
        first = p['models']['ifs']['samples'][0]
        self.assertEqual(gust['kt'], round(12.2 * 1.943844492, 1))
        self.assertEqual(gust['semantics'], 'six-hour maximum from two three-hour maxima')
        self.assertEqual(gust['start'], nw.iso_z(nw._time(p['models']['ifs']['init']) + timedelta(hours=first['lead'] - 6)))
        # Only the half ending at the sample: an honest three-hour maximum.
        half = copy.deepcopy(p)
        for row in half['models']['ifs']['samples']:
            del row['fields']['gust3_prev']
        s['native_wind'] = half
        self.assertEqual(nw.native_wind_evidence(s, NOW)['models']['ifs']['samples'][0]['gust']['semantics'], 'three-hour maximum')
        # Tampering: a previous-step field without its sample half, a wrong file, or a wrong interval is rejected.
        for mutate in (lambda f: f.pop('gust3'), lambda f: f['gust3_prev'].update(url=first['url']),
                       lambda f: f['gust3_prev']['identity'].update(startStep=0), lambda f: f['gust3'].update(value=-1.0)):
            bad = copy.deepcopy(p)
            mutate(bad['models']['ifs']['samples'][0]['fields'])
            self.assertIsNone(nw.validate_native_wind(bad, s, NOW)['models']['ifs'])
        # GFS and AIFS packets never accept the ECMWF three-hour gust names.
        bad = copy.deepcopy(p)
        bad['models']['gfs']['samples'][0]['fields']['gust3'] = copy.deepcopy(first['fields']['gust3'])
        self.assertIsNone(nw.validate_native_wind(bad, s, NOW)['models']['gfs'])

    def test_gust_decode_mismatch_remains_unknown_without_losing_core(self):
        init = NOW.replace(hour=12)
        ranges = {name: (0, 99) for name in (*worker.CORE, 'gust')}
        core = samples(dict(model='ifs', init=nw.iso_z(init), leads=[6]))[0]['fields']
        def decode(raw, model, name, stamp, lead):
            if name == 'gust': raise ValueError('three-hour maximum')
            return {k: v for k, v in core[name].items() if k != 'proof'}
        with patch.object(worker, 'fetch', return_value=(b'index', core['10u']['proof'])), patch.object(worker, 'indexed_ranges', return_value=ranges), patch.object(worker, 'decode', side_effect=decode):
            row = worker.sample('ifs', init, 6)
        self.assertEqual(set(row['fields']), set(worker.CORE))

    def test_invalid_operational_timing_rejects_packet(self):
        tmp_path = self.tmp_path
        s, p = packet(tmp_path)
        for key, value in [('appointment_status', 'unconfirmed'), ('flight_status', 'confirmed')]:
            changed = copy.deepcopy(s)
            changed['event_timing'][key] = value
            assert nw.validate_native_wind(p, changed, NOW) is None
        changed = copy.deepcopy(s)
        changed['event']['window'] = '13-17'
        assert nw.validate_native_wind(p, changed, NOW) is None

    def test_future_at_collection_never_becomes_valid_later(self):
        s,p=legacy_packet()
        for change in ('cache','source','metadata'):
            bad_s,bad_p=copy.deepcopy(s),copy.deepcopy(p)
            if change=='cache':bad_p['models']['gfs']['fetched_at']='2026-09-17T17:00:00Z'
            elif change=='source':bad_s['gfs']['data']['fetched_at']='2026-09-17T17:00:00Z'
            else:bad_s['gfs']['data']['metadata']['datasets']['ncep_gfs025']['fetched_at']='2026-09-17T17:00:00Z'
            for when in (NOW,NOW+timedelta(hours=1)):
                checked=nw.validate_native_wind(bad_p,bad_s,when)
                self.assertIsNone(checked['models']['gfs'])
                self.assertIsNotNone(checked['models']['ifs'])

    def test_context_actual_metadata_and_bracket(self):
        s = snapshot()
        assert nw._selection(s, 'gfs', NOW)[1] == [174, 177, 180]
        assert nw._selection(s, 'ifs', NOW) == (datetime(2026, 9, 17, tzinfo=timezone.utc), [180, 186])
        assert nw._selection(s, 'aifs_single', NOW)[1] == [174, 180]

    def test_reject_bad_source_only(self):
        tmp_path = self.tmp_path
        for mutation in ['units', 'run', 'param', 'level', 'range', 'future', 'stale', 'identity']:
            with self.subTest(mutation=mutation):
                s, p = packet(tmp_path)
                m = p['models']['gfs']
                f = m['samples'][0]['fields']['10u']
                if mutation == 'units':
                    f['identity']['units'] = 'kt'
                elif mutation == 'run':
                    f['identity']['dataTime'] = 0
                elif mutation == 'param':
                    f['identity']['paramId'] = 166
                elif mutation == 'level':
                    f['identity']['level'] = 925
                elif mutation == 'range':
                    f['proof']['bytes'] = 101
                elif mutation == 'future':
                    m['fetched_at'] = '2026-09-17T17:00:00Z'
                elif mutation == 'stale':
                    m['fetched_at'] = '2026-09-17T03:00:00Z'
                else:
                    m['model'] = 'ifs'
                v = nw.validate_native_wind(p, s, NOW)
                assert v['models']['gfs'] is None
                assert v['models']['ifs'] is not None

    def test_binding_and_evidence(self):
        tmp_path = self.tmp_path
        s, p = packet(tmp_path)
        s['native_wind'] = p
        e = nw.native_wind_evidence(s, NOW)
        assert len(json.dumps(e).encode()) <= 5000
        assert len(e['models']['gfs']['samples']) == 3
        changed = copy.deepcopy(s)
        changed['event_timing']['flight_start'] = '11:00'
        assert nw.validate_native_wind(p, changed, NOW) is None
        changed = copy.deepcopy(s)
        changed['collected_at'] = '2026-09-17T15:59:00Z'
        assert nw.validate_native_wind(p, changed, NOW) is None

    def test_cache_no_timestamp_renewal_and_source_isolation(self):
        tmp_path = self.tmp_path
        s, p = packet(tmp_path)
        later = NOW + timedelta(hours=1)
        s['collected_at'] = nw.iso_z(later)
        with patch.object(nw, 'discover_run', side_effect=discovery) as discover, patch.object(nw, '_run_worker', side_effect=AssertionError('cache expected')) as run:
            cached = nw.collect_native_wind(s, tmp_path, later)
        self.assertEqual(discover.call_count, 3)
        run.assert_not_called()
        assert cached['models']['gfs']['fetched_at'] == p['models']['gfs']['fetched_at']
        with patch.object(nw, 'discover_run', side_effect=discovery), patch.object(nw, '_run_worker', side_effect=RuntimeError('offline')):
            stale = nw.collect_native_wind(s, tmp_path, NOW + timedelta(hours=13))
        assert not any(stale['models'].values())
        with patch.object(nw, 'discover_run', side_effect=discovery), patch.object(nw, '_run_worker', side_effect=lambda r: (_ for _ in ()).throw(ValueError()) if r['model'] == 'ifs' else samples(r)):
            fresh = nw.collect_native_wind(snapshot(), tmp_path / 'fresh', NOW)
        assert fresh['models']['ifs'] is None and fresh['models']['gfs']

    def test_old_future_run_and_malformed_metadata(self):
        for stamp in ('2026-09-16T06:00:00Z', '2026-09-17T18:00:00Z', '2026-09-17T06:01:00Z'):
            s = snapshot()
            s['gfs']['data']['metadata']['datasets']['ncep_gfs025']['latest_advertised_init'] = stamp
            with self.assertRaises(ValueError):
                nw._selection(s, 'gfs', NOW)

    def test_expired_cache_is_not_returned_after_refresh_failure(self):
        tmp_path = self.tmp_path
        s, p = packet(tmp_path)
        later = NOW + timedelta(hours=13)
        s['collected_at'] = nw.iso_z(later)
        for key in nw.DATASETS:
            src = s['gfs'] if key == 'gfs' else s['event_moisture']['models'][key]
            src['data']['fetched_at'] = nw.iso_z(later)
            src['data']['metadata']['datasets'][nw.DATASETS[key]]['fetched_at'] = nw.iso_z(later)
        assert nw._selection(s, 'gfs', later)
        with patch.object(nw, 'discover_run', side_effect=discovery), patch.object(nw, '_run_worker', side_effect=RuntimeError('offline')) as run:
            failed = nw.collect_native_wind(s, tmp_path, later)
        assert run.called and failed['models']['gfs'] is None

    def test_http_range_requires_206_exact_range_and_length(self):
        for status, header, body in [(200, 'bytes 0-3/10', b'abcd'), (206, 'bytes 1-4/10', b'abcd'), (206, 'bytes 0-3/10', b'abc'), (206, 'bytes 0-3/3', b'abcd')]:
            with self.subTest(status=status, header=header, body=body):
                from unittest.mock import MagicMock
                response = MagicMock()
                response.__enter__.return_value = response
                response.status_code = status
                response.headers = {'Content-Range': header}
                response.iter_content.return_value = [body]
                with patch('requests.get', return_value=response), self.assertRaises(ValueError):
                    worker.fetch('https://example.invalid', 4, (0, 3))

    def test_decoder_rejects_actual_identity_mismatch(self):
        for field, bad in [('units', 'kt'), ('dataTime', 0), ('paramId', 166), ('level', 925), ('validityTime', 1800), ('stepType', 'avg')]:
            with self.subTest(field=field, bad=bad):
                import sys
                from unittest.mock import MagicMock
                init = datetime(2026, 9, 17, 6, tzinfo=timezone.utc)
                meta = worker.identity('gfs', '10u', init, 177)
                meta[field] = bad
                ec = MagicMock()
                ec.codes_get.side_effect = lambda g, k: meta[k]
                raw = b'GRIB' + b'\x00' * 4 + 20 .to_bytes(8, 'big') + b'7777'
                with patch.dict(sys.modules, {'eccodes': ec}), self.assertRaises(ValueError):
                    worker.decode(raw, 'gfs', '10u', init, 177)
                ec.codes_release.assert_called_once()

    def test_index_rejects_wrong_run_and_duplicate(self):
        init = datetime(2026, 9, 17, 6, tzinfo=timezone.utc)
        text = '1:0:d=2026091700:UGRD:10 m above ground:174 hour fcst:\n2:100:d=2026091700:VGRD:10 m above ground:174 hour fcst:'
        with self.assertRaises(ValueError):
            worker.indexed_ranges('gfs', text, init, 174)
