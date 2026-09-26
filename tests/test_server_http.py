"""The loopback admin surface refuses what a cross-site page can send.

A browser on any origin can POST `text/plain` to 127.0.0.1 without a CORS
preflight. These tests speak real HTTP to the handler so the header checks are
exercised as the browser would hit them.
"""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from standins import down_url

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.server import PionirApp, _make_handler


def _runtime(tmp: str):
    return build_runtime(
        PionirSettings(
            state_root=Path(tmp),
            atani_command=("pionir-test-no-such-binary",),
            galatea_url=down_url(),
            galatea_model_id="stub-model",
            embed_model=None,  # never reach the live Ollama embedder from a test
            daedalus_url=down_url(),
            melete_url=down_url(),
            bryo_status_command=None,
            nyx_status_command=None,
            voodoo_status_command=None,
            evict_to_fit=False,
        )
    )


class CsrfTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = PionirApp(_runtime(self._tmp.name))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def _raw(self, method: str, path: str, body: str | None, headers: dict):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest(method, path, skip_host="Host" in headers)
        for key, value in headers.items():
            conn.putheader(key, value)
        if body is not None and "Content-Length" not in headers:
            conn.putheader("Content-Length", str(len(body.encode("utf-8"))))
        conn.endheaders(body.encode("utf-8") if body is not None else None)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def _json_headers(self, **extra: str) -> dict:
        return {"Content-Type": "application/json", **extra}

    def test_text_plain_simple_request_is_forbidden(self) -> None:
        body = json.dumps({"capability": "coding.daedalus_solve", "permissions": ["daedalus.solve"]})
        status, out = self._raw("POST", "/api/task", body, {"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertEqual(out["error"], "forbidden")

    def test_foreign_origin_is_forbidden(self) -> None:
        body = json.dumps({"request": "hello"})
        status, out = self._raw("POST", "/api/route", body,
                                self._json_headers(Origin="http://evil.example"))
        self.assertEqual(status, 403)
        self.assertEqual(out["error"], "forbidden")

    def test_own_origin_passes(self) -> None:
        body = json.dumps({"request": "reason carefully about this", "execute": False})
        status, out = self._raw("POST", "/api/route", body, self._json_headers(
            Host=f"127.0.0.1:{self.port}", Origin=f"http://127.0.0.1:{self.port}"))
        self.assertEqual(status, 200)
        self.assertFalse(out["executed"])

    def test_localhost_host_header_passes_without_origin(self) -> None:
        body = json.dumps({"request": "reason carefully about this", "execute": False})
        status, _ = self._raw("POST", "/api/route", body,
                              self._json_headers(Host=f"localhost:{self.port}"))
        self.assertEqual(status, 200)

    def test_foreign_host_is_forbidden(self) -> None:
        body = json.dumps({"request": "hello"})
        status, _ = self._raw("POST", "/api/route", body,
                              self._json_headers(Host="pionir.example:80"))
        self.assertEqual(status, 403)

    def test_garbage_content_length_is_400(self) -> None:
        status, out = self._raw("POST", "/api/route", None,
                                self._json_headers(**{"Content-Length": "lots"}))
        self.assertEqual(status, 400)
        self.assertEqual(out["error"], "bad json")

    def test_a_refused_request_body_is_read_before_the_403(self) -> None:
        # The server used to answer a refused POST without reading its body; on Windows,
        # closing a socket with unread bytes sends a reset, and the client could get
        # "connection aborted" (WinError 10053) instead of the 403 - the intermittent failure
        # of the text/plain test above. Over a real socket that is a race, so this drives the
        # handler over in-memory streams and checks the body was consumed, deterministically.
        import email.message
        import io
        body = json.dumps({"request": "x" * 5000}).encode()
        handler_cls = _make_handler(self.app)
        handler = handler_cls.__new__(handler_cls)
        headers = email.message.Message()
        headers["Content-Type"] = "text/plain"
        headers["Content-Length"] = str(len(body))
        handler.headers = headers
        handler.rfile = io.BytesIO(body + b"AFTER")
        handler.wfile = io.BytesIO()
        handler.path, handler.command = "/api/route", "POST"
        handler.request_version, handler.requestline = "HTTP/1.1", "POST /api/route HTTP/1.1"
        handler.client_address = ("127.0.0.1", 0)
        handler.close_connection = True
        handler.do_POST()
        status_line = handler.wfile.getvalue().split(b"\r\n")[0]
        self.assertRegex(status_line, rb"^HTTP/1\.[01] 403 ")
        self.assertEqual(handler.rfile.read(), b"AFTER")     # exactly the body, no more

    def test_oversized_body_is_400(self) -> None:
        status, _out = self._raw("POST", "/api/route", None,
                                self._json_headers(**{"Content-Length": "2000000"}))
        self.assertEqual(status, 400)

    def test_missing_approval_id_is_400(self) -> None:
        for path in ("/api/approvals/approve", "/api/approvals/deny"):
            status, out = self._raw("POST", path, "{}", self._json_headers())
            self.assertEqual(status, 400, path)
            self.assertEqual(out["error"], "id required")

    def test_get_is_untouched(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", "/api/state")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        conn.close()


if __name__ == "__main__":
    unittest.main()
