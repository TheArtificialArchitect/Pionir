"""The Builds division's two capabilities: post a card to the owner, and read his replies.

The crew's Builds division (crew/builds) has Daedalus build small developer tools overnight,
in sandbox repos, and stages each one Claude approved on the product shelf. It tells the
owner what happened in his Discord channel, and he steers its product backlog by REPLYING to
its cards:

- ``builds.card`` (REVERSIBLE_WRITE, never parked, not routable): posts ONE card, by its
  ``key``, in the Discord gate's channel with the gate's bot token - pinging only the owner.
  Its first line is fixed by its ``kind`` here (``HEADS``), not by the crew: a STAGED card
  always says the product is NOT on sale and still goes through the shelf and his approval.
  No files are attached. Idempotent per key: a card already posted is answered with its
  message id.
- ``builds.inbox`` (READ_ONLY): the owner's replies to the cards that take one (the nightly
  report and the backlog card), as the Discord gate recorded them - only replies whose
  author is the configured owner (``pionir.fiverr.FiverrReplies`` over this record), checked
  again here.

Nothing here builds, stages or publishes anything; the bot token is read when a card is
posted, used only in a header, and never logged or returned.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pionir.adapters.owner import neutralise_mentions
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.fiverr import FiverrReplies, owner_replies
from pionir.quotes import QuoteCardStore

_log = logging.getLogger(__name__)

CARD = "builds.card"
INBOX = "builds.inbox"
STORE_NAME = "build-cards.json"
CARD_FIELDS = frozenset({"key", "kind", "title", "body", "replies"})
HEADS = {
    "night": "\U0001f319 **BUILDS — NIGHTLY REPORT**",
    "backlog": "\U0001f5c2️ **BUILDS — PRODUCT BACKLOG**",
    "staged": "\U0001f4e6 **BUILDS — STAGED FOR THE SHELF** — NOT on sale: it goes "
              "through the product shelf and your ✅ like every product.",
    "shelved": "\U0001f6d1 **BUILDS — SHELVED** — nothing was staged or put on sale.",
    "problem": "⚠️ **BUILDS — NEEDS YOU**",
}
KINDS = tuple(HEADS)
INBOX_KINDS = ("night", "backlog")
MAX_BODY = 12000
IN_FLIGHT = timedelta(minutes=2)
_KEY = re.compile(r"[A-Za-z0-9:._-]{3,120}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
RECORDED = ("\U0001f4e5 Recorded. The Builds worker reads it on its next run (within minutes) "
            "and answers on a new backlog card. Nothing is built or sold from a reply.")


def store_for(state_root: Path) -> QuoteCardStore:
    """The Builds card record: the same shape and locking as the Fiverr and quote cards'."""
    return QuoteCardStore(Path(state_root) / "discord" / STORE_NAME)


def build_replies(state_root: Path) -> FiverrReplies:
    """The Discord gate's recorder for the owner's replies to the Builds cards."""
    return FiverrReplies(store_for(state_root), recorded=RECORDED, label="builds")


@dataclass(frozen=True)
class BuildCardSettings:
    """The Discord gate's channel, bot token file and owner. Holds a PATH, never a token."""

    state_root: Path
    channel_id: str | None = None
    owner_user_id: str | None = None
    discord_token_file: Path | None = None
    api_base: str = "https://discord.com/api/v10"
    discord_enabled: bool = True
    timeout: float = 20.0

    def why_not_discord(self) -> str | None:
        if not self.discord_enabled:
            return "the Discord gate is disabled (PIONIR_DISCORD_GATE=0)"
        if not self.channel_id:
            return "no Discord channel is configured (PIONIR_DISCORD_CHANNEL_ID)"
        if not self.owner_user_id:
            return ("no owner Discord user id is configured (PIONIR_DISCORD_USER_ID): nobody "
                    "could answer a card")
        if self.discord_token_file is None:
            return "no Discord bot token file is configured"
        return None


