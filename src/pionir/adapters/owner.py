"""``owner.notify``: Moss sends the owner a short note on Discord - a daily brief, or a
rare alert - and nothing else.

The owner hates calls; Discord is his channel. Moss (Galatea) asked for "concise
summaries delivered via text message", and this is that: ONE plain message to the
configured Discord approvals channel, prefixed so it is plainly hers and plainly not an
approval card::

    📊 **Moss — <title>**        (kind "brief")
    ⚠️ **Moss — <title>**        (kind "alert")

It is REVERSIBLE_WRITE and never parks for approval: it only messages the owner himself,
in his own channel, with no reactions to answer and nothing that runs. What keeps it from
becoming noise is enforced HERE, not in Moss: at most ``BRIEFS_PER_DAY`` briefs and
``ALERTS_PER_DAY`` alerts in any rolling 24 hours, counted from a record kept in Pionir's
state (``<state_root>/discord/owner-notify.json``), so a restart - of Moss or of Pionir -
does not reset it. Over the limit is ``ok: false`` with ``refused`` (Moss's side records
it as refused, not as a fault). Discord not configured, the token rejected, Discord not
answering: ``ok: false`` with ``unavailable``. Neither trips the circuit breaker.

A slot is reserved in the record before the send and settled after it; the send itself
holds no lock, so a slow Discord never holds up ``status()``. An unreadable record is set
aside (``.corrupt-<time>``) and logged, and the window counts as full for 24 hours - then
it recovers by itself.

The payload is checked before anything is sent (``validate``, which Pionir's /api/task
runs first): ``title`` 5-80 characters on one line, ``body_text`` 20-1800 characters of
plain text, ``kind`` "brief" or "alert", and no other field. Mentions are neutralised in
the text (``@everyone``/``@here`` broken with a zero-width space, ``<@id>``/``<@!id>``/
``<@&id>`` written as plain ``@user``/``@role``) AND the message is sent with
``allowed_mentions: {"parse": []}``, so it can never ping anyone.

The bot token is the Discord gate's own (the same file), read when a note is sent, used
only in the Authorization header (``DiscordRest``), and scrubbed from every error. It is
never logged and never in an output.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pionir import atomic
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

NOTIFY = "owner.notify"
KINDS = ("brief", "alert")
BRIEFS_PER_DAY = 2
ALERTS_PER_DAY = 4
LIMITS = {"brief": BRIEFS_PER_DAY, "alert": ALERTS_PER_DAY}
WINDOW = timedelta(hours=24)
TITLE_MIN, TITLE_MAX = 5, 80
BODY_MIN, BODY_MAX = 20, 1800
FIELDS = frozenset({"title", "body_text", "kind"})
PREFIX = {"brief": "\U0001f4ca", "alert": "⚠️"}   # 📊 / ⚠️
MESSAGE_LIMIT = 2000
STATE_NAME = "owner-notify.json"
DEFAULT_API = "https://discord.com/api/v10"

# Mentions, neutralised in the text itself (allowed_mentions is the second lock).
_ROLE = re.compile(r"<@&\d+>")
_USER = re.compile(r"<@!?\d+>")
_EVERYONE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
# Control characters other than a newline or a tab: never in a note.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Discord markdown, escaped in the title (it sits inside **...**).
_MARKDOWN = re.compile(r"([\\*_~`|>#\[\]])")


def neutralise_mentions(text: str) -> str:
    """The text with nothing in it that Discord could turn into a ping."""
    text = _ROLE.sub("@role", text)
    text = _USER.sub("@user", text)
    return _EVERYONE.sub(lambda m: "@\u200b" + m.group(1), text)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OwnerNotifySettings:
    """Where a note goes and where the send record lives. Holds the PATH of the token
    file, never the token. ``channel_id`` None means Discord is not configured."""

    state_dir: Path
    channel_id: str | None = None
    token_file: Path | None = None
    api_base: str = DEFAULT_API
    enabled: bool = True
    timeout: float = 20.0

    @property
    def state_path(self) -> Path:
        return self.state_dir / STATE_NAME

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.channel_id and self.token_file is not None)

    @classmethod
    def from_gate(cls, gate: Any) -> OwnerNotifySettings:
        """The Discord gate's own settings (``DiscordGateSettings``): the same channel,
        the same bot token file, the record next to the gate's."""
        return cls(state_dir=Path(gate.directory), channel_id=gate.channel_id,
                   token_file=Path(gate.token_file), api_base=gate.api_base,
                   enabled=bool(gate.enabled))

    def why_not(self) -> str:
        if not self.enabled:
            return "the Discord gate is disabled (PIONIR_DISCORD_GATE=0)"
        if not self.channel_id:
            return "no Discord channel is configured (PIONIR_DISCORD_CHANNEL_ID)"
        return "no Discord bot token file is configured"


