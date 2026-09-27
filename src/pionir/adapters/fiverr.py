"""The Fiverr desk's four capabilities: read the order events, acknowledge them, post a card
to the owner, and read his replies.

**Nothing here touches Fiverr.** Fiverr has no seller API and automating the account breaks
its terms: no capability logs into, scrapes, posts to or messages on Fiverr. Orders arrive
through Scrooge, which reads Fiverr's notification emails; everything leaves to the OWNER
only, in his Discord channel, for him to deliver on Fiverr himself.

- ``fiverr.events`` (READ_ONLY): ``GET /dash/fiverr/events?after=<id>`` on Scrooge with the
  ops token - Fiverr's order emails as normalised events (id, kind, order_number, buyer,
  gig_title, package, price_text, due, text). Off until Scrooge serves the route
  (``PIONIR_FIVERR_EVENTS=1`` turns it on): until then it answers ``unavailable``, and the
  desk reports its orders as UNKNOWN - never as none.
- ``fiverr.ack`` (REVERSIBLE_WRITE): ``POST /dash/fiverr/ack {id}`` - marks an event read in
  Scrooge's record. Contacts nobody.
- ``fiverr.card`` (REVERSIBLE_WRITE, never parked): posts ONE card, by its ``key``, in the
  Discord gate's channel with the gate's bot token - pinging only the owner, contacting no
  buyer, moving no money. Its first line is fixed by its ``kind`` here, not by the crew: a
  READY card always says nothing was sent and that HE uploads the delivery and sends the
  reply on Fiverr. Files are attached only from inside the Fiverr folder, and every one is
  scanned for the owner's secrets first (the same scan as a client delivery): a hit refuses
  the card. Idempotent per key: a card already posted is answered with its message id.
- ``fiverr.inbox`` (READ_ONLY): the owner's replies to the cards that take one, as the
  Discord gate recorded them (pionir/fiverr.py) - only replies whose author is the configured
  owner, checked again here.

The ops token and the bot token are read when a call runs, used only in a header, and never
logged or returned.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pionir.adapters.content import read_token
from pionir.adapters.deliveries import DeliveryProblem, inspect_zip, load_secrets, scan_bytes
from pionir.adapters.owner import neutralise_mentions
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.fiverr import owner_replies, store_for

_log = logging.getLogger(__name__)

EVENTS = "fiverr.events"
ACK = "fiverr.ack"
CARD = "fiverr.card"
INBOX = "fiverr.inbox"
CARD_FIELDS = frozenset({"key", "kind", "ref", "title", "body", "files", "replies"})
# The first line of every card, by kind: fixed here, so no caller can leave it out.
HEADS = {
    "ready": "\U0001f4e6 **READY FOR YOU TO DELIVER ON FIVERR** — nothing has been sent "
             "to the buyer. **You** upload the files on Fiverr and paste the reply yourself; "
             "Pionir never logs in to, posts to or messages on Fiverr.",
    "message": "\U0001f4ac **BUYER MESSAGE — DRAFTED REPLY** — nothing has been "
               "sent. **You** send it on Fiverr, edited as you like.",
    "gig": "\U0001f9fe **FIVERR GIG DRAFT** — **you** create or edit the gig on Fiverr "
           "by pasting from this card and the attached listing. Pionir posts nothing to "
           "Fiverr.",
    "order": "\U0001f195 **FIVERR ORDER**",
    "files_needed": "\U0001f4c2 **FIVERR ORDER: FILES NEEDED**",
    "problem": "⛔ **FIVERR DESK: NEEDS YOU** — nothing was prepared for the buyer.",
    "cancelled": "\U0001f6d1 **FIVERR ORDER CANCELLED**",
    "note": "ℹ️ **FIVERR DESK**",
}
KINDS = tuple(HEADS)
INBOX_KINDS = ("order", "gig")
MAX_BODY = 12000
MAX_FILES = 10
MAX_FILE_BYTES = 8_000_000
MAX_ATTACH_TOTAL = 9_500_000         # a bot's message may carry about 10 MB in all
MAX_EVENTS = 200
MAX_RESPONSE_BYTES = 4_000_000
IN_FLIGHT = timedelta(minutes=2)
_KEY = re.compile(r"[A-Za-z0-9:._-]{3,120}")
_REF = re.compile(r"[A-Za-z0-9_-]{1,40}")
_EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
_FILE = re.compile(r"(?:[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}/){0,4}[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_FENCE = "```"
SETUP_HINT = r"run Scrooge's tools\setup-ops-token.ps1"
EVENTS_OFF = ("Fiverr intake is off (PIONIR_FIVERR_EVENTS=0) until Scrooge serves "
              "/dash/fiverr/events; the orders are unknown, not none")
CONTENT_TYPES = {".png": "image/png", ".md": "text/markdown", ".zip": "application/zip",
                 ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json",
                 ".html": "text/html"}

Opener = Callable[..., Any]


@dataclass(frozen=True)
class FiverrSettings:
    """Scrooge (the events), the Fiverr folder (the files), the Discord gate's channel, bot
    token and owner (the cards and replies), and every secret a file is scanned for. Holds
    PATHS of token files, never tokens."""

    state_root: Path
    fiverr_dir: Path
    base_url: str = "https://api.dokaz.net"
    ops_token_file: Path | None = None
    events_enabled: bool = False
    channel_id: str | None = None
    owner_user_id: str | None = None
    discord_token_file: Path | None = None
    api_base: str = "https://discord.com/api/v10"
    discord_enabled: bool = True
    secrets_dir: Path | None = None
    secret_files: tuple = field(default_factory=tuple)
    ssh_dir: Path | None = None
    timeout: float = 20.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
            raise ValueError("Scrooge's URL must be https: (or http: on loopback)")

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


def fiverr_settings(configured: Any) -> FiverrSettings:
    """The FiverrSettings for a PionirSettings: its Scrooge and ops token, its Fiverr folder,
    the Discord gate's settings, and every token file it is configured with (each scanned
    for in a card's files)."""
    from pionir.discord_gate import DiscordGateSettings

    gate = DiscordGateSettings.from_environment(configured.state_root)
    token_files = [configured.content_token_path, configured.instagram_token_path,
                   configured.devto_key_path, configured.ops_token_path]
    gumroad = getattr(configured, "gumroad_token_path", None)
    if gumroad is not None:
        token_files.append(gumroad)
    token_files.append(Path(gate.token_file))
    return FiverrSettings(state_root=configured.state_root, fiverr_dir=configured.fiverr_path,
                          base_url=configured.content_url or "https://api.dokaz.net",
                          ops_token_file=configured.ops_token_path,
                          events_enabled=bool(configured.fiverr_events),
                          channel_id=gate.channel_id, owner_user_id=gate.owner,
                          discord_token_file=Path(gate.token_file), api_base=gate.api_base,
                          discord_enabled=bool(gate.enabled),
                          secrets_dir=configured.secrets_path,
                          secret_files=tuple(token_files),
                          ssh_dir=Path.home() / ".ssh")


