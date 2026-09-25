"""The Discord approval gate: every parked action reaches the owner in Discord,
only the owner's reaction answers it, and an answer runs it through the same
approve path the phone uses - exactly once.

Discord itself is faked at the HTTP opener, so the real REST client (auth
header, 429 handling, 401 handling, token scrubbing) is what is under test.
"""
from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Self

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.discord_gate import (
    APPROVE,
    DENY,
    MESSAGE_LIMIT,
    STATUS_RESERVE,
    DiscordGate,
    DiscordGateSettings,
    DiscordRest,
    _escape,
    read_token,
)
from pionir.server import PionirApp

API = "https://discord.test/api/v10"
TOKEN = "MTk4NjIyNDgzNDcx.SECRET-bot-token.Zz9_q"
BOT_ID = "900000000000000001"
OWNER = "111111111111111111"
STRANGER = "222222222222222222"
CHANNEL = "333333333333333333"


def _app(tmp: str) -> PionirApp:
    return PionirApp(
        build_runtime(
            PionirSettings(
                state_root=Path(tmp),
                atani_command=("pionir-test-no-such-binary",),
                galatea_url="http://127.0.0.1:8799",
                galatea_model_id="stub-model",
                daedalus_url="http://127.0.0.1:9998",
                melete_url="http://127.0.0.1:9999",
                bryo_status_command=None,
                nyx_status_command=None,
                voodoo_status_command=None,
                evict_to_fit=False,
            )
        )
    )


