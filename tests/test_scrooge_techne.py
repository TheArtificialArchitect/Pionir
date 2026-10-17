"""Techne: read the site-delivery jobs (PII-redacted), and perform ONE allowlisted doer
action that parks on the owner's Desk on every call.

The admin steps (approve, reject, create, requeue, takedown, keep, dismiss) are the owner's:
``techne.run`` refuses them before anything is parked, so no yes is ever wasted on an action
Scrooge would 403. Scrooge is faked at the urllib opener with the real opener's signature;
nothing touches the network. The token never appears in a result, a log or an error.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Self

from pionir.adapters.scrooge_techne import (
    JOBS,
    RUN,
    RUN_ACTIONS,
    ScroogeTechneAdapter,
    ScroogeTechneSettings,
)
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp

OPS_TOKEN = "opsSECRETtoken0123456789abcdefXYZ+/="
SCROOGE = "https://api.dokaz.test"
JOB_ID = "job_abc123"


def a_job(**over: Any) -> dict[str, Any]:
    """A Techne job as Scrooge lists it, PII fields and all."""
    base = {
        "id": JOB_ID,
        "slug": "acme-coffee",
        "domain": "acme.example",
        "status": "preview_ready",
        "failed_stage": None,
        "preview_url": "https://acme.pages.dev",
        "live_url": None,
        "plan_interval": "month",
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
        # ---- PII that must NEVER leave the adapter ----
        "intake_json": {"brief": "build me a coffee shop site", "phone": "+1 555 123 4567"},
        "customer_email": "buyer@example.com",
        "stripe_session": "cs_test_123",
        "stripe_invoice": "in_test_123",
        "stripe_subscription": "sub_test_123",
        "note": "an internal note about the client",
    }
    base.update(over)
    return base


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeTechne:
    """Scrooge's /dash/techne/jobs endpoints, in memory, checking the ops token. Same call
    signature as OpenerDirector.open."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.valid_tokens = {OPS_TOKEN}
        self.forced: dict[str, tuple[int, Any]] = {}   # step -> (status, body)
        self.down = False
        self.jobs = [a_job(), a_job(id="job_def456", status="live", slug="done-co")]

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None) -> _Response:
        if data is not None:
            raise TypeError(f"opener got a positional data argument: {data!r}")
        url = request.full_url
        parts = urllib.parse.urlsplit(url)
        method = request.get_method()
        last = parts.path.rsplit("/", 1)[-1]
        step = "jobs" if parts.path == "/dash/techne/jobs" else last
        body = json.loads(request.data) if request.data else None
        self.calls.append({"step": step, "method": method, "url": url, "query": parts.query,
                           "body": body, "timeout": timeout,
                           "token": request.get_header("X-dash-token")})
        if self.down:
            raise urllib.error.URLError(f"connection refused: {url} token={OPS_TOKEN}")
        if request.get_header("X-dash-token") not in self.valid_tokens:
            self._error(url, 401, {"ok": False, "error": "unauthorized"})
        if step in self.forced:
            status, payload = self.forced[step]
            if status >= 400:
                self._error(url, status, payload)
            return _Response(status, payload)
        if step == "jobs" and method == "GET":
            wanted = urllib.parse.parse_qs(parts.query).get("status")
            rows = [j for j in self.jobs if not wanted or j["status"] == wanted[0]]
            return _Response(200, {"ok": True, "jobs": rows})
        if step in RUN_ACTIONS and method == "POST":
            return _Response(200, {"ok": True, "id": body.get("id"), "action": step})
        self._error(url, 404, {"ok": False, "error": "not found"})
        raise AssertionError  # unreachable

    @staticmethod
    def _error(url: str, status: int, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(raw))  # type: ignore[arg-type]


def _settings(root: Path, **kw: Any) -> PionirSettings:
    return PionirSettings(
        state_root=root,
        atani_command=("pionir-test-no-such-binary",),
        daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
        bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
        embed_model=None, evict_to_fit=False, **kw,
    )


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.token_file = self.root / "secrets" / "scrooge-ops-token.txt"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(OPS_TOKEN + "\n", encoding="utf-8")
        self.world = FakeTechne()
        self.adapter = ScroogeTechneAdapter(
            ScroogeTechneSettings(base_url=SCROOGE, token_file=self.token_file),
            opener=self.world)

    def execute(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.adapter.execute(Task(capability, payload)).output)


