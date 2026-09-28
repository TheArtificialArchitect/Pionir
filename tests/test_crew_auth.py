"""The crew's side of client authentication.

Its writes (a goal, an allocation) need the crew's client token as a bearer - loopback
alone lets any local process in - and its hands send that token to Pionir, and never a
permission of their own.
"""

import json
import tempfile
import unittest
from pathlib import Path

from crew_support import FakeTime, temp_dir
from test_crew_fakes import catalogue, make_crew

from pionir.auth import ensure_tokens, token_path
from pionir.config import PionirSettings
from pionir.crew.config import CrewSettings
from pionir.crew.hands import PionirClient

GOAL = json.dumps({"division": "beta", "goal": "ship the blog"}).encode()
WRONG = "not-the-crew-token" + "_" * 30


class CrewApiWrites(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "secrets"
        self.token = ensure_tokens(self.dir, ("crew",))["crew"]
        self.crew = make_crew(tmp.name, cat=catalogue({"beta": [{"name": "b1"}]}),
                              now=FakeTime().now, api_port=None)
        from pionir.crew.api import CrewApi
        self.api = CrewApi(self.crew.direction, health=lambda: {"stopping": False}, port=0,
                           token_file=token_path(self.dir, "crew"), compat=False)

    def post(self, **headers):
        headers = {"Content-Type": "application/json", "Content-Length": str(len(GOAL)),
                   **headers}
        return self.api.handle("POST", "/api/goal", client_host="127.0.0.1", headers=headers,
                               read_body=lambda n: GOAL[:n])

    def goal(self):
        return (self.crew.store.directions().get("beta") or {}).get("goal")

    def test_the_crew_token_writes(self) -> None:
        status, out = self.post(Authorization=f"Bearer {self.token}")
        self.assertEqual(status, 200, out)
        self.assertEqual(self.goal(), "ship the blog")

    def test_no_token_or_a_wrong_one_changes_nothing(self) -> None:
        for headers in ({}, {"Authorization": f"Bearer {WRONG}"},
                        {"Authorization": "Bearer "}, {"Authorization": self.token}):
            status, out = self.post(**headers)
            self.assertEqual(status, 401, (headers, out))
        self.assertIsNone(self.goal())

    def test_a_wrong_token_is_refused_even_in_the_window(self) -> None:
        self.api.compat = True
        status, _ = self.post(Authorization=f"Bearer {WRONG}")
        self.assertEqual(status, 401)
        self.assertIsNone(self.goal())
        status, _ = self.post()                          # no token: served, with a warning
        self.assertEqual(status, 200)

    def test_no_token_file_means_no_bearer_can_match(self) -> None:
        self.api.token_file = None
        status, _ = self.post(Authorization=f"Bearer {self.token}")
        self.assertEqual(status, 401)

    def test_reads_stay_open(self) -> None:
        status, _ = self.api.handle("GET", "/api/health", client_host="127.0.0.1",
                                    headers={"Host": "127.0.0.1"})
        self.assertEqual(status, 200)


class _Opener:
    def __init__(self) -> None:
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        body = json.dumps({"ok": True, "result": {}}).encode()

        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner, n=-1):
                return body

        return _Response()


class CrewHands(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _client(self, token_file):
        client = PionirClient("http://127.0.0.1:1", token_file=token_file)
        client._opener = _Opener()
        return client

    def test_the_crew_sends_its_token_and_never_a_permission(self) -> None:
        token = ensure_tokens(self.dir, ("crew",))["crew"]
        client = self._client(token_path(self.dir, "crew"))
        client.run_task("coding.daedalus_solve", {"intent": "x"}, permissions=("daedalus.solve",))
        request = client._opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {token}")
        self.assertNotIn("permissions", json.loads(request.data.decode()))
        client.approvals()
        self.assertEqual(client._opener.requests[1].get_header("Authorization"),
                         f"Bearer {token}")

    def test_no_token_file_sends_no_header(self) -> None:
        client = self._client(self.dir / "missing.token")
        client.run_task("gate.read", {})
        self.assertIsNone(client._opener.requests[0].get_header("Authorization"))

    def test_the_crew_settings_point_at_pionirs_client_token_dir(self) -> None:
        pionir = PionirSettings(state_root=self.dir, client_token_dir=self.dir / "tok")
        crew = CrewSettings.from_pionir(pionir)
        self.assertEqual(crew.pionir_token_file, self.dir / "tok" / "pionir-client-crew.token")
        self.assertIsNone(CrewSettings(state_dir=self.dir / "c", gpu_lock_path=self.dir / "g",
                                       secrets_dir=self.dir).pionir_token_file)


if __name__ == "__main__":
    unittest.main()
