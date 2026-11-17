"""面板 HTTP 服务：只读 state.json，绑 127.0.0.1，外面走 SSH 隧道。"""

from __future__ import annotations

import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("leadmon.panel")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_handler(state_path: str, html_path: str, stale_after: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "leadmon"
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - 标准库约定
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                try:
                    with open(html_path, "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, b"panel.html missing", "text/plain; charset=utf-8")
                return

            if path == "/api/state":
                try:
                    with open(state_path, "rb") as fh:
                        self._send(200, fh.read(), "application/json; charset=utf-8")
                except FileNotFoundError:
                    self._send(503, b'{"error":"state not ready"}', "application/json")
                except OSError as exc:
                    self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
                return

            if path == "/healthz":
                try:
                    age = time.time() - os.path.getmtime(state_path)
                    ok = age <= stale_after
                    body = {"ok": ok, "state_age_sec": round(age, 1)}
                    self._send(200 if ok else 503, json.dumps(body).encode(), "application/json")
                except OSError:
                    self._send(503, b'{"ok":false}', "application/json")
                return

            self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):  # 静音，访问日志没价值
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(state_path: str, html_path: str, bind: str, port: int, stale_after: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind, port), make_handler(state_path, html_path, stale_after))
    httpd.daemon_threads = True
    return httpd
