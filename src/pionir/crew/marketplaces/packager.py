"""``marketplaces.packager``: approved builds -> listing drafts -> a publish the owner approves.

One run:

1. **Follow up** every publish waiting on the owner (``ctx.approval``): approved and done
   with ``published: true`` (Apify) or ``submitted: true`` (Chrome: in Google's review) is
   settled so; denied is DENIED; refused is FAILED; an approved one that failed for a passing
   reason is UNDELIVERED and offered again, at most ``RETRY_UNDELIVERED`` times.
2. **Draft** every staged build (staging.py) and every Shopify spec not drafted yet:
   apify_pack / chrome_pack / shopify_pack write ``listings/<slug>/`` (listing.json, the
   package, the images) into a hidden folder renamed into place. A draft with any problem
   (dishonest words, a manifest that asks for more than the spec, no ExtPay.js ...) is
   BLOCKED with its reasons and never submitted. A second Chrome draft for a niche already
   drafted is blocked: the Store forbids two extensions with the same functionality.
3. **Submit** at most ONE draft a run, only when its credential exists (a publish that cannot
   run would waste the owner's yes): ``Job("apify.publish", listing)`` or
   ``Job("chrome.publish_update", {...})`` - Pionir PARKS both for the owner on every call.
   A Chrome draft whose item the owner has not created yet gets one card saying exactly what
   to do by hand; a Shopify pack gets one card saying where it is. Nothing is ever published
   from here.
"""
from __future__ import annotations

import json
import os
import shutil
import time

from ..blog import FORGOTTEN_AFTER, _clip, _Unreadable, read_record, record_path, save_record
from ..delivery import _inner_why, _refused
from ..figures import Figure
from ..hands import Job, outcome_of
from ..log import log
from ..orders import _NOT_SET_UP, RETRY_UNDELIVERED
from ..result import Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import apify_pack, chrome_pack, paths, shopify_pack, staging

APIFY_PUBLISH = "apify.publish"
CHROME_PUBLISH = "chrome.publish_update"
CARD = "builds.card"
CARD_KIND = "market"
APIFY_TOKEN = "apify-token.txt"
CHROME_CREDENTIALS = "chrome-webstore.json"
MAX_SUBMITS_PER_RUN = 1
SETTLED = ("published", "submitted", "denied", "failed", "unknown", "gave_up")


# The owner dropped the Chrome Web Store on 2026-10-08 (too much effort for almost no income).
# Its credentials are no longer required, or every Apify-only setup would read NOT CONFIGURED
# for ever. Set True to bring Chrome back (and re-add its scout to catalogue.json).
CHROME_ACTIVE = False


def missing_credentials(secrets_dir) -> list:
    """Which ACTIVE store credential files are not there yet, with the script that writes each."""
    out = []
    if secrets_dir is None:
        return ["no secrets folder is configured"]
    if not (secrets_dir / APIFY_TOKEN).is_file():
        out.append(rf"no Apify token at {secrets_dir / APIFY_TOKEN} (run tools\setup-apify.ps1)")
    if CHROME_ACTIVE and not (secrets_dir / CHROME_CREDENTIALS).is_file():
        out.append(rf"no Chrome Web Store credentials at {secrets_dir / CHROME_CREDENTIALS} "
                   r"(run tools\setup-chrome-webstore.ps1)")
    return out


