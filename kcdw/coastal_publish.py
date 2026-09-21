"""Upload a completed EE comparison, then atomically attach it to an event."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .cloud_publish import Client, publish_event
from .coastal_maps import validate
from .common import atomic_write, load_json

MAX_MAP_BYTES = 4_000_000


def public_manifest(manifest):
    result = {k:v for k,v in manifest.items() if k != 'frames'}
    result['frames'] = [{**{k:v for k,v in f.items() if k != 'file'},
        'url': f"/events/{manifest['event_slug']}/maps/{f['sha256']}.png"} for f in manifest['frames']]
    return validate(result, {'slug': result['event_slug'], 'date': result['event_date']})


def upload_frame(client, slug, frame):
    path = Path(frame['file'])
    if path.stat().st_size > MAX_MAP_BYTES:
        raise ValueError('Rendered PNG exceeds upload limit')
    png = path.read_bytes()
    if hashlib.sha256(png).hexdigest() != frame['sha256'] or png[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', png[16:24]) != (frame['width'], frame['height']):
        raise ValueError('Rendered PNG failed integrity checks')
    route = f"/events/{slug}/maps/{frame['sha256']}.png"
    request = Request(client.url+'/api'+route, data=png, headers={
        'Authorization': 'Bearer '+client.token, 'Content-Type': 'image/png',
        'User-Agent': 'KCDW-Flyability-Publisher/1.0'})
    for attempt in range(3):
        try:
            with client.opener.open(request, timeout=60) as response:
                confirmation = json.loads(response.read(4096))
            if confirmation != {'stored': True, 'sha256': frame['sha256'], 'width': frame['width'], 'height': frame['height']}:
                raise ValueError('Worker did not confirm map integrity')
            with client.opener.open(Request(client.url+route, headers={'Authorization': 'Bearer '+client.token,
                    'User-Agent': 'KCDW-Flyability-Publisher/1.0'}), timeout=60) as response:
                fetched = response.read(MAX_MAP_BYTES+1)
            if hashlib.sha256(fetched).hexdigest() != frame['sha256']:
                raise ValueError('Published PNG checksum mismatch')
            return route
        except HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise RuntimeError(f'Map upload failed: HTTP {error.code}') from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError('Map upload failed after retries') from None
        time.sleep(2**attempt)


def publish(manifest_path, var, config):
    manifest = load_json(manifest_path)
    public = public_manifest(manifest)
    slug = public['event_slug']
    client = Client(load_json(config))
    for index, frame in enumerate(manifest['frames']):
        upload_frame(client, slug, frame)
        print(json.dumps({'uploaded': index+1, 'total': len(manifest['frames']), 'model': frame['model'], 'run': frame['run'], 'valid': frame['valid']}), flush=True)
    # Use the same lock as scheduled event refreshes, and preserve the observation clock.
    with (var/'events-update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        from .event_renderer import render
        from .event_update import archive_run, summary
        current = var/'events'/slug/'current'
        snapshot = load_json(current/'snapshot.json')
        validate(public, snapshot['event'])
        snapshot['coastal_maps'] = public
        now = datetime.now(timezone.utc)
        html, health = render(snapshot, now)
        if len(html.encode()) > 790_000:
            raise ValueError('Event with maps exceeds page budget')
        run_id = now.strftime('%Y%m%dT%H%M%SZ')+f'.maps-{os.getpid()}'
        archive = archive_run(var, slug, run_id, snapshot, html, health, summary(snapshot))
        publish_event(client, archive)
        if client.request(f'/events/{slug}/health.json').get('run_id') != run_id:
            raise RuntimeError('Map archive was stored, but the live event pointer did not advance')
        atomic_write(var/'events'/slug/'coastal-maps.json', json.dumps(public, indent=2)+'\n')
        print(json.dumps({'published': client.url+'/events/'+slug+'#coastal-low', 'run_id': run_id, 'frames': len(public['frames'])}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--var', type=Path, default=Path('var'))
    parser.add_argument('--config', type=Path, default=Path('var/cloudflare.json'))
    args = parser.parse_args()
    publish(args.manifest, args.var, args.config)


if __name__ == '__main__':
    main()
