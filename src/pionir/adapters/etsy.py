"""Adapter for Etsy (Open API v3): a listing goes on the shop only after the owner's yes.

Etsy charges $0.20 to list an item, so the money rule applies as well as the publishing
rule. Every capability that can cost money or put anything in front of the public is
PRIVILEGED with ``spends_money=True`` - Pionir parks it on EVERY call, as its own card (never
the daily digest), whatever permission the caller holds:

- ``etsy.create_draft_listing`` - creates the listing as a DRAFT (``POST
  /v3/application/shops/{shop_id}/listings``, ``type=download``, ``should_auto_renew=false``
  so nothing renews - and charges again - without a new yes), then uploads the staged files
  (``uploadListingFile``) and photos (``uploadListingImage``). The Discord card shows the
  title, the price, the listing fee, the full description with its AI disclosure, every
  tag, every file and photo as inspected on disk, and carries the first photo.
- ``etsy.activate_listing`` - ``PATCH .../listings/{listing_id}`` with ``state=active``: the
  draft this adapter made goes live (this is when Etsy charges the listing fee).
- ``etsy.search_active`` - READ_ONLY, Etsy's public search (``GET
  /v3/application/listings/active``) for the crew's demand scout: counts and favourites only.
- ``etsy.receipts`` - READ_ONLY: the shop's receipts as amounts and listing ids, never a
  buyer's name, address, e-mail or message. Etsy's API has no way to message a buyer, so no
  customer message can ever be sent from here.

Before anything is parked and again before anything is sent: the listing text passes the
crew's Etsy rules (``pionir.crew.etsy.rules``: the AI disclosure word for word, no brand,
no proper noun, no claim, exactly 13 tags), every staged file is read from
``<stage_dir>/<slug>/`` with its pinned SHA-256 and checked by type (a real .xlsx with no
macros or links, a PDF with no scripts, PNG photos at least 2000 px wide, nothing holding
one of the owner's secrets), the ledger (``<state_root>/etsy/listings.json``) keeps a slug
from ever being listed twice, and at most ``max_new_listings_per_day`` drafts are created
per UTC day (one shop: a suspension would end both Etsy streams, so volume stays low).

The way back from each guard: the daily cap resets at 00:00 UTC (``max_new_listings_per_day``
in EtsySettings sets it; 0 stops new drafts); a slug in the ledger is refused for ever, until
its entry is deleted from ``listings.json`` (do that only after deleting the listing on Etsy);
a draft left ``incomplete`` by a failed upload stays a private draft on Etsy - delete it in
the shop manager and its ledger entry, and the crew's next keyword takes its place.

Credentials are ``~/.pionir/secrets/etsy.json`` (written by ``tools\\setup-etsy.ps1``): the
app's keystring and shared secret (sent as ``x-api-key: keystring:shared_secret``, Etsy's
rule since 2026-02-09), the shop id, and the OAuth tokens. The access token lives an hour;
it is refreshed on the way to a call (``grant_type=refresh_token``) and the new pair is
written back atomically. No credential ever appears in a log, an error or a result.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pionir.adapters import _shop_http as shop
from pionir.adapters.deliveries import DeliveryProblem
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.crew.etsy import rules
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

CREATE = "etsy.create_draft_listing"
ACTIVATE = "etsy.activate_listing"
SEARCH = "etsy.search_active"
RECEIPTS = "etsy.receipts"

DEFAULT_ETSY_URL = "https://api.etsy.com/v3"
AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
SCOPES = "listings_r listings_w transactions_r shops_r"
SETUP_HINT = r"run tools\setup-etsy.ps1"
AGENT = "pionir-etsy/0.1"
# What Etsy charges per listing (and per renewal - which is why auto-renew is off).
LISTING_FEE = {"amount": "0.20", "currency": "USD"}
NOTHING_LISTED = "nothing was listed"

MAX_FILE_BYTES = 20_000_000       # Etsy: a digital file is at most 20 MB
MAX_FILES = 5                     # Etsy: at most 5 files per digital listing
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10_000_000
MIN_IMAGE_WIDTH = 2000
PRICE_CENTS = (100, 50_000)
QUANTITY = 999                    # a digital item never runs out
REFRESH_MARGIN = 300.0            # refresh an access token with less than this left
CREDENTIAL_FIELDS = ("keystring", "shared_secret", "shop_id", "access_token",
                     "refresh_token", "expires_at")
PUBLIC_FIELDS = ("keystring", "shared_secret")

CREATE_FIELDS = frozenset({"slug", "title", "description", "tags", "price_cents",
                           "taxonomy_id", "files", "images", "ai_disclosure", "spend",
                           "spends_money"})
ACTIVATE_FIELDS = frozenset({"slug", "listing_id", "title", "spend", "spends_money"})
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,59}")
_FILE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\.(?:xlsx|pdf)")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\.png")


# ---- credentials -------------------------------------------------------------------------
def credentials_problem(path: Path, need: tuple = CREDENTIAL_FIELDS) -> str | None:
    """The exact missing credential, or None. Read locally; never shows a value. The crew
    calls this for its workers' readiness, so they idle with Ian's next step named."""
    try:
        doc = shop.read_json_file(Path(path))
    except shop.Failure as failure:
        return f"{failure.output.get('error')} - {SETUP_HINT}"
    if doc is None:
        return f"no Etsy credentials at {path} - {SETUP_HINT} (after opening the Etsy shop " \
               "and creating an app at etsy.com/developers)"
    for key in need:
        value = doc.get(key)
        if value in (None, "") or (key == "shop_id" and not str(value).isdigit()):
            return f"{path} has no {key} - {SETUP_HINT}"
    return None


# ---- the payload rules --------------------------------------------------------------------
def _int(payload: Mapping[str, Any], key: str, low: int, high: int) -> int:
    v = payload.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise ValueError(f"{key}: a whole number {low}-{high}")
    return v


def _money_fields(payload: Mapping[str, Any]) -> None:
    if payload.get("spends_money") is not True:
        raise ValueError("spends_money: must be true (an Etsy listing costs a listing fee)")
    if payload.get("spend") != LISTING_FEE:
        raise ValueError(f"spend: must state Etsy's listing fee exactly: {LISTING_FEE}")


def check_create(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The draft exactly as it will be sent, or ValueError("<field>: <why>")."""
    unknown = sorted(set(payload) - CREATE_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a listing field")
    missing = sorted(CREATE_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    slug = payload["slug"]
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError("slug: 3-60 of a-z, 0-9 and '-' (the stage folder's name)")
    if payload.get("ai_disclosure") != rules.AI_DISCLOSURE:
        raise ValueError(f"ai_disclosure: must be exactly {rules.AI_DISCLOSURE!r}")
    rules.check_listing(payload["title"], payload["description"], payload["tags"],
                        kind="digital")
    _money_fields(payload)
    out = {"slug": slug, "title": payload["title"], "description": payload["description"],
           "tags": list(payload["tags"]),
           "price_cents": _int(payload, "price_cents", *PRICE_CENTS),
           "taxonomy_id": _int(payload, "taxonomy_id", 1, 10_000_000)}
    files = payload["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError(f"files: 1-{MAX_FILES} staged files")
    images = payload["images"]
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES:
        raise ValueError(f"images: 1-{MAX_IMAGES} staged photos")
    seen: set = set()
    for i, f in enumerate(files):
        if not isinstance(f, Mapping) or set(f) != {"name", "sha256"} \
                or not isinstance(f["name"], str) or not _FILE.fullmatch(f["name"]):
            raise ValueError(f"files[{i}]: {{name, sha256}}, name a .xlsx or .pdf")
        seen.add(f["name"])
    if not any(f["name"].endswith(".xlsx") for f in files):
        raise ValueError("files: the workbook (.xlsx) is the product; it must be one of them")
    for i, im in enumerate(images):
        if not isinstance(im, Mapping) or set(im) != {"name", "sha256", "alt_text"} \
                or not isinstance(im["name"], str) or not _IMAGE.fullmatch(im["name"]):
            raise ValueError(f"images[{i}]: {{name, sha256, alt_text}}, name a .png")
        alt = im["alt_text"]
        if not isinstance(alt, str) or not 5 <= len(alt) <= 250:
            raise ValueError(f"images[{i}].alt_text: 5-250 characters")
        problems = rules.text_problems(f"images[{i}].alt_text", alt)
        if problems:
            raise ValueError(problems[0])
        seen.add(im["name"])
    if len(seen) != len(files) + len(images):
        raise ValueError("files/images: each file once")
    out["files"] = [dict(f) for f in files]
    out["images"] = [dict(im) for im in images]
    return out


def check_activate(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - ACTIVATE_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not an activation field")
    missing = sorted(ACTIVATE_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    slug = payload["slug"]
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError("slug: the slug the draft was created with")
    _money_fields(payload)
    if not isinstance(payload["title"], str) or not payload["title"].strip():
        raise ValueError("title: the draft's title, for the card")
    return {"slug": slug, "listing_id": _int(payload, "listing_id", 1, 10**15),
            "title": payload["title"]}


def check_search(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - {"keywords", "limit"})
    if unknown:
        raise ValueError(f"{unknown[0]}: not a search field")
    problems = rules.keyword_problems(payload.get("keywords"))
    if problems:
        raise ValueError(f"keywords: {problems[0]}")
    limit = payload.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit: 1-100")
    return {"keywords": payload["keywords"], "limit": limit}


def check_receipts(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - {"min_created"})
    if unknown:
        raise ValueError(f"{unknown[0]}: not a receipts field")
    mc = payload.get("min_created", 0)
    if isinstance(mc, bool) or not isinstance(mc, int) or mc < 0:
        raise ValueError("min_created: a unix time (seconds), 0 or more")
    return {"min_created": mc}


# ---- settings -------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EtsySettings:
    """Where Etsy is, where the credentials live (their PATH), where listings are staged,
    the ledger, and the daily cap."""

    api_url: str = DEFAULT_ETSY_URL
    credentials_file: Path = Path("~/.pionir/secrets/etsy.json")
    stage_dir: Path = Path("~/.pionir/etsy/digital")
    ledger_file: Path = Path("~/.pionir/etsy/listings.json")
    secrets_dir: Path | None = Path("~/.pionir/secrets")
    max_new_listings_per_day: int = 3
    who_made: str = "i_did"
    when_made: str = "made_to_order"
    timeout_seconds: int = 30
    upload_timeout_seconds: int = 180

    def __post_init__(self) -> None:
        shop.check_url(self.api_url, "Etsy API")
        if self.timeout_seconds < 5 or self.upload_timeout_seconds < self.timeout_seconds:
            raise ValueError("Etsy timeouts: at least 5 s, uploads no shorter than calls")
        if not 0 <= self.max_new_listings_per_day <= 20:
            raise ValueError("max_new_listings_per_day: 0-20")
        if self.who_made not in ("i_did", "someone_else", "collective"):
            raise ValueError("who_made: i_did, someone_else or collective")
        for name in ("credentials_file", "stage_dir", "ledger_file"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser())
        if self.secrets_dir is not None:
            object.__setattr__(self, "secrets_dir", Path(self.secrets_dir).expanduser())


def etsy_settings(configured: Any) -> EtsySettings:
    """The EtsySettings for a PionirSettings (bootstrap)."""
    return EtsySettings(api_url=configured.etsy_url or DEFAULT_ETSY_URL,
                        credentials_file=configured.etsy_credentials_path,
                        stage_dir=configured.etsy_path / "digital",
                        ledger_file=configured.etsy_path / "listings.json",
                        secrets_dir=configured.secrets_path)


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


class EtsyAdapter:
    """Etsy listings and reads as gated, audited Pionir capabilities."""

    def __init__(self, settings: EtsySettings | None = None, *, opener: shop.Opener | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.settings = settings or EtsySettings()
        self._api = self.settings.api_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._clock = clock or time.time
        self._manifest = AgentManifest(
            agent_id="etsy", version="pionir/etsy",
            capabilities=(
                Capability(
                    name=CREATE,
                    description="Create an Etsy digital-download listing as a draft and upload "
                                "its files and photos (only after the owner approves it; "
                                "Etsy's listing fee)",
                    risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({CREATE}),
                    spends_money=True, routable=False),
                Capability(
                    name=ACTIVATE,
                    description="Make a draft Etsy listing this shop created live (only after "
                                "the owner approves it; Etsy charges the listing fee)",
                    risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({ACTIVATE}),
                    spends_money=True, routable=False),
                Capability(
                    name=SEARCH,
                    description="Count Etsy's active listings for a keyword and read their "
                                "favourites (public search; demand scouting)",
                    risk=RiskLevel.READ_ONLY, routable=False),
                Capability(
                    name=RECEIPTS,
                    description="Read the Etsy shop's receipts as amounts and listing ids "
                                "(no buyer details)",
                    risk=RiskLevel.READ_ONLY, routable=False),
            ))

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- configuration (local files only) --------------------------------------------
    def status(self) -> Mapping[str, Any]:
        """Local only - no call to Etsy, so doctor stays hermetic and fast."""
        problem = credentials_problem(self.settings.credentials_file)
        if problem:
            raise AdapterUnavailable(f"not configured: {problem}")
        try:
            listed = len((self._ledger()).get("listings", {}))
        except shop.Failure as failure:
            listed = failure.output.get("error")
        return {"api": self._api, "credentials": "configured", "listings": listed,
                "stage_dir": str(self.settings.stage_dir)}

    # ---- the request -----------------------------------------------------------------
    def _check(self, task: Task) -> dict[str, Any]:
        checker = {CREATE: check_create, ACTIVATE: check_activate, SEARCH: check_search,
                   RECEIPTS: check_receipts}.get(task.capability)
        if checker is None:
            raise AdapterProtocolError(f"etsy has no capability {task.capability!r}")
        try:
            return checker(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") \
                from error

    def validate(self, task: Task) -> None:
        """Refuse a bad listing, missing or changed files, a twin, a full day, or a call that
        cannot run, before it is parked: asking the owner to approve any of those wastes his
        yes."""
        item = self._check(task)
        need = PUBLIC_FIELDS if task.capability == SEARCH else CREDENTIAL_FIELDS
        problem = credentials_problem(self.settings.credentials_file, need)
        if problem:
            raise AdapterUnavailable(f"not configured: {problem}")
        try:
            if task.capability == CREATE:
                self._stage(item)
                self._may_create(item["slug"])
            elif task.capability == ACTIVATE:
                self._may_activate(item)
        except DeliveryProblem as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") \
                from error
        except shop.Failure as failure:
            why = str(failure.output.get("error"))
            if "refused" in failure.output:
                raise AdapterProtocolError(f"{task.capability} refused by Pionir - {why}") \
                    from None
            raise AdapterUnavailable(why) from None

    def _stage(self, listing: Mapping[str, Any]) -> tuple[list, list]:
        root = self.settings.stage_dir
        folder = root / listing["slug"]
        secrets = shop.secrets_for(self.settings.secrets_dir)
        files = [shop.stage_file(folder, root, f["name"], f["sha256"],
                                 max_bytes=MAX_FILE_BYTES, secrets=secrets)
                 for f in listing["files"]]
        images = []
        for im in listing["images"]:
            staged = shop.stage_file(folder, root, im["name"], im["sha256"],
                                     max_bytes=MAX_IMAGE_BYTES, secrets=secrets)
            if staged.width < MIN_IMAGE_WIDTH:
                raise DeliveryProblem(f"{im['name']}: {staged.width} px wide; Etsy wants at "
                                      f"least {MIN_IMAGE_WIDTH}")
            images.append((staged, im["alt_text"]))
        return files, images

    def listing_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """What the Discord card shows for a parked draft: the files and photos as they are
        on disk now, checked as execute() will check them, and the first photo's bytes (the
        card attaches it). Never a secret value."""
        try:
            files, images = self._stage(check_create(payload))
        except (ValueError, DeliveryProblem, shop.Failure) as error:
            return {"ok": False, "problem": str(error)}
        return {"ok": True,
                "files": [{"name": f.name, "size": f.size, "sha256": f.sha256} for f in files],
                "images": [{"name": s.name, "size": s.size, "width": s.width,
                            "height": s.height, "alt_text": alt} for s, alt in images],
                "cover_name": images[0][0].name, "cover_bytes": images[0][0].data}

    # ---- the ledger --------------------------------------------------------------------
    def _ledger(self) -> dict[str, Any]:
        doc = shop.read_json_file(self.settings.ledger_file) or {}
        doc.setdefault("listings", {})
        if not isinstance(doc["listings"], dict):
            raise shop.unavailable(f"{self.settings.ledger_file}: listings is not an object")
        return doc

    def _may_create(self, slug: str) -> None:
        ledger = self._ledger()
        done = ledger["listings"].get(slug)
        if done is not None:
            raise shop.refused(f"slug {slug!r} was already listed on Etsy as listing "
                               f"{done.get('listing_id')} ({done.get('state')}); never twice")
        today = _utc_day(self._clock())
        made = sum(1 for e in ledger["listings"].values()
                   if isinstance(e, dict) and _utc_day(float(e.get("created_at") or 0)) == today)
        cap = self.settings.max_new_listings_per_day
        if made >= cap:
            raise shop.refused(f"the daily cap of {cap} new Etsy listings is reached "
                               f"({made} today, UTC); try tomorrow")

    def _may_activate(self, item: Mapping[str, Any]) -> dict[str, Any]:
        entry = self._ledger()["listings"].get(item["slug"])
        if not isinstance(entry, dict) or entry.get("listing_id") != item["listing_id"]:
            raise shop.refused(f"listing {item['listing_id']} is not a draft this shop created "
                               f"for {item['slug']!r}; only Pionir's own drafts are activated")
        if entry.get("state") == "active":
            raise shop.refused(f"listing {item['listing_id']} is already active")
        if entry.get("state") != "draft":
            raise shop.refused(f"listing {item['listing_id']} is {entry.get('state')!r}, not a "
                               "complete draft (its files or photos did not all upload)")
        return entry

    def _record(self, slug: str, entry: Mapping[str, Any]) -> str | None:
        """Write one ledger entry at once. Returns a warning when the write failed (the
        listing exists either way: the process remembers it so it is not made twice)."""
        try:
            ledger = self._ledger()
        except shop.Failure:
            ledger = {"listings": {}}
        ledger["listings"][slug] = {**ledger["listings"].get(slug, {}), **entry}
        try:
            shop.write_json_file(self.settings.ledger_file, ledger)
        except OSError as error:
            _log.warning("etsy: the ledger write for %s failed (%s)", slug,
                         type(error).__name__)
            return (f"the ledger {self.settings.ledger_file} could not be written "
                    f"({type(error).__name__}); do not submit {slug} again")
        return None

    # ---- credentials and tokens ----------------------------------------------------------
    def _credentials(self) -> dict[str, Any]:
        doc = shop.read_json_file(self.settings.credentials_file)
        if doc is None:
            raise shop.unavailable(f"not configured: no Etsy credentials at "
                                   f"{self.settings.credentials_file} - {SETUP_HINT}",
                                   not_configured=True)
        return doc

    def _api_key(self, creds: Mapping[str, Any]) -> str:
        return f"{creds.get('keystring')}:{creds.get('shared_secret')}"

    def _secrets_of(self, creds: Mapping[str, Any]) -> list[str]:
        return [str(creds.get(k) or "") for k in ("shared_secret", "access_token",
                                                  "refresh_token")] + [self._api_key(creds)]

    def _fresh(self, creds: dict[str, Any]) -> dict[str, Any]:
        """The credentials with an access token that has at least REFRESH_MARGIN left:
        refreshed (and written back) when it has not."""
        try:
            expires = float(creds.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        if expires - self._clock() > REFRESH_MARGIN:
            return creds
        body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                       "client_id": creds.get("keystring"),
                                       "refresh_token": creds.get("refresh_token")}).encode()
        status, doc = shop.http(self._open, "POST", f"{self._api}/public/oauth/token", body,
                                {"Content-Type": "application/x-www-form-urlencoded"},
                                self.settings.timeout_seconds, AGENT)
        token = doc.get("access_token") if isinstance(doc, Mapping) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise shop.unavailable(f"the Etsy access token expired and could not be refreshed "
                                   f"(HTTP {status}) - {SETUP_HINT}", key_rejected=True)
        fresh = dict(creds)
        fresh["access_token"] = token
        if isinstance(doc.get("refresh_token"), str) and doc["refresh_token"]:
            fresh["refresh_token"] = doc["refresh_token"]
        lifetime = doc.get("expires_in") if isinstance(doc.get("expires_in"), int) else 3600
        fresh["expires_at"] = self._clock() + lifetime
        fresh["refreshed_at"] = self._clock()
        try:
            shop.write_json_file(self.settings.credentials_file, fresh)
        except OSError as error:
            # Etsy's refresh tokens rotate: losing the new one means running setup again.
            _log.warning("etsy: token refreshed but could not be saved (%s)",
                         type(error).__name__)
        else:
            shop.restrict_to_owner(self.settings.credentials_file)
            _log.info("etsy: access token refreshed")
        return fresh

    def _headers(self, creds: Mapping[str, Any], *, oauth: bool) -> dict[str, str]:
        headers = {"x-api-key": self._api_key(creds), "Accept": "application/json"}
        if oauth:
            headers["Authorization"] = f"Bearer {creds.get('access_token')}"
        return headers

    def _call(self, method: str, path: str, creds: Mapping[str, Any], *, oauth: bool = True,
              form: Mapping[str, Any] | None = None, body: bytes | None = None,
              content_type: str | None = None, step: str, upload: bool = False) -> Any:
        headers = self._headers(creds, oauth=oauth)
        data = body
        if form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
            content_type = "application/x-www-form-urlencoded; charset=utf-8"
        if content_type:
            headers["Content-Type"] = content_type
        timeout = self.settings.upload_timeout_seconds if upload else \
            self.settings.timeout_seconds
        status, doc = shop.http(self._open, method, f"{self._api}{path}", data, headers,
                                timeout, AGENT)
        if 200 <= status < 300:
            return doc
        said = ""
        if isinstance(doc, Mapping):
            said = str(doc.get("error") or doc.get("error_description") or "")[:300]
        said = shop.scrub(said, self._secrets_of(creds))
        if status in (401, 403):
            raise shop.unavailable(f"{step}: Etsy refused the credentials (HTTP {status}"
                                   f"{': ' + said if said else ''}) - {SETUP_HINT}",
                                   status=status, key_rejected=True)
        if status in (400, 404, 409, 422):
            raise shop.refused(f"{step}: Etsy said no (HTTP {status}"
                               f"{': ' + said if said else ''})", status=status)
        if status == 429:
            raise shop.unavailable(f"{step}: Etsy is rate limiting this app - try later",
                                   status=429)
        return self._down(step, status, said)

    @staticmethod
    def _down(step: str, status: int, said: str):
        if status == 0:
            raise shop.unavailable(f"{step}: Etsy did not answer", status=0)
        raise shop.unavailable(f"{step}: Etsy answered HTTP {status}"
                               f"{': ' + said if said else ''}", status=status)

    # ---- the call --------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        item = self._check(task)
        creds: dict[str, Any] = {}
        try:
            creds = self._credentials()
            if task.capability == SEARCH:
                output = self._search(item, creds)
            else:
                creds = self._fresh(creds)
                if task.capability == CREATE:
                    with shop.lock_for(self.settings.ledger_file):
                        output = self._create(item, creds)
                elif task.capability == ACTIVATE:
                    with shop.lock_for(self.settings.ledger_file):
                        output = self._activate(item, creds)
                else:
                    output = self._receipts(item, creds)
        except shop.Failure as failure:
            output = failure.output
        text = shop.scrub(json.dumps(output), self._secrets_of(creds) if creds else [])
        return self._result(task, json.loads(text))

    def _create(self, listing: Mapping[str, Any], creds: Mapping[str, Any]) -> dict[str, Any]:
        try:
            files, images = self._stage(listing)   # again, from the bytes on disk NOW
        except DeliveryProblem as error:
            raise shop.refused(f"{error} - {NOTHING_LISTED}") from None
        self._may_create(listing["slug"])
        shop_id = str(creds["shop_id"])
        form = {
            "quantity": QUANTITY,
            "title": listing["title"],
            "description": listing["description"],
            "price": f"{listing['price_cents'] / 100:.2f}",
            "who_made": self.settings.who_made,
            "when_made": self.settings.when_made,
            "taxonomy_id": listing["taxonomy_id"],
            "type": "download",
            "is_supply": "false",
            "should_auto_renew": "false",
            "tags": listing["tags"],
        }
        doc = self._call("POST", f"/application/shops/{shop_id}/listings", creds, form=form,
                         step="create the draft listing")
        listing_id = doc.get("listing_id") if isinstance(doc, Mapping) else None
        if isinstance(listing_id, bool) or not isinstance(listing_id, int):
            raise shop.unavailable("Etsy accepted the draft but gave no listing_id; check the "
                                   "shop's drafts before trying again")
        warning = self._record(listing["slug"], {
            "listing_id": listing_id, "state": "uploading", "title": listing["title"],
            "created_at": self._clock(), "price_cents": listing["price_cents"]})
        base = f"/application/shops/{shop_id}/listings/{listing_id}"
        step = "upload the files"
        try:
            for rank, f in enumerate(files, 1):
                body, ctype = shop.multipart({"name": f.name, "rank": str(rank)},
                                             [("file", f.name, f.content_type, f.data)])
                self._call("POST", f"{base}/files", creds, body=body, content_type=ctype,
                           step=f"upload {f.name}", upload=True)
            step = "upload the photos"
            for rank, (im, alt) in enumerate(images, 1):
                body, ctype = shop.multipart({"rank": str(rank), "alt_text": alt},
                                             [("image", im.name, im.content_type, im.data)])
                self._call("POST", f"{base}/images", creds, body=body, content_type=ctype,
                           step=f"upload {im.name}", upload=True)
        except shop.Failure as failure:
            self._record(listing["slug"], {"state": "incomplete",
                                           "why": str(failure.output.get("error"))[:300]})
            out = dict(failure.output)
            why = (f"{out.get('error')} - the listing {listing_id} is left as a DRAFT on Etsy "
                   f"(not public, not activated); delete it in the shop manager or let it be")
            out.update(error=why, listing_id=listing_id, step=step)
            out["refused" if "refused" in out else "unavailable"] = why
            return out
        warning = self._record(listing["slug"], {"state": "draft", "files": len(files),
                                                 "images": len(images)}) or warning
        _log.info("etsy: created draft listing %s for %s", listing_id, listing["slug"])
        out = {"ok": True, "listing_id": listing_id, "state": "draft",
               "slug": listing["slug"], "files": len(files), "images": len(images),
               "edit_url": f"https://www.etsy.com/your/shops/me/listing-editor/edit/"
                           f"{listing_id}"}
        if warning:
            out["ledger_error"] = warning
        return out

    def _activate(self, item: Mapping[str, Any], creds: Mapping[str, Any]) -> dict[str, Any]:
        self._may_activate(item)
        shop_id = str(creds["shop_id"])
        lid = item["listing_id"]
        current = self._call("GET", f"/application/listings/{lid}", creds,
                             step="read the draft")
        if not isinstance(current, Mapping) or str(current.get("shop_id")) != shop_id:
            raise shop.refused(f"listing {lid} is not in this shop; nothing activated")
        if current.get("state") not in ("draft", "edit"):
            raise shop.refused(f"listing {lid} is {current.get('state')!r} on Etsy, not a "
                               "draft; nothing activated")
        doc = self._call("PATCH", f"/application/shops/{shop_id}/listings/{lid}", creds,
                         form={"state": "active"}, step="activate the listing")
        state = doc.get("state") if isinstance(doc, Mapping) else None
        url = doc.get("url") if isinstance(doc, Mapping) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            url = f"https://www.etsy.com/listing/{lid}"
        warning = self._record(item["slug"], {"state": "active" if state == "active"
                                              else str(state), "activated_at": self._clock(),
                                              "url": url})
        if state != "active":
            return {"ok": False, "unavailable": f"Etsy answered state {state!r}, not active",
                    "error": f"Etsy answered state {state!r}, not active", "listing_id": lid}
        _log.info("etsy: activated listing %s", lid)
        out = {"ok": True, "listing_id": lid, "state": "active", "url": url}
        if warning:
            out["ledger_error"] = warning
        return out

    def _search(self, item: Mapping[str, Any], creds: Mapping[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({"keywords": item["keywords"], "limit": item["limit"],
                                        "sort_on": "score"})
        doc = self._call("GET", f"/application/listings/active?{query}", creds, oauth=False,
                         step="search Etsy")
        if not isinstance(doc, Mapping) or not isinstance(doc.get("count"), int) \
                or not isinstance(doc.get("results"), list):
            raise shop.unavailable("Etsy's search answered without count and results")
        rows = []
        for r in doc["results"][:item["limit"]]:
            if not isinstance(r, Mapping):
                continue
            price = r.get("price") if isinstance(r.get("price"), Mapping) else {}
            amount, divisor = price.get("amount"), price.get("divisor")
            cents = (round(amount * 100 / divisor) if isinstance(amount, int)
                     and isinstance(divisor, int) and divisor > 0 else None)
            tags = [t for t in (r.get("tags") or []) if isinstance(t, str)][:13]
            rows.append({
                "listing_id": r.get("listing_id"),
                "num_favorers": r.get("num_favorers") if isinstance(r.get("num_favorers"), int)
                else None,
                "views": r.get("views") if isinstance(r.get("views"), int) else None,
                "price_cents": cents, "currency": price.get("currency_code"),
                "taxonomy_id": r.get("taxonomy_id") if isinstance(r.get("taxonomy_id"), int)
                else None,
                "tags": [t.lower()[:40] for t in tags],
                "created": r.get("original_creation_timestamp") or r.get("creation_timestamp"),
            })
        return {"ok": True, "keywords": item["keywords"], "count": doc["count"],
                "results": rows}

    def _receipts(self, item: Mapping[str, Any], creds: Mapping[str, Any]) -> dict[str, Any]:
        shop_id = str(creds["shop_id"])
        rows: list = []
        offset = 0
        while offset < 1000:
            query = urllib.parse.urlencode({"limit": 100, "offset": offset,
                                            "min_created": item["min_created"]})
            doc = self._call("GET", f"/application/shops/{shop_id}/receipts?{query}", creds,
                             step="read the receipts")
            results = doc.get("results") if isinstance(doc, Mapping) else None
            if not isinstance(results, list):
                raise shop.unavailable("Etsy's receipts answered without results")
            for r in results:
                if isinstance(r, Mapping):
                    rows.append(_receipt_row(r))
            if len(results) < 100:
                break
            offset += 100
        return {"ok": True, "receipts": rows, "min_created": item["min_created"]}

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"etsy:{task.capability.split('.', 1)[1]}"]
        if output.get("ok") is True and output.get("listing_id") is not None:
            evidence.append(f"etsy:listing:{output['listing_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))


def _money_cents(m: Any) -> int | None:
    if not isinstance(m, Mapping):
        return None
    amount, divisor = m.get("amount"), m.get("divisor")
    if isinstance(amount, bool) or not isinstance(amount, int) or not isinstance(divisor, int) \
            or divisor <= 0:
        return None
    return round(amount * 100 / divisor)


def _receipt_row(r: Mapping[str, Any]) -> dict[str, Any]:
    """Amounts and listing ids only: no name, address, e-mail or message ever leaves here."""
    total = r.get("grandtotal") if isinstance(r.get("grandtotal"), Mapping) else {}
    txs = [t for t in (r.get("transactions") or []) if isinstance(t, Mapping)]
    return {"receipt_id": r.get("receipt_id"),
            "created": r.get("create_timestamp") or r.get("created_timestamp"),
            "paid": r.get("is_paid") is True,
            "grandtotal_cents": _money_cents(total),
            "currency": total.get("currency_code"),
            "listing_ids": [t.get("listing_id") for t in txs if isinstance(t.get("listing_id"),
                                                                            int)],
            "items": sum(int(t.get("quantity") or 0) for t in txs
                         if isinstance(t.get("quantity"), int))}


# ---- what the Discord card says --------------------------------------------------------------
ETSY_LINE = ("\U0001f6cd️ **LISTS ON ETSY (PUBLIC SHOP) - Etsy charges a $0.20 listing "
             "fee** - read the title, price, description and its AI disclosure, and look at "
             "the photo before you approve.")
ACTIVATE_LINE = ("\U0001f6cd️ **MAKES AN ETSY LISTING LIVE - Etsy charges the $0.20 "
                 "listing fee now**")


def card_lines(row: Mapping[str, Any], preview: Mapping[str, Any] | None) -> list[str]:
    """The Etsy part of an approval card (discord_gate.render_request): what will be listed,
    at what price, with which files and photos as inspected on disk, and whether the AI
    disclosure is in the description. Empty for any other capability."""
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    cap = row.get("capability")
    if cap == ACTIVATE:
        return [ACTIVATE_LINE,
                f"**Listing:** {payload.get('listing_id')} - {str(payload.get('title'))[:140]}"]
    if cap != CREATE:
        return []
    price = payload.get("price_cents")
    lines = [ETSY_LINE,
             f"**Title:** {str(payload.get('title'))[:140]}",
             f"**Price:** {price / 100:.2f} USD" if isinstance(price, int)
             else "**Price:** (not stated - do not approve)",
             "**AI disclosure in the description:** "
             + ("yes, word for word" if rules.AI_DISCLOSURE in str(payload.get("description"))
                else "**NO - do not approve**")]
    tags = payload.get("tags")
    if isinstance(tags, list):
        lines.append("**Tags (13):** " + ", ".join(f"`{t}`" for t in tags))
    if preview is None or not preview.get("ok"):
        why = (preview or {}).get("problem") or "the files could not be inspected"
        lines.append(f"⚠️ **DO NOT APPROVE: {str(why)[:300]}**")
    else:
        lines.append("**Files the buyer downloads:** " + ", ".join(
            f"`{f['name']}` ({f['size']:,} bytes)" for f in preview["files"]))
        lines.append("**Photos:** " + ", ".join(
            f"`{i['name']}` {i['width']}x{i['height']}" for i in preview["images"])
            + " (the first is attached)")
    return lines


def card_body(row: Mapping[str, Any]) -> list[str]:
    """The whole description, verbatim, below the card's header (split, never cut)."""
    if row.get("capability") != CREATE:
        return []
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return ["**Description, in full, exactly as it will be listed:**", "```",
            str(payload.get("description")).replace("```", "'''"), "```"]