class PackagerWorker(_Base):
    record_what = "the Marketplaces packager's record of every draft and publish"

    def readiness(self, secrets_dir) -> str | None:
        missing = missing_credentials(secrets_dir)
        if missing:
            return ("NOT CONFIGURED for publishing (drafts are still made): "
                    + "; ".join(missing))
        return None

    def _blank(self) -> dict:
        return {"drafts": {}, "cards": {}, "counts": {}}

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        return self._blank() if doc is None else {**self._blank(), **doc}

    # ---- one run ---------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        root = paths.root_for(ctx.builds_dir)
        if root is None or ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no Builds folder or state dir: no "
                             "Marketplaces folder and no record", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); refusing "
                             "to submit anything, since it could submit twice",
                             retryable=False)
        events: list = []
        self._follow_up(ctx, rec, events)
        self._draft_all(ctx, root, rec, events)
        self._submit(ctx, root, rec, events)
        save_record(record_path(ctx.state_dir, self.worker_id), rec)
        return Ok((*events, self._tally(ctx, rec)))

    # ---- 1. following up ---------------------------------------------------------------------
    def _follow_up(self, ctx: WorkContext, rec: dict, events: list) -> None:
        for slug, d in rec["drafts"].items():
            if d.get("status") != "pending_approval" or not d.get("approval_id"):
                continue
            if ctx.approval is None:
                return
            got = ctx.approval(d["approval_id"]) or {}
            state = got.get("status")
            if state in ("pending", "running", "unreachable", None):
                continue
            if state == "unknown":
                if ctx.now - float(d.get("submitted_at") or ctx.now) > FORGOTTEN_AFTER:
                    self._settle(ctx, slug, d, "unknown", "Pionir no longer lists the "
                                 "approval; it was never seen published", events)
                continue
            if state == "denied":
                self._settle(ctx, slug, d, "denied", f"the owner did not approve it "
                             f"({got.get('reason') or 'denied'})", events)
                continue
            result = got.get("result")
            out = outcome_of(d["capability"], result)
            done = out.result if isinstance(out.result, dict) else {}
            if out.ran and done.get("published") is True and str(done.get("url") or "") \
                    .startswith("https://"):
                d["url"] = _clip(done["url"], 300)
                self._settle(ctx, slug, d, "published", "approved and live on the Apify Store",
                             events)
            elif out.ran and done.get("submitted") is True:
                d["url"] = _clip(done.get("url") or "", 300)
                self._settle(ctx, slug, d, "submitted", "approved, uploaded and submitted for "
                             "Google's review (not live until the review passes)", events)
            elif not out.ran and _refused(result):
                self._settle(ctx, slug, d, "failed", _inner_why(result) or out.error or
                             "refused when it was approved", events)
            else:
                why = (_inner_why(result) or out.error) if not out.ran else \
                    "Pionir did not say it was published"
                tries = int(d.get("undelivered") or 0) + 1
                d["undelivered"] = tries
                status = "ready" if tries < RETRY_UNDELIVERED else "gave_up"
                self._settle(ctx, slug, d, status, f"{why or 'it failed'}; "
                             + ("it will be offered again" if status == "ready"
                                else "given up after retries"), events)

    def _settle(self, ctx, slug: str, d: dict, status: str, why: str, events: list) -> None:
        d.update(status=status, why=_clip(why, 300), settled_at=ctx.now)
        self._count(d.get("market") or "?", status, ctx, events, slug=slug, why=why, url=d.get(
            "url"))

    def _count(self, market, status, ctx, events, **payload) -> None:
        events.append(self._event(ctx, f"market.{status}", {"market": market, **{
            k: _clip(v, 200) if isinstance(v, str) else v for k, v in payload.items()}}))

    # ---- 2. drafting --------------------------------------------------------------------------
    def _draft_all(self, ctx: WorkContext, root, rec: dict, events: list) -> None:
        todo = [(slug, "staged") for slug in staging.staged_slugs(root)]
        specs_dir = root / "specs"
        try:
            names = sorted(p.stem for p in specs_dir.glob("*.json")) if specs_dir.is_dir() \
                else []
        except OSError as exc:
            log.warning("%s: cannot list %s: %s", self.worker_id, specs_dir, exc)
            names = []
        for slug in names:
            if slug not in dict(todo):
                todo.append((slug, "spec"))
        for slug, source in todo:
            old = rec["drafts"].get(slug)
            if old is not None and not self._fixed(root, slug, old):
                continue
            try:
                spec = paths.read_json(paths.spec_path(root, slug), {})
            except paths.Unreadable as exc:
                spec = {}
                log.warning("%s: %s", self.worker_id, exc)
            if source == "spec" and spec.get("product_type") != "shopify_app":
                continue            # a buildable spec waits for its build to be staged
            self._draft(ctx, root, rec, slug, spec, source, events)

    @staticmethod
    def _fixed(root, slug: str, d: dict) -> bool:
        """The way back for a BLOCKED draft: its spec or staged build changed after it was
        blocked (the owner fixed it), so it is drafted again. Anything else is never redone."""
        if d.get("status") != "blocked":
            return False
        for path in (paths.spec_path(root, slug), paths.staged_dir(root, slug) / "entry.json"):
            try:
                if path.is_file() and path.stat().st_mtime > float(d.get("drafted_at") or 0):
                    return True
            except OSError as exc:
                log.warning("marketplaces packager: cannot look at %s: %s", path, exc)
        return False

    def _draft(self, ctx, root, rec, slug: str, spec: dict, source: str, events: list) -> None:
        market = spec.get("market") or "?"
        d = {"market": market, "slug": slug, "drafted_at": ctx.now, "name": spec.get("name")}
        rec["drafts"][slug] = d
        if spec.get("slug") != slug:
            self._blocked(ctx, d, [f"no spec for {slug} in specs/ (a staged build with no "
                                   "marketplace spec cannot be listed)"], events)
            return
        try:
            if market == "shopify":
                listing, files, problems = shopify_pack.draft(spec)
            else:
                _entry, built = staging.read_staged(root, slug)
                if market == "apify":
                    listing, files, problems = apify_pack.draft(spec, built)
                else:
                    listing, files, problems = chrome_pack.draft(spec, built)
                    problems += self._same_purpose(rec, spec)
        except (paths.Unreadable, ValueError, KeyError, TypeError) as exc:
            self._blocked(ctx, d, [f"the draft could not be made: {type(exc).__name__}: "
                                   f"{_clip(exc, 200)}"], events)
            return
        if problems:
            self._blocked(ctx, d, problems, events)
            return
        folder = paths.listing_dir(root, slug)
        try:
            self._write(folder, listing, files)
        except OSError as exc:
            self._blocked(ctx, d, [f"the draft could not be written ({exc})"], events)
            return
        d.update(status="ready" if market != "shopify" else "pack_ready",
                 folder=str(folder), niche=(spec.get("evidence") or {}).get("niche"),
                 listing=listing if market != "shopify" else None)
        self._count(market, "drafted", ctx, events, slug=slug, name=spec.get("name"),
                    folder=str(folder))

    @staticmethod
    def _same_purpose(rec: dict, spec: dict) -> list:
        niche = (spec.get("evidence") or {}).get("niche")
        for other in rec["drafts"].values():
            if other is not None and other.get("market") == "chrome" and \
                    other.get("slug") != spec["slug"] and other.get("niche") == niche and \
                    other.get("status") not in ("blocked",):
                return [f"another extension ({other.get('slug')}) already serves the niche "
                        f"{niche!r}; the Chrome Web Store forbids two with the same "
                        "functionality"]
        return []

    @staticmethod
    def _write(folder, listing: dict, files: dict) -> None:
        if folder.exists():
            raise OSError(f"{folder} already exists; a draft is never overwritten")
        folder.parent.mkdir(parents=True, exist_ok=True)
        tmp = folder.parent / f".draft-{folder.name}-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            for name, data in files.items():
                (tmp / name).write_bytes(data)
            (tmp / "listing.json").write_text(json.dumps(listing, indent=1, ensure_ascii=False)
                                              + "\n", encoding="utf-8")
            for attempt in range(6):
                try:
                    os.rename(tmp, folder)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.2 * (attempt + 1))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _blocked(self, ctx, d: dict, reasons: list, events: list) -> None:
        d.update(status="blocked", reasons=[_clip(r, 240) for r in reasons[:10]])
        log.warning("%s: %s BLOCKED: %s", self.worker_id, d["slug"], "; ".join(reasons[:3]))
        self._count(d["market"], "draft_blocked", ctx, events, slug=d["slug"],
                    reasons=[_clip(r, 120) for r in reasons[:3]])

    # ---- 3. submitting ---------------------------------------------------------------------------
    def _submit(self, ctx: WorkContext, root, rec: dict, events: list) -> None:
        submitted = 0
        for slug, d in sorted(rec["drafts"].items()):
            status = d.get("status")
            if status == "pack_ready":
                self._card(ctx, rec, f"market:shopify:{slug}:pack",
                           f"Shopify submission pack ready: {d.get('name')}",
                           f"A Shopify App Store submission pack for **{slug}** is in "
                           f"`{d.get('folder')}` (submission-pack.md, the app icon, a feature "
                           "card and the spec with the measured demand behind it).\n\nNothing "
                           "was built or submitted: a Shopify app needs your Partner account "
                           "($19, once), code the night builds cannot build yet, and Shopify's "
                           "review (weeks). The pack lists what only you can supply.",
                           then=lambda: d.update(status="pack_sent"))
                continue
            if status != "ready" or submitted >= MAX_SUBMITS_PER_RUN or ctx.job is None:
                continue
            market = d.get("market")
            if market == "apify":
                if not (ctx.secrets_dir / APIFY_TOKEN).is_file():
                    d["waiting"] = r"no Apify token yet: run tools\setup-apify.ps1"
                    continue
                job = Job(APIFY_PUBLISH, dict(d["listing"]),
                          what=f"publish the Actor {d['listing']['title']!r} on the Apify Store")
            else:
                item = self._chrome_item(root, slug, ctx, rec, d)
                if item is None:
                    continue
                lst = d["listing"]
                job = Job(CHROME_PUBLISH, {
                    "slug": slug, "item_id": item, "name": lst["name"],
                    "summary": lst["summary"], "version": lst["version"],
                    "package_name": lst["package_name"],
                    "package_sha256": lst["package_sha256"], "publish_type": "DEFAULT_PUBLISH"},
                    what=f"upload {lst['name']!r} {lst['version']} to the Chrome Web Store "
                         "and submit it for review")
            submitted += 1
            self._send(ctx, slug, d, job, events)

    def _chrome_item(self, root, slug, ctx, rec, d) -> str | None:
        if not (ctx.secrets_dir / CHROME_CREDENTIALS).is_file():
            d["waiting"] = r"no Chrome Web Store credentials yet: run tools\setup-chrome-webstore.ps1"
            return None
        try:
            item = paths.chrome_items(root).get(slug)
        except paths.Unreadable as exc:
            d["waiting"] = _clip(exc, 200)
            return None
        if item:
            return item
        d["waiting"] = "the item does not exist yet: the owner creates it once by hand"
        lst = d.get("listing") or {}
        self._card(ctx, rec, f"market:chrome:{slug}:create",
                   f"Create a Chrome Web Store item by hand: {lst.get('name')}",
                   f"The extension **{slug}** is packaged in `{d.get('folder')}`. The Chrome Web "
                   "Store API cannot create an item, so once, by hand:\n"
                   "1. In the developer dashboard ($5 one-time fee), New item -> upload "
                   "`extension.zip` from that folder.\n"
                   "2. Paste the name, summary, description, category, single purpose, "
                   "permission justifications and data-usage answers from `listing.json`, and "
                   "upload icon128.png, screenshot-1.png and promo-small.png.\n"
                   "3. Do NOT press submit; record the item id with:\n"
                   f"`tools\\setup-chrome-webstore.ps1 -Slug {slug} -ItemId <item id>`\n\n"
                   "The next version is then uploaded and submitted only on your approval.")
        return None

    def _send(self, ctx, slug: str, d: dict, job: Job, events: list) -> None:
        out = ctx.job(job)
        d.update(capability=job.capability, submitted_at=ctx.now, task_id=out.task_id)
        d.pop("waiting", None)
        if out.status == "pending_approval":
            d.update(status="pending_approval", approval_id=out.approval_id)
            self._count(d["market"], "submitted_for_approval", ctx, events, slug=slug,
                        capability=job.capability)
        elif out.status == "unreachable":
            d["why"] = _clip(out.error, 200)        # stays ready: tried again next run
        elif out.status == "failed" and (out.error_type == "AdapterProtocolError"):
            self._blocked(ctx, d, [f"Pionir refused it: {out.error}"], events)
        elif out.status == "failed" and (out.error_type == "CapabilityNotFound"
                                         or _NOT_SET_UP.search(out.error or "")):
            d["waiting"] = _clip(f"{job.capability} is not set up in Pionir ({out.error})", 240)
        elif out.status == "done":
            # never expected: both capabilities park on every call
            log.error("%s: %s ran WITHOUT approval: %s", self.worker_id, job.capability,
                      out.answer)
            d.update(status="failed", why="Pionir ran it without parking it for approval")
        else:
            d["why"] = _clip(out.error, 200)

    def _card(self, ctx, rec, key: str, title: str, body: str, *, then=None) -> None:
        if rec["cards"].get(key) or ctx.job is None:
            return
        out = ctx.job(Job(CARD, {"key": key, "kind": CARD_KIND, "title": title[:120],
                                 "body": body[:11000], "replies": False},
                          what="post a Marketplaces card to the owner"))
        result = out.result if isinstance(out.result, dict) else {}
        if out.status == "done" and result.get("ok") is not False:
            rec["cards"][key] = ctx.now
            if then is not None:
                then()

    # ---- what the leader reads ----------------------------------------------------------------------
    def _event(self, ctx, kind: str, payload: dict, figures=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx, rec: dict):
        drafts = list(rec["drafts"].values())

        def n(*states) -> int:
            return sum(1 for d in drafts if d.get("status") in states)

        figures = [
            Figure(len(drafts), "count", "listing drafts made", window="all_time"),
            Figure(n("blocked"), "count", "drafts blocked by a check", window="all_time"),
            Figure(n("ready"), "count", "drafts waiting to be submitted", window="now"),
            Figure(n("pending_approval"), "count", "publishes waiting for the owner",
                   window="now"),
            Figure(n("published"), "count", "Actors published", window="all_time"),
            Figure(n("submitted"), "count", "extension versions submitted for review",
                   window="all_time"),
        ]
        waiting = [{"slug": d["slug"], "why": d.get("waiting")} for d in drafts
                   if d.get("waiting")][:3]
        return make_output(self, kind="market.packager", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"waiting": waiting,
                                    "missing_credentials": missing_credentials(ctx.secrets_dir),
                                    "published": [{"slug": d["slug"], "url": d.get("url")}
                                                  for d in drafts
                                                  if d.get("status") == "published"][-5:]},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
