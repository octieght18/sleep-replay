"""Start the complete local app: python -m backend.api.run (Docker is optional)."""

# ruff: noqa: E402
# Version checks intentionally precede Python-3.11 dependency imports.
from backend.api.runtime import check_python

check_python()  # Before importing packages that need Python 3.11 features.

import os
import sys
import threading

from backend.api.runtime import check_python, bind_socket, api_server
from backend.api.static_server import static_server
from backend.domain.errors import User_Error


def main():
    check_python()
    api_socket = frontend_socket = frontend = None
    try:
        # Reserve both ports before starting either process. A conflicting port
        # leaves the whole application unstarted rather than half available.
        api_socket = bind_socket(
            os.environ.get("SLEEP_REPLAY_API_HOST", "127.0.0.1"), 8735
        )
        frontend_socket = bind_socket(
            os.environ.get("SLEEP_REPLAY_FRONTEND_HOST", "127.0.0.1"), 8734
        )
        frontend = static_server(frontend_socket)
        worker = threading.Thread(target=frontend.serve_forever, daemon=True)
        worker.start()
        print("Sleep Replay: http://127.0.0.1:8734 — Ctrl+C to stop", flush=True)
        server = api_server()
        server.run(sockets=[api_socket])
        return 0 if server.started else 1
    except (OSError, User_Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if frontend:
            frontend.shutdown()
            frontend.server_close()
        elif frontend_socket:
            frontend_socket.close()
        if api_socket:
            api_socket.close()


if __name__ == "__main__":
    raise SystemExit(main())
