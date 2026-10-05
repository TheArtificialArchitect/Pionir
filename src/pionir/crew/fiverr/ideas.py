"""``fiverr.ideas``: new Fiverr gig ideas, chosen from what the crew's own products measure.

**Pionir never posts a gig.** Like the drafter (gigs.py) this only PREPARES: it picks the next
idea from a short list of fully written, checked listings (``IDEAS`` - fixed text the owner can
read and edit here; no model writes a listing), posts it to the owner's Discord as an ``idea``
card with the listing attached, and waits. He decides; if he wants it he creates the gig on
Fiverr himself.

**Chosen from measured demand only.** Each idea names the API product whose work it sells
(``IDEAS[...].api``). ``demand.py`` ranks those products from Scrooge's recorded usage and page
views; an idea is proposed only when its product has a measured, positive score, the best score
first. The card says what was measured and says plainly that it is NOT Fiverr search volume,
which nothing here can see. An idea whose product has no measured demand is not proposed; an
unreadable or stale ranking proposes nothing (unknown is never "go").

**One at a time.** An idea waits on the owner's reply before another is proposed, and an idea he
skipped is never proposed again. His replies on the card: ``skip`` (drop it), ``redraft`` (a
fresh card), ``price <basic> <standard> <premium>`` (sets the prices, new card), ``listed``
(he put it on Fiverr - recorded, and the card explains the delivery gap below).

**No delivery path yet, and the card says so.** The desk (desk.py) routes an order only to a
gig in ``gigs.SERVICES``; an order for an idea's gig stops on a NEEDS YOU card for him to
deliver by hand until a delivery path is built. Accepting an idea does not build one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..blog import _clip, _Unreadable, read_record, record_path, save_record
from ..demand import read_ranking
from ..figures import Figure
from ..log import log
from ..result import Err, Ok
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import checks
from .gigs import (
    AUTO_MADE,
    Package,
    Service,
    check_gig,
    post_card,
    read_inbox,
    render_card,
    render_image,
    render_markdown,
)
from .prices import (
    Proposal,
    parse_owner_prices,
    price_packages,
    unchecked_sources,
    verify_site_prices,
)

FIVERR_GIG_CAP_NOTE = (
    "Fiverr caps how many gigs a seller can have live at once; the cap depends on your seller "
    "level, so check your Gigs page before adding one. You have four live today.")


@dataclass(frozen=True)
class Idea:
    service: Service
    api: str                # the demand ranking's ``api`` ref whose work this gig sells
    product: str            # that product's name, as the card words it
    cannot: str             # what the gig does NOT do that a buyer might assume


EMAIL_CLEAN = Service(
    key="emailclean",
    title="I will clean and verify your email list and remove bad addresses",
    description=(
        "Sending to a list full of typos, fake and dead addresses? Send me your list and I'll "
        "check every address, then give you back a cleaned list and a report of what was "
        "removed and why.\n\n"
        "Every address is checked for:\n"
        "- Valid email syntax\n"
        "- A domain that can receive mail (a mail server record exists)\n"
        "- Disposable or throwaway domains\n"
        "- Role addresses such as info or sales (flagged, not removed)\n"
        "- Likely typos in popular domains, with the fix suggested\n"
        "- Duplicates (removed)\n\n"
        "What I can't do: confirm that one particular mailbox exists or that a person reads "
        "it. That needs a message to be sent, and I never send anything to your list.\n\n"
        "You get a cleaned CSV, a CSV of the removed addresses with the reason for each, and a "
        "short summary in counts.\n\n"
        + AUTO_MADE),
    made_by=AUTO_MADE, uses_ai=False,
    packages={
        "basic": Package("Up to 500", "Up to 500 addresses checked, cleaned list and removed "
                         "list with reasons", ("Up to 500 addresses", "Cleaned CSV",
                                               "Removed CSV with reasons"), 1, 1),
        "standard": Package("Up to 2,000", "Up to 2,000 addresses checked, cleaned list and "
                            "removed list with reasons", ("Up to 2,000 addresses",
                                                          "Cleaned CSV",
                                                          "Removed CSV with reasons"), 2, 1),
        "premium": Package("Up to 5,000", "Up to 5,000 addresses checked, cleaned list and "
                           "removed list with reasons", ("Up to 5,000 addresses",
                                                         "Cleaned CSV",
                                                         "Removed CSV with reasons"), 3, 2),
    },
    prices={
        "basic": Proposal(1500, "No site price for a one-off list check (the site sells the "
                                "email check by subscription). Automated work, priced under one "
                                "find ($19 net) and far under the smallest custom build "
                                "($149 net).", ("find", "small")),
        "standard": Proposal(3500, "A bit over twice the 500-address price for four times the "
                                   "addresses; still automated.", ("find",)),
        "premium": Proposal(7000, "Twice the 2,000-address price for 5,000 addresses; still "
                                  "well under the smallest custom build ($149 net).",
                            ("small",)),
    },
    faq=(
        ("What format should my list be in?",
         "A CSV or a plain text file: one address per line, or a column of addresses. Tell me "
         "the column name if there are several."),
        ("Will you tell me which mailboxes are real?",
         "No. I check the address and its domain, not the individual mailbox. A clean address "
         "can still bounce."),
        ("Do you remove role addresses like info or sales?",
         "No, they are flagged in the report and left in the cleaned list, because many are "
         "real. Tell me if you want them removed."),
        ("What do I get back?",
         "A cleaned CSV, a CSV of the removed addresses with the reason for each, and a short "
         "summary in counts, all through Fiverr."),
    ),
    tags=("email list cleaning", "email verification", "email validation",
          "remove bad emails", "data cleaning"),
    requirements=(
        "Attach your list (CSV or plain text), one address per line or in one column.",
        "If it is a CSV with several columns, which column holds the email addresses?",
        "Should role addresses such as info or sales be kept (the default) or removed?",
        "Anything else about the list I should know, such as where the addresses came from? "
        "(optional)",
    ),
    image_headline="Clean Your Email List",
    image_points=("Bad addresses found and listed", "A cleaned list and a report",
                  "Checked automatically"),
    limits={"basic": 500, "standard": 2000, "premium": 5000},
)

CODES = Service(
    key="codes",
    title="I will make QR codes or barcodes for you in bulk as clean SVG files",
    description=(
        "Need a set of QR codes or product barcodes made properly? Send me a list of what each "
        "one should hold and I'll make each as a crisp SVG file that prints sharp at any size.\n\n"
        "What I can make:\n"
        "- QR codes for web addresses, plain text, Wi-Fi network details or contact cards\n"
        "- Code 128, EAN-13 and UPC-A barcodes\n"
        "- EAN-13 and UPC-A check digits worked out for you, or checked if you give one\n"
        "- Your colours for the code and its background\n\n"
        "You get one SVG file per code, named from your list, in a single zip.\n\n"
        "What I can't do: register barcode numbers for retail. Numbers for selling in shops "
        "come from the barcode registry, and I only draw the number you give me.\n\n"
        + AUTO_MADE),
    made_by=AUTO_MADE, uses_ai=False,
    packages={
        "basic": Package("Up to 10 codes", "Up to 10 QR codes or barcodes as SVG files in a "
                         "zip", ("Up to 10 codes", "SVG files", "One zip"), 1, 1),
        "standard": Package("Up to 50 codes", "Up to 50 QR codes or barcodes as SVG files in a "
                            "zip", ("Up to 50 codes", "SVG files", "One zip"), 2, 1),
        "premium": Package("Up to 200 codes", "Up to 200 QR codes or barcodes as SVG files in "
                           "a zip", ("Up to 200 codes", "SVG files", "One zip"), 3, 2),
    },
    prices={
        "basic": Proposal(1200, "No site price for a one-off batch (the site sells the QR and "
                                "barcode API by subscription). Automated work, priced under "
                                "one find ($19 net).", ("find",)),
        "standard": Proposal(3000, "Five times the codes for two and a half times the price; "
                                   "still automated.", ("find",)),
        "premium": Proposal(7500, "Four times the codes for two and a half times the price; "
                                  "well under the smallest custom build ($149 net).",
                            ("small",)),
    },
    faq=(
        ("What do I send you?",
         "A list, one line per code: what it should contain and, if you like, the file name "
         "you want. For barcodes also the type."),
        ("Can the codes go on printed material?",
         "Yes. SVG files are vector, so they print sharp at any size. Test one scan before "
         "you print a large run."),
        ("Can you put a logo in the middle?",
         "No. These packages are plain codes only, which keeps them reliably scannable."),
        ("Can you give me barcode numbers to sell in shops?",
         "No. I draw the number you give me; shop barcode numbers are issued by the barcode "
         "registry."),
    ),
    tags=("qr code", "barcode", "qr code generator", "bulk qr codes", "vector files"),
    requirements=(
        "A list of what each code should hold, one per line (web address, text, Wi-Fi "
        "details or contact details).",
        "For barcodes: which type (Code 128, EAN-13 or UPC-A) and the numbers.",
        "Colours for the code and background, if you want something other than black on "
        "white. (optional)",
        "File names you want for the codes. (optional)",
    ),
    image_headline="QR Codes and Barcodes in Bulk",
    image_points=("Crisp SVG, sharp at any size", "QR, Code 128, EAN-13, UPC-A",
                  "One zip, named your way"),
    limits={"basic": 10, "standard": 50, "premium": 200},
)

IDEAS = {i.service.key: i for i in (
    Idea(EMAIL_CLEAN, "email", "Email Verify",
         "It checks the address and its domain, not that a particular mailbox exists."),
    Idea(CODES, "qr", "QR Code and Barcode",
         "It draws codes; it does not issue retail barcode numbers.")
)}
# the two products behind CODES share one idea: the better of their two scores counts
EXTRA_APIS = {"codes": ("barcode",)}

PRODUCT_INPUTS = ("api_calls", "api_caller_days", "guide_views")
_BLANK = {"status": "new", "owner_prices": None, "version": 0, "cards": [], "redraft": True}


def measured(ranking, idea: Idea) -> dict | None:
    """The idea's measured demand: the best ``api`` row among its products with a positive
    score - ``{score, name, figures: {input: value}}`` - or None when none was measured."""
    refs = {idea.api, *EXTRA_APIS.get(idea.service.key, ())}
    best = None
    for row in ranking or []:
        score = row.get("score")
        if row.get("kind") != "api" or row.get("ref") not in refs:
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)) or score <= 0:
            continue
        if best is None or score > best["score"]:
            figs = {}
            for k in PRODUCT_INPUTS:
                v = (row.get("inputs") or {}).get(k)
                v = v.get("value") if isinstance(v, dict) else None
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    figs[k] = v
            best = {"score": score, "name": str(row.get("name") or idea.product),
                    "figures": figs}
    return best


def evidence_line(idea: Idea, m: dict) -> str:
    f = m["figures"]
    parts = []
    if "api_calls" in f:
        parts.append(f"{f['api_calls']:g} API calls")
    if "api_caller_days" in f:
        parts.append(f"{f['api_caller_days']:g} caller-days")
    used = " from ".join(parts) if len(parts) == 2 else (parts[0] if parts else "usage recorded")
    guide = f", and {f['guide_views']:g} views of its guides" if "guide_views" in f else ""
    return (f"{m['name']} shows {used}{guide} in the demand ranking (score {m['score']:g}). "
            "That is how much people use what this gig sells on our own sites and API. It is "
            "NOT Fiverr search volume, which Pionir cannot see, so treat it as a hint only.")


def pick(ranking, record_ideas: dict) -> tuple | None:
    """``(idea, measurement)`` for the next idea to propose: the best measured one not yet
    proposed, skipped or listed. None when nothing qualifies."""
    best = None
    for key, idea in IDEAS.items():
        if (record_ideas.get(key) or {}).get("status", "new") != "new":
            continue
        m = measured(ranking, idea)
        if m is not None and (best is None or m["score"] > best[1]["score"]):
            best = (idea, m)
    return best


def render_idea_card(idea: Idea, m: dict, priced: dict, version: int, unchecked: list) -> str:
    body = render_card(idea.service, priced, version, unchecked)
    head = ["**A new gig idea** - nothing has been posted to Fiverr.", "",
            "**Why this one:** " + evidence_line(idea, m), "",
            f"**What it does not do:** {idea.cannot}", "",
            "**Delivery:** Pionir has no automatic delivery for this gig yet. An order for it "
            "will stop on a NEEDS YOU card for you to deliver by hand until one is built.", "",
            FIVERR_GIG_CAP_NOTE, ""]
    tail = ["", "Replies: `skip` drops this idea for good; `listed` records that you put it "
                "on Fiverr; `price <basic> <standard> <premium>` sets the prices; `redraft` "
                "gives a fresh card."]
    return "\n".join(head) + body.replace(
        "Reply `redraft` to get a fresh draft of this gig.", "").rstrip() + "\n" + "\n".join(tail)


class GigIdeaProposer(_Base):
    """``fiverr.ideas``: the next gig idea, from measured demand, to the owner's Discord."""

    record_what = "the idea proposer's own record of every gig idea it handed to the owner"

    def __init__(self, spec, *, source_root: str | None = None,
                 ssh_dir: str | None = None) -> None:
        super().__init__(spec)
        self.source_root = Path(source_root) if source_root else None
        self.ssh_dir = ssh_dir

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        blank = {"ideas": {}, "replies_seen": []}
        return blank if doc is None else {**blank, **doc}

    @never_raises()
    def run(self, ctx: WorkContext):
        if ctx.state_dir is None or ctx.fiverr_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state or Fiverr folder: the idea "
                             "proposer cannot keep its record or write the listings",
                             retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: idea cards are posted only "
                             "through Pionir", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc})",
                             retryable=False)
        inbox = read_inbox(ctx, "idea")
        if isinstance(inbox, Err):
            why = str(inbox.error)
            if re.search(r"(?i)unknown capability|not configured|no such capability|"
                         r"CapabilityNotFound|fiverr\.inbox", why):
                return self._err(ErrorKind.NOT_CONFIGURED, f"Pionir's Fiverr desk is off "
                                 f"({_clip(why, 160)}); set PIONIR_FIVERR_DESK=1",
                                 retryable=False)
            return self._err(ErrorKind.UNAVAILABLE, f"could not read the owner's replies "
                             f"({_clip(why, 160)})")
        events: list = []
        try:
            guard = checks.load_guard(ctx.secrets_dir, self.ssh_dir, checks.owner_markers())
        except Exception as exc:  # noqa: BLE001 - fail closed: no guard, no card
            return self._err(ErrorKind.UNAVAILABLE, f"the secrets scan could not load "
                             f"({_clip(exc, 160)}); no idea is handed over unchecked")
        ranking = read_ranking(ctx.state_dir, ctx.now)
        self._replies(ctx, rec, inbox.value, events)
        for key, state in rec["ideas"].items():
            if state.get("redraft") and state.get("status") == "proposed" and key in IDEAS:
                m = measured(ranking, IDEAS[key]) or state.get("measured")
                if m:
                    self._card(ctx, IDEAS[key], m, state, guard, events)
        awaiting = [k for k, s in rec["ideas"].items() if s.get("status") == "proposed"]
        if not awaiting:
            chosen = pick(ranking, rec["ideas"])
            if chosen is not None:
                idea, m = chosen
                state = rec["ideas"].setdefault(idea.service.key, dict(_BLANK, cards=[]))
                if self._card(ctx, idea, m, state, guard, events):
                    state["status"] = "proposed"
        save_record(record_path(ctx.state_dir, self.worker_id), rec)
        return self._ok(ctx, rec, ranking, events)

    def _ok(self, ctx: WorkContext, rec: dict, ranking, events: list):
        counts: dict = {}
        for s in rec["ideas"].values():
            counts[s.get("status", "new")] = counts.get(s.get("status", "new"), 0) + 1
        figs = [Figure(counts.get(st, 0), "count", f"Fiverr gig ideas {st}", window="now")
                for st in ("proposed", "listed", "skipped")]
        tally = make_output(
            self, kind="fiverr.idea_tally", valid_at=ctx.now, observed_at=ctx.now,
            payload={"ideas": sorted(IDEAS), "ranking_read": ranking is not None},
            figures=figs, entities=self.entities,
            provenance={"source": "real", "provider": self.provider,
                        "record": self.record_what})
        return Ok((*events, tally))

    def _replies(self, ctx: WorkContext, rec: dict, replies: list, events: list) -> None:
        seen = set(rec["replies_seen"])
        for r in sorted(replies, key=lambda r: str(r.get("reply_id"))):
            rid, key = r["reply_id"], r.get("ref")
            if rid in seen or key not in IDEAS:
                continue
            seen.add(rid)
            rec["replies_seen"].append(rid)
            state = rec["ideas"].setdefault(key, dict(_BLANK, cards=[]))
            text = " ".join(str(r.get("text") or "").split()).lower()
            if text in ("skip", "no", "drop"):
                state.update(status="skipped", redraft=False)
                events.append(self._event(ctx, "fiverr.idea_decided",
                                          {"idea": key, "decision": "skipped"}))
            elif text in ("listed", "i listed it", "done"):
                state.update(status="listed", redraft=False)
                post_card(ctx, {"key": f"idea-note:{key}:{rid}", "kind": "note", "ref": key,
                                "title": f"Gig idea {key}: recorded as listed",
                                "body": "Recorded that you put it on Fiverr. Pionir cannot "
                                        "deliver this gig on its own yet: an order for it "
                                        "stops on a NEEDS YOU card for you to deliver by "
                                        "hand. Tell the Pionir pane to build its delivery "
                                        "path."},
                          f"tell the owner the {key} idea is recorded as listed")
                events.append(self._event(ctx, "fiverr.idea_decided",
                                          {"idea": key, "decision": "listed"}))
            elif text in ("redraft", "redraft please", "redo"):
                state["redraft"] = True
            else:
                try:
                    state["owner_prices"] = parse_owner_prices(text)
                    state["redraft"] = True
                except ValueError as exc:
                    post_card(ctx, {"key": f"idea-note:{key}:{rid}", "kind": "note",
                                    "ref": key, "title": f"Gig idea {key}: reply not read",
                                    "body": f"Your reply wasn't `skip`, `listed`, `redraft` "
                                            f"or a price: {exc}. Nothing changed."},
                              f"tell the owner his reply to the {key} idea was not read")

    def _card(self, ctx: WorkContext, idea: Idea, m: dict, state: dict, guard,
              events: list) -> bool:
        """Check the listing and post the idea card. True when the card went up."""
        svc = idea.service
        version = int(state.get("version") or 0) + 1
        site_keys = sorted({k for r in svc.prices.values() for k in r.cites})
        reasons = verify_site_prices(self.source_root, site_keys) + check_gig(svc, guard)
        priced = price_packages(svc.prices, state.get("owner_prices"))
        folder = Path(ctx.fiverr_dir) / "gigs" / svc.key
        files: list = []
        if not reasons:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "gig.md").write_text(render_markdown(svc, priced), encoding="utf-8")
            files.append(folder / "gig.md")
            png = render_image(svc.image_headline, svc.image_points)
            if png is not None:
                (folder / "gig.png").write_bytes(png)
                files.append(folder / "gig.png")
            for f in files:
                reasons += checks.check_file(f, guard)
        if reasons:
            log.warning("%s: the %s idea is BLOCKED: %s", self.worker_id, svc.key,
                        "; ".join(reasons[:4]))
            events.append(self._event(ctx, "fiverr.idea_blocked", {
                "idea": svc.key, "reasons": [_clip(r, 120) for r in reasons[:3]]}))
            state["blocked"] = [_clip(r, 200) for r in reasons[:10]]
            return False
        key = f"idea:{svc.key}:v{version}"
        posted, why = post_card(ctx, {
            "key": key, "kind": "idea", "ref": svc.key,
            "title": f"New Fiverr gig idea: {svc.key}",
            "body": render_idea_card(idea, m, priced, version,
                                     unchecked_sources(self.source_root, site_keys)),
            "files": [f.relative_to(Path(ctx.fiverr_dir)).as_posix() for f in files],
            "replies": True}, f"hand the owner the {svc.key} gig idea")
        state["cards"].append({"key": key, "status": "posted" if posted else "not_posted",
                               "why": why, "at": ctx.now})
        if not posted:
            log.warning("%s: the %s idea card was not posted: %s", self.worker_id, svc.key, why)
            return False
        state.update(version=version, redraft=False, proposed_at=ctx.now, blocked=None,
                     measured=m)
        events.append(self._event(ctx, "fiverr.idea_proposed", {
            "idea": svc.key, "version": version, "ranking_score": m["score"]}))
        return True

    def _event(self, ctx: WorkContext, kind: str, payload: dict, figures=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})


__all__ = ["IDEAS", "GigIdeaProposer", "measured", "pick"]
