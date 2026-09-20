"""Replay archived evidence with optional source ablation; never publishes."""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

from .common import load_json
from .prompt import build_prompt
from .radar_evidence import validate_radar_evidence


def prepare_replay(archive, output, omit):
    snapshot = load_json(archive / 'snapshot.json')
    unknown = set(omit) - snapshot['sources'].keys()
    if unknown:
        raise ValueError(f'unknown sources: {sorted(unknown)}')
    output.mkdir(parents=True, exist_ok=False)
    for key in omit:
        snapshot['sources'][key] = {'status': 'failed', 'error': 'Withheld for source-ablation replay'}
    (output / 'snapshot.json').write_text(json.dumps(snapshot, indent=2) + '\n')
    prompt = build_prompt(snapshot)
    prompt += '\nREPLAY MODE: Use only the supplied archived evidence and attached images. Do not search the web, access the network, or inspect other files. Sources withheld for ablation are unavailable. Preserve the original assessment time.\n'
    (output / 'prompt.txt').write_text(prompt)
    if 'radar_mosaic' not in omit and (archive / 'radar').is_dir():
        shutil.copytree(archive / 'radar', output / 'radar')
    (output / 'replay.json').write_text(json.dumps({'archive': str(archive.resolve()), 'omitted_sources': omit, 'published': False}, indent=2))
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--omit', action='append', default=[])
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    snapshot = prepare_replay(args.archive, args.output, args.omit)
    if not args.run:
        return
    # Validation binds image attachments to the retained snapshot.
    _, frames = validate_radar_evidence(snapshot, args.output / 'radar')
    from . import claude_agent
    from .prose_agent import Session
    schema = json.loads(Path('schema/analysis.schema.json').read_text())
    schema['properties'].pop('assessment', None)
    for key in ('score', 'confidence_score'):
        schema['$defs']['day']['properties'].pop(key, None)
    schema = claude_agent.cli_schema(schema)
    # Replays stay offline: radar frames are readable, web research is not.
    Session().run((args.output / 'prompt.txt').read_text(), schema, args.output / 'analysis.json', args.output / 'codex.log',
                     tools=('Read',) if frames else (), images=frames, timeout=720)
    subprocess.run(['python3', '-m', 'kcdw.renderer', str(args.output / 'snapshot.json'), str(args.output / 'analysis.json'), '--now', snapshot['collected_at'], '--output', str(args.output / 'index.html'), '--health', str(args.output / 'health.json')], check=True)


if __name__ == '__main__':
    main()
