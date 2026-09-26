"""Quote to paid, Pionir's side: the owner's Discord reply is the ONLY source of a price,
it becomes exactly one quote card for his yes, and nothing reaches a client without it.

Discord and Scrooge are faked at the HTTP opener (the real REST client, the real client
adapter and the real approval queue are under test); the crew's desks run against a fake
Pionir, as in test_crew_orders.py. Each test fails if the rule it names is reverted:
a reply from someone else read as a price, one reply turned into two cards (or two quotes)
by a second poll or a restart, a quote sent without the owner's yes, a split other than
the rule, a reminder twice, the work handed over while half the price is owed, a release
before the balance.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any

from test_client_adapter import OPS_TOKEN, SCROOGE, _Response, _settings
from test_client_deliver import GOOD_FILES, ZIP_NAME, DeliverScrooge, make_zip, sha
from test_client_deliver import _Case as DeliverCase
from test_crew_orders import FakePionir
from test_discord_gate import API, BOT_ID, CHANNEL, OWNER, STRANGER, FakeDiscord
from test_discord_gate import TOKEN as DISCORD_TOKEN

from pionir.adapters.clients import (
    DELIVER,
    QUOTE,
    RELEASE,
    REMIND,
    ClientAdapter,
    ClientSettings,
    check_quote,
)
from pionir.adapters.quote_cards import QuoteCardAdapter, QuoteCardSettings
from pionir.bootstrap import build_runtime
from pionir.contracts import RiskLevel
from pionir.crew import delivery as ddesk
from pionir.crew import orders as desk
from pionir.crew.hands import JobOutcome
from pionir.crew.registry import default_registry
from pionir.crew.worker import WorkContext
from pionir.discord_gate import APPROVE, DiscordGate, DiscordGateSettings, render_request
from pionir.quotes import (
    CARD,
    INTENT_HINT,
    Price,
    QuoteCardStore,
    QuoteSettings,
    build_quote,
    deposit_for,
    parse_reply,
    usd,
)
from pionir.server import PionirApp

ORDER = "c0c0c0c0c0c0"
CLIENT = "cy@example.com"
BRIEF = "Sync our two inventory spreadsheets every night and email me the differences."
PAY = "https://api.dokaz.net/pay/" + "a" * 64


# ---- the reply --------------------------------------------------------------------------------
class ReplyTests(unittest.TestCase):
    def test_the_owners_forms_are_read_exactly(self) -> None:
        for text, want in [("$350 7d", Price(35_000, 7)), ("350", Price(35_000, None)),
                           ("$1,200 10 days", Price(120_000, 10)),
                           ("$1,200.50 3 business days", Price(120_050, 3)),
                           ("  $2,500  14 days ", Price(250_000, 14))]:
            self.assertEqual(parse_reply(text), want, text)

    def test_anything_else_is_not_guessed_at(self) -> None:
        for text in ["", "   ", "abc", "350k", "$3507d", "1,20", "$350.5", "about 350",
                     "$350 7", "350 or 400", "0", "$5", "$60,000", "$350 0d", "$350 400d",
                     "-350", "$350 7d please"]:
            with self.assertRaises(ValueError, msg=text):
                parse_reply(text)

    def test_the_split_is_a_rule_not_a_suggestion(self) -> None:
        self.assertEqual(deposit_for(49_999, 50_000), 0)
        self.assertEqual(deposit_for(50_000, 50_000), 25_000)
        self.assertEqual(deposit_for(100_001, 50_000), 50_000)    # the balance carries the cent
        self.assertEqual(QuoteSettings.from_environment(
            {"PIONIR_QUOTE_DEPOSIT_THRESHOLD_CENTS": "100000"}).deposit_threshold_cents, 100_000)
        self.assertEqual(QuoteSettings.from_environment(
            {"PIONIR_QUOTE_DEPOSIT_THRESHOLD_CENTS": "lots"}).deposit_threshold_cents, 50_000)

    def test_the_email_states_exactly_what_is_charged(self) -> None:
        order = {"id": ORDER, "email": CLIENT, "name": "Cy"}
        q = build_quote(order, Price(120_000, None), QuoteSettings(), "1300000000000000999",
                        "$1,200")
        self.assertEqual((q["total_cents"], q["deposit_cents"], q["days"], q["days_defaulted"]),
                         (120_000, 60_000, 7, True))
        self.assertEqual(q["quote_ref"], "discord-1300000000000000999")
        for words in ("Price: $1,200 (USD)", "within 7 business days of your payment",
                      "- $600 now, a 50% deposit, before work starts",
                      "- $600 on delivery; your files are released once it is paid",
                      "{pay_link}", "valid for 14 days"):
            self.assertIn(words, q["body_text"])
        check_quote(q, 50_000)                                     # passes the adapter's check
        small = build_quote(order, Price(35_000, 5), QuoteSettings(), "1", "$350 5d")
        self.assertIn("Payment: in full, up front", small["body_text"])
        self.assertEqual(small["deposit_cents"], 0)

    def test_the_adapter_refuses_a_quote_whose_words_or_split_disagree(self) -> None:
        order = {"id": ORDER, "email": CLIENT, "name": "Cy"}
        q = build_quote(order, Price(120_000, 10), QuoteSettings(), "1300000000000000999", "x")
        for change, why in [({"deposit_cents": 0}, "deposit_cents"),
                            ({"total_cents": 130_000, "deposit_cents": 65_000},
                             r"must state \$1,300"),
                            ({"days": 9}, "9 business days"),
                            ({"body_text": q["body_text"].replace("{pay_link}", "")},
                             "exactly once"),
                            ({"note": "x"}, "not a quote field")]:
            with self.assertRaisesRegex(ValueError, why):
                check_quote({**q, **change}, 50_000)
        # the other Pionir's rule: a threshold of $2,000 takes no deposit on $1,200
        with self.assertRaisesRegex(ValueError, "deposit_cents"):
            check_quote(q, 200_000)


# ---- the fakes --------------------------------------------------------------------------------
class ReplyDiscord(FakeDiscord):
    """FakeDiscord plus the channel's message list (GET ...?after=), with authors and
    replies, as Discord's REST API returns it."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, dict[str, Any]] = {}      # message id -> author, reference

    def reply(self, to: str, content: str, user: str = OWNER) -> str:
        with self.lock:
            self._next += 1
            mid = str(self._next)
            self.messages[mid] = {"id": mid, "channel_id": CHANNEL, "content": content,
                                  "reactions": {}}
            self.meta[mid] = {"author": user, "ref": to}
            return mid

    def answers(self) -> list[dict[str, Any]]:
        """The bot's answers under a reply (message_reference set)."""
        return [b for m, p, b in self.calls if m == "POST" and p.endswith("/messages")
                and isinstance(b, dict) and "message_reference" in b]

    def _route(self, url: str, method: str, path: str, body: Any) -> Any:
        parts = [urllib.parse.unquote(p) for p in path.split("?")[0].strip("/").split("/")]
        if parts[:1] == ["channels"] and parts[2:] == ["messages"] and method == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            after = int(q["after"][0])
            limit = int(q.get("limit", ["50"])[0])
            out = []
            for mid in sorted(self.messages, key=int):
                if int(mid) <= after:
                    continue
                m = self.messages[mid]
                meta = self.meta.get(mid, {"author": BOT_ID})
                item = {"id": mid, "channel_id": CHANNEL, "content": m["content"],
                        "author": self.users.get(meta["author"], {"id": meta["author"]})}
                if meta.get("ref"):
                    item["message_reference"] = {"message_id": meta["ref"],
                                                 "channel_id": CHANNEL}
                out.append(item)
            return list(reversed(out[:limit]))           # Discord lists newest first
        return super()._route(url, method, path, body)


