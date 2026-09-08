"""Serve public artifacts only; compute health from assessment age per request."""
import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .common import UTC, parse_time


def current_public(var, fallback):
    current = var / "current"
    return current.resolve(strict=True) / "public" if current.is_symlink() else fallback


def live_health(data, now=None):
    now = now or datetime.now(UTC)
    age = max(0, int((now - parse_time(data["generated_at"])).total_seconds()))
    stale = age > data.get("stale_after", 5400)
    return data | {"age_seconds": age, "stale": stale, "status": "stale" if stale else "ok"}


def handler(var, public):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            path = urlsplit(self.path).path
            name = {"/": "index.html", "/index.html": "index.html", "/health.json": "health.json"}.get(path)
            if name is None:
                self.send_error(404)
                return
            try:
                root = current_public(var, public)
                if name == "health.json":
                    payload = json.dumps(live_health(json.loads((root / name).read_text()))).encode()
                else:
                    payload = (root / name).read_bytes()
            except (OSError, ValueError, KeyError, TypeError):
                self.send_error(503, "Report unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if name.endswith("json") else "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not head:
                self.wfile.write(payload)
    return Handler


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8794)
    p.add_argument("--var", type=Path, default=Path("var"))
    p.add_argument("--public", type=Path, default=Path("public"))
    a = p.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), handler(a.var, a.public)).serve_forever()


if __name__ == "__main__":
    main()