# ---- the gate: the manifest flags that make techne.run a Desk card ----------------------
class ManifestTests(_Case):
    def test_the_declarations(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        self.assertEqual(set(caps), {JOBS, RUN})
        self.assertIs(caps[JOBS].risk, RiskLevel.READ_ONLY)
        self.assertFalse(caps[JOBS].requires_approval)
        self.assertTrue(caps[JOBS].routable)
        # techne.run parks on the Desk on EVERY call: PRIVILEGED + requires_approval, and
        # never NL-routed (reached only by name), exactly like client.email / security.nyx_run
        self.assertIs(caps[RUN].risk, RiskLevel.PRIVILEGED)
        self.assertTrue(caps[RUN].requires_approval)
        self.assertFalse(caps[RUN].routable)
        self.assertFalse(caps[RUN].classifiable)
        self.assertEqual(self.adapter.manifest.agent_id, "scrooge")


# ---- techne.jobs: read-only, PII redacted ------------------------------------------------
class JobsTests(_Case):
    def test_jobs_are_redacted(self) -> None:
        out = self.execute(JOBS, {})
        self.assertIs(out["ok"], True)
        self.assertEqual(len(out["jobs"]), 2)
        for job in out["jobs"]:
            for leaked in ("intake_json", "customer_email", "stripe_session",
                           "stripe_invoice", "stripe_subscription", "note"):
                self.assertNotIn(leaked, job, f"{leaked} must be redacted")
        first = out["jobs"][0]
        self.assertEqual(first["id"], JOB_ID)
        self.assertEqual(first["slug"], "acme-coffee")
        self.assertEqual(first["domain"], "acme.example")
        self.assertEqual(first["status"], "preview_ready")
        self.assertEqual(first["preview_url"], "https://acme.pages.dev")
        self.assertEqual(first["created_at"], "2026-10-01T10:00:00Z")
        self.assertEqual(first["updated_at"], "2026-10-02T10:00:00Z")
        # the raw PII never appears anywhere in the serialised output
        blob = json.dumps(out)
        for secret in ("buyer@example.com", "cs_test_123", "coffee shop site", "555 123"):
            self.assertNotIn(secret, blob)

    def test_jobs_can_be_filtered_by_status(self) -> None:
        out = self.execute(JOBS, {"status": "live"})
        self.assertEqual([j["id"] for j in out["jobs"]], ["job_def456"])
        self.assertEqual(self.world.calls[0]["url"],
                         f"{SCROOGE}/dash/techne/jobs?status=live")
        self.assertEqual(self.world.calls[0]["method"], "GET")

    def test_invalid_status_is_refused_before_any_call(self) -> None:
        with self.assertRaisesRegex(AdapterProtocolError, "status"):
            self.adapter.validate(Task(JOBS, {"status": "not_a_state"}))
        self.assertEqual(self.world.calls, [])

    def test_unreachable_is_unavailable_not_a_refusal(self) -> None:
        self.world.down = True
        out = self.execute(JOBS, {})
        self.assertIs(out["ok"], False)
        self.assertIn("unavailable", out)
        self.assertNotIn("refused", out)
        self.assertNotIn(OPS_TOKEN, json.dumps(out))   # the URLError text held the token

    def test_scrooge_no_is_refused(self) -> None:
        self.world.forced["jobs"] = (400, {"ok": False, "error": "bad request"})
        out = self.execute(JOBS, {})
        self.assertIs(out["ok"], False)
        self.assertIn("refused", out)


# ---- techne.run: one allowlisted doer action, parked on the Desk -------------------------
GOOD_ACTIONS = {
    "claim": {"id": JOB_ID, "stage": "build"},
    "preview": {"id": JOB_ID, "preview_url": "https://acme.pages.dev"},
    "live": {"id": JOB_ID, "live_url": "https://acme.example"},
    "down": {"id": JOB_ID},
    "fail": {"id": JOB_ID, "note": "the build never finished"},
}
PATHS = {a: f"/dash/techne/jobs/{a}" for a in RUN_ACTIONS}


class RunValidateTests(_Case):
    def test_the_allowlist_is_exactly_the_five_doer_actions(self) -> None:
        self.assertEqual(set(RUN_ACTIONS), {"claim", "preview", "live", "down", "fail"})

    def test_every_allowlisted_action_with_good_args_validates(self) -> None:
        for action, payload in GOOD_ACTIONS.items():
            with self.subTest(action=action):
                self.adapter.validate(Task(RUN, {"action": action, **payload}))
        self.assertEqual(self.world.calls, [])   # validate parks/refuses, never calls

    def test_admin_actions_are_refused_never_parked(self) -> None:
        for bad in ("approve", "reject", "create", "attach", "requeue", "takedown",
                    "keep", "dismiss", "", "CLAIM"):
            with self.subTest(action=bad), self.assertRaisesRegex(AdapterProtocolError, "action"):
                self.adapter.validate(Task(RUN, {"action": bad, "id": JOB_ID}))

    def test_missing_or_bad_args_are_refused(self) -> None:
        bad = [
            {"action": "claim", "id": JOB_ID},                               # no stage
            {"action": "claim", "id": JOB_ID, "stage": "sideways"},          # bad stage
            {"action": "preview", "id": JOB_ID},                             # no preview_url
            {"action": "preview", "id": JOB_ID, "preview_url": "https://acme.example"},  # not *.pages.dev
            {"action": "live", "id": JOB_ID},                                # no live_url
            {"action": "fail", "id": JOB_ID},                                # no note
            {"action": "down"},                                             # no id
            {"action": "down", "id": JOB_ID, "extra": 1},                    # unknown field
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(AdapterProtocolError):
                self.adapter.validate(Task(RUN, payload))
        self.assertEqual(self.world.calls, [])

    def test_no_token_makes_run_unavailable_before_parking(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
            self.adapter.validate(Task(RUN, {"action": "down", "id": JOB_ID}))


class RunExecuteTests(_Case):
    def test_each_action_posts_to_its_path_with_its_body(self) -> None:
        for action, payload in GOOD_ACTIONS.items():
            with self.subTest(action=action):
                self.world.calls.clear()
                out = self.execute(RUN, {"action": action, **payload})
                self.assertIs(out["ok"], True, out)
                (call,) = self.world.calls
                self.assertEqual(call["method"], "POST")
                self.assertEqual(call["url"], f"{SCROOGE}{PATHS[action]}")
                self.assertEqual(call["token"], OPS_TOKEN)
                # the body is exactly the action's fields, never 'action' itself
                self.assertEqual(call["body"], payload)

    def test_scrooge_no_is_refused(self) -> None:
        self.world.forced["down"] = (409, {"ok": False, "error": "wrong status"})
        out = self.execute(RUN, {"action": "down", "id": JOB_ID})
        self.assertIs(out["ok"], False)
        self.assertIn("refused", out)

    def test_the_token_never_leaves_in_the_result(self) -> None:
        self.world.forced["fail"] = (200, {"ok": True, "echo": OPS_TOKEN})
        out = self.execute(RUN, {"action": "fail", "id": JOB_ID, "note": "nope"})
        self.assertNotIn(OPS_TOKEN, json.dumps(out))


# ---- status(): local only, no network ----------------------------------------------------
class StatusTests(_Case):
    def test_status_is_local_only(self) -> None:
        self.assertEqual(self.adapter.status(), {"url": SCROOGE, "token": "configured"})
        self.assertEqual(self.world.calls, [])   # no call to Scrooge

    def test_status_unavailable_without_a_token(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
            self.adapter.status()


# ---- the Desk gate, end to end through PionirApp -----------------------------------------
class ParksOnTheDeskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        token = self.root / "ops.txt"
        token.write_text(OPS_TOKEN + "\n", encoding="utf-8")
        self.world = FakeTechne()
        self.adapter = ScroogeTechneAdapter(
            ScroogeTechneSettings(base_url=SCROOGE, token_file=token), opener=self.world)
        # content_url=None: no real techne/client adapters; the faked one is used
        runtime = build_runtime(_settings(self.root, content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)
        self.addCleanup(self.app.runtime.cortex.close)

    def test_run_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            out = self.app.run_task(RUN, {"action": "down", "id": JOB_ID}, permissions=[RUN])
            self.assertEqual(out["status"], "pending_approval", out)
        self.assertEqual(len(self.app.approvals.pending()), 3)
        self.assertEqual(self.world.calls, [])   # nothing ran until the owner says yes

    def test_an_approved_run_executes_once(self) -> None:
        out = self.app.run_task(RUN, {"action": "down", "id": JOB_ID}, permissions=[RUN])
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        row = self.app.approvals.get(out["approval_id"])
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), ["down"])

    def test_reading_jobs_does_not_park(self) -> None:
        out = self.app.run_task(JOBS, {}, wait=30)
        self.assertIs(out["ok"], True, out)
        self.assertEqual(len(out["result"]["jobs"]), 2)
        self.assertEqual(self.app.approvals.pending(), [])


# ---- bootstrap registration --------------------------------------------------------------
class RegistrationTests(unittest.TestCase):
    def test_registered_with_content_url_and_the_toggle_on(self) -> None:
        cases = [
            ("https://api.dokaz.net", True, True),    # content on, toggle on -> present
            ("https://api.dokaz.net", False, False),  # content on, toggle off -> absent
            (None, True, False),                      # content off -> absent
        ]
        for content, monitor, present in cases:
            with self.subTest(content=content, monitor=monitor), \
                    tempfile.TemporaryDirectory() as root:
                runtime = build_runtime(_settings(
                    Path(root), content_url=content, devto_url=None, instagram_graph_url=None,
                    techne_monitor=monitor,
                    content_token_file=Path(root) / "no-token.txt",
                    ops_token_file=Path(root) / "no-ops.txt"))
                try:
                    self.assertEqual("scrooge" in runtime.adapters, present)
                    if present:
                        caps = {c.name for c in
                                runtime.adapters["scrooge"].manifest.capabilities}
                        self.assertEqual(caps, {JOBS, RUN})
                        # local health: no token, no call
                        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
                            runtime.adapters["scrooge"].status()
                finally:
                    runtime.cortex.close()


if __name__ == "__main__":
    unittest.main()
