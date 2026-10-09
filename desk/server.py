"""Dashboard server — stdlib only. Serves dashboard/ and two endpoints:

    GET  /api/state            the board JSON (see board.py)
    POST /api/kill             {"switch": "flat_kill" | "entry_kill" | "jev_kill", "on": true|false}

Binds to 127.0.0.1 by default. If you expose it (e.g. on a VPS), set DASHBOARD_TOKEN
and the kill switches will require the header `X-Desk-Token: <token>`.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Kills

STATIC = Path(__file__).resolve().parent.parent / "dashboard"


class Board:
    """Thread-safe holder the trading loop writes and the server reads."""

    def __init__(self, kills_path: Path):
        self._state: dict | None = None
        self._lock = threading.Lock()
        self.kills_path = kills_path

    def set(self, state: dict) -> None:
        with self._lock:
            self._state = state

    def get(self) -> dict | None:
        with self._lock:
            return self._state


def make_handler(board: Board):
    token = os.environ.get("DASHBOARD_TOKEN", "")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(STATIC), **kw)

        def log_message(self, *args):  # keep the trading log readable
            pass

        def _json(self, code: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                state = board.get()
                return self._json(200 if state else 503, state or {"error": "desk is starting"})
            return super().do_GET()

        def do_POST(self):
            if not self.path.startswith("/api/kill"):
                return self._json(404, {"error": "not found"})
            if token and self.headers.get("X-Desk-Token") != token:
                return self._json(403, {"error": "bad token"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                switch, on = body["switch"], bool(body["on"])
                if switch not in ("flat_kill", "entry_kill", "jev_kill"):
                    raise ValueError(switch)
            except (KeyError, ValueError, TypeError):
                return self._json(400, {"error": "expected {switch: flat_kill|entry_kill|jev_kill, on: bool}"})
            kills = Kills.load(board.kills_path)
            setattr(kills, switch, on)
            kills.save(board.kills_path)
            return self._json(200, kills.__dict__)

    return Handler


def serve(board: Board, host: str = "127.0.0.1", port: int = 8787, background: bool = True):
    httpd = ThreadingHTTPServer((host, port), make_handler(board))
    if background:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd
    httpd.serve_forever()
