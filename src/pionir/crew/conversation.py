"""Speech and conversation: the only place words come from.

Ported from Hearth, with the room replaced by the CHANNEL. A channel is a workstream
(``outreach``, ``prospecting``, ``ops``...), and an agent belongs to the channels its
cast entry names. Two agents can talk only if they share a channel, the conversation
happens IN one of those channels, and a line said there is perceived only by that
channel's members - an agent outside it does not hear it, remember it or learn from it.

The rest is Hearth's: a deterministic gate opens a conversation (a thought worth saying,
an intention being pursued, or plain sociability), a speech-act frame is chosen before
the model is called, each line is one model call built from the speaker's OWN store,
hearers store what they heard as hearsay (``source="told"``, ``told_by``) at their own
fidelity, a figure somebody states is checked against what the hearer saw (trust moves
either way), an echo is caught, silence is remembered, and an intention closes with an
outcome.

The critic is the anti-confabulation gate, and for a business crew it is the part that
matters most:

- an own claim ("I sent the follow-ups") must be backed by the agent's own ``did`` /
  ``did_own`` episodes - things Pionir reported as actually run, or real project progress;
- a FIGURE ("we made $470", "12 replies") must match a figure the agent holds from a
  real source (a ``seen`` episode). Hearsay figures pass only when the sentence says it
  is hearsay ("Skopos said 12 replies") and the agent really was told that number;
- a proper noun must be one the agent has actually encountered in its own store, and a
  colleague's name only once they have met.

Hearth's OUTSIDE regex (which banned "work", "job", "money", "office", weekdays) and its
LEAVING regex are gone: that is now the whole vocabulary. A dropped line gets one retry,
then the conversation closes.
"""
from __future__ import annotations

import re

from .grounding import (
    ALWAYS_KNOWN,
    figures_in,
    held_figures,
    holds,
    known_entities,
    proper_nouns,
    same,
    words,
)
from .log import log

MAX_TURNS = 6
CONV_TIMEOUT_S = 240               # no turn for this long: it petered out
INITIATE_COOLDOWN_S = 2400         # per agent, after any conversation
REPLY_COOLDOWN_S = 4               # seconds before a reply is even considered
INTENT_RETRY_S = 1200              # an intention whose target was unavailable waits this long

# thought kind -> the speech act it becomes; the thought's own ``say`` is the directive
ACTS = {"doubt": "raise", "stalled": "ask", "news": "ask", "quiet": "ask",
        "trouble": "share", "waiting": "share"}

# A first-person claim to have done a piece of business work. Checked against the
# agent's own record: a thing a colleague did, or a thing only intended, bleeds into the
# first person otherwise.
DID_VERBS = {
    "sent": ("send", "sent"), "drafted": ("draft",), "posted": ("post",),
    "published": ("publish",), "built": ("build", "built"), "found": ("find", "found"),
    "scanned": ("scan",), "approved": ("approv",), "listed": ("list",),
    "replied": ("repl",), "emailed": ("email",), "wrote": ("writ", "wrote"),
    "written": ("writ", "wrote"), "called": ("call",), "contacted": ("contact",),
    "pitched": ("pitch",), "launched": ("launch",), "shipped": ("ship",),
    "updated": ("updat",), "ran": ("run", "ran"), "closed": ("clos",), "signed": ("sign",),
    "booked": ("book",), "finished": ("finish",), "completed": ("complet",),
    "submitted": ("submit",), "messaged": ("messag",), "uploaded": ("upload",),
    "created": ("creat",), "fixed": ("fix",), "deployed": ("deploy",),
    "checked": ("check",), "invoiced": ("invoic",), "followed up": ("follow",),
    "reached out": ("reach",),
}
DID_CLAIM = re.compile(
    r"\bI(?:'ve|\s+have|\s+had)?\s+(?:just\s+|already\s+|earlier\s+|also\s+|finally\s+)?("
    + "|".join(sorted((re.escape(v) for v in DID_VERBS), key=len, reverse=True))
    + r")\b", re.IGNORECASE)
# what counts as evidence of having done it: work Pionir reported as run, real progress
OWN_EVIDENCE_KINDS = ("did", "did_own")
# a sentence that says its figure came from somebody else
ATTRIBUTION = re.compile(r"\b(?:says?|said|told|tells|according to|reported|reports|"
                         r"mentioned|heard)\b", re.IGNORECASE)
VALENCE_POS = re.compile(r"\b(thanks?|thank you|sorry|please|nice|good|great|glad|"
                         r"no problem|welcome|happy|kind|help)\b", re.IGNORECASE)
