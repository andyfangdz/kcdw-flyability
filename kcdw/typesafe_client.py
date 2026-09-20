"""Bounded TypeSafe HTTP calls with private, reproducible request artifacts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-1.13.0'
MAX_REQUEST_BYTES = 180_000
MAX_RESPONSE_BYTES = 1_000_000


class TypeSafeError(ValueError):
    """Safe to log: never contains credentials or service response bodies."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise TypeSafeError('TypeSafe artifact directory is a symlink')
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(encoded(value) + '\n')


def _number(value, low=0, high=1):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise TypeSafeError('Invalid TypeSafe numeric answer')
    return value


def validate_response(response, questions, model):
    if not isinstance(response, dict) or response.get('model') != model:
        raise TypeSafeError('TypeSafe response model mismatch')
    answers = response.get('answers')
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise TypeSafeError('TypeSafe response has missing or unexpected answers')
    clean = {}
    for key, question in questions.items():
        answer = answers[key]
        kind = question['type']
        if not isinstance(answer, dict) or answer.get('type') != kind:
            raise TypeSafeError('TypeSafe answer type mismatch')
        if kind == 'noul':
            clean[key] = {'type': kind, 'noul': _number(answer.get('noul'))}
            continue
        criteria = question['criteria']
        options = set(criteria) if kind == 'choice' else {str(i) for i in range(len(criteria))}
        probabilities = answer.get('probabilities')
        if not isinstance(probabilities, dict) or set(probabilities) != options:
            raise TypeSafeError('TypeSafe distribution does not match criteria')
        values = {k: _number(v) for k, v in probabilities.items()}
        # The live API serializes probabilities to hundredths. Independent
        # rounding can move the total by up to half a hundredth per option.
        # Retain the returned values rather than silently renormalizing them.
        rounded = all(math.isclose(v * 100, round(v * 100), abs_tol=1e-8) for v in values.values())
        mass_tolerance = 0.005 * len(values) + 1e-9 if rounded else 0.002
        if not math.isclose(sum(values.values()), 1, abs_tol=mass_tolerance):
            raise TypeSafeError(f'TypeSafe distribution is not normalized (sum={sum(values.values()):.6f})')
        result = {'type': kind, 'probabilities': values, 'confidence': _number(answer.get('confidence'))}
        if kind == 'choice':
            choice = answer.get('choice')
            if choice not in options:
                raise TypeSafeError('Invalid TypeSafe choice')
            if values[choice] < max(values.values()) - 0.0001:
                raise TypeSafeError(f'TypeSafe choice disagrees with distribution (selected={values[choice]:.4f}, maximum={max(values.values()):.4f})')
            result['choice'] = choice
        elif kind == 'score':
            score = _number(answer.get('score'), 0, len(criteria) - 1)
            score_tolerance = max(0.025, 0.005 * sum(range(len(criteria))) + 0.005) if rounded else 0.025
            if abs(score - sum(int(k) * v for k, v in values.items())) > score_tolerance + 1e-9:
                raise TypeSafeError('TypeSafe score disagrees with distribution')
            if answer.get('legend') != {str(i): value for i, value in enumerate(criteria)}:
                raise TypeSafeError('TypeSafe score legend mismatch')
            result.update(score=score, legend=answer['legend'])
        else:
            raise TypeSafeError('Unsupported TypeSafe question')
        clean[key] = result
    usage = response.get('usage')
    if not isinstance(usage, dict):
        raise TypeSafeError('Missing TypeSafe token usage')
    usage = {k: usage[k] for k in ('input_tokens', 'output_tokens') if k in usage}
    if any(type(v) is not int or v < 0 for v in usage.values()):
        raise TypeSafeError('Invalid TypeSafe token usage')
    return {'model': model, 'answers': clean, 'usage': usage}


