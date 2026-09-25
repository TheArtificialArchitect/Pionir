"""The product shelf: each product the owner stages goes on sale on Gumroad, on his yes.

The owner (or a Claude pane) stages a product in its own folder:
``<products_dir>/<slug>/`` (``CrewSettings.products_dir``, by default ``~/.pionir/products`` -
the folder Pionir reads too) holding a ``listing.json``, the product zip and a cover image.
This worker notices a ready product and asks Pionir to publish it:
``Job("product.gumroad_publish", {**listing, zip_sha256, cover_sha256})``. Pionir checks the
listing, the zip and the cover BEFORE anything is parked (a secrets scan, a README,
executables without ``allow_executables``, the shas, a cover too small) and refuses with the
reason (typed ``AdapterProtocolError``); a product that passes is parked for the owner, and
only on his yes does it go on sale.

**No model, no words.** Every word of a listing is the owner's own ``listing.json``, checked
here first (``check_listing``, fail closed): a malformed listing is reported with its reasons
and never submitted.

One run:

1. **Follow up** every product waiting on the owner: approved and done with
   ``published: true`` and a ``url`` is PUBLISHED; denied is DENIED; refused on approval is
   FAILED; an approved one that failed for a passing reason is UNDELIVERED and offered to him
   again, up to ``RETRY_UNDELIVERED`` times. Nothing is ever assumed to be on sale.
2. **The shelf**, folder by folder in name order: a folder whose listing, zip or cover
   changed in the last ``SETTLE_SECONDS`` is still being copied and waits. A ready product is
   known by ``(slug, version, zip_sha256, cover_sha256, listing hash)``; content already
   submitted is never submitted again - published, denied, refused by Pionir's checks or
   failed - except a submission that never reached Pionir (``unreachable``, up to
   ``RETRY_UNREACHABLE`` runs) or an approved one that hit an outage (``undelivered``). A
   changed listing, zip or cover is new content: a new submission (an update of the
   product). At most ``MAX_PUBLISHES_PER_RUN`` a run, and one waiting on the owner per slug.
3. **The sales**: ``Job("product.gumroad_list", {})``, every run. Each live product's sales
   and revenue exactly as Gumroad reports them, and the totals. Unreadable is an honest
   UNAVAILABLE, never zeros. A product's sales count rising since the last look is news.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .blog import FORGOTTEN_AFTER, _clip, _Unreadable, read_record, record_path, save_record
from .delivery import _PASSING, PASSING_TYPES, SETTLE_SECONDS, _inner_why, _refused, sha256_of
from .figures import Figure
from .hands import Job, outcome_of
from .log import log
from .orders import _NOT_SET_UP, RETRY_UNDELIVERED, RETRY_UNREACHABLE
from .result import Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base

PUBLISH = "product.gumroad_publish"
LIST = "product.gumroad_list"
LISTING_FILE = "listing.json"
MAX_PUBLISHES_PER_RUN = 1
RETRYABLE = {"unreachable": RETRY_UNREACHABLE, "undelivered": RETRY_UNDELIVERED}
MAX_LISTING_BYTES = 64 * 1024
LISTING_KEYS = frozenset({"slug", "name", "version", "price_cents", "pay_what_you_want",
                          "summary", "description_md", "tags", "zip_name", "cover_name",
                          "allow_executables"})
PAYLOAD_KEYS = LISTING_KEYS | {"zip_sha256", "cover_sha256"}
NEW, UPDATE = "new", "update"

_SLUG = re.compile(r"[a-z0-9-]{3,40}")
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_TAG = re.compile(r"[a-z0-9-]{2,24}")
_BARE = re.compile(r"[^\\/:*?\"<>|\x00-\x1f\x7f]{1,200}")
_LINE_BAD = re.compile(r"[\x00-\x1f\x7f]")                     # a single line: no controls
_TEXT_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")     # text: newlines and tabs only


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---- the check: fail closed, on the listing exactly as it would be submitted ----------------
def check_listing(listing, folder: Path) -> list:
    """Every reason this listing may not be submitted from ``folder``; empty is the only pass."""
    if not isinstance(listing, dict):
        return ["listing.json is not a JSON object"]
    reasons: list = []
    keys = set(listing)
    if keys != LISTING_KEYS:
        missing, extra = sorted(LISTING_KEYS - keys), sorted(keys - LISTING_KEYS)
        reasons.append("listing.json must hold exactly the listing keys"
                       + (f"; missing: {', '.join(missing)}" if missing else "")
                       + (f"; unknown: {', '.join(_clip(k, 30) for k in extra[:5])}"
                          if extra else ""))
    g = listing.get
    slug = g("slug")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        reasons.append("slug must be 3 to 40 of a-z, 0-9 and -")
    elif slug != folder.name:
        reasons.append(f"slug {slug!r} is not the folder's name {_clip(folder.name, 50)!r}")
    name = g("name")
    if not isinstance(name, str) or not 5 <= len(name) <= 80:
        reasons.append("name must be 5 to 80 characters")
    elif _LINE_BAD.search(name) or name != name.strip():
        reasons.append("name must be a single line with no surrounding spaces")
    if not isinstance(g("version"), str) or not _VERSION.fullmatch(g("version")):
        reasons.append("version must be like 1.0.0")
    price = g("price_cents")
    if not _int(price) or not 100 <= price <= 100000:
        reasons.append("price_cents must be whole cents from 100 to 100000")
    for key in ("pay_what_you_want", "allow_executables"):
        if not isinstance(g(key), bool):
            reasons.append(f"{key} must be true or false")
    summary = g("summary")
    if not isinstance(summary, str) or not 20 <= len(summary) <= 200:
        reasons.append("summary must be 20 to 200 characters")
    elif _LINE_BAD.search(summary):
        reasons.append("summary must be a single line")
    desc = g("description_md")
    if not isinstance(desc, str) or not 100 <= len(desc) <= 8000:
        reasons.append("description_md must be 100 to 8000 characters")
    elif _TEXT_BAD.search(desc):
        reasons.append("description_md has control characters")
    tags = g("tags")
    if not isinstance(tags, list) or len(tags) > 5:
        reasons.append("tags must be a list of at most 5")
    elif not all(isinstance(t, str) and _TAG.fullmatch(t) for t in tags):
        reasons.append("each tag must be 2 to 24 of a-z, 0-9 and -")
    for key in ("zip_name", "cover_name"):
        fname = g(key)
        if (not isinstance(fname, str) or not _BARE.fullmatch(fname) or fname.startswith(".")
                or fname != fname.strip() or fname.lower() == LISTING_FILE):
            reasons.append(f"{key} must be a bare file name in the product's folder")
            continue
        path = folder / fname
        try:
            there = path.is_file() and not path.is_symlink()
        except OSError:
            there = False
        if not there:
            reasons.append(f"{key} {_clip(fname, 60)!r} is not a file in the product's folder")
    if isinstance(g("zip_name"), str) and g("zip_name") == g("cover_name"):
        reasons.append("zip_name and cover_name are the same file")
    return reasons


def listing_hash(listing: dict) -> str:
    """The listing's content, however it is laid out on disk."""
    canon = json.dumps(listing, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def content_key(slug: str, version: str, zip_sha: str, cover_sha: str, listing_sha: str) -> str:
    """What a submission IS: the same key is never submitted twice."""
    seed = f"{slug}\x1f{version}\x1f{zip_sha}\x1f{cover_sha}\x1f{listing_sha}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def build_publish(listing: dict, zip_sha: str, cover_sha: str) -> dict:
    """The exact ``product.gumroad_publish`` payload. Pure."""
    return {**listing, "zip_sha256": zip_sha, "cover_sha256": cover_sha}


def _product_dirs(root: Path) -> list:
    try:
        entries = list(root.iterdir()) if root.is_dir() else []
    except OSError as exc:
        log.warning("product shelf: cannot list %s: %s", root, exc)
        return []
    out = []
    for p in entries:
        try:
            if p.name.startswith(".") or p.is_symlink() or not p.is_dir():
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.name)