VALENCE_NEG = re.compile(r"\b(stop|never|why did you|your fault|liar|lie|lying|hate|ugh|"
                         r"seriously|wrong again)\b", re.IGNORECASE)
_STOP = frozenset(words("""
that this with from have were been them then they what when your just about there their
would could should will into said says also some more than very really okay sure
"""))
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> str:
    text = (text or "").strip().strip('"').strip("*").strip()
    text = re.sub(r"\([^)]*\)", "", text)                 # (stage directions)
    text = re.sub(r"\*[^*]*\*", "", text)                 # *actions*
    text = re.sub(r"^\s*[A-Z][a-z]+\s*:\s*", "", text)    # "Scrooge: ..."
    text = re.sub(r"\s+", " ", text).strip()
    return text


def content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


class Conversation:
    _next = 1

    def __init__(self, channel: str, a: str, b: str, t: int) -> None:
        self.id = Conversation._next
        Conversation._next += 1
        self.channel = channel
        self.participants = [a, b]
        self.turns: list = []               # (speaker, text, t)
        self.started_t = t
        self.last_turn_t = t
        self.awaiting: str | None = None    # who owes a reply
        self.awaiting_since_t = 0
        self.pending_request: int | None = None
        self.silences = 0
        self.closed: str | None = None
        self.retried = False
        self.intention: tuple | None = None   # (agent id, intention id) if one opened it
        self.topic: str = ""
        self.on_mind: dict = {}               # agent id -> the thought that brought them

    def other(self, me: str) -> str | None:
        for p in self.participants:
            if p != me:
                return p
        return None


def shared_channels(a, b) -> list:
    """Channels both belong to, in ``a``'s order."""
    theirs = set(b.channels)
    return [c for c in a.channels if c in theirs]


