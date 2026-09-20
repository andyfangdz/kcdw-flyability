"""Prefer Claude; use Codex when Claude explicitly exhausts its quota or credits."""
import json
from pathlib import Path

from . import claude_agent, codex_agent


class Session:
    def __init__(self):
        self.backend = claude_agent

    @property
    def provenance(self):
        return {'provider': self.backend.PROVIDER, 'model': self.backend.MODEL}

    def run(self, prompt, schema, output, log, **kwargs):
        try:
            result = self.backend.run(prompt, schema, output, log, **kwargs)
        except claude_agent.UsageLimitError:
            self.backend = codex_agent
            with Path(log).open('a', encoding='utf-8') as handle:
                handle.write('\n' + json.dumps({'event': 'prose_fallback', 'reason': 'claude_usage_limit', **self.provenance}) + '\n')
            result = self.backend.run(prompt, schema, output, log, **kwargs)
        with Path(log).open('a', encoding='utf-8') as handle:
            handle.write('\n' + json.dumps({'event': 'prose_complete', **self.provenance}) + '\n')
        return result
