import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests.test_cloud_layer_signals import Client, SNAPSHOT, NOW
from kcdw import cloud_layer_signals as layers


def direct_status():
    init = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
    times = [datetime(2026, 9, 24, h, tzinfo=timezone.utc) for h in range(12, 22)]
    values = dict(sp=101000, r2=92, t2=285.15, d2=284.15, u10=3, v10=-4,
                  r1000=95, r925=96, r850=80, t1000=282.15, t925=281.15, t850=283.15,
                  h1000=120, h925=800, h850=1500)
    proof = {'model': 'ifs', 'initialization_time': init.isoformat().replace('+00:00','Z'),
             'samples': [{'lead': lead, 'fields': {k: {'value': v} for k, v in values.items()}}
                         for lead in (216,222,228)]}
    hourly = {'time': [t.isoformat().replace('+00:00','Z') for t in times],
              'surface_pressure': [1010]*len(times), 'relative_humidity_2m':[92]*len(times)}
    data = {'metadata': {'provenance':'direct-native','model_init_is_response_bound':True,
                         'source_provider':'ECMWF','initialization_time':proof['initialization_time'],
                         'native_proof':proof,'sampling':'Native steps, display interpolation.'},
            'hourly':hourly,'fetched_at':NOW.isoformat().replace('+00:00','Z'),
            'endpoint':'https://data.ecmwf.int/forecasts/','model':'ECMWF IFS', 'model_id':'ecmwf_ifs025'}
    return {'models':{'ifs':{'available':True,'data':data}}}


class DirectLayersTests(unittest.TestCase):
    def test_direct_profile_reuses_validated_snapshot_without_profile_fetch(self):
        client=Client();client.direct_native=True
        with patch('kcdw.event_moisture.validate_moisture', return_value=direct_status()):
            packet=layers.collect_layer_signals(client, SNAPSHOT, NOW)
            self.assertEqual(packet['version'],2)
            source=packet['profiles']['ifs']
            self.assertTrue(source['ok'])
            self.assertTrue(source['data']['metadata']['model_init_is_response_bound'])
            self.assertTrue(source['data']['points'][1]['thermal']['925_850']['inversion'])
            self.assertEqual(source['data']['points'][1]['levels']['850']['relative_humidity'],80)
            self.assertFalse(any('models=ecmwf_ifs025&' in url for url in client.urls if 'ensemble-api' not in url))
            again=layers.validate_layer_signals(packet,SNAPSHOT,NOW)
            self.assertEqual(again,packet)
            changed=copy.deepcopy(packet)
            changed['profiles']['ifs']['data']['points'][1]['levels']['850']['relative_humidity']=40
            result=layers.validate_layer_signals(changed,SNAPSHOT,NOW)
            self.assertFalse(result['profiles']['ifs']['ok'])
            self.assertTrue(result['ensembles']['gefs']['ok'])

    def test_legacy_layer_packet_is_unchanged(self):
        packet=layers.collect_layer_signals(Client(),SNAPSHOT,NOW)
        self.assertEqual(packet['version'],1)
        self.assertFalse(packet['profiles']['ifs']['data']['metadata']['model_init_is_response_bound'])
