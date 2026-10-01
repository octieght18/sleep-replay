"""Local startup checks and server configuration shared by all entry points."""

import ipaddress
import os
import socket
import sys


def check_python(version=None):
    version = sys.version_info if version is None else version
    if version[:2] < (3, 11):
        raise SystemExit(
            f"Python {'.'.join(map(str, version[:3]))} detected; Python 3.11 or later is required."
        )


def warn_host(host):
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host.lower() == "localhost"
    if not local:
        print(
            "Warning: the Backend_API has no authentication. Imported data and replays are reachable from other devices on the network.",
            flush=True,
        )


def bind_socket(host, port):
    warn_host(host)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
        return sock
    except OSError:
        sock.close()
        raise OSError(
            f"Port {port} on {host} cannot be bound; it may already be in use. Stop the process using port {port} and retry."
        ) from None


def api_server():
    import uvicorn
    from backend.api.app import create_app

    # Uvicorn's access logger prints URLs/parameters; keep operational logging
    # inside the metadata-only logger instead.
    return uvicorn.Server(
        uvicorn.Config(
            create_app(),
            host=os.environ.get("SLEEP_REPLAY_API_HOST", "127.0.0.1"),
            port=8735,
            access_log=False,
            log_level="warning",
            lifespan="on",
        )
    )
