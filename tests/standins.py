"""Local stand-ins for the specialists a test runtime points at.

Not a test module (unittest discovery looks for ``test*.py``). A runtime built by a
test must never reach a live service: not Moss on 8799, not Ollama on 11434. A dead
port is no answer either - a refused connect took ~2 s each on the owner's machine,
so retries ran an approved job past its test's wait. ``down_url()`` is a specialist
that is up but failing: every request gets an instant 503.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Down(BaseHTTPRequestHandler):
    def _fail(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = b'{"ok": false, "error": "down (test stand-in)"}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _fail

    def log_message(self, *_args) -> None:
        pass


_LOCK = threading.Lock()
_DOWN: ThreadingHTTPServer | None = None


def down_url() -> str:
    """The address of the shared 503 stand-in, started on first use (daemon thread)."""
    global _DOWN
    with _LOCK:
        if _DOWN is None:
            _DOWN = ThreadingHTTPServer(("127.0.0.1", 0), _Down)
            _DOWN.daemon_threads = True
            threading.Thread(target=_DOWN.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{_DOWN.server_address[1]}"