def _unavailable(why: str, **extra: Any) -> dict:
    return {"ok": False, "unavailable": why, "error": why, **extra}


def _fence_safe(text: str) -> str:
    return text.replace(_FENCE, "`​`​`")


def render(kind: str, title: str, body: str, owner: str, replies: bool) -> str:
    """The card: the fixed head for its kind, the caller's title and body (mentions made
    harmless), and - for a card that takes a reply - how to answer it."""
    parts = [HEADS[kind], f"**{neutralise_mentions(title)}**", "", neutralise_mentions(body)]
    if replies:
        parts += ["", f"↩️ **Reply to this message** to answer. Only <@{owner}>'s "
                      "reply counts, and a reply sends nothing to the buyer."]
    return "\n".join(parts)


class FiverrAdapter:
    """The Fiverr desk's capabilities, as audited Pionir capabilities."""

    def __init__(self, settings: FiverrSettings, *, opener: Opener | None = None,
                 discord_opener: Opener | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.store = store_for(settings.state_root)
        self._open = opener or urllib.request.build_opener().open
        self._discord_opener = discord_opener
        self._clock = clock or (lambda: datetime.now(UTC))
        self._manifest = AgentManifest(
            agent_id="fiverr", version="pionir/fiverr",
            capabilities=(
                Capability(name=EVENTS, description="Read the new Fiverr order events Scrooge "
                           "took from Fiverr's emails (reads only)", risk=RiskLevel.READ_ONLY,
                           routable=False),
                Capability(name=ACK, description="Mark a Fiverr order event read in Scrooge "
                           "(records only; nobody is contacted)",
                           risk=RiskLevel.REVERSIBLE_WRITE, routable=False),
                Capability(name=CARD, description="Post one Fiverr desk card to the owner's "
                           "Discord channel (to the owner only; contacts no buyer, posts "
                           "nothing to Fiverr)", risk=RiskLevel.REVERSIBLE_WRITE,
                           routable=False),
                Capability(name=INBOX, description="Read the owner's replies to the Fiverr "
                           "desk's cards (reads only)", risk=RiskLevel.READ_ONLY,
                           routable=False),
            ))

    def __repr__(self) -> str:
        return "FiverrAdapter(tokens=<read at call time>)"

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Local only: no call to Scrooge or Discord."""
        why = self.settings.why_not_discord()
        if why:
            raise AdapterUnavailable(f"fiverr: {why}")
        cards = self.store.read()["cards"]
        return {"ok": True, "cards": len(cards), "events": "on" if
                self.settings.events_enabled else "off (PIONIR_FIVERR_EVENTS=0)"}

    # ---- validation ----------------------------------------------------------------------
    def validate(self, task: Task) -> None:
        p = task.payload
        if task.capability == EVENTS:
            extra = set(p) - {"after"}
            after = p.get("after")
            if extra or (after is not None and (isinstance(after, bool) or not _EVENT_ID
                                                .fullmatch(str(after)))):
                raise AdapterProtocolError(f"{EVENTS}: the payload is {{after?: event id}}")
        elif task.capability == ACK:
            eid = p.get("id")
            if set(p) != {"id"} or isinstance(eid, bool) or not _EVENT_ID.fullmatch(str(eid)):
                raise AdapterProtocolError(f"{ACK}: the payload is {{id: event id}}")
        elif task.capability == INBOX:
            if set(p) - {"kind"} or (p.get("kind") is not None
                                     and p.get("kind") not in INBOX_KINDS):
                raise AdapterProtocolError(f"{INBOX}: the payload is {{kind?: "
                                           f"{' | '.join(INBOX_KINDS)}}}")
        elif task.capability == CARD:
            self._card(p)
        else:
            raise AdapterProtocolError(f"the fiverr adapter has no capability "
                                       f"{task.capability!r}")

    def _card(self, p: Mapping[str, Any]) -> dict:
        extra = sorted(set(p) - CARD_FIELDS)
        if extra:
            raise AdapterProtocolError(f"{CARD}: {extra[0]}: not a card field")
        key, kind, ref = p.get("key"), p.get("kind"), p.get("ref")
        title, body = p.get("title"), p.get("body")
        files, replies = p.get("files", []), p.get("replies", False)
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise AdapterProtocolError(f"{CARD}: key: 3-120 of A-Z a-z 0-9 : . _ -")
        if kind not in KINDS:
            raise AdapterProtocolError(f"{CARD}: kind: one of {', '.join(KINDS)}")
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise AdapterProtocolError(f"{CARD}: ref: an order number or a service")
        if not isinstance(title, str) or not 1 <= len(title) <= 120 or "\n" in title \
                or _CONTROL.search(title):
            raise AdapterProtocolError(f"{CARD}: title: one line of 1-120 characters")
        if not isinstance(body, str) or not 1 <= len(body) <= MAX_BODY \
                or _CONTROL.search(body.replace("\t", " ")):
            raise AdapterProtocolError(f"{CARD}: body: 1-{MAX_BODY} characters of text")
        if not isinstance(replies, bool):
            raise AdapterProtocolError(f"{CARD}: replies: true or false")
        if replies and kind not in INBOX_KINDS:
            raise AdapterProtocolError(f"{CARD}: only an order or a gig card takes replies")
        if not isinstance(files, list) or len(files) > MAX_FILES:
            raise AdapterProtocolError(f"{CARD}: files: a list of at most {MAX_FILES}")
        paths = [self._file(f) for f in files]
        self._scan(paths)
        return {"key": key, "kind": kind, "ref": ref, "title": title, "body": body,
                "files": paths, "replies": replies}

    def _file(self, rel: Any) -> Path:
        """A file inside the Fiverr folder, named plainly: no absolute path, no ``..``, no
        backslash, no link - or AdapterProtocolError."""
        if not isinstance(rel, str) or not _FILE.fullmatch(rel) or ".." in rel.split("/"):
            raise AdapterProtocolError(f"{CARD}: files: {str(rel)[:80]!r} is not a plain "
                                       "relative path in the Fiverr folder")
        root = Path(self.settings.fiverr_dir)
        path = root / rel
        try:
            inside = path.resolve().is_relative_to(root.resolve())
        except OSError:
            inside = False
        if not inside or path.is_symlink() or not path.is_file():
            raise AdapterProtocolError(f"{CARD}: files: {rel!r} is not a file in the Fiverr "
                                       "folder")
        return path

    def _scan(self, paths: list) -> None:
        """Every file, scanned for every secret Pionir knows of: a hit refuses the card
        (naming the file and which secret, never the value)."""
        if not paths:
            return
        try:
            secrets = load_secrets(self.settings.secrets_dir, self.settings.secret_files,
                                   self.settings.ssh_dir)
        except DeliveryProblem as error:
            raise AdapterProtocolError(f"{CARD}: the secrets scan could not run ({error})") \
                from error
        for path in paths:
            if path.suffix.lower() == ".zip":
                try:
                    inspect_zip(path, secrets, root=Path(self.settings.fiverr_dir),
                                folder="the Fiverr folder")
                except DeliveryProblem as error:
                    raise AdapterProtocolError(f"{CARD}: {path.name}: {error}") from error
                continue
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            problem = scan_bytes(path.name, data, secrets)
            if problem:
                raise AdapterProtocolError(f"{CARD}: refused - {problem}")

    # ---- execution -----------------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        self.validate(task)
        if task.capability == EVENTS:
            return self._result(task, self._events(task.payload.get("after")))
        if task.capability == ACK:
            return self._result(task, self._ack(str(task.payload["id"])))
        if task.capability == INBOX:
            replies = owner_replies(self.store, self.settings.owner_user_id,
                                    task.payload.get("kind"))
            return self._result(task, {"ok": True, "replies": replies})
        return self._result(task, self._post_card(self._card(task.payload)))

    def _events(self, after: Any) -> dict:
        if not self.settings.events_enabled:
            return _unavailable(EVENTS_OFF, not_configured=True)
        query = f"?{urllib.parse.urlencode({'after': str(after)})}" if after is not None else ""
        status, doc = self._scrooge("GET", f"/dash/fiverr/events{query}", None)
        if status != 200 or not isinstance(doc, dict):
            return self._scrooge_error(status, doc)
        events = doc.get("events")
        if not isinstance(events, list):
            return _unavailable("Scrooge answered without an event list")
        return {"ok": True, "events": events[:MAX_EVENTS], "more": len(events) > MAX_EVENTS}

    def _ack(self, eid: str) -> dict:
        if not self.settings.events_enabled:
            return _unavailable(EVENTS_OFF, not_configured=True)
        status, doc = self._scrooge("POST", "/dash/fiverr/ack", {"id": eid})
        if status == 200 and isinstance(doc, dict) and doc.get("ok") is not False:
            return {"ok": True, "id": eid}
        return self._scrooge_error(status, doc)

    def _scrooge_error(self, status: int, doc: Any) -> dict:
        if status == 0:
            return _unavailable("Scrooge did not answer")
        if status in (401, 403):
            return _unavailable(f"the ops token was rejected - {SETUP_HINT}")
        if status == 404:
            return _unavailable("Scrooge does not serve the Fiverr routes yet (not set up)",
                                not_configured=True)
        said = doc.get("error") if isinstance(doc, dict) else None
        return _unavailable(f"Scrooge answered HTTP {status}"
                            + (f": {str(said)[:160]}" if said else ""))

    def _scrooge(self, method: str, path: str, body: Mapping[str, Any] | None) -> tuple:
        token = read_token(self.settings.ops_token_file) if self.settings.ops_token_file \
            else None
        if token is None:
            return 401, {"error": "no ops token"}
        headers = {"Accept": "application/json", "User-Agent": "pionir-fiverr/0.1",
                   "x-dash-token": token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.settings.base_url.rstrip('/')}{path}",
                                         data=data, method=method, headers=headers)
        try:
            with self._open(request, timeout=self.settings.timeout) as response:
                return int(getattr(response, "status", 200)), self._json(response)
        except urllib.error.HTTPError as error:
            return error.code, self._json(error)
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, None

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return None
            return json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    # ---- the card ------------------------------------------------------------------------------
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
            doc["cards"][key] = {"key": key, "kind": card["kind"], "ref": card["ref"],
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
        done = {"ok": True, "message_id": ids[0], "messages": len(ids),
                "attached": outcome.get("attached", []),
                "not_attached": outcome.get("not_attached", [])}
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
        attach, skipped, total = [], [], 0
        for path in card["files"]:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES or total + size > MAX_ATTACH_TOTAL:
                skipped.append(path.name)
                continue
            total += size
            attach.append((path.name, CONTENT_TYPES.get(path.suffix.lower(),
                                                        "application/octet-stream"),
                           path.read_bytes()))
        body = card["body"]
        if skipped:
            body += ("\n\nNot attached (too big for Discord; it is in the Fiverr folder on this "
                     "machine): " + ", ".join(skipped))
        chunks = split_message(render(card["kind"], card["title"], body, owner,
                                      card["replies"]))
        path = f"/channels/{self.settings.channel_id}/messages"
        ids: list = []
        try:
            for i, chunk in enumerate(chunks):
                message = {"content": chunk,
                           "allowed_mentions": {"parse": [], "users": [owner] if i == 0
                                                else []}}
                sent = client.call("POST", path, message,
                                   files=attach if i == 0 and attach else None)
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
        return {"ok": True, "message_ids": ids, "attached": [a[0] for a in attach],
                "not_attached": skipped}

    def _result(self, task: Task, output: dict) -> TaskResult:
        evidence = [f"fiverr:{task.capability.split('.', 1)[1]}"]
        key = task.payload.get("key")
        if isinstance(key, str) and _KEY.fullmatch(key):
            evidence.append(f"fiverr:card:{key}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))