class Talk:
    """The conversation manager. One per crew. Deterministic gates; the brain only
    supplies words."""

    def __init__(self, sim) -> None:
        self.sim = sim
        self.active: dict[int, Conversation] = {}
        self.last_talk_t: dict[str, int] = {}
        self.started = 0
        self.closed = 0
        self.utterances = 0
        self.silences = 0
        self.dropped = 0
        self.no_brain = 0
        self.refused_no_channel = 0
        last = sim.store.conn.execute(
            "SELECT COALESCE(MAX(conv_id), 0) FROM utterances").fetchone()[0]
        Conversation._next = max(Conversation._next, int(last) + 1)
        # cooldowns survive a restart: a resume must not let everybody start talking at once
        self.last_talk_t = {k: int(v) for k, v in (sim.store.get("last_talk_t", {}) or {}).items()}

    def checkpoint(self) -> None:
        self.sim.store.set("last_talk_t", self.last_talk_t)

    # ---- who can talk to whom ------------------------------------------------
    def available(self, a) -> bool:
        return a.working(self.sim) and a.conversation is None

    def can_talk(self, a, b) -> bool:
        return a is not b and bool(shared_channels(a, b))

    # ---- per tick -------------------------------------------------------------
    def tick(self) -> None:
        sim = self.sim
        t = sim.clock.t
        for conv in list(self.active.values()):
            for p in conv.participants:
                ag = sim.agent(p)
                if not ag.working(sim) or conv.channel not in ag.channels:
                    self._close(conv, f"{ag.name} was no longer in #{conv.channel}")
                    break
            if conv.closed:
                continue
            if conv.awaiting and conv.pending_request is None \
                    and t - conv.awaiting_since_t >= REPLY_COOLDOWN_S:
                self._consider_reply(conv)
            if not conv.closed and t - conv.last_turn_t > CONV_TIMEOUT_S \
                    and conv.pending_request is None:
                self._close(conv, "petered out")
        if sim.brain is None or sim.brain.ceiling_hit():
            return
        self._pursue_intentions()
        for a in sim.agents:
            if not self.available(a):
                continue
            if t - self.last_talk_t.get(a.id, -10 ** 9) < INITIATE_COOLDOWN_S * self._tighten():
                continue
            others = [o for o in sim.agents if o is not a and self.can_talk(a, o)
                      and self.available(o)]
            if not others:
                continue
            frame = self._frame_for(a, others)
            if frame is None:
                continue
            target, kind, instruction, thought_id, content, topic, mind, channel = frame
            need = a.drives.error("social")
            per_s = a.t.initiative * (0.6 + 1.4 * min(1.0, need * 3)) / 600.0
            if content:
                per_s *= 2.5
            if a.rng.random() < min(1.0, per_s * max(sim.dt, 1e-3)):
                self.start(a, target, kind, instruction, thought_id, topic, mind, channel)

    def _tighten(self) -> float:
        brain = self.sim.brain
        if brain is None:
            return 1.0
        return min(1.0 + brain.throttled / 10.0, 6.0)

    def _pursue_intentions(self) -> None:
        sim = self.sim
        for a in sim.agents:
            if not self.available(a):
                continue
            it = a.pursuable_intention(sim)
            if it is None:
                continue
            target = sim.agent(it["target"])
            if target is None or not self.can_talk(a, target) or not self.available(target):
                a.mem.note_attempt(it["id"], sim.clock.t + INTENT_RETRY_S)
                continue
            self.initiate_for_intention(a, target, it)

    # ---- frames: what to say, chosen before any model call ------------------
    def _frame_for(self, a, others: list):
        t = self.sim.clock.t
        thoughts = [th for th in a.mem.recent_thoughts(8, unused_only=True)
                    if th["t"] >= t - 6 * 3600]
        by_id = {o.id: o for o in others}
        best = None
        for th in thoughts:
            if th["kind"] not in ACTS or not th.get("say"):
                continue
            if th["urge"] < a.t.reticence * 0.8:
                continue
            o = by_id.get(th["about"]) or self._pick_target(a, others)
            score = th["urge"] + (0.4 if th["about"] in by_id else 0.0)
            if best is None or score > best[0]:
                best = (score, th, o)
        if best is not None:
            _, th, o = best
            return (o, ACTS[th["kind"]], f"Speak to {o.name}: {th['say']}.", th["id"],
                    True, th.get("topic", ""), th.get("text", ""), None)
        o = self._pick_target(a, others)
        if a.rng.random() < 0.15 + 0.85 * a.t.sociability * (1 - a.t.reticence):
            ch = shared_channels(a, o)[0]
            ask = f"Speak to {o.name}: ask briefly how their work in #{ch} is going."
            return (o, "checkin", ask, None, False, "", "", ch)
        return None

    def _pick_target(self, a, others: list):
        def score(o):
            p = a.mem.person(o.id)
            return p["warmth"] + 0.5 * p["familiarity"] - 0.5 * p["grievance"] \
                + a.rng.random() * 0.2
        return max(others, key=score)

    # ---- starting -----------------------------------------------------------
    def initiate_for_intention(self, a, target, it: dict) -> bool:
        if not self.available(a) or not self.available(target) or not self.can_talk(a, target):
            return False
        if self.sim.brain is None or self.sim.brain.ceiling_hit():
            return False                      # no budget to finish it; it keeps its turn
        verb = {"ask": "ask", "tell": "tell them", "raise": "raise this with them",
                "share": "share this with them"}.get(it["kind"], "say")
        started = self.start(a, target, f"intent:{it['kind']}",
                             f"Speak to {target.name}: {verb}: {it['want']}",
                             it.get("thought_id"), it["want"], it["want"], None)
        if started:
            a.conversation.intention = (a.id, it["id"])
            a.mem.bump("intentions_pursued")
        return started

    def start(self, a, target, frame_kind: str, instruction: str, thought_id=None,
              topic: str = "", mind: str = "", channel: str | None = None) -> bool:
        """Open a conversation between two colleagues in a channel they share. Refused
        (False) when they share none - there is nowhere for them to talk."""
        sim = self.sim
        shared = shared_channels(a, target)
        if not shared or a is target:
            self.refused_no_channel += 1
            a.mem.bump("talk_refused_no_channel")
            return False
        if a.conversation is not None or target.conversation is not None:
            return False
        ch = channel if channel in shared else shared[0]
        conv = Conversation(ch, a.id, target.id, sim.clock.t)
        conv.topic = topic
        conv.on_mind = {a.id: mind}
        self.active[conv.id] = conv
        a.conversation = conv
        target.conversation = conv
        self.started += 1
        for x, y in ((a, target), (target, a)):
            x.meet(y, ch)                     # face to face in a channel: now they have met
            self.last_talk_t[x.id] = sim.clock.t
        log.info("conversation %d: %s -> %s in #%s (%s)", conv.id, a.name, target.name, ch,
                 frame_kind)
        a.mem.bump("conversations_started")
        if thought_id:
            a.mem.mark_thought_used(thought_id)
        self._request_speech(a, conv, frame_kind, instruction, priority=1)
        return True

    # ---- speaking -----------------------------------------------------------
    def _request_speech(self, a, conv: Conversation, frame_kind: str, instruction: str,
                        priority: int) -> None:
        brain = self.sim.brain
        if brain is None:
            self.no_brain += 1
            self._close(conv, "no language faculty")
            return
        messages = self.build_prompt(a, conv, instruction)
        options = {
            "temperature": min(0.95, 0.6 + 0.4 * a.t.impulsivity),
            "top_p": 0.9, "top_k": 50, "repeat_penalty": 1.08, "repeat_last_n": 64,
            "num_predict": a.t.speech_cap,
            "seed": a.rng.randrange(1, 2 ** 31),
        }
        a.mem.bump("model_calls")
        conv.pending_request = brain.request(
            a.id, f"speech:{frame_kind}", None, None, options,
            lambda text, meta, err, a=a, conv=conv, fk=frame_kind:
                self._on_speech(a, conv, fk, text, err),
            priority=priority, messages=messages)
        if conv.pending_request is None:
            self._close(conv, "ceiling: no words came")

    def _on_speech(self, a, conv: Conversation, frame_kind: str, text, err) -> None:
        conv.pending_request = None
        if conv.closed:
            return
        if err or not text:
            self._close(conv, f"the words did not come ({err or 'empty'})")
            return
        clean = self.critic(a, text)
        if clean is None:
            self.dropped += 1
            a.mem.bump("confab_dropped")
            log.info("critic dropped %s's line: %r", a.name, str(text)[:120])
            if not conv.retried:
                conv.retried = True
                self._request_speech(
                    a, conv, frame_kind,
                    "Say it again, differently: only what is listed, no figure you did not "
                    "see yourself, no name you have not come across.", priority=0)
                return
            self._close(conv, "critic dropped the line twice")
            return
        if self._is_echo(a, conv, clean):
            a.mem.bump("echo_caught")
            if not conv.retried:
                conv.retried = True
                self._request_speech(
                    a, conv, frame_kind,
                    "Say something different: do not repeat what was just said or what you "
                    "said before. Answer it, ask something, or mention something you know.",
                    priority=0)
                return
            self.silences += 1
            other = self.sim.agent(conv.other(a.id))
            a.mem.add_episode(self.sim.clock.t, conv.channel, "kept_quiet",
                              f"had nothing to say to {other.name}", actor=a.id,
                              people=[other.id], salience=0.5, source="did")
            other.mem.add_episode(self.sim.clock.t, conv.channel, "silence",
                                  f"{a.name} did not answer", people=[a.id],
                                  salience=0.8 * other.t.people_weight, source="seen")
            self._close(conv, f"{a.name} ran out of words")
            return
        conv.retried = False
        self.say(a, conv, clean, frame_kind)

    @staticmethod
    def _norm(s: str) -> set:
        return set(re.findall(r"[a-z']+", s.lower())) - {"i", "the", "a", "to", "it", "you",
                                                          "and", "that", "is", "of"}

    def _is_echo(self, a, conv: Conversation, text: str) -> bool:
        same_line = text.strip().lower().rstrip(".!?")
        if any(spk == a.id and prev.strip().lower().rstrip(".!?") == same_line
               for spk, prev, _ in conv.turns):
            return True
        for e in a.mem.recent(12, kinds=("said",)):
            prev = e["text"].split(": ", 1)[-1].strip().strip('"').lower().rstrip(".!?")
            if prev == same_line:
                return True
        mine = self._norm(text)
        if len(mine) < 2:
            return False
        for _spk, prev, _ in conv.turns[-3:]:
            theirs = self._norm(prev)
            if min(len(mine), len(theirs)) < 4:
                continue
            overlap = len(mine & theirs) / max(1, min(len(mine), len(theirs)))
            if overlap >= 0.75 and len(mine & theirs) >= 4:
                return True
        return False

    def say(self, a, conv: Conversation, text: str, frame_kind: str) -> None:
        """A line said in ``conv.channel``: recorded, and heard by that channel's members
        who are at work - and by nobody else."""
        sim = self.sim
        t = sim.clock.t
        target_id = conv.other(a.id)
        conv.turns.append((a.id, text, t))
        conv.last_turn_t = t
        conv.awaiting = target_id
        conv.awaiting_since_t = t
        self.utterances += 1
        a.mem.bump("utterances")
        if target_id:
            tp = a.mem.person(target_id)
            if tp["unanswered"]:
                tp["unanswered"] = 0
                a.mem.save_person(tp)
        a.mem.set("last_utterance_t", t)
        self.last_talk_t[a.id] = t
        sim.store.add_utterance(t, a.id, conv.channel, target_id, text, conv.id, frame_kind)
        target = sim.agent(target_id) if target_id else None
        a.mem.add_episode(t, conv.channel, "said",
                          f'said to {target.name if target else "the channel"} in '
                          f'#{conv.channel}: "{text}"',
                          actor=a.id, people=[target_id] if target_id else [],
                          salience=0.8 + 0.4 * a.t.people_weight, source="did",
                          detail={"conv": conv.id, "frame": frame_kind})
        a.speech_shown = (text, t)
        if target is not None:
            a.drives.apply({"social": 0.04})          # talking with a colleague
        for h in sim.agents:
            if h is a or conv.channel not in h.channels or not h.working(sim):
                continue                              # not a member, or not at work: unheard
            addressed = h.id == target_id
            if not addressed and h.rng.random() >= 0.3 + 0.7 * h.t.perception:
                continue                              # a member, but it did not register
            self._hear_words(h, a, text, t, addressed, conv)
        if len(conv.turns) >= MAX_TURNS:
            self._close(conv, "ran its course")

    def _hear_words(self, h, speaker, text: str, t: int, addressed: bool,
                    conv: Conversation) -> None:
        fid = h.t.memory_fidelity
        if h.rng.random() < 0.35 + 0.65 * fid:
            kept = text
        else:
            words = text.split()
            kept = " ".join(words[:8]) + ("..." if len(words) > 8 else "")   # honest omission
        target = self.sim.agent(conv.other(speaker.id))
        who = "me" if addressed else (target.name if target else "the channel")
        before = content_words(" ".join(e["text"] for e in h.mem.recent(60)))
        h.mem.add_episode(t, conv.channel, "heard_say",
                          f'{speaker.name} said to {who} in #{conv.channel}: "{kept}"',
                          actor=speaker.id, people=[speaker.id],
                          salience=(1.0 if addressed else 0.6) * h.t.people_weight,
                          source="told", told_by=speaker.id,
                          detail={"verbatim": kept == text, "conv": conv.id,
                                  "addressed": addressed})
        self._check_claim(h, speaker, text)
        p = h.mem.person(speaker.id)
        p["talks"] += 1
        p["last_seen_t"] = t
        p["last_seen_channel"] = conv.channel
        p["familiarity"] = min(1.0, p["familiarity"] + 0.04 * (1.0 - p["familiarity"]))
        pos = len(VALENCE_POS.findall(text))
        neg = len(VALENCE_NEG.findall(text))
        if addressed:
            p["warmth"] = max(-1.0, min(1.0, p["warmth"] + 0.02 + 0.06 * pos - 0.08 * neg))
            if neg > pos:
                p["grievance"] = min(1.0, p["grievance"] + 0.05)
                h.affect.push("irritation", 0.6, 0.25)
            elif pos:
                h.affect.push("warmth", 0.7, 0.12 + 0.20 * h.t.sociability)
                p["grievance"] = max(0.0, p["grievance"] - 0.03)
            h.drives.apply({"social": 0.04})          # somebody talked WITH me
        h.mem.save_person(p)
        # stimulation is relieved by what is genuinely new to me, not by any line at all
        words = content_words(text)
        if words:
            novelty = len(words - before) / len(words)
            if novelty >= 0.3:
                h.drives.apply({"stimulation": 0.10 * novelty * (0.5 + h.t.curiosity)})
                h.mem.bump("news_taken_in")
                h.affect.push("curiosity", 0.55, 0.08 + 0.25 * h.t.curiosity)

    def _check_claim(self, h, speaker, text: str) -> None:
        """Does a figure they just stated match one I saw myself for the same thing? Only
        what I hold counts; a stale figure of mine can make me wrongly doubt them, which is
        honest."""
        claims = [f for f in figures_in(text) if f.noun]
        if not claims:
            return
        mine = [e for e in h.mem.recent(200, since_t=self.sim.clock.t - 12 * 3600)
                if e["source"] == "seen"]
        for claim in claims:
            for e in reversed(mine):
                for held in figures_in(e["text"]):
                    if held.noun.rstrip("s") != claim.noun.rstrip("s"):
                        continue
                    if same(held.value, claim.value):
                        h.note_trust(speaker.id, 0.08 * h.t.memory_fidelity,
                                     f"was right about {claim.text} {claim.noun}, from what "
                                     f"I saw myself")
                    else:
                        h.note_trust(speaker.id, -0.10 * h.t.memory_fidelity,
                                     f"said {claim.text} {claim.noun}; I saw {held.text}")
                    return

    # ---- replying -------------------------------------------------------------
    def _consider_reply(self, conv: Conversation) -> None:
        sim = self.sim
        h = sim.agent(conv.awaiting)
        speaker = sim.agent(conv.other(h.id))
        p = h.mem.person(speaker.id)
        urge = (0.35 + 0.25 * h.t.sociability + 0.3 * min(1.0, h.drives.error("social") * 3)
                + 0.15 * p["warmth"] - 0.1 * p["grievance"] - 0.06 * len(conv.turns)
                + 0.12 * min(6, p["unanswered"]))
        last_text = conv.turns[-1][1] if conv.turns else ""
        if "?" in last_text:
            urge += 0.25 + 0.25 * h.t.curiosity
        if h.name in last_text:
            urge += 0.2
        urge += h.rng.random() * 0.1
        conv.awaiting = None
        p["addressed"] = int(p.get("addressed", 0)) + 1
        h.mem.save_person(p)
        if urge < h.t.reticence:
            self.silences += 1
            conv.silences += 1
            p["unanswered"] = int(p.get("unanswered", 0)) + 1
            h.mem.save_person(p)
            h.mem.bump("kept_quiet")
            h.mem.add_episode(sim.clock.t, conv.channel, "kept_quiet",
                              f"did not answer {speaker.name}", actor=h.id,
                              people=[speaker.id], salience=0.5, source="did")
            speaker.mem.add_episode(sim.clock.t, conv.channel, "silence",
                                    f"{h.name} did not answer", people=[h.id],
                                    salience=0.9 * speaker.t.people_weight, source="seen")
            sp = speaker.mem.person(h.id)
            sp["warmth"] = max(-1.0, sp["warmth"] - 0.02)
            speaker.mem.save_person(sp)
            speaker.affect.push("loneliness" if speaker.t.sociability > 0.5 else "irritation",
                                0.5, 0.2)
            self._close(conv, f"{h.name} did not answer")
            return
        if "?" in last_text:
            instruction = (f"{speaker.name} asked you something. Answer it from what is listed. "
                           f"If you do not know, say so in a few words and say what you do know.")
        else:
            instruction = (f"Reply to what {speaker.name} just said: take it up, agree or "
                           f"disagree, or add something of your own that bears on it.")
        if h.mem.counter("utterances") == 0:
            instruction += " This is the first time you have spoken on this team."
        self._request_speech(h, conv, "reply", instruction, priority=0)

    # ---- prompt: only this agent's own store --------------------------------
    def build_prompt(self, a, conv: Conversation, instruction: str) -> list:
        sim = self.sim
        t = sim.clock.t
        tt = a.t
        length = ("in a few words" if tt.speech_cap <= 40 else
                  "in one short sentence" if tt.speech_cap <= 90 else "in one or two sentences")
        system = (
            f"You are {tt.name} ({tt.they}/{tt.them}). {tt.disposition}. You work on a small "
            f"business team. Your job: {a.role}. You know ONLY what is listed in this message; "
            f"anything not listed did not happen and you do not know it. Never state a figure - "
            f"money, counts, results - that is not listed below exactly as you saw it. Never "
            f"say you did something unless it is listed as done; something waiting on approval "
            f"has NOT been done. Never name a customer, company, product or person that is not "
            f"listed. Speak only as {tt.name}, {length}, plainly, the way you would write in a "
            f"team chat. Respond to what was actually said; never repeat the other person's "
            f"words back, never repeat yourself. You are talking TO them, so call them 'you'. "
            f"Output the message only: no narration, no quotation marks, no name prefix."
        )
        other = sim.agent(conv.other(a.id)) if conv.other(a.id) else None
        feelings = [a.affect.describe()]
        for k, word in (("purpose", "wanting to get real work done"),
                        ("social", "wanting to talk"),
                        ("stimulation", "wanting something new to work with")):
            if a.drives.error(k) > 0.15:
                feelings.append(word)
        last_heard = next((txt for spk, txt, _ in reversed(conv.turns) if spk != a.id), "")
        query = " ".join(x for x in [conv.topic, last_heard or instruction,
                                     other.name if other else ""] if x)
        mems = a.mem.recall(t, query=query, people=[other.id] if other else [],
                            channel=conv.channel, limit=7,
                            half_life_days=1.0 + 2.0 * tt.memory_fidelity,
                            exclude_kinds=("said", "heard_say", "silence", "reflection",
                                           "intended", "kept_quiet"))
        if not conv.turns and other is not None:
            told = [e for e in a.mem.recent(12, kinds=("heard_say",))
                    if e["detail"].get("conv") != conv.id and e["told_by"] == other.id]
            if told:
                mems.append(told[-1])
        mems.sort(key=lambda e: e["t"])
        mem_lines = []
        for m in mems:
            tag = {"told": "heard", "inferred": "your guess", "felt": "a feeling",
                   "noticed": "noticed"}.get(m["source"], "")
            line = m["text"]
            if other is not None:
                line = re.sub(rf"\b{re.escape(other.name)}'s\b", "your", line)
                line = re.sub(rf"\b{re.escape(other.name)}\b", "you", line)
            where = f"#{m['channel']}" if m["channel"] else "on your own"
            mem_lines.append(f"- {self._when(m['t'])}, {where}: {line}"
                             + (f" ({tag})" if tag else ""))
        today = sim.clock.day_start_t()
        done = a.mem.recent(8, kinds=OWN_EVIDENCE_KINDS, since_t=today)
        results = a.mem.recent(6, kinds=("result",), since_t=today)
        pending = a.mem.recent(4, kinds=("job_pending",), since_t=today)
        failed = a.mem.recent(4, kinds=("tried",), since_t=today)
        own = a.mem.recent(3, kinds=("said",))
        known = [sim.agent(p["other"]) for p in a.mem.people() if sim.agent(p["other"])]
        who = ", ".join(f"{o.name} ({o.t.they}/{o.t.them})" for o in known) or "nobody yet"
        project = a.project.title(sim) if a.project is not None else None
        situation = (
            f"\n\nIt is {sim.clock.local().strftime('%A %H:%M')}. You are in #{conv.channel}.\n"
            f"Colleagues you have met: {who}.\n"
            f"You feel {', '.join(feelings)}.\n"
            + (f"What you are working on: {project}.\n" if project
               else "You have no project on right now.\n")
        )
        if mem_lines:
            situation += "What you remember (this is everything):\n" + "\n".join(mem_lines) + "\n"
        else:
            situation += "You remember nothing in particular yet.\n"
        if done:
            situation += ("What you have done yourself today (this is all of it; you did nothing "
                          "else): " + "; ".join(self._you(d["text"]) for d in done) + "\n")
        else:
            # saying nothing here is what invites "I sent those emails this morning" from an
            # agent who did no such thing; the absence has to be stated
            situation += "You have not done anything yourself today.\n"
        if results:
            situation += ("Results you have seen yourself (the only figures you may state): "
                          + "; ".join(r["text"] for r in results) + "\n")
        else:
            situation += "You have not seen any results today, so you have no figures to give.\n"
        if pending:
            situation += ("Waiting on Ian's approval - these have NOT run: "
                          + "; ".join(self._you(p["text"]) for p in pending) + "\n")
        if failed:
            situation += ("Tried and it did not work: "
                          + "; ".join(self._you(f["text"]) for f in failed) + "\n")
        if own:
            situation += ("What you yourself said earlier (do not say it again): "
                          + " / ".join(o["text"].split(": ", 1)[-1].strip('"') for o in own)
                          + "\n")
        if other is not None:
            situation += (f"You are talking with {other.name} in #{conv.channel}. Say 'you' to "
                          f"{other.t.them}, never {other.t.their} name in the third person.\n")
        mind = conv.on_mind.get(a.id)
        if mind:
            situation += f"What is on your mind: {mind}\n"
        if conv.topic:
            situation += f"You are talking about {conv.topic}.\n"
        messages = [{"role": "system", "content": system + situation}]
        for spk, text, _ in conv.turns[-6:]:
            messages.append({"role": "assistant" if spk == a.id else "user", "content": text})
        if messages[-1]["role"] == "assistant" or len(messages) == 1:
            messages.append({"role": "user",
                             "content": f"({instruction} Say it now, {length}. Message only.)"})
        else:
            messages[-1]["content"] += f"\n({instruction} {length.capitalize()}, message only.)"
        return messages

    @staticmethod
    def _you(text: str) -> str:
        return text.removeprefix("you ")

    def _when(self, t: int) -> str:
        gap = max(0, self.sim.clock.t - t)
        if gap < 3600:
            return f"{max(1, gap // 60)} minutes ago"
        if gap < 86400:
            return f"{gap // 3600} hours ago"
        return f"{gap // 86400} days ago"

    # ---- the critic -----------------------------------------------------------
    def critic(self, a, text: str) -> str | None:
        """The cleaned line, or None if it asserts something this agent does not hold."""
        clean = _sentences(text)
        if not clean:
            return None
        if not self._own_claim_ok(a, clean):
            return None
        if not self._figures_ok(a, clean):
            return None
        if not self._names_ok(a, clean):
            return None
        words = clean.split()
        cap = max(6, int(a.t.speech_cap * 0.9))
        if len(words) > cap:
            clean = " ".join(words[:cap]).rstrip(",;") + "."
        return clean

    def _own_claim_ok(self, a, text: str) -> bool:
        """Did they really do the thing they say they did? Their own completed work is on
        record; a colleague's work, an intention or a job still waiting is not."""
        for m in DID_CLAIM.finditer(text):
            verb = re.sub(r"\s+", " ", m.group(1).lower())
            stems = DID_VERBS.get(verb, (verb,))
            mine = " ".join(e["text"].lower() for e in a.mem.recent(
                60, kinds=OWN_EVIDENCE_KINDS, since_t=self.sim.clock.t - 7 * 86400))
            if any(re.search(rf"\b{re.escape(st)}\w*", mine) for st in stems):
                continue
            a.mem.bump("false_own_claim")
            log.info("%s claimed to have %s something with nothing behind it: %r",
                     a.name, verb, text[:90])
            return False
        return True

    def _figures_ok(self, a, text: str) -> bool:
        """Every figure stated must be one this agent SAW from a real source; a figure it was
        only told passes only when the sentence says so, and it really was told it."""
        sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]
        claims = [(s, f) for s in sentences for f in figures_in(s)]
        if not claims:
            return True
        seen = held_figures(a.mem, ("seen",))
        told = None
        for sentence, fig in claims:
            if holds(seen, fig.value):
                continue
            if ATTRIBUTION.search(sentence):
                if told is None:
                    told = held_figures(a.mem, ("told",))
                if holds(told, fig.value):
                    continue
            a.mem.bump("unbacked_figure")
            log.info("%s stated a figure nobody showed them (%s): %r", a.name, fig.text,
                     text[:90])
            return False
        return True

    def _names_ok(self, a, text: str) -> bool:
        """Colleagues only once met; any other name only once actually encountered."""
        colleagues = {ag.name: ag.id for ag in self.sim.agents}
        met = {p["other"] for p in a.mem.people()} | {a.id}
        for tok in re.findall(r"\b[A-Z][a-z]+\b", text):
            if tok in colleagues and colleagues[tok] not in met:
                a.mem.bump("unmet_name")
                log.info("%s named %s, whom they have never met: %r", a.name, tok, text[:90])
                return False
        known = None
        for tok in proper_nouns(text):
            if tok in colleagues or tok in ALWAYS_KNOWN or tok == a.name:
                continue
            if known is None:
                known = known_entities(a.mem)
            if tok not in known:
                a.mem.bump("unknown_entity")
                log.info("%s named %r, which they have never come across: %r", a.name, tok,
                         text[:90])
                return False
        return True

    # ---- closing ----------------------------------------------------------------
    def _close(self, conv: Conversation, reason: str) -> None:
        if conv.closed:
            return
        conv.closed = reason
        self.closed += 1
        sim = self.sim
        if conv.intention:
            aid, iid = conv.intention
            a = sim.agent(aid)
            spoke = any(spk == aid for spk, _, _ in conv.turns)
            answered = any(spk != aid for spk, _, _ in conv.turns)
            try:
                if spoke and answered:
                    a.mem.resolve_intention(iid, "done", f"said it and got an answer: {reason}",
                                            sim.clock.t)
                    a.mem.bump("intentions_done")
                elif spoke:
                    a.mem.resolve_intention(iid, "done", f"said it; no answer: {reason}",
                                            sim.clock.t)
                    a.mem.bump("intentions_done")
                else:
                    attempts = a.mem.note_attempt(iid, sim.clock.t + INTENT_RETRY_S)
                    if attempts >= 3:
                        a.mem.resolve_intention(iid, "tried", f"never found the words: {reason}",
                                                sim.clock.t)
                        a.mem.bump("intentions_tried")
            except ValueError as exc:
                log.warning("intention %s could not be closed: %s", iid, exc)
        if conv.pending_request is not None and sim.brain is not None:
            sim.brain.cancel(conv.pending_request)
        for p in conv.participants:
            ag = sim.agent(p)
            if ag.conversation is conv:
                ag.conversation = None
            self.last_talk_t[p] = sim.clock.t
        self.active.pop(conv.id, None)
        names = " and ".join(sim.agent(p).name for p in conv.participants)
        log.info("conversation %d (%s, #%s, %d turns) closed: %s", conv.id, names,
                 conv.channel, len(conv.turns), reason)

    def snapshot(self) -> dict:
        return {
            "active": [{"id": c.id, "channel": c.channel, "participants": c.participants,
                        "turns": len(c.turns), "awaiting": c.awaiting,
                        "pending": c.pending_request is not None}
                       for c in self.active.values()],
            "started": self.started, "closed": self.closed, "utterances": self.utterances,
            "silences": self.silences, "dropped": self.dropped, "no_brain": self.no_brain,
            "refused_no_channel": self.refused_no_channel, "tighten": round(self._tighten(), 2),
        }
