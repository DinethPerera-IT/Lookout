"""Read-only HTTP server: JSON API, Server-Sent Events and the static dashboard.

Read-only is enforced, not promised:
- SQLite is opened with mode=ro, so the connection physically can't write.
- Only GET (and HEAD) are handled; every other method gets 405.
- Static files come from one directory, with no directory listings and no path escapes.
"""
import json
import mimetypes
import re
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import api
from .scrub import make_scrubber

STATIC = Path(__file__).resolve().parent / "static"
SSE_POLL = 2.0
SSE_KEEPALIVE = 15.0
SECURITY_HEADERS = {
    # No inline scripts, no third-party anything: the page works offline and can't leak.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def connect_ro(path):
    """A connection that can only read. A missing file is an error, never a new database."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


class Handler(BaseHTTPRequestHandler):
    server_version = "Lookout"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet: no client addresses in the journal
        pass

    def _send(self, code, body, content_type, extra=None):
        self.send_response(code)
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        body = json.dumps(self.server.scrub(obj)).encode()
        self._send(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _refuse(self):
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

    # --- routing ----------------------------------------------------------
    ROUTES = [
        (re.compile(r"/api/status"), "status"),
        (re.compile(r"/api/incidents"), "incidents"),
        (re.compile(r"/api/incidents/(\d+)"), "incident"),
        (re.compile(r"/api/runs/latest"), "runs_latest"),
        (re.compile(r"/api/benchmark"), "benchmark"),
        (re.compile(r"/api/stream"), "stream"),
    ]

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            for pattern, name in self.ROUTES:
                m = pattern.fullmatch(path)
                if m:
                    return self._api(name, *m.groups())
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return self._static(path)

    def _api(self, name, *groups):
        if name == "benchmark":
            data = api.benchmark(self.server.bench_dir)
            return self._json(200, data) if data else self._json(404, {"error": "no benchmark results"})
        if name == "stream":
            return self._stream()
        try:
            conn = connect_ro(self.server.db_path)
        except sqlite3.Error:
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no data yet"})
        try:
            if name == "status":
                data = api.status(conn, time.time())
            elif name == "incidents":
                data = api.incidents(conn)
            elif name == "incident":
                data = api.incident(conn, int(groups[0]))
                if data is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such incident"})
            else:
                data = api.runs_latest(conn)
            return self._json(200, data)
        except sqlite3.Error:  # e.g. a state DB from before the dashboard existed
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no data yet"})
        finally:
            conn.close()

    def _stream(self):
        """Server-Sent Events: push the status whenever a run finishes or an incident changes."""
        self.send_response(200)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last_sig, last_write = None, 0.0
        try:
            while not self.server.stopping.is_set():
                try:
                    conn = connect_ro(self.server.db_path)
                    try:
                        sig = api.signature(conn)
                        data = api.status(conn, time.time()) if sig != last_sig else None
                    finally:
                        conn.close()
                except sqlite3.Error:
                    sig, data = None, None
                if data is not None:
                    last_sig = sig
                    self.wfile.write(b"event: update\ndata: " + json.dumps(self.server.scrub(data)).encode() + b"\n\n")
                    self.wfile.flush()
                    last_write = time.time()
                elif time.time() - last_write > SSE_KEEPALIVE:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_write = time.time()
                self.server.stopping.wait(SSE_POLL)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser went away

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC) or not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})


def make_server(db_path, host="127.0.0.1", port=8088, bench_dir=api.BENCH_DIR, extra_names=()):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.db_path = db_path
    server.bench_dir = bench_dir
    server.scrub = make_scrubber(extra_names)
    server.stopping = threading.Event()
    return server