class ProductShelf(_Base):
    """``products.shelf``: each staged product submitted for Gumroad, for the owner's yes;
    and what the live ones sold, as Gumroad reports it."""

    record_what = "the product shelf's own record of every product it submitted"

    def record_path(self, state_dir):
        return record_path(state_dir, self.worker_id)

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        blank = {"submissions": [], "malformed": {}, "sales_seen": {}}
        return blank if doc is None else {**blank, **doc}

    def save(self, state_dir, rec: dict) -> None:
        save_record(self.record_path(state_dir), rec)

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the shelf cannot keep "
                             "its record, and without it could submit a product twice",
                             retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: products are published and "
                             "their sales read only through Pionir", retryable=False)
        if ctx.products_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no products folder is set "
                             "(products_dir): the shelf cannot see the staged products",
                             retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); "
                             "refusing to submit anything, since it could submit twice",
                             retryable=False)
        events: list = []
        names: set = set()
        self._follow_up(ctx, rec, events)
        shelf = self._shelf(ctx, rec, events, names)
        sales = self._sales(ctx, rec, events, names)
        self.save(ctx.state_dir, rec)
        return Ok((*events, self._tally(ctx, rec, shelf, names), sales))

    # ---- the record ------------------------------------------------------------------------
    @staticmethod
    def _tries(rec: dict, key: str) -> list:
        return [e for e in rec["submissions"] if e.get("key") == key]

    def _closed(self, rec: dict, key: str) -> bool:
        """True when this content must never be submitted (again)."""
        tries = self._tries(rec, key)
        if any(e.get("status") not in RETRYABLE for e in tries):
            return True
        return any(sum(1 for e in tries if e.get("status") == status) >= cap
                   for status, cap in RETRYABLE.items())

    def _state(self, rec: dict, key: str) -> str:
        """What became of this content: ready (never submitted), retrying, or the latest
        attempt's status (``gave_up`` once the retries ran out)."""
        tries = self._tries(rec, key)
        if not tries:
            return "ready"
        latest = max(tries, key=lambda e: float(e.get("submitted_at") or
                                                e.get("settled_at") or 0))
        status = latest.get("status") or "unknown"
        if status in RETRYABLE:
            return "gave_up" if self._closed(rec, key) else "retrying"
        return status

    # ---- 1. what became of the products already waiting -------------------------------------
    def _follow_up(self, ctx: WorkContext, rec: dict, events: list) -> None:
        for e in rec["submissions"]:
            if e.get("status") != "pending_approval" or not e.get("approval_id"):
                continue
            if ctx.approval is None:
                return
            got = ctx.approval(e["approval_id"]) or {}
            state = got.get("status")
            e["checked_at"] = ctx.now
            if state in ("pending", "running", "unreachable"):
                continue
            if state == "unknown":
                if ctx.now - float(e.get("submitted_at") or ctx.now) > FORGOTTEN_AFTER:
                    self._settle(ctx, e, "unknown", "Pionir no longer lists this approval; it "
                                 "was never seen published", events)
                continue
            if state == "denied":
                self._settle(ctx, e, "denied", f"the owner did not approve it "
                             f"({got.get('reason') or 'denied'}); left to him", events)
            elif state in ("approved", "approved_failed"):
                self._approved(ctx, e, got.get("result"), events)
            else:
                log.warning("%s: approval %s has a status nobody knows (%r); still waiting",
                            self.worker_id, e["approval_id"], state)

    def _approved(self, ctx: WorkContext, e: dict, result, events: list) -> None:
        out = outcome_of(PUBLISH, result)
        if not out.ran:
            out.error = _inner_why(result) or out.error
        done = out.result if isinstance(out.result, dict) else {}
        if out.ran and self._took(e, done):
            self._settle(ctx, e, "published", "approved by the owner and published on Gumroad",
                         events)
        elif not out.ran and _refused(result):
            self._settle(ctx, e, "failed", out.error or "refused when it was approved", events)
        else:
            why = out.error if not out.ran else "Pionir did not say it was published with a URL"
            self._settle(ctx, e, "undelivered", (why or "it failed") + "; it will be offered "
                         "to the owner again", events)

    @staticmethod
    def _took(e: dict, done: dict) -> bool:
        """Published: Pionir's result says ``published: true`` and gives the product's URL."""
        url = done.get("url")
        if done.get("published") is not True or not isinstance(url, str) \
                or not url.startswith("https://"):
            return False
        e["url"] = _clip(url, 300)
        for key in ("product_id", "created"):
            v = done.get(key)
            if isinstance(v, (str, int, bool)):
                e[key] = v
        return True

    # ---- 2. the shelf ----------------------------------------------------------------------
    def _shelf(self, ctx: WorkContext, rec: dict, events: list, names: set) -> dict:
        """Look at every staged product; submit at most ``MAX_PUBLISHES_PER_RUN``. Returns
        what was seen of each folder, for the tally."""
        root = Path(ctx.products_dir)
        seen = {"folder_missing": not root.is_dir(), "products": {}, "malformed": [],
                "no_listing": [], "copying": [], "ready_next_run": 0, "held": [],
                "not_set_up": None}
        submitted = 0
        valid_folders = set()
        for folder in _product_dirs(root):
            fname = folder.name
            listing_path = folder / LISTING_FILE
            try:
                if not listing_path.is_file() or listing_path.is_symlink():
                    seen["no_listing"].append(_clip(fname, 50))
                    continue
                if ctx.now - listing_path.stat().st_mtime < SETTLE_SECONDS:
                    seen["copying"].append(_clip(fname, 50))
                    continue
                raw = listing_path.read_bytes()
            except OSError as exc:
                log.warning("%s: cannot read %s: %s", self.worker_id, listing_path, exc)
                seen["copying"].append(_clip(fname, 50))
                continue
            listing, reasons = self._parse(raw)
            if not reasons:
                reasons = check_listing(listing, folder)
            if reasons:
                self._malformed(ctx, rec, fname, raw, reasons, seen, events)
                continue
            valid_folders.add(fname)
            slug = listing["slug"]
            names.update(_name_words(listing["name"]))
            paths = [folder / listing["zip_name"], folder / listing["cover_name"]]
            try:
                if any(ctx.now - p.stat().st_mtime < SETTLE_SECONDS for p in paths):
                    seen["copying"].append(slug)
                    continue
                zip_sha, cover_sha = (sha256_of(p) for p in paths)
            except OSError as exc:
                log.warning("%s: cannot read %s's files: %s", self.worker_id, slug, exc)
                seen["copying"].append(slug)
                continue
            key = content_key(slug, listing["version"], zip_sha, cover_sha,
                              listing_hash(listing))
            state = self._state(rec, key)
            seen["products"][slug] = {"state": state, "version": listing["version"],
                                      "name": listing["name"]}
            if self._closed(rec, key):
                continue            # this exact content was submitted: never twice
            if any(e.get("slug") == slug and e.get("status") == "pending_approval"
                   for e in rec["submissions"]):
                seen["held"].append(slug)       # one per product waits on the owner
                continue
            if submitted >= MAX_PUBLISHES_PER_RUN or seen["not_set_up"]:
                seen["ready_next_run"] += 1
                continue
            kind = UPDATE if any(e.get("slug") == slug and e.get("status") == "published"
                                 for e in rec["submissions"]) else NEW
            entry = {"slug": slug, "key": key, "kind": kind, "version": listing["version"],
                     "name": _clip(listing["name"], 80), "price_cents": listing["price_cents"],
                     "zip_name": listing["zip_name"], "cover_name": listing["cover_name"],
                     "zip_sha256": zip_sha, "cover_sha256": cover_sha,
                     "listing_sha256": listing_hash(listing)}
            payload = build_publish(listing, zip_sha, cover_sha)
            if set(payload) != PAYLOAD_KEYS:        # cannot happen past check_listing
                continue
            if self._submit(ctx, rec, entry, payload, events, seen):
                submitted += 1
                seen["products"][slug]["state"] = self._state(rec, key)
        for fname in list(rec["malformed"]):
            if fname in valid_folders or not (root / fname).is_dir():
                rec["malformed"].pop(fname, None)       # fixed or gone: forgotten
        return seen

    @staticmethod
    def _parse(raw: bytes):
        if len(raw) > MAX_LISTING_BYTES:
            return None, [f"listing.json is larger than {MAX_LISTING_BYTES} bytes"]
        try:
            return json.loads(raw.decode("utf-8")), []
        except (ValueError, UnicodeDecodeError) as exc:
            return None, [f"listing.json is not readable JSON ({_clip(exc, 100)})"]

    def _malformed(self, ctx: WorkContext, rec: dict, fname: str, raw: bytes, reasons: list,
                   seen: dict, events: list) -> None:
        """A listing that fails the check: never submitted; reported once for each version of
        it, and listed in every tally until it is fixed."""
        reasons = [_clip(r, 200) for r in reasons[:12]]
        seen["malformed"].append({"folder": _clip(fname, 50), "reasons": reasons[:5]})
        mark = hashlib.sha256(raw + "\x1f".join(reasons).encode("utf-8")).hexdigest()
        if rec["malformed"].get(fname) == mark:
            return
        rec["malformed"][fname] = mark
        log.warning("%s: the listing in %s is MALFORMED and was NOT submitted: %s",
                    self.worker_id, fname, "; ".join(reasons))
        events.append(self._event(ctx, "product.listing_malformed", {
            "folder": _clip(fname, 50), "reasons": [_clip(r, 120) for r in reasons[:5]]}))

    def _submit(self, ctx: WorkContext, rec: dict, entry: dict, payload: dict, events: list,
                seen: dict) -> bool:
        """Submit one product. False when nothing was attempted (the capability is not set up
        in Pionir: nothing is held against the product)."""
        out = ctx.job(Job(PUBLISH, dict(payload),
                          what=f"publish the product {entry['slug']} {entry['version']} on "
                               f"Gumroad"))
        why = out.error or f"Pionir said {out.status}"
        if out.status == "failed" and (_NOT_SET_UP.search(why)
                                       or why.strip() in (PUBLISH, "CapabilityNotFound")
                                       or out.error_type == "CapabilityNotFound"):
            seen["not_set_up"] = _clip(why, 160)
            log.error("%s: %s is not set up in Pionir (%s); nothing published",
                      self.worker_id, PUBLISH, why)
            events.append(self._event(ctx, "product.not_set_up", {
                "slug": entry["slug"], "why": _clip(why, 160)}))
            return False
        entry.update(submitted_at=ctx.now, status=out.status, task_id=out.task_id,
                     approval_id=out.approval_id)
        rec["submissions"].append(entry)
        if out.status == "pending_approval":
            log.info("%s: %s %s is PENDING the owner's approval (approval %s)",
                     self.worker_id, entry["slug"], entry["version"], out.approval_id)
            events.append(self._event(ctx, "product.pending", {
                "slug": entry["slug"], "version": entry["version"], "kind": entry["kind"],
                "approval_id": out.approval_id}))
        elif out.status == "done":
            # ran without being parked: the owner did NOT approve it - Pionir's gate must.
            log.error("%s: %s ran WITHOUT the owner's approval for %s; it must be "
                      "approval-gated in Pionir", self.worker_id, PUBLISH, entry["slug"])
            entry["approved_by_owner"] = False
            done = out.result if isinstance(out.result, dict) else {}
            if self._took(entry, done):
                self._settle(ctx, entry, "published", "Pionir published it without parking "
                             "it for the owner", events)
            else:
                self._settle(ctx, entry, "unknown", "Pionir ran it without parking it and did "
                             "not say it was published", events)
        elif out.status == "failed" and out.error_type == "AdapterProtocolError":
            # Pionir's own typed refusal (its checks said no): final for this content
            self._record_refused(ctx, entry, why, events)
        elif out.status == "failed" and (out.error_type in PASSING_TYPES
                                         or (not out.error_type and _PASSING.search(why))):
            self._settle(ctx, entry, "unreachable", why, events)
        elif out.status == "failed":
            self._settle(ctx, entry, "failed", why, events)
        elif out.status == "unreachable":
            self._settle(ctx, entry, "unreachable", why, events)
        else:       # still running: not published, never assumed to be, never sent again
            self._settle(ctx, entry, "unknown", why, events)
        return True

    def _record_refused(self, ctx: WorkContext, entry: dict, why: str, events: list) -> None:
        """Pionir's checks refused this product before parking it (a secret in the zip, no
        README, an executable, a cover too small...). Recorded with the reason and never
        retried: only changed content is a new attempt. Urgent for the owner."""
        entry.update(status="refused", why=_clip(why, 300), settled_at=ctx.now)
        log.error("%s: Pionir's checks REFUSED %s %s: %s", self.worker_id, entry["slug"],
                  entry["version"], why)
        events.append(self._event(ctx, "product.blocked", {
            "slug": entry["slug"], "version": entry["version"], "why": _clip(why, 200)}))

    def _settle(self, ctx: WorkContext, e: dict, status: str, why: str, events: list) -> None:
        e.update(status=status, why=_clip(why, 300), settled_at=ctx.now)
        if status == "published":
            log.info("%s: %s %s is PUBLISHED at %s", self.worker_id, e["slug"], e["version"],
                     e.get("url"))
            events.append(self._event(ctx, "product.published", {
                "slug": e["slug"], "version": e["version"], "kind": e.get("kind"),
                "url": e.get("url"), "approved_by_owner": e.get("approved_by_owner", True)}))
            return
        log.warning("%s: %s %s is %s: %s", self.worker_id, e.get("slug"), e.get("version"),
                    status, why)
        events.append(self._event(ctx, "product.not_published", {
            "slug": e["slug"], "version": e["version"], "status": status,
            "why": _clip(why, 160)}))

    # ---- 3. the sales, as Gumroad reports them ----------------------------------------------
    def _sales(self, ctx: WorkContext, rec: dict, events: list, names: set):
        out = ctx.job(Job(LIST, {}, what="read the Gumroad products and their sales"))
        if out.status != "done":
            why = out.error or f"Pionir said {out.status}"
            if out.status == "failed" and (_NOT_SET_UP.search(why)
                                           or out.error_type == "CapabilityNotFound"):
                why = f"{LIST} is not set up in Pionir ({_clip(why, 120)})"
            return self._unavailable(ctx, why)
        listed = out.result.get("products") if isinstance(out.result, dict) else None
        if not isinstance(listed, list):
            return self._unavailable(ctx, f"Pionir answered {LIST} without a product list")
        rows, malformed = [], 0
        for p in listed[:200]:
            if (isinstance(p, dict) and isinstance(p.get("slug"), str) and p["slug"].strip()
                    and isinstance(p.get("published"), bool)
                    and _int(p.get("sales_count")) and p["sales_count"] >= 0
                    and _int(p.get("sales_usd_cents")) and p["sales_usd_cents"] >= 0):
                rows.append(p)
            else:
                malformed += 1
        slugs = [p["slug"] for p in rows]
        twice = {s for s in slugs if slugs.count(s) > 1}
        if twice:           # ambiguous: never add up a product listed twice
            malformed += sum(1 for p in rows if p["slug"] in twice)
            rows = [p for p in rows if p["slug"] not in twice]
        live = sorted((p for p in rows if p["published"]), key=lambda p: p["slug"])
        figures, products = [], []
        for p in live:
            slug = _clip(p["slug"], 60)
            names.update(_name_words(p.get("name")))
            figures += [Figure(p["sales_count"], "count", "sales", stream=slug,
                               window="all_time"),
                        Figure(p["sales_usd_cents"], "usd_cents", "revenue", stream=slug,
                               window="all_time")]
            row = {"slug": slug, "name": _clip(p.get("name") or "", 80),
                   "sales_count": p["sales_count"], "sales_usd_cents": p["sales_usd_cents"]}
            if _int(p.get("price_cents")):
                row["price_cents"] = p["price_cents"]
            if isinstance(p.get("url"), str) and p["url"].startswith("https://"):
                row["url"] = _clip(p["url"], 200)
            products.append(row)
            self._new_sales(ctx, rec, p, slug, events, names)
        figures += [
            Figure(len(live), "count", "products live on Gumroad", window="now"),
            Figure(sum(p["sales_count"] for p in live), "count", "sales", stream="all",
                   window="all_time"),
            Figure(sum(p["sales_usd_cents"] for p in live), "usd_cents", "revenue",
                   stream="all", window="all_time"),
        ]
        not_live = [_clip(p["slug"], 60) for p in rows if not p["published"]]
        return make_output(self, kind="product.sales", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"sales": "as Gumroad reports them", "live": products[:20],
                                    "listed_not_live": not_live[:20],
                                    "malformed_rows": malformed},
                           figures=figures, entities=self._entities(names),
                           provenance={"source": "real", "provider": self.provider,
                                       "capability": LIST})

    def _new_sales(self, ctx: WorkContext, rec: dict, p: dict, slug: str, events: list,
                   names: set) -> None:
        """A product's sales count risen since the last look is news. The first look is the
        baseline: sales made before it are not new."""
        before = rec["sales_seen"].get(p["slug"])
        rec["sales_seen"][p["slug"]] = {"sales_count": p["sales_count"],
                                        "sales_usd_cents": p["sales_usd_cents"],
                                        "seen_at": ctx.now}
        if not isinstance(before, dict) or not _int(before.get("sales_count")):
            return
        more = p["sales_count"] - before["sales_count"]
        if more <= 0:
            return
        figs = [Figure(more, "count", "new sales", stream=slug, window="since_last_look")]
        if _int(before.get("sales_usd_cents")):
            gained = p["sales_usd_cents"] - before["sales_usd_cents"]
            if gained >= 0:
                figs.append(Figure(gained, "usd_cents", "new revenue", stream=slug,
                                   window="since_last_look"))
        log.info("%s: NEW SALES for %s: %d", self.worker_id, slug, more)
        events.append(self._event(ctx, "product.new_sales", {
            "slug": slug, "name": _clip(p.get("name") or "", 80), "new_sales": more,
            "sales_count": p["sales_count"], "sales_usd_cents": p["sales_usd_cents"]},
            figs, names))

    def _unavailable(self, ctx: WorkContext, why: str):
        log.warning("%s: the Gumroad sales are UNAVAILABLE: %s", self.worker_id, why)
        return make_output(self, kind="product.sales_unavailable", valid_at=ctx.now,
                           observed_at=ctx.now,
                           payload={"sales": "UNAVAILABLE", "why": _clip(why, 200),
                                    "note": "the sales and revenue are UNKNOWN, not zero"},
                           entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "capability": LIST})

    # ---- what the leader reads --------------------------------------------------------------
    def _entities(self, names: set) -> tuple:
        return (*self.entities, *sorted(names)[:40])

    def _event(self, ctx: WorkContext, kind: str, payload: dict, figures=(), names=()):
        # no model wrote any of it; the listing's words are the owner's own
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures,
                           entities=self._entities(set(names)),
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx: WorkContext, rec: dict, seen: dict, names: set):
        subs = rec["submissions"]

        def n(*statuses) -> int:
            return sum(1 for e in subs if e.get("status") in statuses)

        products = seen["products"]

        def now(*states) -> list:
            return sorted(s for s, p in products.items() if p["state"] in states)

        blocked = []
        for slug in now("refused"):
            latest = max((e for e in subs if e.get("slug") == slug
                          and e.get("status") == "refused"),
                         key=lambda e: float(e.get("settled_at") or 0))
            blocked.append({"slug": slug, "version": latest.get("version"),
                            "why": latest.get("why") or ""})
        pending = [{"slug": e["slug"], "version": e.get("version"), "kind": e.get("kind")}
                   for e in subs if e.get("status") == "pending_approval"]
        published = {}
        for e in subs:
            if e.get("status") == "published":
                published[e["slug"]] = {"slug": e["slug"], "version": e.get("version"),
                                        "url": e.get("url")}
        staged = len(products)
        figures = [
            Figure(staged, "count", "products staged", window="now"),
            Figure(len([s for s, p in products.items() if p["state"] != "published"]),
                   "count", "products staged but not yet approved", window="now"),
            Figure(len(pending), "count", "products pending the owner's approval",
                   window="now"),
            Figure(len(now("published")), "count",
                   "products staged whose current version is published", window="now"),
            Figure(len(blocked), "count", "products blocked by the checks", window="now"),
            Figure(len(now("denied")), "count", "products denied by the owner", window="now"),
            Figure(len(seen["malformed"]), "count",
                   "product listings malformed (not submitted)", window="now"),
            Figure(n("published"), "count", "product versions published", window="all_time"),
            Figure(n("refused"), "count", "product submissions refused by Pionir's checks",
                   window="all_time"),
            Figure(n("denied"), "count", "product submissions denied", window="all_time"),
            Figure(len(now("failed", "unknown", "gave_up")), "count",
                   "products whose submission failed", window="now"),
            Figure(n("undelivered"), "count",
                   "approved products that failed to publish (offered to the owner again)",
                   window="all_time"),
        ]
        return make_output(self, kind="product.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"pending_approval": pending[-5:], "blocked": blocked[:5],
                                    "published": list(published.values())[-10:],
                                    "denied": now("denied")[:10],
                                    "malformed": seen["malformed"][:5],
                                    "failed": now("failed", "unknown", "gave_up")[:10],
                                    "ready_not_submitted": now("ready", "retrying")[:10],
                                    "waiting_behind_a_pending_one": seen["held"][:10],
                                    "still_copying": seen["copying"][:10],
                                    "folders_without_listing": seen["no_listing"][:10],
                                    "products_folder_missing": seen["folder_missing"],
                                    "ready_next_run": seen["ready_next_run"],
                                    "publish_not_set_up": seen["not_set_up"]},
                           figures=figures, entities=self._entities(names),
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})


def _name_words(name) -> set:
    """A product's name and each of its words, so the leader may name the product."""
    if not isinstance(name, str) or not name.strip():
        return set()
    words = {w for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", name) if len(w) > 1}
    return {_clip(name, 80), *sorted(words)[:8]}
