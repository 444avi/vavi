#!/usr/bin/env python3
"""Local single-user settings API and static UI (stdlib only)."""

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import sentinel_config as sentinel_cfg
import vavi
from notification_app import AppStore, ValidationError, static_preview


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP_DB = os.path.join(HERE, "app.db")
STATIC_FILES = {
    "/": os.path.join(HERE, "templates", "settings.html"),
    "/settings.css": os.path.join(HERE, "static", "settings.css"),
    "/settings.js": os.path.join(HERE, "static", "settings.js"),
}


class SettingsHandler(BaseHTTPRequestHandler):
    server_version = "VaviSettings/1.0"

    def _store(self):
        return AppStore(self.server.app_db)

    def _headers(self, status, content_type="application/json; charset=utf-8",
                 length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _read_json(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValidationError({"request": "Content-Type must be application/json."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValidationError({"request": "Invalid content length."})
        if length < 1 or length > 64 * 1024:
            raise ValidationError({"request": "Expected a small JSON request body."})
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError({"request": "Request body must be valid JSON."})

    def _serve_file(self, path):
        target = STATIC_FILES.get(path)
        if not target or not os.path.isfile(target):
            self._json(404, {"error": "not_found"})
            return
        with open(target, "rb") as handle:
            body = handle.read()
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/settings":
            store = self._store()
            try:
                settings = store.get_settings()
            finally:
                store.close()
            if settings is None:
                self._json(404, {"error": "not_seeded",
                                 "message": "Seed a recipient from the legacy email lists first."})
            else:
                self._json(200, settings)
            return
        self._serve_file(path)

    def do_PUT(self):
        path = urlparse(self.path).path
        monitor = path.rsplit("/", 1)[-1]
        if path not in {"/api/settings/vavi", "/api/settings/sentinel"}:
            self._json(404, {"error": "not_found"})
            return
        try:
            payload = self._read_json()
            store = self._store()
            try:
                saved = store.update_preference(monitor, payload)
            finally:
                store.close()
            self._json(200, {"preference": saved})
        except ValidationError as exc:
            self._json(422, {"error": "validation", "fields": exc.errors})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/settings/test-email":
                payload = self._read_json()
                self._json(200, {"preview": static_preview(payload.get("monitor"))})
                return
            if path == "/api/unsubscribe":
                self._read_json()
                store = self._store()
                try:
                    store.unsubscribe()
                finally:
                    store.close()
                self._json(200, {"status": "unsubscribed"})
                return
            self._json(404, {"error": "not_found"})
        except ValidationError as exc:
            self._json(422, {"error": "validation", "fields": exc.errors})

    def log_message(self, fmt, *args):
        print("[settings] " + fmt % args, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the Vavi notification settings UI.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="listen address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--app-db", default=DEFAULT_APP_DB)
    parser.add_argument("--env", default=os.path.join(HERE, ".env"))
    args = parser.parse_args(argv)

    vavi.load_env(args.env)
    env = dict(os.environ)
    store = AppStore(args.app_db)
    try:
        store.seed_from_env(
            env,
            default_timezone=env.get("APP_DEFAULT_TIMEZONE", "America/Los_Angeles"),
            sentinel_digest_time=f"{sentinel_cfg.DIGEST_HOUR_LOCAL:02d}:00",
            sentinel_market_cap_floor=sentinel_cfg.MARKET_CAP_MIN_USD)
    finally:
        store.close()

    server = ThreadingHTTPServer((args.host, args.port), SettingsHandler)
    server.app_db = args.app_db
    print(f"Settings UI listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