@dataclass(frozen=True, slots=True)
class Note:
    title: str
    body_text: str
    kind: str

    def message(self) -> str:
        title = _MARKDOWN.sub(r"\\\1", neutralise_mentions(self.title))
        head = f"{PREFIX[self.kind]} **Moss — {title}**"
        room = MESSAGE_LIMIT - len(head) - 2
        body = neutralise_mentions(self.body_text)
        if len(body) > room:
            body = body[:room - 1] + "…"
        return f"{head}\n\n{body}"


def parse_note(payload: Mapping[str, Any]) -> Note:
    """The note, or ValueError saying exactly what is wrong."""
    extra = sorted(set(payload) - FIELDS)
    if extra:
        raise ValueError(f"unknown field(s) {', '.join(extra)}; a note is title, body_text "
                         "and kind")
    kind = payload.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind is 'brief' or 'alert', not {kind!r}")
    title = payload.get("title")
    if not isinstance(title, str):
        raise TypeError("title is a string")
    if "\n" in title or "\r" in title:
        raise ValueError("title is a single line")
    title = title.strip()
    if _CONTROL.search(title) or "\t" in title:
        raise ValueError("title has control characters")
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        raise ValueError(f"title is {TITLE_MIN}-{TITLE_MAX} characters, not {len(title)}")
    body = payload.get("body_text")
    if not isinstance(body, str):
        raise TypeError("body_text is a string")
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if _CONTROL.search(body):
        raise ValueError("body_text is plain text: no control characters")
    if not BODY_MIN <= len(body) <= BODY_MAX:
        raise ValueError(f"body_text is {BODY_MIN}-{BODY_MAX} characters, not {len(body)}")
    return Note(title=title, body_text=body, kind=kind)


