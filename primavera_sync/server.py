"""Tiny dashboard server (stdlib only). Run: python -m primavera_sync.server <base.db>"""
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import analytics, db

WEB = Path(__file__).resolve().parent.parent / "web"

ROUTES = {
    "/api/summary": analytics.summary,
    "/api/monthly": analytics.monthly,
    "/api/customers": analytics.by_customer,
    "/api/open": analytics.open_invoices,
}


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ROUTES:
                conn = db.connect(db_path)
                try:
                    body = json.dumps(ROUTES[path](conn)).encode()
                finally:
                    conn.close()
                return self._send(200, body, "application/json")
            name = "index.html" if path == "/" else path.lstrip("/")
            file = (WEB / name).resolve()
            if WEB not in file.parents or not file.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self._send(200, file.read_bytes(), ctype)

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: python -m primavera_sync.server <base.db> [porta]")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    print(f"Dashboard em http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(sys.argv[1])).serve_forever()