class Client:
    def __init__(self, key, artifacts, *, model=MODEL, transport=None, sleep=time.sleep):
        if not re.fullmatch(r'jev-\d+\.\d+\.\d+', model):
            raise TypeSafeError('Use a versioned KCDW_TYPESAFE_MODEL, not an alias')
        self.key, self.artifacts, self.model = key, Path(artifacts), model
        if transport is None:
            import requests
            transport = requests.post
        self.transport, self.sleep = transport, sleep
        self.calls = 0

    def evaluate(self, purpose, state, questions):
        if not re.fullmatch(r'[a-z0-9_-]{1,80}', purpose) or not questions:
            raise TypeSafeError('Invalid TypeSafe request purpose/questions')
        from .typesafe_packing import compact_state
        state = compact_state(state)
        # Live object requests used more context than equivalent compact JSON
        # text. Both formats are documented; large packets use the smaller one.
        if len(encoded(state).encode()) > 50_000:
            state = encoded(state)
        request = {'model': self.model, 'state': state, 'questions': questions}
        body = encoded(request).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise TypeSafeError('TypeSafe request exceeds local byte budget')
        self.calls += 1
        stem = self.artifacts / f'{self.calls:03d}-{purpose}'
        private_json(stem.with_suffix('.request.json'), request)
        started = time.monotonic()
        try:
            for attempt in range(2):
                with self.transport(ENDPOINT, data=body, headers={'Authorization': f'Bearer {self.key}',
                                    'Content-Type': 'application/json'}, timeout=(10, 60),
                                    allow_redirects=False, stream=True) as response:
                    if response.status_code in (429, 529) and attempt == 0:
                        self.sleep(2)
                        continue
                    if response.status_code != 200:
                        raise TypeSafeError(f'TypeSafe HTTP status {response.status_code}')
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise TypeSafeError('TypeSafe response exceeds byte budget')
                        chunks.append(chunk)
                    try:
                        result = validate_response(json.loads(b''.join(chunks)), questions, self.model)
                    except TypeSafeError as exc:
                        if attempt == 0:
                            # A transient service response can violate its own
                            # typed contract. Retry once, without relaxing it or
                            # selecting for a favorable weather judgment.
                            private_json(stem.with_suffix('.retry.json'), {'error': str(exc), 'request_sha256': digest(request)})
                            self.sleep(2)
                            continue
                        raise
                    result.update(request_sha256=digest(request), elapsed_seconds=round(time.monotonic() - started, 3))
                    private_json(stem.with_suffix('.response.json'), result)
                    return result
        except Exception as exc:
            # Service error bodies and transport exception messages can echo headers/state.
            message = str(exc) if isinstance(exc, TypeSafeError) else 'TypeSafe transport or JSON failure'
            private_json(stem.with_suffix('.error.json'), {'error': message, 'request_sha256': digest(request)})
            raise TypeSafeError(message) from None


def configured_client(artifacts, *, var=None):
    """Off by default; auto uses an available key, required fails if it is missing."""
    mode = os.environ.get('KCDW_TYPESAFE', 'off')
    if mode not in ('auto', 'off', 'required'):
        raise TypeSafeError('KCDW_TYPESAFE must be auto, off, or required')
    if mode == 'off':
        return None
    key = os.environ.get('TYPESAFE_API_KEY', '').strip()
    explicit = os.environ.get('TYPESAFE_API_KEY_FILE')
    path = Path(explicit) if explicit else Path(var or os.environ.get('VAR_DIR', 'var')) / 'typesafe-api-key'
    if not key and (explicit or path.exists()):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise TypeSafeError('TypeSafe key file must be a private regular file (mode 0600)')
            key = path.read_text().strip()
        except OSError:
            raise TypeSafeError('Cannot read TypeSafe key file') from None
    if not key:
        if mode == 'required':
            raise TypeSafeError('TypeSafe API key is required but not configured')
        return None
    if any(c.isspace() for c in key) or len(key) > 4096:
        raise TypeSafeError('Invalid TypeSafe API key format')
    return Client(key, artifacts, model=os.environ.get('KCDW_TYPESAFE_MODEL', MODEL))
