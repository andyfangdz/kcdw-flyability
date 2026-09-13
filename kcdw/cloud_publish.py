"""Publish validated results and retrieve previous assessments from the R2-backed Worker."""
import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .common import atomic_write, load_json
from .validation import validate_analysis

MAX_BYTES = 1_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the publisher credential to a redirect target.


class Client:
    def __init__(self, config):
        self.url = config['url'].rstrip('/')
        parts = urlsplit(self.url)
        if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost')):
            raise ValueError('Worker URL must use HTTPS')
        if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
            raise ValueError('Worker URL must be an origin')
        token_file = Path(config['token_file'])
        if token_file.stat().st_mode & 0o077:
            raise ValueError('Publisher token file must be private (mode 600)')
        self.token = token_file.read_text().strip()
        if not self.token:
            raise ValueError('Empty publisher token')
        self.opener = build_opener(NoRedirect())

    def request(self, path, data=None):
        payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False).encode() if data is not None else None
        if payload and len(payload) > MAX_BYTES:
            raise ValueError('Report exceeds upload limit')
        request = Request(self.url + path, data=payload, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
            'User-Agent': 'KCDW-Flyability-Publisher/1.0',
        })
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=40) as result:
                    raw = result.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('Worker response exceeds limit')
                return json.loads(raw)
            except HTTPError as error:
                if error.code < 500 or attempt == 2:
                    raise RuntimeError(f'Worker request failed: HTTP {error.code}') from None
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise RuntimeError('Worker request failed after retries') from None
            time.sleep(2 ** attempt)


def bundle(archive):
    manifest = load_json(archive / 'manifest.json')
    if manifest.get('status') != 'validated':
        raise ValueError('Only validated runs can be published')
    snapshot = load_json(archive / 'snapshot.json')
    analysis = load_json(archive / 'analysis.json')
    validate_analysis(analysis, snapshot)
    health = load_json(archive / 'public/health.json')
    if health['generated_at'] != snapshot['collected_at']:
        raise ValueError('Health assessment time mismatch')
    return {'version': 1, 'run_id': manifest['run_id'], 'assessed_at': snapshot['collected_at'],
            'html': (archive / 'public/index.html').read_text(), 'health': health,
            'analysis': analysis, 'changes': load_json(archive / 'changes.json')}


def publish(client, archive):
    data = bundle(archive)
    result = client.request('/api/publish', data)
    if result.get('stored') is not True or result.get('run_id') != data['run_id']:
        raise ValueError('Worker did not confirm report storage')
    atomic_write(archive / 'cloud-publication.json', json.dumps(result | {'worker_url': client.url}, indent=2) + '\n')
    print(json.dumps(result))


def event_bundle(archive):
    manifest = load_json(archive / 'manifest.json')
    if manifest.get('status') != 'validated' or manifest.get('kind') != 'event':
        raise ValueError('Only validated event runs can be published')
    snapshot = load_json(archive / 'snapshot.json')
    from .event_ensemble import validate_snapshot
    validate_snapshot(snapshot)
    health = load_json(archive / 'health.json')
    if health['generated_at'] != snapshot['collected_at']:
        raise ValueError('Event health assessment time mismatch')
    event = snapshot['event']
    return {'version': 1, 'slug': event['slug'], 'run_id': manifest['run_id'], 'assessed_at': snapshot['collected_at'],
            'html': (archive / 'index.html').read_text(), 'health': health, 'summary': manifest['summary'],
            'event': {key: event[key] for key in ('slug', 'title', 'date', 'window', 'nav_label')}}


def publish_event(client, archive):
    data = event_bundle(archive)
    result = client.request('/api/events/' + quote(data['slug'], safe='') + '/publish', data)
    if result.get('stored') is not True or result.get('run_id') != data['run_id']:
        raise ValueError('Worker did not confirm event storage')
    # Read back the immutable HTML and history before recording publication.
    path = '/events/' + quote(data['slug'], safe='')
    with client.opener.open(Request(client.url + path + '/runs/' + quote(data['run_id'], safe=''), headers={'User-Agent': 'KCDW-Flyability-Publisher/1.0'}), timeout=40) as response:
        html = response.read(MAX_BYTES + 1).decode()
    if data['html'].split('<body', 1)[0] not in html or data['run_id'] not in {r['run_id'] for r in client.request('/api/events/' + quote(data['slug'], safe='') + '/history')['reports']}:
        raise ValueError('Published event readback mismatch')
    health = client.request(path + '/health.json')
    if result.get('latest') and health.get('run_id') != data['run_id']:
        raise ValueError('Published event pointer readback mismatch')
    atomic_write(archive / 'cloud-publication.json', json.dumps(result | {'worker_url': client.url}, indent=2) + '\n')
    print(json.dumps(result))


def publish_events_index(client, events, now):
    from .common import iso_z
    payload = {'version': 1, 'updated_at': iso_z(now),
               'events': [{key: getattr(event, key) for key in ('slug', 'title', 'date', 'window', 'nav_label')} for event in events]}
    result = client.request('/api/events/index', payload)
    if result.get('stored') is not True or result.get('events') != len(events):
        raise ValueError('Worker did not confirm event index storage')
    if client.request('/api/events').get('events') != payload['events']:
        raise ValueError('Event index readback mismatch')
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('CLOUD_PUBLISH_CONFIG', 'var/cloudflare.json')))
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('publish'); p.add_argument('archive', type=Path)
    p = sub.add_parser('publish-event'); p.add_argument('archive', type=Path)
    sub.add_parser('publish-events-index')
    p = sub.add_parser('backfill'); p.add_argument('runs', type=Path)
    p = sub.add_parser('previous'); p.add_argument('output', type=Path)
    p = sub.add_parser('history'); p.add_argument('--output', type=Path)
    p = sub.add_parser('fetch'); p.add_argument('run_id'); p.add_argument('output', type=Path)
    args = parser.parse_args()
    client = Client(load_json(args.config))
    if args.command == 'publish':
        publish(client, args.archive)
    elif args.command == 'publish-event':
        publish_event(client, args.archive)
    elif args.command == 'publish-events-index':
        from datetime import datetime
        from .common import UTC
        from .events import load_events
        publish_events_index(client, load_events(), datetime.now(UTC))
    elif args.command == 'backfill':
        for archive in sorted(args.runs.iterdir()):
            if not (archive / 'manifest.json').is_file():
                continue
            if load_json(archive / 'manifest.json').get('status') == 'validated':
                publish(client, archive)
    elif args.command == 'previous':
        data = client.request('/api/latest')
        if data['analysis']['source_collected_at'] != data['assessed_at']:
            raise ValueError('Previous assessment time mismatch')
        atomic_write(args.output, json.dumps(data['analysis'], indent=2) + '\n')
    elif args.command == 'fetch':
        data = client.request('/reports/' + quote(args.run_id, safe='') + '/analysis.json')
        if data.get('run_id') != args.run_id:
            raise ValueError('Historical report ID mismatch')
        atomic_write(args.output, json.dumps(data, indent=2) + '\n')
    else:
        records, cursor = [], None
        while True:
            data = client.request('/api/history' + ('?cursor=' + quote(cursor, safe='') if cursor else ''))
            records.extend(data['reports'])
            cursor = data.get('cursor')
            if not cursor:
                break
        text = json.dumps(records, indent=2) + '\n'
        if args.output:
            atomic_write(args.output, text)
        else:
            print(text, end='')


if __name__ == '__main__':
    main()