class _Response:
    def __init__(self, payload: Any) -> None:
        self._raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeDiscord:
    """Just enough of Discord's REST API, in memory. Checks the bot token on
    every call exactly as Discord would (401 if it is wrong)."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.users = {
            OWNER: {"id": OWNER, "username": "ian", "global_name": "Ian"},
            STRANGER: {"id": STRANGER, "username": "mallory", "global_name": "Mallory"},
            BOT_ID: {"id": BOT_ID, "username": "pionir-gate", "bot": True},
        }
        self.down = False
        self.always: tuple[int, Any] | None = None
        self.fail: list[tuple[str, str, int, Any]] = []   # one-shot (method, path-prefix, ...)
        self._next = 1_300_000_000_000_000_000
        self.lock = threading.Lock()

    # -- test controls --------------------------------------------------
    def react(self, message_id: str, emoji: str, user_id: str) -> None:
        self.messages[message_id]["reactions"].setdefault(emoji, []).append(user_id)

    def delete(self, message_id: str) -> None:
        del self.messages[message_id]

    def posts(self) -> list[dict[str, Any]]:
        return [body for method, path, body in self.calls
                if method == "POST" and path.endswith("/messages")]

    def edits(self) -> list[dict[str, Any]]:
        return [body for method, _path, body in self.calls if method == "PATCH"]

    def content(self, message_id: str) -> str:
        return self.messages[message_id]["content"]

    # -- the opener -----------------------------------------------------
    def __call__(self, request: Any, timeout: float | None = None) -> _Response:
        with self.lock:
            method = request.get_method()
            url = request.full_url
            assert url.startswith(API), url
            path = url[len(API):]
            body = json.loads(request.data) if request.data else None
            self.calls.append((method, path, body))
            if self.down:
                raise urllib.error.URLError(f"connection refused (Bot {TOKEN})")
            if self.always is not None:
                self._error(url, *self.always)
            if request.get_header("Authorization") != f"Bot {TOKEN}":
                self._error(url, 401, {"message": "401: Unauthorized", "code": 0})
            for i, (m, prefix, status, payload) in enumerate(self.fail):
                if m == method and path.startswith(prefix):
                    del self.fail[i]
                    self._error(url, status, payload)
            return _Response(self._route(url, method, path, body))

    @staticmethod
    def _error(url: str, status: int, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(raw))  # type: ignore[arg-type]

    def _message_json(self, message: dict[str, Any]) -> dict[str, Any]:
        out = {"id": message["id"], "channel_id": message["channel_id"],
               "content": message["content"]}
        reactions = [{"emoji": {"id": None, "name": emoji}, "count": len(users),
                      "me": BOT_ID in users}
                     for emoji, users in message["reactions"].items() if users]
        if reactions:
            out["reactions"] = reactions
        return out

    def _route(self, url: str, method: str, path: str, body: Any) -> Any:
        parts = [urllib.parse.unquote(p) for p in path.split("?")[0].strip("/").split("/")]
        if parts == ["users", "@me"] and method == "GET":
            return self.users[BOT_ID]
        if parts[0] != "channels" or parts[1] != CHANNEL:
            self._error(url, 404, {"message": "Unknown Channel", "code": 10003})
        if len(parts) == 2 and method == "GET":
            return {"id": CHANNEL, "name": "approvals"}
        if parts[2:] == ["messages"] and method == "POST":
            self._next += 1
            mid = str(self._next)
            self.messages[mid] = {"id": mid, "channel_id": CHANNEL,
                                  "content": body["content"], "reactions": {}}
            return self._message_json(self.messages[mid])
        mid = parts[3]
        message = self.messages.get(mid)
        if message is None:
            self._error(url, 404, {"message": "Unknown Message", "code": 10008})
        if len(parts) == 4 and method == "GET":
            return self._message_json(message)
        if len(parts) == 4 and method == "PATCH":
            message["content"] = body["content"]
            return self._message_json(message)
        if len(parts) == 7 and parts[6] == "@me" and method == "PUT":
            users = message["reactions"].setdefault(parts[5], [])
            if BOT_ID not in users:
                users.append(BOT_ID)
            return None
        if len(parts) == 6 and method == "GET":
            return [self.users[u] for u in message["reactions"].get(parts[5], [])]
        self._error(url, 405, {"message": "fake: not handled", "code": 0})
        return None


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(self.format(r) for r in self.records)

    def at(self, level: int) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno == level]


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.app = _app(self._t.name)
        self.runs: list[tuple[str, dict[str, Any], list[str]]] = []

        def execute(capability: str, payload: dict[str, Any], granted: list[str],
                    *, deferrable: bool = False) -> dict[str, Any]:
            # stands in for the specialist; approve()'s claim/job/settle is real
            self.runs.append((capability, dict(payload), list(granted)))
            return {"ok": True, "agent_id": "daedalus", "result": {"ok": True}, "evidence": []}

        self.app._execute_task = execute  # type: ignore[method-assign]
        self.token_file = self.root / "secrets" / "discord-bot-token.txt"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.fake = FakeDiscord()
        self.sleeps: list[float] = []
        self.logs = _Capture()
        logger = logging.getLogger("pionir.discord_gate")
        self._old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self.logs)

    def tearDown(self) -> None:
        logger = logging.getLogger("pionir.discord_gate")
        logger.removeHandler(self.logs)
        logger.setLevel(self._old_level)
        self.app.runtime.cortex.close()
        self._t.cleanup()

    # -- helpers ----------------------------------------------------------
    def settings(self, owner: str | None = OWNER) -> DiscordGateSettings:
        return DiscordGateSettings(state_root=self.root, channel_id=CHANNEL,
                                   owner_user_id=owner, token_file=self.token_file,
                                   api_base=API, poll_seconds=0.01)

    def gate(self, owner: str | None = OWNER, **kwargs: Any) -> DiscordGate:
        if kwargs:
            return DiscordGate(self.settings(owner), opener=self.fake,
                               sleep=self.sleeps.append, **kwargs)
        return DiscordGate.for_app(self.app, self.settings(owner), opener=self.fake,
                                   sleep=self.sleeps.append)

    def park(self, payload: dict[str, Any] | None = None) -> str:
        out = self.app.run_task("coding.daedalus_solve",
                                payload or {"content": "refactor the parser"})
        self.assertEqual(out["status"], "pending_approval")
        return out["approval_id"]

    def enqueue(self, payload: dict[str, Any]) -> str:
        capability = "coding.daedalus_solve"
        return self.app.approvals.enqueue(capability, payload, ["daedalus.solve"],
                                          summary=self.app._summarize(capability, payload))

    def settle(self, approval_id: str) -> dict[str, Any]:
        row = self.app.approvals.get(approval_id)
        if row and row.get("task_id"):
            self.assertTrue(self.app.jobs.wait(row["task_id"], 30))
        return self.app.approvals.get(approval_id)

    def message_id(self, approval_id: str) -> str:
        state = json.loads(self.settings().state_path.read_text(encoding="utf-8"))
        return state["messages"][approval_id]["message_id"]


class PostingTests(GateCase):
    def test_posts_once_with_everything_it_will_do_and_adds_both_reactions(self):
        aid = self.park()
        gate = self.gate()
        self.assertTrue(gate.run_once())
        posts = self.fake.posts()
        self.assertEqual(len(posts), 1)
        content = posts[0]["content"]
        row = self.app.approvals.get(aid)
        self.assertIn("`coding.daedalus_solve`", content)
        self.assertIn(_escape(row["summary"]), content)
        self.assertIn("**Asked by:** moss", content)
        self.assertIn('"content": "refactor the parser"', content)   # the full payload
        self.assertIn(f"<@{OWNER}>", content)
        self.assertEqual(posts[0]["allowed_mentions"], {"parse": [], "users": [OWNER]})
        mid = self.message_id(aid)
        self.assertEqual(sorted(self.fake.messages[mid]["reactions"]), sorted([APPROVE, DENY]))
        # the next pass does not post it again
        self.assertTrue(gate.run_once())
        self.assertEqual(len(self.fake.posts()), 1)

    def test_a_command_is_shown_whole(self):
        args = ["-sV", "-p", "1-65535", "--script", "vuln", "10.0.0.5"]
        aid = self.enqueue({"action": "nmap", "args": args})
        self.gate().run_once()
        head = self.fake.content(self.message_id(aid))
        self.assertIn("nmap -sV -p 1-65535 --script vuln 10.0.0.5", head)

    def test_money_is_bold_on_the_first_line(self):
        self.enqueue({"content": "register pionir.dev",
                      "spend": {"amount": 12.5, "currency": "usd"}})
        self.gate().run_once()
        first = self.fake.posts()[0]["content"].split("\n")[0]
        self.assertEqual(first, "\U0001f4b8 **SPENDS MONEY: 12.50 USD**")

    def test_money_flag_without_amount_says_so_in_bold(self):
        self.enqueue({"content": "renew the thing", "spends_money": True})
        self.gate().run_once()
        first = self.fake.posts()[0]["content"].split("\n")[0]
        self.assertTrue(first.startswith("\U0001f4b8 **"))
        self.assertIn("AMOUNT NOT STATED", first)

    def test_no_money_no_money_line(self):
        self.park()
        self.gate().run_once()
        self.assertNotIn("MONEY", self.fake.posts()[0]["content"])

    def test_long_payload_is_split_across_messages_never_cut(self):
        words = " ".join(f"step-{i:04d}" for i in range(900))      # ~9 KB
        aid = self.enqueue({"content": words})
        self.gate().run_once()
        posts = [p["content"] for p in self.fake.posts()]
        self.assertGreater(len(posts), 1)
        self.assertLessEqual(len(posts[0]), MESSAGE_LIMIT - STATUS_RESERVE)
        for chunk in posts:
            self.assertLessEqual(len(chunk), MESSAGE_LIMIT)
            self.assertEqual(chunk.count("```") % 2, 0)             # every block closed
        body = "".join(line for chunk in posts for line in chunk.split("\n")
                       if not line.startswith("```"))
        self.assertIn(words, body)
        # the head still says what it does
        self.assertIn("**What it does:**", posts[0])
        self.assertIn("`coding.daedalus_solve`", posts[0])
        # and resolving it edits a status on top without cutting the head
        head = posts[0]
        self.app.deny(aid)
        self.gate().run_once()
        edited = self.fake.content(self.message_id(aid))
        self.assertLessEqual(len(edited), MESSAGE_LIMIT)
        self.assertTrue(edited.endswith(head))

    def test_one_bad_approval_does_not_block_the_others(self):
        self.park({"content": "first"})
        self.park({"content": "second"})
        self.fake.fail.append(("POST", f"/channels/{CHANNEL}/messages", 403,
                               {"message": "Missing Permissions", "code": 50013}))
        gate = self.gate()
        gate.run_once()
        self.assertEqual(len(self.fake.messages), 1)        # the second still went out
        self.assertIn("Missing Permissions", gate.state()["last_error"])
        self.assertTrue(self.logs.at(logging.WARNING))
        gate.run_once()                                       # and the first is retried
        self.assertEqual(len(self.fake.messages), 2)


class OwnerOnlyTests(GateCase):
    def test_a_strangers_reactions_do_nothing(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        mid = self.message_id(aid)
        self.fake.react(mid, APPROVE, STRANGER)
        gate.run_once()
        gate.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.runs, [])
        self.fake.react(mid, DENY, STRANGER)
        gate.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.fake.edits(), [])

    def test_owner_approval_runs_the_parked_action_exactly_once(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        mid = self.message_id(aid)
        self.fake.react(mid, APPROVE, STRANGER)
        self.fake.react(mid, APPROVE, OWNER)
        gate.run_once()
        row = self.settle(aid)
        self.assertEqual(row["status"], "approved")
        # it ran through PionirApp.approve: claimed as a job, exact permission
        self.assertEqual(self.app.jobs.get(row["task_id"])["kind"], "approval")
        self.assertEqual(self.runs, [("coding.daedalus_solve",
                                      {"content": "refactor the parser"}, ["daedalus.solve"])])
        for _ in range(3):
            gate.run_once()
        self.assertEqual(len(self.runs), 1)
        content = self.fake.content(mid)
        self.assertIn("**APPROVED** by **Ian** in Discord", content)
        self.assertIn("**ran**, finished OK", content)

    def test_a_failed_run_is_reported_as_failed(self):
        def failing(capability, payload, granted, *, deferrable=False):
            self.runs.append((capability, payload, granted))
            return {"ok": False, "error": {"type": "Boom", "message": "daedalus is down"}}

        self.app._execute_task = failing  # type: ignore[method-assign]
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        self.fake.react(self.message_id(aid), APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved_failed")
        gate.run_once()
        content = self.fake.content(self.message_id(aid))
        self.assertIn("ran and FAILED", content)
        self.assertIn("daedalus is down", content)

    def test_owner_deny_never_runs_it(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        self.fake.react(self.message_id(aid), DENY, OWNER)
        gate.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "denied")
        self.assertEqual(self.runs, [])
        self.assertIn("**DENIED** by **Ian** in Discord", self.fake.content(self.message_id(aid)))

    def test_both_reactions_from_the_owner_refuse(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        mid = self.message_id(aid)
        self.fake.react(mid, APPROVE, OWNER)
        self.fake.react(mid, DENY, OWNER)
        gate.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "denied")
        self.assertEqual(self.runs, [])

    def test_no_owner_configured_means_nothing_is_approvable(self):
        for owner in (None, "", "not-a-discord-id"):
            with self.subTest(owner=owner):
                fake = self.fake = FakeDiscord()
                aid = self.park()
                gate = self.gate(owner=owner)
                self.assertTrue(gate.run_once())
                post = [p for p in fake.posts()][-1]["content"]
                self.assertIn("cannot accept answers", post)
                mid = self.message_id(aid)
                self.assertEqual(fake.messages[mid]["reactions"], {})   # no buttons offered
                for who in (OWNER, STRANGER, BOT_ID):
                    fake.react(mid, APPROVE, who)
                gate.run_once()
                gate.run_once()
                self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
                self.assertEqual(self.runs, [])
                self.assertFalse(any("/reactions/" in p and m == "GET"
                                     for m, p, _b in fake.calls))
                state = gate.state()
                self.assertFalse(state["owner_configured"])
                self.assertFalse(state["accepting_answers"])
                self.app.deny(aid)
                gate.run_once()

    def test_unset_owner_in_environment_fails_closed(self):
        settings = DiscordGateSettings.from_environment(
            self.root, environ={"PIONIR_DISCORD_CHANNEL_ID": CHANNEL})
        self.assertIsNone(settings.owner)


class ExactlyOnceTests(GateCase):
    def test_owner_approval_after_a_phone_approval_is_not_run_again(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        self.assertTrue(self.app.approve(aid)["ok"])       # the phone got there first
        self.settle(aid)
        self.fake.react(self.message_id(aid), APPROVE, OWNER)
        gate.run_once()
        gate.run_once()
        self.assertEqual(len(self.runs), 1)
        content = self.fake.content(self.message_id(aid))
        self.assertIn("**APPROVED** outside Discord", content)
        self.assertNotIn("in Discord -", content)

    def test_owner_answer_racing_the_phone_says_already_resolved(self):
        aid = self.park()

        def racing_approve(approval_id: str) -> dict[str, Any]:
            self.app.approve(approval_id)      # the phone lands between read and act
            self.settle(approval_id)
            return self.app.approve(approval_id)

        gate = self.gate(approvals=self.app.approvals, approve=racing_approve,
                         deny=self.app.deny)
        gate.run_once()
        self.fake.react(self.message_id(aid), APPROVE, OWNER)
        gate.run_once()
        gate.run_once()
        self.assertEqual(len(self.runs), 1)
        content = self.fake.content(self.message_id(aid))
        self.assertIn("came after it was already approved elsewhere - nothing ran twice",
                      content)
        self.assertIn("**APPROVED** outside Discord", content)

    def test_phone_denial_is_shown(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        self.app.deny(aid)
        gate.run_once()
        self.assertIn("**DENIED** outside Discord", self.fake.content(self.message_id(aid)))


class DurabilityTests(GateCase):
    def test_restart_does_not_repost_and_keeps_following(self):
        aid = self.park()
        self.gate().run_once()
        self.assertEqual(len(self.fake.posts()), 1)
        mid = self.message_id(aid)
        again = self.gate()                   # a fresh process
        again.run_once()
        self.assertEqual(len(self.fake.posts()), 1)
        self.fake.react(mid, APPROVE, OWNER)
        again.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")
        self.assertEqual(len(self.runs), 1)

    def test_a_deleted_message_is_posted_again(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        old = self.message_id(aid)
        self.fake.delete(old)
        gate.run_once()
        new = self.message_id(aid)
        self.assertNotEqual(old, new)
        self.assertEqual(len(self.fake.posts()), 2)
        self.fake.react(new, APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")

    def test_an_update_for_a_deleted_message_is_posted_not_lost(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()
        self.fake.delete(self.message_id(aid))
        self.app.deny(aid)
        gate.run_once()
        self.assertIn("**DENIED**", self.fake.posts()[-1]["content"])

    def test_corrupt_state_file_is_kept_and_reported(self):
        path = self.settings().state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{nope", encoding="utf-8")
        self.park()
        self.gate().run_once()
        self.assertTrue(self.logs.at(logging.ERROR))
        self.assertTrue(list(path.parent.glob("gate.json.corrupt-*")))
        self.assertEqual(len(self.fake.posts()), 1)


class FailureTests(GateCase):
    def test_a_rejected_token_stops_the_loop_and_says_so_once(self):
        aid = self.park()
        self.fake.always = (401, {"message": "401: Unauthorized", "code": 0})
        gate = self.gate()
        self.assertTrue(gate.start())
        deadline = time.time() + 10
        while gate.running and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(gate.running)
        gate.stop()
        calls = len(self.fake.calls)
        time.sleep(0.1)
        self.assertEqual(len(self.fake.calls), calls)        # nothing is still polling
        self.assertFalse(gate.run_once())
        self.assertEqual(len(self.fake.calls), calls)
        state = gate.state()
        self.assertTrue(state["auth_failed"])
        self.assertIn("401", state["last_error"])
        errors = self.logs.at(logging.ERROR)
        self.assertEqual(len(errors), 1)
        self.assertIn("token rejected", errors[0].getMessage())
        # the queue does not care: the phone still works
        self.assertTrue(self.app.approve(aid)["ok"])
        self.assertEqual(self.settle(aid)["status"], "approved")

    def test_an_outage_is_logged_and_leaves_the_queue_alone(self):
        aid = self.park()
        self.fake.down = True
        gate = self.gate()
        self.assertFalse(gate.run_once())
        self.assertFalse(gate.run_once())
        self.assertIn("URLError", gate.state()["last_error"])
        self.assertEqual(len(self.logs.at(logging.WARNING)), 1)   # said once, not every poll
        self.assertEqual(gate.state()["errors"], 2)
        self.assertTrue(self.app.approve(aid)["ok"])              # the phone path is untouched
        self.assertEqual(self.settle(aid)["status"], "approved")
        self.fake.down = False
        other = self.park({"content": "after the outage"})
        self.assertTrue(gate.run_once())
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertIn(other, self.settings().state_path.read_text(encoding="utf-8"))

    def test_rate_limit_waits_then_retries(self):
        self.park()
        self.fake.fail.append(("POST", f"/channels/{CHANNEL}/messages", 429,
                               {"message": "You are being rate limited.", "retry_after": 1.5}))
        self.assertTrue(self.gate().run_once())
        self.assertEqual(self.sleeps, [1.5])
        self.assertEqual(len(self.fake.messages), 1)

    def test_approve_raising_is_logged_not_swallowed(self):
        aid = self.park()

        def broken(_approval_id: str) -> dict[str, Any]:
            raise RuntimeError("wiring bug")

        gate = self.gate(approvals=self.app.approvals, approve=broken, deny=self.app.deny)
        gate.run_once()
        self.fake.react(self.message_id(aid), APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertTrue(any(r.exc_info for r in self.logs.at(logging.ERROR)))

    def test_not_configured_does_not_start_and_says_why(self):
        missing = DiscordGateSettings(state_root=self.root, channel_id=CHANNEL,
                                      owner_user_id=OWNER,
                                      token_file=self.root / "nope.txt", api_base=API)
        gate = DiscordGate.for_app(self.app, missing, opener=self.fake)
        self.assertFalse(gate.start())
        self.assertIn("no bot token", gate.state()["reason"])
        no_channel = DiscordGateSettings(state_root=self.root, owner_user_id=OWNER,
                                         token_file=self.token_file, api_base=API)
        gate = DiscordGate.for_app(self.app, no_channel, opener=self.fake)
        self.assertFalse(gate.start())
        self.assertIn("channel", gate.state()["reason"])
        self.assertEqual(self.fake.calls, [])


class TokenTests(GateCase):
    def test_token_is_sent_only_as_the_auth_header_and_never_leaks(self):
        aid = self.park()
        gate = self.gate()
        gate.run_once()                                   # works: the header carried it
        self.assertEqual(len(self.fake.posts()), 1)
        echo = {"message": f"echo Authorization: Bot {TOKEN}", "code": 0}
        self.fake.fail.append(("GET", f"/channels/{CHANNEL}/messages/", 403, echo))
        gate.run_once()
        self.fake.fail.append(("GET", f"/channels/{CHANNEL}/messages/", 500,
                               f"<html>proxy saw Bot {TOKEN}</html>".encode()))
        gate.run_once()
        self.fake.down = True                             # URLError whose reason names it
        gate.run_once()
        self.fake.down = False
        self.fake.react(self.message_id(aid), APPROVE, OWNER)
        gate.run_once()
        self.settle(aid)
        gate.run_once()
        self.park({"content": "one more, so there is something to call Discord about"})
        self.fake.always = (401, {"message": f"bad token {TOKEN}", "code": 0})
        gate.run_once()

        self.assertTrue(gate.state()["auth_failed"])
        self.assertGreaterEqual(len(self.logs.records), 3)
        leaks = {
            "logs": self.logs.text(),
            "messages": json.dumps([b for _m, _p, b in self.fake.calls]),
            "state": json.dumps(gate.state()),
            "repr": repr(gate) + repr(gate.settings) + repr(gate._client),
            "state file": self.settings().state_path.read_text(encoding="utf-8"),
        }
        for where, text in leaks.items():
            self.assertNotIn(TOKEN, text, where)
        self.assertIn("<redacted>", self.logs.text())

    def test_read_token_tolerates_bom_whitespace_and_prefix(self):
        path = self.root / "t.txt"
        path.write_bytes(b"\xef\xbb\xbfBot " + TOKEN.encode() + b"\r\n")
        self.assertEqual(read_token(path), TOKEN)
        path.write_text("   \n", encoding="utf-8")
        self.assertIsNone(read_token(path))
        self.assertIsNone(read_token(self.root / "missing.txt"))
        self.assertNotIn(TOKEN, repr(DiscordRest(TOKEN)))


class SettingsTests(unittest.TestCase):
    def test_environment_overrides_the_setup_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "discord").mkdir()
            (root / "discord" / "config.json").write_text(json.dumps(
                {"channel_id": CHANNEL, "user_id": OWNER,
                 "token_file": str(root / "tok.txt")}), encoding="utf-8")
            s = DiscordGateSettings.from_environment(root, environ={})
            self.assertEqual((s.channel_id, s.owner), (CHANNEL, OWNER))
            self.assertEqual(s.token_file, root / "tok.txt")
            self.assertTrue(s.configured)
            self.assertEqual(s.state_path, root / "discord" / "gate.json")
            s = DiscordGateSettings.from_environment(root, environ={
                "PIONIR_DISCORD_USER_ID": STRANGER, "PIONIR_DISCORD_GATE": "0",
                "PIONIR_DISCORD_POLL_SECONDS": "7"})
            self.assertEqual(s.owner, STRANGER)
            self.assertFalse(s.configured)
            self.assertEqual(s.poll_seconds, 7.0)
            s = DiscordGateSettings.from_environment(root / "elsewhere", environ={
                "DISCORD_USER_ID": OWNER})
            self.assertEqual(s.owner, OWNER)
            self.assertEqual(s.token_file.name, "discord-bot-token.txt")
            self.assertEqual(s.token_file.parent.name, "secrets")


if __name__ == "__main__":
    unittest.main()