def build_card_settings(configured: Any) -> BuildCardSettings:
    from pionir.discord_gate import DiscordGateSettings

    gate = DiscordGateSettings.from_environment(configured.state_root)
    return BuildCardSettings(state_root=configured.state_root, channel_id=gate.channel_id,
                             owner_user_id=gate.owner, discord_token_file=Path(gate.token_file),
                             api_base=gate.api_base, discord_enabled=bool(gate.enabled))


def _unavailable(why: str) -> dict:
    return {"ok": False, "unavailable": why, "error": why}


def render(kind: str, title: str, body: str, owner: str, replies: bool) -> str:
    parts = [HEADS[kind], f"**{neutralise_mentions(title)}**", "", neutralise_mentions(body)]
    if replies:
        parts += ["", f"↩️ **Reply to this message** to change the backlog. Only "
                      f"<@{owner}>'s reply counts."]
    return "\n".join(parts)


class BuildCardAdapter:
    """The Builds division's cards, as audited Pionir capabilities."""

    def __init__(self, settings: BuildCardSettings, *, discord_opener: Any = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.store = store_for(settings.state_root)
        self._discord_opener = discord_opener
        self._clock = clock or (lambda: datetime.now(UTC))
        self._manifest = AgentManifest(
            agent_id="builds", version="pionir/builds",
            capabilities=(
                Capability(name=CARD, description="Post one Builds division card to the "
                           "owner's Discord channel (to the owner only; builds and sells "
                           "nothing)", risk=RiskLevel.REVERSIBLE_WRITE, routable=False),
                Capability(name=INBOX, description="Read the owner's replies to the Builds "
                           "division's cards (reads only)", risk=RiskLevel.READ_ONLY,
                           routable=False),
            ))

    def __repr__(self) -> str:
        return "BuildCardAdapter(token=<read at call time>)"

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Local only: no call to Discord."""
        why = self.settings.why_not_discord()
        if why:
            raise AdapterUnavailable(f"builds: {why}")
        return {"ok": True, "cards": len(self.store.read()["cards"])}

    def validate(self, task: Task) -> None:
        p = task.payload
        if task.capability == INBOX:
            if set(p) - {"kind"} or (p.get("kind") is not None
                                     and p.get("kind") not in INBOX_KINDS):
                raise AdapterProtocolError(f"{INBOX}: the payload is {{kind?: "
                                           f"{' | '.join(INBOX_KINDS)}}}")
        elif task.capability == CARD:
            self._card(p)
        else:
            raise AdapterProtocolError(f"the builds adapter has no capability "
                                       f"{task.capability!r}")

    @staticmethod
    def _card(p: Mapping[str, Any]) -> dict:
        extra = sorted(set(p) - CARD_FIELDS)
        if extra:
            raise AdapterProtocolError(f"{CARD}: {extra[0]}: not a card field")
        key, kind, title, body = p.get("key"), p.get("kind"), p.get("title"), p.get("body")
        replies = p.get("replies", False)
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise AdapterProtocolError(f"{CARD}: key: 3-120 of A-Z a-z 0-9 : . _ -")
        if kind not in KINDS:
            raise AdapterProtocolError(f"{CARD}: kind: one of {', '.join(KINDS)}")
        if not isinstance(title, str) or not 1 <= len(title) <= 120 or "\n" in title \
                or _CONTROL.search(title):
            raise AdapterProtocolError(f"{CARD}: title: one line of 1-120 characters")
        if not isinstance(body, str) or not 1 <= len(body) <= MAX_BODY \
                or _CONTROL.search(body.replace("\t", " ")):
            raise AdapterProtocolError(f"{CARD}: body: 1-{MAX_BODY} characters of text")
        if not isinstance(replies, bool):
            raise AdapterProtocolError(f"{CARD}: replies: true or false")
        if replies and kind not in INBOX_KINDS:
            raise AdapterProtocolError(f"{CARD}: only a nightly or a backlog card takes replies")
        return {"key": key, "kind": kind, "title": title, "body": body, "replies": replies}

    def execute(self, task: Task) -> TaskResult:
        self.validate(task)
        if task.capability == INBOX:
            output = {"ok": True, "replies": owner_replies(
                self.store, self.settings.owner_user_id, task.payload.get("kind"))}
        else:
            output = self._post_card(self._card(task.payload))
        evidence = [f"builds:{task.capability.split('.', 1)[1]}"]
        key = task.payload.get("key")
        if isinstance(key, str) and _KEY.fullmatch(key):
            evidence.append(f"builds:card:{key}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))

    def _post_card(self, card: dict) -> dict:
        why = self.settings.why_not_discord()
        if why:
            return _unavailable(f"{CARD}: {why}")
        now = self._clock()
        key = card["key"]

        def reserve(doc: dict) -> dict | None:
            got = doc["cards"].get(key)
            if got and got.get("message_ids"):
                return {"ok": True, "already": True, "message_id": got["message_ids"][0]}
            if got and got.get("reserved_at"):
                try:
                    at = datetime.fromisoformat(str(got["reserved_at"]))
                except ValueError:
                    at = now - IN_FLIGHT
                if now - at < IN_FLIGHT:
                    return _unavailable(f"{CARD}: the card {key} is being posted")
            doc["cards"][key] = {"key": key, "kind": card["kind"], "ref": "builds",
                                 "reserved_at": now.isoformat(), "message_ids": [],
                                 "open": False, "replies": {}}
            return None

        early = self.store.update(reserve)
        if early is not None:
            return early
        outcome = self._post(card)
        ids = outcome.get("message_ids") or []

        def settle(doc: dict) -> None:
            if ids:
                doc["cards"][key].update(message_ids=ids, open=card["replies"],
                                         posted_at=now.isoformat())
                doc["cards"][key].pop("reserved_at", None)
            else:
                doc["cards"].pop(key, None)

        self.store.update(settle)
        if not ids:
            return {k: v for k, v in outcome.items() if k != "message_ids"}
        _log.info("%s: card %s posted (%s)", CARD, key, ids[0])
        done = {"ok": True, "message_id": ids[0], "messages": len(ids)}
        if outcome.get("ok") is not True:
            done["partial"] = outcome.get("error")
        return done

    def _post(self, card: dict) -> dict:
        from pionir.discord_gate import (
            DiscordAuthError,
            DiscordError,
            DiscordRest,
            split_message,
        )
        from pionir.discord_gate import read_token as read_bot_token

        token = read_bot_token(self.settings.discord_token_file)
        if not token:
            return _unavailable(f"{CARD}: no Discord bot token in "
                                f"{self.settings.discord_token_file}")
        client = DiscordRest(token, api_base=self.settings.api_base,
                             opener=self._discord_opener, timeout=self.settings.timeout)
        owner = str(self.settings.owner_user_id)
        chunks = split_message(render(card["kind"], card["title"], card["body"], owner,
                                      card["replies"]))
        path = f"/channels/{self.settings.channel_id}/messages"
        ids: list = []
        try:
            for i, chunk in enumerate(chunks):
                message = {"content": chunk,
                           "allowed_mentions": {"parse": [], "users": [owner] if i == 0
                                                else []}}
                sent = client.call("POST", path, message)
                message_id = sent.get("id") if isinstance(sent, dict) else None
                if not message_id:
                    raise DiscordError("Discord answered without a message id")
                ids.append(str(message_id))
        except DiscordAuthError as error:
            why = f"{CARD}: Discord rejected the bot token ({client.scrub(str(error))})"
            return {**_unavailable(why), "message_ids": ids}
        except DiscordError as error:
            what = "Discord did not answer" if error.transport else "Discord refused the card"
            return {**_unavailable(f"{CARD}: {what} ({client.scrub(str(error))})"),
                    "message_ids": ids}
        return {"ok": True, "message_ids": ids}
