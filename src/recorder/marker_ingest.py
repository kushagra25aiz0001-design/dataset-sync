"""
Marker Ingest — network endpoint for a *separate* task app to sync to the recorder
==================================================================================
The stimulus/task interface is intentionally a **separate process** (often on a
separate device, e.g. an iPad browser). To line its events up with the physiology
*without coupling the two codebases*, it POSTs each event here and the recorder
stamps it on the **master clock at receipt** — exactly the same guarantee the
in-process session runner gets. The event also carries the sender's own clock
reading (`t_device_s`), so the true device↔master offset/drift can be fit offline
(`sync_markers.estimate_offset`) and the small network latency removed.

This is the "app emits events to recorder" path (strongly preferred over aligning
by OCR-ing an on-screen counter, which stays available as a fallback).

Transport is plain HTTP on the stdlib `http.server` — no Flask/extra deps — so any
device (browser `fetch`, curl, a phone app) can hit it. CORS is open so an iPad
browser can POST cross-origin.

Endpoints
---------
    POST /mark    body: {"label": "...", "t_device_s"?: float, "source"?: str, ...}
                  → 200 {"ok": true, "t_master_s": .., "offset_s": ..}
                  → 409 {"ok": false, "reason": "not_recording"}   (no active session)
    GET  /time    → {"t_master_s": ..}   (server's current master-clock time)
    GET  /health  → whatever `health_fn()` returns (recording/session/sensors)

`mark_fn` and `health_fn` are injected, so this server is recorder-agnostic and
unit-testable with a plain callable.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


class MarkerIngestServer:
    def __init__(self, mark_fn: Callable[..., Optional[dict]],
                 host: str = '0.0.0.0', port: int = 8181,
                 health_fn: Optional[Callable[[], dict]] = None,
                 master_clock_fn: Optional[Callable[[], Optional[float]]] = None):
        """
        Args:
            mark_fn: called mark_fn(label, source=?, t_device_s=?, **payload); should
                     return the marker row dict, or None if there is no active
                     session (→ 409). The recorder's `mark` satisfies this.
            health_fn: optional () -> dict for GET /health.
            master_clock_fn: optional () -> current master time for GET /time.
        """
        self.mark_fn = mark_fn
        self.health_fn = health_fn
        self.master_clock_fn = master_clock_fn
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        server = self                                   # closure for the handler

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):                  # silence default stderr spam
                pass

            def _cors(self):
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')

            def _json(self, code: int, obj: dict):
                body = json.dumps(obj).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self._cors()
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                if self.path.startswith('/time'):
                    t = server.master_clock_fn() if server.master_clock_fn else None
                    self._json(200, {'t_master_s': t})
                elif self.path.startswith('/health'):
                    self._json(200, server.health_fn() if server.health_fn else {'ok': True})
                else:
                    self._json(404, {'ok': False, 'reason': 'not_found'})

            def do_POST(self):
                if not self.path.startswith('/mark'):
                    self._json(404, {'ok': False, 'reason': 'not_found'})
                    return
                try:
                    n = int(self.headers.get('Content-Length', 0))
                    payload = json.loads(self.rfile.read(n) or b'{}')
                    if not isinstance(payload, dict):
                        raise ValueError('body must be a JSON object')
                    label = payload.pop('label')
                except Exception as e:
                    self._json(400, {'ok': False, 'reason': f'bad request: {e}'})
                    return
                source = payload.pop('source', 'task')
                t_device_s = payload.pop('t_device_s', None)
                try:
                    row = server.mark_fn(label, source=source,
                                         t_device_s=t_device_s, **payload)
                except Exception as e:
                    self._json(500, {'ok': False, 'reason': str(e)[:160]})
                    return
                if row is None:
                    self._json(409, {'ok': False, 'reason': 'not_recording'})
                    return
                self._json(200, {'ok': True,
                                 't_master_s': row.get('t_master_s'),
                                 'offset_s': row.get('offset_s')})

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]       # resolve if port was 0
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name='marker-ingest')
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
