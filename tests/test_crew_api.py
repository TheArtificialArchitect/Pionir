"""The crew's loopback HTTP API: the Direction API over HTTP, and nothing looser.

Each test fails if its rule is reverted: a read that is not the Direction API's own
data, a write that does not change the crew or does not record who asked, bad input
answered with a 500 (or accepted), money becoming allocatable, a non-loopback peer or a
non-local Host being served, an oversized body being read, a fault escaping the
handler, the API outliving ``Crew.stop``, or a second server sharing a taken port.
"""

import http.client
import json
import os
import socket
import unittest
from typing import ClassVar
from unittest import mock

from crew_support import FakeTime, temp_dir
from test_crew_fakes import catalogue, make_crew

from pionir.crew import log as crewlog
from pionir.crew.api import MAX_BODY_BYTES, CrewApi
from pionir.crew.config import CrewSettings

DIVISIONS = {"alpha": [{"name": "a1"}], "beta": [{"name": "b1"}]}


def request(port, method, path, body=None, headers=None):
    """One real HTTP request to the API: (status, JSON object)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.time = FakeTime()
        self.crew = make_crew(tmp.name, cat=catalogue(DIVISIONS), now=self.time.now,
                              api_port=0)
        self.addCleanup(self.crew.stop)
        self.assertTrue(self.crew.api.start())
        self.port = self.crew.api.port

    def get(self, path, **kw):
        return request(self.port, "GET", path, **kw)

    def post(self, path, body, **kw):
        return request(self.port, "POST", path, body, **kw)


class ReadTests(_Case):
    def test_digest_is_the_direction_apis_digest(self) -> None:
        self.crew.direction.set_goal("beta", "grow the blog", priority=1)
        status, got = self.get("/api/digest?max_chars=3000")
        self.assertEqual(status, 200)
        self.assertIs(got.pop("ok"), True)
        self.assertEqual(got.pop("max_chars"), 3000)
        self.assertEqual(got, json.loads(json.dumps(self.crew.direction.digest(max_chars=3000))))
        self.assertEqual({e["division"] for e in got["divisions"]}, {"alpha", "beta"})

    def test_digest_honours_its_budget(self) -> None:
        _s, small = self.get("/api/digest?max_chars=200")
        _s, big = self.get("/api/digest")
        self.assertEqual(big["max_chars"], 4000)                  # the Direction default
        self.assertLessEqual(len(json.dumps(small["divisions"])), 200)
        self.assertTrue(small["truncated"])
        self.assertFalse(big["truncated"])

    def test_divisions_and_compute_are_the_direction_apis(self) -> None:
        status, got = self.get("/api/divisions")
        self.assertEqual(status, 200)
        self.assertEqual(got["divisions"],
                         json.loads(json.dumps(self.crew.direction.divisions())))
        status, got = self.get("/api/compute")
        self.assertEqual(status, 200)
        self.assertEqual(got["compute"], json.loads(json.dumps(self.crew.direction.compute())))
        self.assertEqual(set(got["compute"]), {"model_calls", "claude_escalations"})

    def test_health_says_up_and_which_divisions(self) -> None:
        status, got = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(got["service"], "pionir-crew")
        self.assertEqual(got["divisions"], ["alpha", "beta"])
        self.assertIsNone(got["paused"])
        self.crew.pause("operator")
        self.assertEqual(self.get("/api/health")[1]["paused"], "operator")


class WriteTests(_Case):
    def test_a_goal_changes_the_division_and_records_who_asked(self) -> None:
        status, got = self.post("/api/goal", {"division": "alpha", "goal": "find buyers",
                                              "priority": 2, "by": "moss"})
        self.assertEqual(status, 200)
        self.assertTrue(got["ok"])
        self.assertEqual(got["direction"]["goal"], "find buyers")
        alpha = next(d for d in self.crew.direction.divisions() if d["division"] == "alpha")
        self.assertEqual((alpha["goal"], alpha["priority"]), ("find buyers", 2))
        self.assertEqual(self.crew.store.directions()["alpha"]["by"], "moss")

    def test_a_goal_without_by_is_recorded_as_the_api_not_as_moss(self) -> None:
        self.post("/api/goal", {"division": "alpha", "goal": "x"})
        row = self.crew.store.directions()["alpha"]
        self.assertEqual((row["by"], row["priority"]), ("crew-api", 3))

    def test_an_allocation_changes_compute(self) -> None:
        status, got = self.post("/api/allocate", {"resource": "model_calls",
                                                  "shares": {"alpha": 0.25}, "by": "moss"})
        self.assertEqual(status, 200)
        self.assertEqual(got["allocation"]["shares"]["alpha"], 0.25)
        compute = self.crew.direction.compute()["model_calls"]
        self.assertTrue(compute["allocated"])
        self.assertEqual(compute["shares"], {"alpha": 0.25, "beta": 0.75})
        self.assertEqual(self.crew.store.allocations()["model_calls"]["by"], "moss")

    def test_money_is_not_allocatable(self) -> None:
        for resource in ("money", "usd", "budget"):
            status, got = self.post("/api/allocate", {"resource": resource,
                                                      "shares": {"alpha": 0.5}})
            self.assertEqual(status, 400, resource)
            self.assertIn("no money lever", got["error"])
        self.assertEqual(self.crew.store.allocations(), {})


class BadInputTests(_Case):
    BAD_GOALS: ClassVar[list] = [
        ({"division": "nope", "goal": "x"}, "nope"),              # the registry refuses it
        ({"division": "alpha", "goal": ""}, "goal"),
        ({"division": "alpha", "goal": "x" * 501}, "500"),
        ({"division": "alpha", "goal": "x", "priority": 9}, "priority"),
        ({"division": "alpha", "goal": "x", "priority": True}, "priority"),
        ({"division": "alpha", "goal": "x", "priority": "2"}, "priority"),
        ({"goal": "x"}, "division"),
        ({"division": "alpha", "goal": "x", "by": "a\nb"}, "by"),
    ]
    BAD_ALLOCATIONS: ClassVar[list] = [
        ({"resource": "model_calls", "shares": {"alpha": 0.7, "beta": 0.6}}, "at most 1"),
        ({"resource": "model_calls", "shares": {"alpha": 1.5}}, "fraction"),
        ({"resource": "model_calls", "shares": {"nope": 0.5}}, "nope"),
        ({"resource": "model_calls", "shares": {}}, "non-empty"),
        ({"resource": "model_calls", "shares": [0.5]}, "non-empty"),
        ({"shares": {"alpha": 0.5}}, "resource"),
    ]

    def test_bad_goals_are_400_with_the_reason_and_change_nothing(self) -> None:
        for body, words in self.BAD_GOALS:
            with self.subTest(body=body):
                status, got = self.post("/api/goal", body)
                self.assertEqual(status, 400)
                self.assertIs(got["ok"], False)
                self.assertIn(words, got["error"])
        self.assertEqual(self.crew.store.directions(), {})

    def test_bad_allocations_are_400_with_the_reason_and_change_nothing(self) -> None:
        for body, words in self.BAD_ALLOCATIONS:
            with self.subTest(body=body):
                status, got = self.post("/api/allocate", body)
                self.assertEqual(status, 400)
                self.assertIn(words, got["error"])
        self.assertEqual(self.crew.store.allocations(), {})

    def test_malformed_bodies_and_queries_are_4xx_never_500(self) -> None:
        cases = [
            ("POST", "/api/goal", b"{not json", {"Content-Type": "application/json"}, 400),
            ("POST", "/api/goal", b"[1, 2]", {"Content-Type": "application/json"}, 400),
            ("POST", "/api/goal", b"\xff\xfe", {"Content-Type": "application/json"}, 400),
            ("POST", "/api/goal", b'{"division": "alpha", "goal": "x"}',
             {"Content-Type": "text/plain"}, 415),
            ("GET", "/api/digest?max_chars=abc", None, {}, 400),
            ("GET", "/api/digest?max_chars=5", None, {}, 400),
            ("GET", "/api/digest?max_chars=99999999", None, {}, 400),
            ("GET", "/api/goal", None, {}, 405),
            ("POST", "/api/digest", b"{}", {"Content-Type": "application/json"}, 405),
            ("GET", "/api/nothing", None, {}, 404),
        ]
        for method, path, body, headers, want in cases:
            with self.subTest(path=path, body=body):
                status, got = request(self.port, method, path, body, headers)
                self.assertEqual(status, want)
                self.assertIs(got["ok"], False)
        self.assertEqual(self.crew.store.directions(), {})


class LoopbackOnlyTests(_Case):
    def test_a_non_loopback_peer_is_refused_and_changes_nothing(self) -> None:
        body = json.dumps({"division": "alpha", "goal": "x"}).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        for host in ("192.168.1.20", "100.64.0.7", "::ffff:10.0.0.1", "fe80::1", "", "evil"):
            with self.subTest(host=host):
                status, _got = self.crew.api.handle("POST", "/api/goal", client_host=host,
                                                   headers=headers, read_body=lambda n: body)
                self.assertEqual(status, 403)
                status, _got = self.crew.api.handle("GET", "/api/digest", client_host=host)
                self.assertEqual(status, 403)
        self.assertEqual(self.crew.store.directions(), {})
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertEqual(self.crew.api.handle("GET", "/api/health", client_host=host)[0],
                             200)

    def test_a_non_local_host_header_or_origin_is_refused(self) -> None:
        status, _ = self.get("/api/digest", headers={"Host": "evil.example:8782"})
        self.assertEqual(status, 403)
        status, _ = self.post("/api/goal", {"division": "alpha", "goal": "x"},
                              headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.crew.store.directions(), {})
        status, _ = self.post("/api/goal", {"division": "alpha", "goal": "x"},
                              headers={"Origin": "http://127.0.0.1:8782"})
        self.assertEqual(status, 200)

    def test_it_binds_loopback_only(self) -> None:
        self.assertEqual(self.crew.api._server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            CrewApi(self.crew.direction, health=dict, host="0.0.0.0")

    def test_an_oversized_body_is_refused_unread(self) -> None:
        def read(n):
            raise AssertionError("an oversized body must not be read")
        headers = {"Content-Type": "application/json",
                   "Content-Length": str(MAX_BODY_BYTES + 1)}
        status, got = self.crew.api.handle("POST", "/api/goal", client_host="127.0.0.1",
                                           headers=headers, read_body=read)
        self.assertEqual(status, 413)
        self.assertIs(got["ok"], False)


class _Broken:
    def digest(self, **kw):
        raise RuntimeError("the store fell over")

    def divisions(self):
        return [{"division": "alpha"}]


class FaultTests(unittest.TestCase):
    def test_a_fault_is_a_counted_500_and_the_server_keeps_serving(self) -> None:
        api = CrewApi(_Broken(), health=lambda: {"stopping": False}, port=0)
        self.assertTrue(api.start())
        self.addCleanup(api.stop)
        before = crewlog.lesions["crew.api"]
        status, got = request(api.port, "GET", "/api/digest")
        self.assertEqual(status, 500)
        self.assertIn("RuntimeError", got["error"])
        self.assertEqual(crewlog.lesions["crew.api"], before + 1)
        status, got = request(api.port, "GET", "/api/divisions")   # still serving
        self.assertEqual((status, got["divisions"]), (200, [{"division": "alpha"}]))

    def test_a_write_while_stopping_is_refused(self) -> None:
        api = CrewApi(_Broken(), health=lambda: {"stopping": True}, port=0)
        body = b'{"division": "alpha", "goal": "x"}'
        status, _got = api.handle("POST", "/api/goal", client_host="127.0.0.1",
                                 headers={"Content-Type": "application/json",
                                          "Content-Length": str(len(body))},
                                 read_body=lambda n: body)
        self.assertEqual(status, 503)

    def test_a_taken_port_is_a_lesion_not_a_shared_port(self) -> None:
        first = CrewApi(_Broken(), health=dict, port=0)
        self.assertTrue(first.start())
        self.addCleanup(first.stop)
        before = crewlog.lesions["crew.api.bind"]
        second = CrewApi(_Broken(), health=dict, port=first.port)
        self.assertFalse(second.start())
        self.assertFalse(second.serving)
        self.assertEqual(crewlog.lesions["crew.api.bind"], before + 1)


class LifecycleTests(unittest.TestCase):
    def test_building_opens_nothing_start_serves_stop_closes(self) -> None:
        with temp_dir() as root:
            crew = make_crew(root, cat=catalogue(DIVISIONS), api_port=0, tick_seconds=0.05)
            try:
                self.assertIsNone(crew.api.port)                  # built, not serving
                crew.start()
                port = crew.api.port
                self.assertTrue(crew.api.serving)
                self.assertEqual(request(port, "GET", "/api/health")[0], 200)
            finally:
                crew.stop()
            self.assertFalse(crew.api.serving)
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=3).close()

    def test_no_port_means_no_api(self) -> None:
        with temp_dir() as root:
            crew = make_crew(root, cat=catalogue(DIVISIONS), api_port=None)
            self.addCleanup(crew.stop)
            self.assertIsNone(crew.api)


class ConfigTests(unittest.TestCase):
    def test_the_port_defaults_to_8782_and_the_environment_overrides_it(self) -> None:
        with temp_dir() as root:
            env = {"PIONIR_STATE_ROOT": root}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("PIONIR_CREW_API_PORT", None)
                self.assertEqual(CrewSettings.from_environment().api_port, 8782)
                os.environ["PIONIR_CREW_API_PORT"] = "9123"
                self.assertEqual(CrewSettings.from_environment().api_port, 9123)
                os.environ["PIONIR_CREW_API_PORT"] = "off"
                self.assertIsNone(CrewSettings.from_environment().api_port)
                os.environ["PIONIR_CREW_API_PORT"] = "70000"
                with self.assertRaises(ValueError):
                    CrewSettings.from_environment()


if __name__ == "__main__":
    unittest.main()