class OwnerNotifyAdapter:
    """``owner.notify`` as a gated, audited, rate-limited Pionir capability."""

    def __init__(
        self,
        settings: OwnerNotifySettings,
        *,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._clock = clock or _now
        self._lock = threading.Lock()
        self._stand_ins: list[dict[str, Any]] = []   # an unreadable record's full window
        self._manifest = AgentManifest(
            agent_id="owner", version="pionir/owner",
            capabilities=(Capability(
                name=NOTIFY,
                description="Send the owner ONE plain Discord message from Moss: a daily "
                            "brief or a rare alert (rate-limited; no approval, no pings)",
                risk=RiskLevel.REVERSIBLE_WRITE, routable=False),),
        )

    def __repr__(self) -> str:
        return (f"OwnerNotifyAdapter(channel={self.settings.channel_id!r}, "
                "token=<read at send time>)")

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- health ------------------------------------------------------------------
    def status(self) -> Mapping[str, Any]:
        if not self.settings.configured:
            raise AdapterUnavailable(f"{NOTIFY}: {self.settings.why_not()}")
        # the lock is only ever held for a file read or write, never across a send, so
        # health and doctor do not wait on Discord
        with self._lock:
            sent = self._load()
        now = self._clock()
        counts = {k: len(self._recent(sent, k, now)) for k in KINDS}
        return {"ok": True, "channel_id": self.settings.channel_id,
                "sent_24h": counts, "limits": dict(LIMITS)}

    # ---- the request ---------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a malformed note before it is run, in words Moss can act on."""
        self._note(task)

    def _note(self, task: Task) -> Note:
        if task.capability != NOTIFY:
            raise AdapterProtocolError(f"the owner adapter has no capability "
                                       f"{task.capability!r}")
        try:
            return parse_note(task.payload)
        except (TypeError, ValueError) as error:
            raise AdapterProtocolError(f"{NOTIFY}: {error}") from error

    # ---- the record ----------------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        """What was sent (and is being sent), from Pionir's state. A record that exists
        but cannot be read is set aside (``_quarantine``): the count is then unknown, so
        the window is treated as full until 24 hours have passed, and then it recovers."""
        path = self.settings.state_path
        sent: list[dict[str, Any]] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                listed = data.get("sent") if isinstance(data, dict) else None
                if not isinstance(listed, list):
                    raise ValueError("no 'sent' list")  # noqa: TRY004 - one unreadable path
            except (OSError, ValueError) as error:
                return self._quarantine(path, error)
            sent = [e for e in listed if isinstance(e, dict)]
        # stand-ins for an unreadable record count even when they could not be saved
        now = self._clock()
        self._stand_ins = [e for e in self._stand_ins if self._recent([e], e["kind"], now)]
        return sent + [e for e in self._stand_ins if e not in sent]

    def _quarantine(self, path: Path, error: Exception) -> list[dict[str, Any]]:
        """As the Discord gate does with its own corrupt state: keep the bad file aside for
        a look and say so loudly. Failing safe means the rolling window is full - a
        stand-in for every slot, dated now - so nothing is sent for 24 hours; then the
        stand-ins age out like any send and notes flow again, with no one deleting a file."""
        now = self._clock()
        keep = path.with_name(f"{path.name}.corrupt-{int(now.timestamp())}")
        try:
            path.replace(keep)
        except OSError:
            keep = path
        at = now.isoformat(timespec="seconds")
        self._stand_ins = [{"kind": k, "at": at, "message_id": None, "stand_in": True}
                           for k in KINDS for _ in range(LIMITS[k])]
        _log.error("%s: send record unreadable (%s: %s), kept at %s; no note is sent until "
                   "24 hours have passed", NOTIFY, type(error).__name__, error, keep)
        try:
            self._save(list(self._stand_ins))
        except OSError as save_error:
            _log.error("%s: the stand-in record could not be saved (%s); held in memory",
                       NOTIFY, type(save_error).__name__)
        return list(self._stand_ins)

    def _save(self, sent: list[dict[str, Any]]) -> None:
        path = self.settings.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sent": sent}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        atomic.replace(tmp, path)   # retried: Windows scanners hold files briefly

    @staticmethod
    def _recent(sent: list[dict[str, Any]], kind: str, now: datetime) -> list[datetime]:
        out = []
        for entry in sent:
            if entry.get("kind") != kind:
                continue
            try:
                at = datetime.fromisoformat(str(entry.get("at")))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if now - at < WINDOW:
                out.append(at)
        return sorted(out)

    # ---- the call ------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        note = self._note(task)
        if not self.settings.configured:
            why = f"{NOTIFY}: {self.settings.why_not()}"
            return self._result(task, note, {"ok": False, "unavailable": why, "error": why})
        # Reserve, send, settle. The slot is recorded (under the lock) BEFORE the send, so
        # two notes at once cannot both squeeze under the limit and a send whose record
        # fails afterwards is still counted; the send itself runs outside the lock, so a
        # slow Discord (429 retries, 20 s timeouts) never holds up status() or health.
        with self._lock:
            now = self._clock()
            sent = self._load()
            recent = self._recent(sent, note.kind, now)
            limit = LIMITS[note.kind]
            if len(recent) >= limit:
                next_at = (recent[0] + WINDOW).isoformat(timespec="seconds")
                plural = "briefs" if note.kind == "brief" else "alerts"
                why = (f"{NOTIFY}: the limit of {limit} {plural} in any 24 hours is reached; "
                       f"the next one can go at {next_at}")
                if any(e.get("stand_in") and self._recent([e], note.kind, now) for e in sent):
                    why = (f"{NOTIFY}: the send record was unreadable and was set aside, so "
                           "the count is unknown and treated as full; the next one can go "
                           f"at {next_at}")
                return self._result(task, note, {"ok": False, "refused": why, "error": why,
                                                 "kind": note.kind, "limit": limit,
                                                 "next_at": next_at})
            at = now.isoformat(timespec="seconds")
            slot = uuid.uuid4().hex
            # only the last 24 hours are kept: the record never grows past the limits
            keep = [e for e in sent if e.get("kind") in KINDS
                    and self._recent([e], str(e["kind"]), now)]
            keep.append({"kind": note.kind, "at": at, "message_id": None, "slot": slot})
            try:
                self._save(keep)
            except OSError as error:
                why = (f"{NOTIFY}: the send record could not be written "
                       f"({type(error).__name__}); nothing was sent")
                _log.error("%s", why)
                return self._result(task, note, {"ok": False, "unavailable": why,
                                                 "error": why})
        outcome = self._send(note)
        sent_ok = outcome.get("ok") is True
        with self._lock:
            saved = self._settle(slot, outcome["message_id"] if sent_ok else None)
            recorded = self._load()
        if not sent_ok:
            return self._result(task, note, outcome)
        remaining = {k: max(0, LIMITS[k] - len(self._recent(recorded, k, now))) for k in KINDS}
        done = {**outcome, "sent_at": at, "remaining": remaining}
        if not saved:
            done["record_saved"] = False
        return self._result(task, note, done)

    def _settle(self, slot: str, message_id: str | None) -> bool:
        """The reserved slot: given its message id when the note went, removed when it did
        not (a note Discord did not take does not count). False when the record could not
        be written - logged, never raised: a sent note then stays counted by its
        reservation, and a failed one holds its slot until it ages out (never an extra
        send)."""
        sent = self._load()
        if message_id is None:
            sent = [e for e in sent if e.get("slot") != slot]
        else:
            for entry in sent:
                if entry.get("slot") == slot:
                    entry.pop("slot")
                    entry["message_id"] = message_id
        try:
            self._save(sent)
        except OSError as error:
            _log.error("%s: the send record could not be updated after the send (%s); the "
                       "slot stays reserved", NOTIFY, type(error).__name__)
            return False
        return True

    def _send(self, note: Note) -> dict[str, Any]:
        # Imported here: pionir.discord_gate imports this package, so a module-level
        # import would be circular.
        from pionir.discord_gate import DiscordAuthError, DiscordError, DiscordRest, read_token

        token_file = self.settings.token_file
        token = read_token(token_file) if token_file is not None else None
        if not token:
            why = f"{NOTIFY}: no Discord bot token in {token_file}"
            return {"ok": False, "unavailable": why, "error": why}
        client = DiscordRest(token, api_base=self.settings.api_base, opener=self._opener,
                             timeout=self.settings.timeout)
        channel = self.settings.channel_id
        body = {"content": note.message(), "allowed_mentions": {"parse": []}}
        try:
            sent = client.call("POST", f"/channels/{channel}/messages", body)
        except DiscordAuthError as error:
            why = f"{NOTIFY}: Discord rejected the bot token ({client.scrub(str(error))})"
        except DiscordError as error:
            what = "Discord did not answer" if error.transport else "Discord refused the message"
            why = f"{NOTIFY}: {what} ({client.scrub(str(error))})"
        else:
            message_id = sent.get("id") if isinstance(sent, dict) else None
            if not message_id:
                why = f"{NOTIFY}: Discord answered without a message id"
            else:
                return {"ok": True, "sent": True, "kind": note.kind,
                        "message_id": str(message_id), "channel_id": channel}
        _log.warning("%s", why)
        return {"ok": False, "unavailable": why, "error": why}

    def _result(self, task: Task, note: Note, output: dict[str, Any]) -> TaskResult:
        evidence = ["owner:notify", f"owner:notify:{note.kind}"]
        if output.get("ok") is True:
            evidence.append(f"discord:message:{output['message_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))


__all__ = [
    "ALERTS_PER_DAY",
    "BRIEFS_PER_DAY",
    "NOTIFY",
    "OwnerNotifyAdapter",
    "OwnerNotifySettings",
    "neutralise_mentions",
    "parse_note",
]