def an_order(**over: Any) -> dict[str, Any]:
    base = {"id": ORDER, "created_at": "2026-09-26T10:00:00Z", "name": "Cy", "email": CLIENT,
            "package": "custom", "brief": BRIEF, "status": "quote_requested",
            "amount_cents": None, "paid_at": None, "messages": [], "quote": None,
            "held_delivery": None}
    base.update(over)
    return base


class QuoteScrooge:
    """Scrooge's ops routes the quote path uses, in memory."""

    def __init__(self) -> None:
        self.orders = [an_order()]
        self.calls: list[dict[str, Any]] = []
        self.refs: set[str] = set()

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None) -> _Response:
        assert data is None
        parts = urllib.parse.urlsplit(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.calls.append({"method": request.get_method(), "path": parts.path,
                           "query": parts.query, "body": body})
        assert request.get_header("X-dash-token") == OPS_TOKEN
        if parts.path == "/dash/orders" and request.get_method() == "GET":
            wanted = urllib.parse.parse_qs(parts.query).get("id")
            rows = [o for o in self.orders if not wanted or o["id"] == wanted[0]]
            return _Response(200, {"ok": True, "orders": copy.deepcopy(rows)})
        if parts.path == "/dash/orders/quote":
            if body["quote_ref"] in self.refs:
                raise _http_error(request.full_url, 409, {"ok": False, "error": "quote_ref: "
                                                          "already used; nothing was sent"})
            self.refs.add(body["quote_ref"])
            return _Response(200, {"ok": True, "quote_id": "qt_" + "1" * 24,
                                   "message_id": "om_1"})
        raise AssertionError(f"unexpected call {parts.path}")

    def quotes(self) -> list[dict[str, Any]]:
        return [c["body"] for c in self.calls if c["path"] == "/dash/orders/quote"]


def _http_error(url: str, status: int, payload: Any) -> Exception:
    import io
    import urllib.error
    return urllib.error.HTTPError(url, status, "error", {},  # type: ignore[arg-type]
                                  io.BytesIO(json.dumps(payload).encode()))


class _Gate(unittest.TestCase):
    """A hermetic runtime: the client adapter on QuoteScrooge, the quote-card adapter and
    the gate on ReplyDiscord."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        secrets = self.root / "secrets"
        secrets.mkdir()
        self.ops_file = secrets / "scrooge-ops-token.txt"
        self.ops_file.write_text(OPS_TOKEN, encoding="utf-8")
        self.bot_file = secrets / "discord-bot-token.txt"
        self.bot_file.write_text(DISCORD_TOKEN, encoding="utf-8")
        self.scrooge = QuoteScrooge()
        self.discord = ReplyDiscord()
        self.state = self.root / "state"
        runtime = build_runtime(_settings(self.state, content_url=None))
        runtime.register(ClientAdapter(ClientSettings(base_url=SCROOGE, token_file=self.ops_file),
                                       opener=self.scrooge))
        runtime.register(QuoteCardAdapter(
            QuoteCardSettings.from_gate(self.settings()), opener=self.discord))
        self.app = PionirApp(runtime)
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def settings(self, owner: str | None = OWNER) -> DiscordGateSettings:
        return DiscordGateSettings(state_root=self.state, channel_id=CHANNEL,
                                   owner_user_id=owner, token_file=self.bot_file, api_base=API,
                                   poll_seconds=0.01)

    def gate(self, owner: str | None = OWNER) -> DiscordGate:
        g = DiscordGate.for_app(self.app, self.settings(owner), opener=self.discord,
                                sleep=lambda _s: None)
        if g._quotes is not None:
            g._quotes.every = 0.0
        return g

    def card(self) -> str:
        out = self.app.run_task(CARD, {"order_id": ORDER, "package": "Custom", "brief": BRIEF})
        self.assertTrue(out.get("ok"), out)                   # ran at once: never parked
        self.assertTrue(out["result"]["ok"], out)
        return str(out["result"]["message_id"])

    def quotes_pending(self) -> list[dict[str, Any]]:
        return [a for a in self.app.approvals.pending() if a["capability"] == QUOTE]

    def approve(self, approval_id: str) -> dict[str, Any]:
        res = self.app.approve(approval_id)
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(approval_id)


# ---- the card and the reply, end to end ---------------------------------------------------------
class ReplyToCardTests(_Gate):
    def test_card_reply_approval_quote_once(self) -> None:
        head = self.card()
        text = self.discord.content(head)
        self.assertIn(f"**QUOTE NEEDED** · order `{ORDER}`", text)
        self.assertIn(BRIEF, text)
        self.assertIn(f"<@{OWNER}>", text)
        # a second request for the card (the next desk run, a retry) posts nothing
        again = self.app.run_task(CARD, {"order_id": ORDER, "package": "Custom", "brief": BRIEF})
        self.assertTrue(again["result"]["already"])
        self.assertEqual(len(self.discord.posts()), 1)

        # someone else's reply is not read; the owner's is
        self.discord.reply(head, "$1 1d", user=STRANGER)
        mine = self.discord.reply(head, "$1,200 10 days")
        gate = self.gate()
        self.assertTrue(gate.run_once())
        (row,) = self.quotes_pending()
        p = row["payload"]
        self.assertEqual((p["order_id"], p["to"], p["total_cents"], p["deposit_cents"], p["days"]),
                         (ORDER, CLIENT, 120_000, 60_000, 10))
        self.assertEqual(p["quote_ref"], f"discord-{mine}")
        # nothing went to Scrooge: no quote, no link, no email before the yes
        self.assertEqual(self.scrooge.quotes(), [])
        # the approval card, posted in the same pass, shows exactly what goes out
        card = self.discord.content(gate._entries[row["id"]]["message_id"])
        body = "\n".join(self.discord.messages[m]["content"] for m in
                         [gate._entries[row["id"]]["message_id"],
                          *gate._entries[row["id"]]["extra_ids"]])
        self.assertTrue(card.startswith("\U0001f4b5 **SENDS A QUOTE**"))
        for words in ("**Price:** **$1,200** (USD)", "50% deposit **$600** before work starts",
                      "**$600** on delivery", "10 business days from payment",
                      "valid 14 days", "<private pay link>", "`$1,200 10 days`"):
            self.assertIn(words, body)
        self.assertIn(p["body_text"].replace("{pay_link}", "<private pay link>").split("\n")[4],
                      body)
        (answer,) = self.discord.answers()
        self.assertEqual(answer["message_reference"]["message_id"], mine)
        self.assertIn(f"approval `{row['id']}`", answer["content"])
        self.assertEqual(answer["allowed_mentions"], {"parse": [], "replied_user": False})

        # the next poll, and a restart, act on nothing twice
        self.assertTrue(gate.run_once())
        self.assertTrue(self.gate().run_once())
        self.assertEqual(len(self.quotes_pending()), 1)
        self.assertEqual(len(self.discord.answers()), 1)

        # the owner's yes: exactly the checked quote goes to Scrooge, once
        self.assertEqual(self.approve(row["id"])["status"], "approved")
        (sent,) = self.scrooge.quotes()
        self.assertEqual(sent, {k: p[k] for k in ("order_id", "to", "quote_ref", "total_cents",
                                                  "deposit_cents", "days", "subject",
                                                  "body_text")})

    def test_the_owners_check_mark_on_the_card_is_what_sends_it(self) -> None:
        head = self.card()
        self.discord.reply(head, "$350 7d")
        gate = self.gate()
        gate.run_once()
        (row,) = self.quotes_pending()
        mid = gate._entries[row["id"]]["message_id"]
        self.discord.react(mid, APPROVE, STRANGER)
        gate.run_once()
        self.assertEqual(self.scrooge.quotes(), [])                 # not his to approve
        self.discord.react(mid, APPROVE, OWNER)
        gate.run_once()
        self.app.jobs.wait(self.app.approvals.get(row["id"])["task_id"], 30)
        self.assertEqual(len(self.scrooge.quotes()), 1)
        self.assertEqual(self.scrooge.quotes()[0]["deposit_cents"], 0)

    def test_a_non_owner_reply_alone_does_nothing_at_all(self) -> None:
        head = self.card()
        self.discord.reply(head, "$350 7d", user=STRANGER)
        self.gate().run_once()
        self.assertEqual(self.quotes_pending(), [])
        self.assertEqual(self.discord.answers(), [])
        self.assertEqual([c for c in self.scrooge.calls], [])       # not even the order is read

    def test_no_owner_configured_no_reply_is_read(self) -> None:
        head = self.card()
        self.discord.reply(head, "$350 7d")
        self.gate(owner=None).run_once()
        self.assertEqual(self.quotes_pending(), [])
        self.assertFalse(any("messages?after" in p for _m, p, _b in self.discord.calls))

    def test_a_restart_between_the_claim_and_the_record_finds_the_card_instead_of_posting_another(self) -> None:
        head = self.card()
        mine = self.discord.reply(head, "$350 7d")
        gate = self.gate()
        gate.run_once()
        (row,) = self.quotes_pending()
        # the crash: the record says only "claimed", and the cursor never moved
        store = QuoteCardStore.for_state_root(self.state)

        def forget(doc: dict[str, Any]) -> None:
            doc["cursor"] = None
            doc["cards"][ORDER]["replies"][mine] = {"state": "claimed", "text": "$350 7d",
                                                    "tries": 0, "no_content": False}
        store.update(forget)
        self.gate().run_once()
        self.assertEqual([r["id"] for r in self.quotes_pending()], [row["id"]])
        self.assertEqual(store.read()["cards"][ORDER]["replies"][mine]["approval_id"], row["id"])

    def test_even_a_lost_record_cannot_double_a_quote(self) -> None:
        head = self.card()
        self.discord.reply(head, "$350 7d")
        self.gate().run_once()
        (row,) = self.quotes_pending()
        self.approve(row["id"])
        store = QuoteCardStore.for_state_root(self.state)
        store.update(lambda d: d["cards"][ORDER].update(replies={}) or d.update(cursor=None))
        self.gate().run_once()
        self.assertEqual(self.quotes_pending(), [])
        self.assertEqual(len(self.scrooge.quotes()), 1)

    def test_a_newer_reply_withdraws_the_older_card(self) -> None:
        head = self.card()
        self.discord.reply(head, "$350 7d")
        gate = self.gate()
        gate.run_once()
        (first,) = self.quotes_pending()
        self.discord.reply(head, "$420 7d")
        gate.run_once()
        (second,) = self.quotes_pending()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.app.approvals.get(first["id"])["status"], "denied")
        self.assertEqual(second["payload"]["total_cents"], 42_000)
        self.assertIn("withdrawn", self.discord.answers()[-1]["content"])

    def test_an_unreadable_reply_is_answered_and_sends_nothing(self) -> None:
        head = self.card()
        self.discord.reply(head, "maybe 350ish?")
        self.gate().run_once()
        self.assertEqual(self.quotes_pending(), [])
        self.assertIn("couldn't read that as a price", self.discord.answers()[-1]["content"])

    def test_an_empty_reply_means_the_message_content_intent_is_off_and_says_so(self) -> None:
        head = self.card()
        self.discord.reply(head, "")
        self.gate().run_once()
        self.assertEqual(self.quotes_pending(), [])
        self.assertEqual(self.discord.answers()[-1]["content"], INTENT_HINT)
        self.assertIn("Message Content Intent", INTENT_HINT)

    def test_an_order_no_longer_waiting_on_a_quote_is_refused_and_its_card_closed(self) -> None:
        head = self.card()
        self.scrooge.orders[0]["status"] = "paid"
        self.discord.reply(head, "$350 7d")
        gate = self.gate()
        gate.run_once()
        self.assertEqual(self.quotes_pending(), [])
        self.assertIn("can't be quoted", self.discord.answers()[-1]["content"])
        self.assertFalse(QuoteCardStore.for_state_root(self.state).read()["cards"][ORDER]["open"])
        self.discord.reply(head, "$360 7d")
        gate.run_once()
        self.assertEqual(len(self.discord.answers()), 1)

    def test_a_quoted_order_can_be_re_quoted_by_a_new_reply(self) -> None:
        head = self.card()
        self.discord.reply(head, "$350 7d")
        gate = self.gate()
        gate.run_once()
        self.approve(self.quotes_pending()[0]["id"])
        self.scrooge.orders[0]["status"] = "quoted"
        self.discord.reply(head, "$300 7d")
        gate.run_once()
        (row,) = self.quotes_pending()
        self.approve(row["id"])
        self.assertEqual([q["total_cents"] for q in self.scrooge.quotes()], [35_000, 30_000])
        self.assertEqual(len({q["quote_ref"] for q in self.scrooge.quotes()}), 2)

    def test_the_quote_parks_every_time_even_with_its_permission(self) -> None:
        order = an_order()
        payload = build_quote(order, Price(35_000, 7), QuoteSettings(), "1300000000000000001", "x")
        out = self.app.run_task(QUOTE, payload, permissions=[QUOTE])
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.scrooge.quotes(), [])
        caps = {c.name: c for m in self.app.runtime.executive.registry.manifests()
                for c in m.capabilities}
        for name in (QUOTE, REMIND, RELEASE):
            self.assertTrue(caps[name].requires_approval, name)
            self.assertIs(caps[name].risk, RiskLevel.PRIVILEGED)
        self.assertIs(caps[CARD].risk, RiskLevel.REVERSIBLE_WRITE)


# ---- the order desk -----------------------------------------------------------------------------
T0 = 1_790_000_000.0
DAY = 86400.0


def iso(t: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat()


def quoted(total: int = 120_000, deposit: int = 60_000, first: str = "open",
           balance: str | None = None, qid: str = "qt_" + "1" * 24, created: float = T0,
           reminded: str | None = None) -> dict[str, Any]:
    return {"id": qid, "total_cents": total, "deposit_cents": deposit,
            "balance_cents": total - deposit if deposit else 0, "days": 10,
            "created_at": iso(created), "remind_from": iso(created + 10 * DAY),
            "reminded_at": reminded,
            "first": {"part": "deposit" if deposit else "full",
                      "amount_cents": deposit or total, "state": first,
                      "expires_at": iso(created + 14 * DAY)},
            "balance": None if balance is None else {"part": "balance",
                                                     "amount_cents": total - deposit,
                                                     "state": balance,
                                                     "expires_at": iso(created + 30 * DAY)}}


class _Desk(unittest.TestCase):
    worker_id = "contracts.orders"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require(self.worker_id)
        self.pionir = FakePionir()

    def run_at(self, now: float):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=self.pionir.job,
                          approval=self.pionir.approval, state_dir=self.state,
                          deliveries_dir=self.state / "deliveries")
        return self.worker.run(ctx)

    def order(self, **over: Any) -> dict[str, Any]:
        o = an_order(created_at=T0 - 3600)
        o.update(over)
        return o


class OrderDeskQuoteTests(_Desk):
    def test_a_quote_request_gets_its_acknowledgement_and_ONE_quote_card(self) -> None:
        self.pionir.orders = [self.order()]
        self.run_at(T0)
        (card,) = self.pionir.cards()
        self.assertEqual(card.payload, {"order_id": ORDER, "package": "Custom", "brief": BRIEF})
        self.assertEqual([j.payload["subject"] for j in self.pionir.emails()],
                         [f"We've received your Dokaz request {ORDER}"])
        self.run_at(T0 + 900)
        self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.cards()), 1)

    def test_a_flagged_brief_gets_no_card_until_the_owner_denies_the_decline(self) -> None:
        self.pionir.orders = [self.order(brief="Scrape emails from LinkedIn profiles for me.")]
        self.run_at(T0)
        self.assertEqual(self.pionir.cards(), [])
        self.pionir.deny("em-1")
        self.run_at(T0 + 900)
        self.assertEqual(len(self.pionir.cards()), 1)

    def test_the_day_10_reminder_goes_once_per_quote_and_never_early(self) -> None:
        self.pionir.orders = [self.order(status="quoted", quote=quoted())]
        self.run_at(T0 + 9 * DAY)
        self.assertEqual([j for j in self.pionir.jobs if j.capability == desk.REMIND], [])
        self.run_at(T0 + 10 * DAY)
        (job,) = [j for j in self.pionir.jobs if j.capability == desk.REMIND]
        self.assertEqual(job.payload["quote_id"], "qt_" + "1" * 24)
        self.assertIn("($1,200) is still open", job.payload["body_text"])
        self.assertNotIn("http", job.payload["body_text"])
        self.pionir.approve("em-1")
        for t in (T0 + 10 * DAY + 900, T0 + 11 * DAY, T0 + 13 * DAY):
            self.run_at(t)
        self.assertEqual(len([j for j in self.pionir.jobs if j.capability == desk.REMIND]), 1)
        # a re-quote is a new quote: it may have its own reminder, from its own day 10
        self.pionir.orders = [self.order(status="quoted",
                                         quote=quoted(qid="qt_" + "2" * 24, created=T0 + 12 * DAY,
                                                      total=100_000, deposit=50_000))]
        self.run_at(T0 + 21 * DAY)
        self.assertEqual(len([j for j in self.pionir.jobs if j.capability == desk.REMIND]), 1)
        self.run_at(T0 + 22 * DAY)
        self.assertEqual(len([j for j in self.pionir.jobs if j.capability == desk.REMIND]), 2)

    def test_no_reminder_for_a_quote_paid_expired_or_already_reminded(self) -> None:
        for q in (quoted(first="paid"), quoted(first="expired"), quoted(reminded=iso(T0))):
            self.pionir = FakePionir()
            self.state = Path(tempfile.mkdtemp(dir=self.state))
            self.pionir.orders = [self.order(status="quoted", quote=q)]
            self.run_at(T0 + 11 * DAY)
            self.assertEqual([j for j in self.pionir.jobs if j.capability == desk.REMIND], [])

    def test_a_paid_deposit_is_confirmed_with_the_balance_and_the_order_started(self) -> None:
        self.pionir.orders = [self.order(status="paid", amount_cents=60_000,
                                         quote=quoted(first="paid"))]
        self.run_at(T0)
        (job,) = self.pionir.emails()
        body = job.payload["body_text"]
        self.assertEqual(job.payload["subject"], f"Payment received for your Dokaz order {ORDER}")
        self.assertIn("your payment of $600 for request", body)
        self.assertIn("The balance of $600 is due when the work is delivered", body)
        self.assertIn("within 10 business days", body)
        self.assertEqual(self.pionir.statuses(), [])                  # not before it is sent
        self.pionir.approve("em-1")
        self.run_at(T0 + 900)
        self.assertEqual(self.pionir.statuses(), [{"order_id": ORDER, "status": "in_progress"}])

    def test_a_quote_paid_in_full_says_nothing_more_is_owed(self) -> None:
        self.pionir.orders = [self.order(status="paid", amount_cents=35_000,
                                         quote=quoted(total=35_000, deposit=0, first="paid"))]
        self.run_at(T0)
        (job,) = self.pionir.emails()
        self.assertIn("your payment of $350", job.payload["body_text"])
        self.assertIn("there is nothing more to pay", job.payload["body_text"])

    def test_a_custom_order_paid_without_its_pay_link_is_held_for_the_owner(self) -> None:
        self.pionir.orders = [self.order(status="paid", amount_cents=35_000, quote=None)]
        self.run_at(T0)
        self.assertEqual(self.pionir.emails(), [])


# ---- the delivery desk: held for the balance, then released -------------------------------------
class DeliveryDeskBalanceTests(_Desk):
    worker_id = "contracts.delivery"

    def put_zip(self) -> str:
        folder = self.state / "deliveries" / ORDER
        folder.mkdir(parents=True, exist_ok=True)
        data = make_zip(GOOD_FILES)
        (folder / ZIP_NAME).write_bytes(data)
        return sha(data)

    def job(self, job):
        self.pionir.jobs.append(job)
        n = len(self.pionir.jobs)
        if job.capability == desk.ORDERS:
            return JobOutcome("done", desk.ORDERS,
                              result={"ok": True, "orders": copy.deepcopy(self.pionir.orders)})
        return JobOutcome("pending_approval", job.capability, task_id=f"t{n}",
                          approval_id=f"ap-{n}")

    def run_at(self, now: float):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=self.job,
                          approval=self.pionir.approval, state_dir=self.state,
                          deliveries_dir=self.state / "deliveries")
        return self.worker.run(ctx)

    def test_the_work_is_held_until_the_balance_then_released_on_a_second_yes(self) -> None:
        digest = self.put_zip()
        self.pionir.orders = [self.order(status="in_progress", amount_cents=60_000,
                                         quote=quoted(first="paid"))]
        later = T0 + 10**6
        self.run_at(later)
        (job,) = [j for j in self.pionir.jobs if j.capability == DELIVER]
        p = job.payload
        self.assertIs(p["hold_for_balance"], True)
        self.assertEqual(p["zip_sha256"], digest)
        self.assertIn("The balance of $600 is now due", p["body_text"])
        self.assertIn("{link}", p["body_text"])
        self.assertNotIn("Download it here", p["body_text"])
        record = json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))
        (entry,) = record["deliveries"]
        self.assertEqual((entry["kind"], entry["status"]), (ddesk.HELD, "pending_approval"))
        aid = entry["approval_id"]
        self.pionir.approvals[aid] = {"status": "approved", "result": {
            "ok": True, "result": {"ok": True, "emailed": True, "held": True,
                                   "delivery_id": "dl_" + "9" * 24}}}
        # Scrooge moved it to balance_due: nothing more is asked of the owner
        self.pionir.orders[0].update(status="balance_due",
                                     held_delivery={"id": "dl_" + "9" * 24, "released_at": None})
        self.run_at(later + 900)
        self.run_at(later + 1800)
        self.assertEqual(len([j for j in self.pionir.jobs if j.capability in (DELIVER, RELEASE)]),
                         1)
        record = json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(record["deliveries"][0]["status"], "held")
        # the balance is paid: ONE release, of the held delivery, with the download template
        self.pionir.orders[0].update(status="balance_paid", amount_cents=120_000,
                                     quote=quoted(first="paid", balance="paid"))
        self.run_at(later + 2700)
        self.run_at(later + 3600)
        (rel,) = [j for j in self.pionir.jobs if j.capability == RELEASE]
        self.assertEqual(rel.payload["delivery_id"], "dl_" + "9" * 24)
        self.assertIn("your balance is paid", rel.payload["body_text"])
        self.assertIn("{link}", rel.payload["body_text"])

    def test_no_release_while_the_balance_is_unpaid(self) -> None:
        self.pionir.orders = [self.order(status="balance_due", quote=quoted(first="paid"),
                                         held_delivery={"id": "dl_" + "9" * 24,
                                                        "released_at": None})]
        self.run_at(T0)
        self.assertEqual([j for j in self.pionir.jobs if j.capability == RELEASE], [])

    def test_an_order_paid_in_full_is_delivered_as_before(self) -> None:
        self.put_zip()
        self.pionir.orders = [self.order(status="in_progress", amount_cents=35_000,
                                         quote=quoted(total=35_000, deposit=0, first="paid"))]
        self.run_at(T0 + 10**6)
        (job,) = [j for j in self.pionir.jobs if j.capability == DELIVER]
        self.assertNotIn("hold_for_balance", job.payload)
        self.assertIn("Download it here", job.payload["body_text"])


# ---- the client adapter: a held delivery, and its release -----------------------------------------
class HoldScrooge(DeliverScrooge):
    def __init__(self) -> None:
        super().__init__()
        self.hold = True
        self.released: list[dict[str, Any]] = []
        self.release_answer: tuple[int, Any] | None = None

    def _handle(self, request: Any, timeout: float | None) -> _Response:
        parts = urllib.parse.urlsplit(request.full_url)
        if parts.path == "/dash/orders/delivery/release":
            body = json.loads(request.data)
            self.calls.append({"step": "release", "body": body})
            self.released.append(body)
            if self.release_answer:
                status, payload = self.release_answer
                self._error(request.full_url, status, payload)
            return _Response(200, {"ok": True, "delivery_id": body["delivery_id"],
                                   "url": "https://api.dokaz.net/d/" + "b" * 64,
                                   "expires_at": "2026-11-01T00:00:00Z"})
        out = super()._handle(request, timeout)
        if parts.path == "/dash/orders/delivery" and self.hold:
            doc = json.loads(out._raw)
            doc.pop("url")
            doc.update(held=True, balance={"url": PAY, "amount_cents": 60_000,
                                           "expires_at": "2026-10-10T00:00:00Z"})
            return _Response(200, doc)
        return out


BALANCE_BODY = ("Hi Sam,\n\nYour work is ready. The balance of $600 is due; pay it here:\n"
                "{link}\n\nYour files are released once it is paid.\n\nDokaz\n")


class HeldDeliveryTests(DeliverCase):
    def setUp(self) -> None:
        super().setUp()
        self.world = HoldScrooge()
        self.adapter._open = self.world

    def held(self, **over: Any) -> dict[str, Any]:
        from test_client_deliver import a_delivery
        return a_delivery(self.data, body_text=BALANCE_BODY, hold_for_balance=True, **over)

    def test_a_held_delivery_emails_the_balance_link_and_never_marks_delivered(self) -> None:
        row = self.deliver(self.held())
        self.assertEqual(row["status"], "approved", row)
        steps = [c["step"] for c in self.world.calls]
        self.assertEqual(steps, ["delivery", "email"])                # no status change
        email = self.world.calls[-1]["body"]
        self.assertIn(PAY, email["body_text"])
        self.assertNotIn("/d/", email["body_text"])
        self.assertTrue(row["result"]["result"]["held"])

    def test_scrooge_holding_a_delivery_the_email_did_not_expect_sends_nothing(self) -> None:
        from test_client_deliver import a_delivery
        row = self.deliver(a_delivery(self.data))
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual([c["step"] for c in self.world.calls], ["delivery"])
        self.assertIn("HELD", row["result"]["result"]["refused"])

    def test_a_balance_email_for_an_order_that_owes_nothing_is_revoked_unsent(self) -> None:
        self.world.hold = False
        row = self.deliver(self.held())
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual([c["step"] for c in self.world.calls], ["delivery", "revoke"])

    def release(self) -> dict[str, Any]:
        return {"order_id": self.world.orders[0]["id"], "to": self.world.orders[0]["email"],
                "delivery_id": "dl_" + "9" * 24, "subject": "Your files",
                "body_text": "Hi Sam,\n\nThe balance is paid. Download here:\n{link}\n\nDokaz\n"}

    def test_a_release_parks_then_emails_the_download_link_and_marks_delivered(self) -> None:
        out = self.app.run_task(RELEASE, self.release(), permissions=[RELEASE])
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.world.released, [])
        row = self.approve(out)
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual([c["step"] for c in self.world.calls], ["release", "email", "status"])
        self.assertIn("https://api.dokaz.net/d/" + "b" * 64, self.world.calls[1]["body"]["body_text"])
        self.assertEqual(self.world.calls[2]["body"]["status"], "delivered")

    def test_a_release_scrooge_refuses_emails_nothing(self) -> None:
        self.world.release_answer = (409, {"ok": False, "error": "order: its balance is not paid"})
        row = self.approve(self.app.run_task(RELEASE, self.release()))
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual([c["step"] for c in self.world.calls], ["release"])
        self.assertIn("nothing was released or emailed", row["result"]["result"]["refused"])

    def test_the_cards_say_what_is_held_and_what_is_released(self) -> None:
        text = render_request({"id": "a1", "capability": DELIVER, "payload": self.held()},
                              OWNER, delivery={"ok": True, "size": 1, "sha256": "0" * 64,
                                               "files": [], "secret_values": 0})
        self.assertIn("**HELD FOR THE BALANCE**", text)
        self.assertIn("<balance pay link>", text)
        text = render_request({"id": "a2", "capability": RELEASE, "payload": self.release()},
                              OWNER)
        self.assertTrue(text.startswith("\U0001f4e6 **RELEASES A HELD DELIVERY**"))
        self.assertIn("<private download link>", text)


class RenderTests(unittest.TestCase):
    def test_the_quote_card_shows_the_default_days_plainly(self) -> None:
        payload = build_quote(an_order(), Price(35_000, None), QuoteSettings(), "1", "350")
        text = render_request({"id": "a1", "capability": QUOTE, "payload": payload}, OWNER)
        self.assertIn("**the default: your reply gave no days**", text)
        self.assertIn(f"**Payment:** in full, **{usd(35_000)}**, up front", text)
        self.assertNotIn("SPENDS MONEY", text)


if __name__ == "__main__":
    unittest.main()
