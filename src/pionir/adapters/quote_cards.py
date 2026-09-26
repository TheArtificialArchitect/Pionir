"""``quotes.card``: post the ONE quote card for a custom order, in the Discord approvals
channel, for the owner to answer with a price.

The order desk (crew/orders.py) calls it for every custom order waiting on a quote. The
card names the order, shows the client's whole brief, and tells the owner to REPLY to it
with the price and delivery time (``$350 7d``). It carries no reactions and runs nothing:
the Discord gate reads the owner's reply (pionir/quotes.py ``QuoteReplies``) and turns it
into a ``client.quote`` approval card, which is what the owner approves.

REVERSIBLE_WRITE and never parked: it only posts to the owner's own channel, pings only the
owner, contacts no client and moves no money. Idempotent per order: a card already posted
(recorded in ``<state_root>/discord/quote-cards.json``) is answered with its message id and
not posted again - a retry, a second desk run or a restart never doubles it. A post still
in flight (reserved in the record but without a message id) is not repeated for two
minutes; after that the reservation is taken to have died with its process and the card is
posted.

The bot token is the Discord gate's own (the same file), read when a card is posted, used
only in the Authorization header and scrubbed from every error. Without an owner user id
configured nothing could ever answer a card, so none is posted (``unavailable``).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.quotes import CARD, FORMAT_HINT, QuoteCardStore, QuoteSettings, usd

_log = logging.getLogger(__name__)

FIELDS = frozenset({"order_id", "package", "brief"})
MAX_BRIEF = 6000
IN_FLIGHT = timedelta(minutes=2)
_ORDER_ID = re.compile(r"[0-9a-f]{12}")
_FENCE = "```"


@dataclass(frozen=True)
class QuoteCardSettings:
    """Where the card goes (the Discord gate's channel, bot token and owner) and where the
    card record lives. Holds the PATH of the token file, never the token."""

    state_root: Path
    channel_id: str | None
    owner_user_id: str | None
    token_file: Path | None
    api_base: str
    enabled: bool = True
    timeout: float = 20.0

    @classmethod
    def from_gate(cls, gate: Any) -> QuoteCardSettings:
        return cls(state_root=Path(gate.state_root), channel_id=gate.channel_id,
                   owner_user_id=gate.owner, token_file=Path(gate.token_file),
                   api_base=gate.api_base, enabled=bool(gate.enabled))

    def why_not(self) -> str | None:
        if not self.enabled:
            return "the Discord gate is disabled (PIONIR_DISCORD_GATE=0)"
        if not self.channel_id:
            return "no Discord channel is configured (PIONIR_DISCORD_CHANNEL_ID)"
        if not self.owner_user_id:
            return ("no owner Discord user id is configured (PIONIR_DISCORD_USER_ID): nobody "
                    "could answer a quote card")
        if self.token_file is None:
            return "no Discord bot token file is configured"
        return None


def _fence_safe(text: str) -> str:
    return text.replace(_FENCE, "`\u200b`\u200b`")


def render_card(order_id: str, package: str, brief: str, owner: str,
                quotes: QuoteSettings) -> str:
    """The card, before it is split to fit Discord. The brief is shown whole, in a block."""
    return "\n".join([
        f"\U0001f4ac **QUOTE NEEDED** · order `{order_id}` ({_fence_safe(package)})",
        f"**Reply to this message** with the price. {FORMAT_HINT}",
        (f"Only <@{owner}>'s reply counts. Your reply sends nothing: it becomes a quote email "
         "card for your ✅, and the pay link is created only on that ✅. A newer reply "
         "replaces an older one; reply again after 14 days to re-quote."),
        (f"At or above {usd(quotes.deposit_threshold_cents)} the client pays 50% up front and "
         "50% on delivery (the files are held until then); below it, in full up front. With "
         f"no days in your reply, delivery is {quotes.default_days} business days."),
        f"**The brief, in full ({len(brief):,} characters):**",
        _FENCE + "text", _fence_safe(brief), _FENCE,
    ])


class QuoteCardAdapter:
    """``quotes.card`` as an audited Pionir capability."""

    def __init__(self, settings: QuoteCardSettings, *, quotes: QuoteSettings | None = None,
                 opener: Callable[..., Any] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.quotes = quotes or QuoteSettings.from_environment()
        self.store = QuoteCardStore.for_state_root(settings.state_root)
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(UTC))
        self._manifest = AgentManifest(
            agent_id="quotes", version="pionir/quotes",
            capabilities=(Capability(
                name=CARD,
                description="Post the one quote card for a custom order in the owner's Discord "
                            "channel, for him to reply to with a price (posts to the owner "
                            "only; contacts no client)",
                risk=RiskLevel.REVERSIBLE_WRITE, routable=False),),
        )

    def __repr__(self) -> str:
        return f"QuoteCardAdapter(channel={self.settings.channel_id!r}, token=<read at send time>)"

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        why = self.settings.why_not()
        if why:
            raise AdapterUnavailable(f"{CARD}: {why}")
        cards = self.store.read()["cards"]
        return {"ok": True, "cards": len(cards),
                "open": sum(1 for c in cards.values() if c.get("open"))}

    def validate(self, task: Task) -> None:
        self._card(task)

    @staticmethod
    def _card(task: Task) -> tuple[str, str, str]:
        if task.capability != CARD:
            raise AdapterProtocolError(f"the quotes adapter has no capability {task.capability!r}")
        p = task.payload
        extra = sorted(set(p) - FIELDS)
        if extra:
            raise AdapterProtocolError(f"{CARD}: {extra[0]}: not a card field (order_id, "
                                       "package, brief)")
        oid, package, brief = p.get("order_id"), p.get("package"), p.get("brief")
        if not isinstance(oid, str) or not _ORDER_ID.fullmatch(oid):
            raise AdapterProtocolError(f"{CARD}: order_id: 12 lowercase hex characters")
        if not isinstance(package, str) or not 1 <= len(package) <= 40:
            raise AdapterProtocolError(f"{CARD}: package: 1-40 characters")
        if not isinstance(brief, str) or not brief.strip() or len(brief) > MAX_BRIEF:
            raise AdapterProtocolError(f"{CARD}: brief: 1-{MAX_BRIEF} characters")
        return oid, package, brief

    def execute(self, task: Task) -> TaskResult:
        oid, package, brief = self._card(task)
        why = self.settings.why_not()
        if why:
            return self._result(task, {"ok": False, "unavailable": f"{CARD}: {why}",
                                       "error": f"{CARD}: {why}"})
        now = self._clock()

        def reserve(doc: dict[str, Any]) -> dict[str, Any] | None:
            card = doc["cards"].get(oid)
            if card and card.get("message_ids"):
                return {"ok": True, "already": True, "message_id": card["message_ids"][0]}
            if card and card.get("reserved_at"):
                try:
                    at = datetime.fromisoformat(str(card["reserved_at"]))
                except ValueError:
                    at = now - IN_FLIGHT
                if now - at < IN_FLIGHT:
                    why = f"{CARD}: the card for {oid} is being posted"
                    return {"ok": False, "unavailable": why, "error": why}
            doc["cards"][oid] = {"order_id": oid, "reserved_at": now.isoformat(),
                                 "message_ids": [], "open": False, "replies": {}}
            return None

        # Reserved before the post: two runs at once cannot both post the card.
        early = self.store.update(reserve)
        if early is not None:
            return self._result(task, early)
        outcome = self._post(oid, package, brief)
        ids = outcome.get("message_ids") or []

        def settle(doc: dict[str, Any]) -> None:
            if ids:
                doc["cards"][oid].update(message_ids=ids, open=True, posted_at=now.isoformat(),
                                         package=package)
                doc["cards"][oid].pop("reserved_at", None)
            else:
                doc["cards"].pop(oid, None)       # not posted: nothing to answer, try again

        self.store.update(settle)
        if not ids:
            return self._result(task, outcome)
        _log.info("%s: quote card for %s posted (%s)", CARD, oid, ids[0])
        done: dict[str, Any] = {"ok": True, "message_id": ids[0], "messages": len(ids)}
        if outcome.get("ok") is not True:
            # the head is up (a reply to it counts); only part of the brief followed it
            done["partial"] = outcome.get("error")
        return self._result(task, done)

    def _post(self, oid: str, package: str, brief: str) -> dict[str, Any]:
        from pionir.discord_gate import (
            DiscordAuthError,
            DiscordError,
            DiscordRest,
            read_token,
            split_message,
        )

        token = read_token(self.settings.token_file) if self.settings.token_file else None
        if not token:
            why = f"{CARD}: no Discord bot token in {self.settings.token_file}"
            return {"ok": False, "unavailable": why, "error": why}
        client = DiscordRest(token, api_base=self.settings.api_base, opener=self._opener,
                             timeout=self.settings.timeout)
        owner = str(self.settings.owner_user_id)
        chunks = split_message(render_card(oid, package, brief, owner, self.quotes))
        path = f"/channels/{self.settings.channel_id}/messages"
        ids: list[str] = []
        try:
            for i, chunk in enumerate(chunks):
                sent = client.call("POST", path, {
                    "content": chunk,
                    # only the owner is pinged, and only by the first part
                    "allowed_mentions": {"parse": [], "users": [owner] if i == 0 else []}})
                message_id = sent.get("id") if isinstance(sent, dict) else None
                if not message_id:
                    raise DiscordError("Discord answered without a message id")
                ids.append(str(message_id))
        except DiscordAuthError as error:
            why = f"{CARD}: Discord rejected the bot token ({client.scrub(str(error))})"
            return {"ok": False, "unavailable": why, "error": why, "message_ids": ids}
        except DiscordError as error:
            what = "Discord did not answer" if error.transport else "Discord refused the card"
            why = f"{CARD}: {what} ({client.scrub(str(error))})"
            return {"ok": False, "unavailable": why, "error": why, "message_ids": ids}
        return {"ok": True, "message_ids": ids}

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"quotes:card:{task.payload.get('order_id')}"]
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output={k: v for k, v in output.items() if k != "message_ids"},
                          evidence=tuple(evidence))
