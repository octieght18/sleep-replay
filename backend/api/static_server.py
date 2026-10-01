"""Local static assets and an API reverse proxy; no JavaScript build step."""

import http.client
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from backend.api.runtime import bind_socket, check_python

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self.proxy()
        # Resolve symlinks as well as .. components so static serving cannot
        # reach the data directory or repository files.
        path = Path(self.translate_path(self.path)).resolve()
        if (
            not path.is_relative_to(FRONTEND_DIR.resolve())
            or path.is_dir()
            and path != FRONTEND_DIR.resolve()
        ):
            return self.send_error(404)
        super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/api/"):
            return self.proxy()
        path = Path(self.translate_path(self.path)).resolve()
        if (
            not path.is_relative_to(FRONTEND_DIR.resolve())
            or path.is_dir()
            and path != FRONTEND_DIR.resolve()
        ):
            return self.send_error(404)
        super().do_HEAD()

    def do_POST(self):
        self.proxy()

    def do_PUT(self):
        self.proxy()

    def do_OPTIONS(self):
        self.proxy()

    def proxy(self):
        if not self.path.startswith("/api/"):
            return self.send_error(404)
        target = urlsplit(
            os.environ.get("SLEEP_REPLAY_BACKEND_URL", "http://127.0.0.1:8735")
        )
        if target.scheme != "http" or target.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
            "backend",
        ):
            return self.send_error(
                502, "The backend URL must identify the local backend service."
            )
        connection = http.client.HTTPConnection(
            target.hostname, target.port or 8735, timeout=240
        )
        response_started = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or self.headers.get("Transfer-Encoding"):
                return self.send_error(400, "Use a Content-Length request")
            connection.putrequest(self.command, self.path)
            for name in (
                "Content-Type",
                "Origin",
                "Range",
                "If-Range",
                "If-None-Match",
            ):
                if name in self.headers:
                    connection.putheader(name, self.headers[name])
            connection.putheader("Content-Length", str(length))
            connection.endheaders()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    raise OSError("Incomplete request body")
                connection.send(chunk)
                remaining -= len(chunk)
            response = connection.getresponse()
            self.send_response(response.status)
            response_started = True
            for name, value in response.getheaders():
                if name.lower() in {
                    "content-type",
                    "content-length",
                    "content-range",
                    "accept-ranges",
                    "etag",
                    "last-modified",
                }:
                    self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
        except (OSError, ValueError, http.client.HTTPException):
            # This runs before any response headers for an unreachable backend.
            if response_started:
                return
            payload = json.dumps(
                dict(
                    code="SOURCE_LOAD_FAILED",
                    description="The backend is not reachable.",
                    action="Start it with python -m backend.api.run.",
                    file_name=None,
                )
            ).encode()
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass
        finally:
            connection.close()


def static_server(sock=None):
    host = os.environ.get("SLEEP_REPLAY_FRONTEND_HOST", "127.0.0.1")
    if sock is None:
        sock = bind_socket(host, 8734)
    server = ThreadingHTTPServer((host, 8734), Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = sock
    server.server_address = sock.getsockname()
    server.server_name, server.server_port = host, 8734
    server.daemon_threads = True
    return server


def main():
    check_python()
    try:
        with static_server() as server:
            print("Sleep Replay: http://127.0.0.1:8734", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
