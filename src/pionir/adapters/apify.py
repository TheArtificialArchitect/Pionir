"""Apify: an Actor the Marketplaces packager drafted goes on the Apify Store, only on the
owner's yes; and what the published ones did, read only.

- ``apify.publish`` - PRIVILEGED, ``requires_approval=True`` (PionirApp parks it on EVERY
  call, whatever the caller holds; the Discord card shows the whole listing: title,
  descriptions, categories, the price per event and the Store README), ``routable=False``.
  On approval, with the Apify REST API v2 (``Authorization: Bearer <token>``):

  1. ``GET /v2/users/me`` - whose account (its ``username``);
  2. ``GET /v2/acts/<username>~<name>`` - is the Actor there already;
  3. new: ``POST /v2/acts`` with the Actor PRIVATE and its first version; existing:
     ``PUT /v2/acts/<id>/versions/<n>`` (``POST /v2/acts/<id>/versions`` when that version is
     new). A version is ``{versionNumber, sourceType: SOURCE_FILES, buildTag: latest,
     sourceFiles: [{name, format: TEXT|BASE64, content}]}`` - every file of the drafted
     package, after its SHA-256 and a secrets scan of every byte (marketplace_listing.py);
  4. ``POST /v2/acts/<id>/builds?version=<n>&tag=latest&waitForFinish=60``, then
     ``GET /v2/actor-builds/<build>?waitForFinish=60`` until it ends or ``build_wait`` runs
     out. A build that does not SUCCEED stops here: the Actor stays private, nothing is
     priced, and the answer says so;
  5. ``PUT /v2/acts/<id>`` - ``isPublic: true``, the title, descriptions, SEO fields and
     categories, and the pay-per-event price appended to ``pricingInfos`` (append-only, per
     Apify) only when it differs from the current one: ``{pricingModel: PAY_PER_EVENT,
     pricingPerEvent: {actorChargeEvents: {<event>: {eventTitle, eventDescription,
     eventPriceUsd}}}, apifyMarginPercentage, createdAt, startedAt}``. ``startedAt`` is now
     for an Actor that was not public yet, and ``PRICE_NOTICE_DAYS`` ahead for one that was
     (its users are owed notice of a price change).

- ``apify.stats`` - READ_ONLY: for the named Actors of this account, ``GET /v2/acts/<u>~<n>``
  (``stats``: users and runs), ``GET /v2/acts/<id>/runs?desc=true&limit=100`` (runs of the
  last 7 days by status) and one ``GET /v2/store?username=<u>`` (the Store's rating and review
  count). Earnings are not in the public API: they are never reported as zero.

The token is read from a file on every call (no restart after setup), sent only in the
Authorization header, and scrubbed from every answer; it never appears in a log or result.
A missing or rejected token, a rate limit or Apify not answering is ``ok: false`` with
``unavailable``; Apify refusing the request is ``ok: false`` with ``refused``.
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pionir.adapters.content import read_token
from pionir.adapters.deliveries import DeliveryProblem, load_secrets
from pionir.adapters.marketplace_listing import (
    ListingProblem,
    check_sha,
    check_slug,
    claim_problems,
    read_package,
)
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

PUBLISH = "apify.publish"
STATS = "apify.stats"
DEFAULT_API = "https://api.apify.com"
SETUP_HINT = r"run tools\setup-apify.ps1"
TOKEN_REJECTED = f"the Apify token was rejected - {SETUP_HINT}"
PACKAGE_NAME = "actor-source.zip"
ICON_NAME = "icon.png"
PUBLISH_KEYS = frozenset({"slug", "actor_name", "title", "description", "seo_title",
                          "seo_description", "categories", "version", "pricing",
                          "package_name", "package_sha256", "files", "readme_md",
                          "icon_name", "icon_sha256"})
PRICING_KEYS = frozenset({"model", "event_name", "event_title", "event_description",
                          "price_usd"})
MIN_EVENT_USD, MAX_EVENT_USD = 0.0005, 0.05
APIFY_MARGIN = 0.2                  # Apify's share of PPE revenue (the owner keeps 80%)
PRICE_NOTICE_DAYS = 14
BANNED_CATEGORIES = frozenset({"LEAD_GENERATION", "SOCIAL_MEDIA"})
MAX_STATS_ACTORS = 20
MAX_RESPONSE_BYTES = 4_000_000
_VERSION = re.compile(r"(?:[0-9]|[1-9][0-9])\.(?:[0-9]|[1-9][0-9])")
_CATEGORY = re.compile(r"[A-Z][A-Z0-9_]{1,30}")
_EVENT = re.compile(r"[a-z][a-z0-9-]{1,39}")
_LINE_BAD = re.compile(r"[\x00-\x1f\x7f<>]")
_TEXT_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
BUILD_DONE = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT")

Opener = Callable[..., Any]


def _line(p: Mapping, key: str, lo: int, hi: int) -> str:
    v = p.get(key)
    if not isinstance(v, str) or not lo <= len(v) <= hi or _LINE_BAD.search(v) \
            or v != v.strip():
        raise ValueError(f"{key}: one line of {lo}-{hi} characters, no < or >")
    return v


def check_publish(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The listing exactly as it will be sent, or ValueError("<field>: <why>")."""
    keys = set(payload)
    if keys != PUBLISH_KEYS:
        extra, missing = sorted(keys - PUBLISH_KEYS), sorted(PUBLISH_KEYS - keys)
        raise ValueError(f"{(extra or missing)[0]}: " + ("not a listing field" if extra
                                                         else "required"))
    p = dict(payload)
    try:
        check_slug(p["slug"])
        check_sha(p["package_sha256"], "package_sha256")
        check_sha(p["icon_sha256"], "icon_sha256")
    except ListingProblem as exc:
        raise ValueError(str(exc)) from None
    if p["actor_name"] != p["slug"]:
        raise ValueError("actor_name: must be the slug")
    _line(p, "title", 3, 60)
    _line(p, "seo_title", 3, 60)
    _line(p, "description", 10, 300)
    _line(p, "seo_description", 10, 160)
    if not isinstance(p["version"], str) or not _VERSION.fullmatch(p["version"]):
        raise ValueError("version: MAJOR.MINOR, like 0.1")
    cats = p["categories"]
    if not isinstance(cats, list) or not 1 <= len(cats) <= 3 or not all(
            isinstance(c, str) and _CATEGORY.fullmatch(c) for c in cats):
        raise ValueError("categories: 1-3 of the Store's category names")
    if BANNED_CATEGORIES & set(cats):
        raise ValueError("categories: no lead generation or social media Actor is published")
    pr = p["pricing"]
    if not isinstance(pr, dict) or set(pr) != PRICING_KEYS or pr["model"] != "PAY_PER_EVENT":
        raise ValueError("pricing: {model: PAY_PER_EVENT, event_name, event_title, "
                         "event_description, price_usd}")
    if not isinstance(pr["event_name"], str) or not _EVENT.fullmatch(pr["event_name"]):
        raise ValueError("pricing.event_name: 2-40 of a-z, 0-9 and -")
    _line(pr, "event_title", 3, 40)
    _line(pr, "event_description", 10, 200)
    price = pr["price_usd"]
    if isinstance(price, bool) or not isinstance(price, (int, float)) \
            or not MIN_EVENT_USD <= float(price) <= MAX_EVENT_USD:
        raise ValueError(f"pricing.price_usd: {MIN_EVENT_USD}-{MAX_EVENT_USD} dollars an event")
    if p["package_name"] != PACKAGE_NAME or p["icon_name"] != ICON_NAME:
        raise ValueError(f"package_name/icon_name: {PACKAGE_NAME} and {ICON_NAME}")
    files = p["files"]
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        raise ValueError("files: the package's file names")
    for need in (".actor/actor.json", "src/main.py", "README.md"):
        if need not in files:
            raise ValueError(f"files: the package has no {need}")
    readme = p["readme_md"]
    if not isinstance(readme, str) or not 200 <= len(readme) <= 50_000 \
            or _TEXT_BAD.search(readme):
        raise ValueError("readme_md: 200-50000 characters of text")
    problems = claim_problems({"title": p["title"], "description": p["description"],
                               "seo_description": p["seo_description"], "readme_md": readme,
                               "pricing": [pr["event_title"], pr["event_description"]]})
    if problems:
        raise ValueError(problems[0])
    return p


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
        raise ValueError("the Apify API URL must be https: (or http: on loopback)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("the Apify API URL cannot carry credentials or query data")


@dataclass(frozen=True, slots=True)
class ApifySettings:
    """Where Apify is, where the token lives (its PATH, never the token), the Marketplaces
    folder the packages are read from, and the secrets every package is scanned for."""

    api_url: str = DEFAULT_API
    token_file: Path = Path("~/.pionir/secrets/apify-token.txt")
    root: Path = Path("~/.pionir/marketplaces")
    secrets_dir: Path | None = None
    secret_files: tuple = ()
    ssh_dir: Path | None = None
    timeout_seconds: int = 60
    build_wait_seconds: int = 900
    scan_secrets: bool = True
    extra_secrets: tuple = field(default=(), repr=False)

    def __post_init__(self) -> None:
        _check_url(self.api_url)
        object.__setattr__(self, "token_file", Path(self.token_file).expanduser())
        object.__setattr__(self, "root", Path(self.root).expanduser())


class _Failure(Exception):
    def __init__(self, output: dict[str, Any]) -> None:
        super().__init__(output.get("error"))
        self.output = output


def _refused(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "refused": why, "error": why, **extra})


def _unavailable(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "unavailable": why, "error": why, **extra})


def _utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def source_files(package: bytes) -> list[dict[str, str]]:
    """Every file of the package as an Apify source file: TEXT when it is UTF-8, else BASE64."""
    out = []
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            data = zf.read(info)
            try:
                out.append({"name": info.filename, "format": "TEXT",
                            "content": data.decode("utf-8")})
            except UnicodeDecodeError:
                out.append({"name": info.filename, "format": "BASE64",
                            "content": base64.b64encode(data).decode("ascii")})
    return out


def ppe_pricing(pricing: Mapping[str, Any], *, now: datetime, notice: bool) -> dict[str, Any]:
    start = now + timedelta(days=PRICE_NOTICE_DAYS) if notice else now
    return {"pricingModel": "PAY_PER_EVENT",
            "pricingPerEvent": {"actorChargeEvents": {pricing["event_name"]: {
                "eventTitle": pricing["event_title"],
                "eventDescription": pricing["event_description"],
                "eventPriceUsd": float(pricing["price_usd"])}}},
            "apifyMarginPercentage": APIFY_MARGIN,
            "createdAt": _utc(now), "startedAt": _utc(start)}


def _same_price(infos: Any, pricing: Mapping[str, Any]) -> bool:
    """The Actor's latest pricing is already exactly this one."""
    if not isinstance(infos, list) or not infos:
        return False
    last = infos[-1] if isinstance(infos[-1], dict) else {}
    if last.get("pricingModel") != "PAY_PER_EVENT":
        return False
    events = ((last.get("pricingPerEvent") or {}).get("actorChargeEvents") or {})
    ev = events.get(pricing["event_name"]) if isinstance(events, dict) else None
    return isinstance(ev, dict) and len(events) == 1 \
        and ev.get("eventPriceUsd") == float(pricing["price_usd"]) \
        and ev.get("eventTitle") == pricing["event_title"]


class ApifyAdapter:
    """``apify.publish`` (always parked for the owner) and ``apify.stats`` (reads only)."""

    def __init__(self, settings: ApifySettings | None = None, *, opener: Opener | None = None,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings or ApifySettings()
        self._api = self.settings.api_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._now = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._manifest = AgentManifest(
            agent_id="apify", version="pionir/apify",
            capabilities=(
                Capability(name=PUBLISH,
                           description="Publish (or update) an Actor the Marketplaces packager "
                                       "drafted on the Apify Store with its pay-per-event price "
                                       "(only after the owner approves it)",
                           risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({PUBLISH}),
                           requires_approval=True, routable=False),
                Capability(name=STATS,
                           description="Read the published Actors' users, runs and Store "
                                       "rating (reads only)",
                           risk=RiskLevel.READ_ONLY, routable=False),
            ))

    def __repr__(self) -> str:
        return f"ApifyAdapter(api={self._api!r}, token=<read at call time>)"

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- configuration -----------------------------------------------------------------
    def _not_configured(self) -> str:
        return f"not configured: no Apify token at {self.settings.token_file} - {SETUP_HINT}"

    def status(self) -> Mapping[str, Any]:
        """Local only: no call to Apify."""
        if read_token(self.settings.token_file) is None:
            raise AdapterUnavailable(self._not_configured())
        return {"api": self._api, "token": "configured", "root": str(self.settings.root)}

    def _secrets(self):
        if not self.settings.scan_secrets:
            return None
        try:
            return load_secrets(self.settings.secrets_dir,
                                (*self.settings.secret_files, self.settings.token_file),
                                self.settings.ssh_dir, self.settings.extra_secrets)
        except DeliveryProblem as exc:
            raise _unavailable(f"the secrets check could not run ({exc}); nothing was "
                               "sent") from None

    # ---- the request ---------------------------------------------------------------------
    def validate(self, task: Task) -> None:
        if task.capability == STATS:
            self._stats_names(task.payload)
            return
        listing = self._check(task)
        if read_token(self.settings.token_file) is None:
            # asking the owner to approve a publish that cannot run wastes his yes
            raise AdapterUnavailable(self._not_configured())
        try:
            read_package(self.settings.root, listing["slug"], listing["package_name"],
                         listing["package_sha256"], self._secrets())
        except ListingProblem as exc:
            raise AdapterProtocolError(f"{PUBLISH} refused by Pionir - {exc}") from None
        except _Failure as failure:
            raise AdapterUnavailable(str(failure.output.get("error"))) from None

    @staticmethod
    def _check(task: Task) -> dict[str, Any]:
        if task.capability != PUBLISH:
            raise AdapterProtocolError(f"apify has no capability {task.capability!r}")
        try:
            return check_publish(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{PUBLISH} refused by Pionir - {error}") from error

    @staticmethod
    def _stats_names(payload: Mapping[str, Any]) -> list[str]:
        names = payload.get("actors")
        if set(payload) != {"actors"} or not isinstance(names, list) \
                or len(names) > MAX_STATS_ACTORS:
            raise AdapterProtocolError(f"{STATS}: the payload is {{actors: [up to "
                                       f"{MAX_STATS_ACTORS} Actor names]}}")
        for n in names:
            try:
                check_slug(n)
            except ListingProblem:
                raise AdapterProtocolError(f"{STATS}: {n!r} is not an Actor name") from None
        return list(names)

    # ---- the call ----------------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        if task.capability == STATS:
            names = self._stats_names(task.payload)
        else:
            listing = self._check(task)
        token = read_token(self.settings.token_file)
        if token is None:
            why = self._not_configured()
            return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                       "not_configured": True})
        try:
            output = self._stats(names, token) if task.capability == STATS \
                else self._publish(listing, token)
        except _Failure as failure:
            output = failure.output
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)))

    def _me(self, token: str) -> str:
        status, doc = self._http("GET", "/v2/users/me", None, token)
        data = doc.get("data") if isinstance(doc, dict) else None
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("username"),
                                                                         str):
            raise self._failure(status, doc, "reading the account")
        return data["username"]

    def _publish(self, listing: dict[str, Any], token: str) -> dict[str, Any]:
        try:
            package = read_package(self.settings.root, listing["slug"],
                                   listing["package_name"], listing["package_sha256"],
                                   self._secrets())
        except ListingProblem as exc:
            raise _refused(str(exc)) from None
        files = source_files(package)
        if sorted(f["name"] for f in files) != sorted(listing["files"]):
            raise _refused("files: the package does not hold exactly the listed files")
        user = self._me(token)
        name = listing["actor_name"]
        version = {"versionNumber": listing["version"], "sourceType": "SOURCE_FILES",
                   "buildTag": "latest", "sourceFiles": files}
        meta = {"title": listing["title"], "description": listing["description"],
                "seoTitle": listing["seo_title"], "seoDescription": listing["seo_description"],
                "categories": list(listing["categories"])}
        status, doc = self._http("GET", f"/v2/acts/{urllib.parse.quote(user)}~"
                                        f"{urllib.parse.quote(name)}", None, token)
        created = False
        if status == 404:
            status, doc = self._http("POST", "/v2/acts", {
                "name": name, **meta, "isPublic": False, "versions": [version]}, token)
            if status not in (200, 201):
                raise self._failure(status, doc, "creating the Actor")
            actor = doc["data"]
            created = True
        elif status == 200 and isinstance(doc, dict) and isinstance(doc.get("data"), dict):
            actor = doc["data"]
            path = f"/v2/acts/{actor['id']}/versions/{listing['version']}"
            status, vdoc = self._http("PUT", path, version, token)
            if status == 404:
                status, vdoc = self._http("POST", f"/v2/acts/{actor['id']}/versions", version,
                                          token)
            if status not in (200, 201):
                raise self._failure(status, vdoc, "uploading the version")
        else:
            raise self._failure(status, doc, "looking the Actor up")
        actor_id = str(actor.get("id") or "")
        was_public = bool(actor.get("isPublic"))
        build_id, build_status = self._build(actor_id, listing["version"], token)
        url = f"https://apify.com/{user}/{name}"
        if build_status != "SUCCEEDED":
            raise _refused(f"the Actor's build ended {build_status}; it was NOT made public "
                           "and nothing was priced (see the build log on Apify)",
                           actor_id=actor_id, build_id=build_id, build_status=build_status,
                           created=created)
        update = {**meta, "isPublic": True}
        priced = "unchanged"
        if not _same_price(actor.get("pricingInfos"), listing["pricing"]):
            infos = [i for i in actor.get("pricingInfos") or [] if isinstance(i, dict)]
            update["pricingInfos"] = infos + [ppe_pricing(listing["pricing"], now=self._now(),
                                                          notice=was_public)]
            priced = "set" if not was_public else \
                f"changes in {PRICE_NOTICE_DAYS} days (notice for current users)"
        status, doc = self._http("PUT", f"/v2/acts/{actor_id}", update, token)
        if status != 200:
            raise self._failure(status, doc, "making the Actor public with its price",
                                actor_id=actor_id, build_id=build_id, built=True)
        return {"ok": True, "published": True, "url": url, "actor_id": actor_id,
                "build_id": build_id, "build_status": build_status, "created": created,
                "pricing": priced, "version": listing["version"]}

    def _build(self, actor_id: str, version: str, token: str) -> tuple[str, str]:
        q = urllib.parse.urlencode({"version": version, "tag": "latest", "waitForFinish": 60})
        status, doc = self._http("POST", f"/v2/acts/{actor_id}/builds?{q}", None, token)
        data = doc.get("data") if isinstance(doc, dict) else None
        if status not in (200, 201) or not isinstance(data, dict):
            raise self._failure(status, doc, "starting the build", actor_id=actor_id)
        build_id, state = str(data.get("id") or ""), str(data.get("status") or "")
        deadline = time.monotonic() + self.settings.build_wait_seconds
        while state not in BUILD_DONE:
            if time.monotonic() >= deadline:
                raise _unavailable(f"the build {build_id} had not finished after "
                                   f"{self.settings.build_wait_seconds} s; the Actor stays "
                                   "private - approve again once it is done",
                                   actor_id=actor_id, build_id=build_id)
            status, doc = self._http("GET", f"/v2/actor-builds/{build_id}?waitForFinish=60",
                                     None, token)
            data = doc.get("data") if isinstance(doc, dict) else None
            if status != 200 or not isinstance(data, dict):
                raise self._failure(status, doc, "following the build", actor_id=actor_id,
                                    build_id=build_id)
            state = str(data.get("status") or "")
            if state not in BUILD_DONE:
                self._sleep(5.0)
        return build_id, state

    def _stats(self, names: list[str], token: str) -> dict[str, Any]:
        user = self._me(token)
        status, doc = self._http("GET", "/v2/store?" + urllib.parse.urlencode(
            {"username": user, "limit": 100}), None, token)
        store: dict = {}
        data = doc.get("data") if isinstance(doc, dict) else None
        if status == 200 and isinstance(data, dict):
            for it in data.get("items") or []:
                if isinstance(it, dict) and isinstance(it.get("name"), str):
                    store[it["name"]] = it
        cutoff = self._now() - timedelta(days=7)
        out = []
        for name in names:
            status, doc = self._http("GET", f"/v2/acts/{urllib.parse.quote(user)}~"
                                            f"{urllib.parse.quote(name)}", None, token)
            act = doc.get("data") if isinstance(doc, dict) else None
            if status == 404:
                out.append({"name": name, "found": False})
                continue
            if status != 200 or not isinstance(act, dict):
                raise self._failure(status, doc, f"reading {name}")
            s = act.get("stats") if isinstance(act.get("stats"), dict) else {}
            status, rdoc = self._http("GET", f"/v2/acts/{act.get('id')}/runs?desc=true&limit=100",
                                      None, token)
            runs = ((rdoc or {}).get("data") or {}).get("items") if isinstance(rdoc, dict) \
                else None
            by: dict = {}
            if status == 200 and isinstance(runs, list):
                for r in runs:
                    started = str((r or {}).get("startedAt") or "")
                    try:
                        when = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if when >= cutoff:
                        st = str(r.get("status") or "UNKNOWN")
                        by[st] = by.get(st, 0) + 1
            item = store.get(name) or {}
            out.append({"name": name, "found": True, "id": act.get("id"),
                        "public": bool(act.get("isPublic")),
                        "users_total": s.get("totalUsers"),
                        "users_30d": s.get("totalUsers30Days"),
                        "runs_total": s.get("totalRuns"),
                        "runs_7d": by if status == 200 and isinstance(runs, list) else None,
                        "rating": item.get("actorReviewRating"),
                        "reviews": item.get("actorReviewCount"),
                        "url": f"https://apify.com/{user}/{name}"})
        return {"ok": True, "actors": out, "earnings": "not in Apify's public API"}

    # ---- transport ---------------------------------------------------------------------------
    def _http(self, method: str, path: str, body: Any, token: str) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "User-Agent": "pionir-apify/0.1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._api + path, data=data, method=method,
                                         headers=headers)
        try:
            with self._open(request, timeout=self.settings.timeout_seconds) as response:
                return int(getattr(response, "status", 200)), self._json(response)
        except urllib.error.HTTPError as error:
            return error.code, self._json(error)
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, None

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError):
            return None
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _said(doc: Any) -> str:
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "")[:300]
        return ""

    def _failure(self, status: int, doc: Any, step: str, **extra: Any) -> _Failure:
        said = self._said(doc)
        if status in (401, 403):
            return _unavailable(TOKEN_REJECTED, status=status, key_rejected=True, **extra)
        if status == 429:
            return _unavailable(f"Apify is rate limiting this account ({step}); try again "
                                "later", status=429, **extra)
        if status == 0:
            return _unavailable(f"Apify did not answer ({step})", status=0, **extra)
        if 400 <= status < 500:
            return _refused(f"Apify refused {step} (HTTP {status})" +
                            (f": {said}" if said else ""), status=status, **extra)
        return _unavailable(f"Apify answered HTTP {status} ({step})" +
                            (f": {said}" if said else ""), status=status, **extra)

    @staticmethod
    def _scrub(text: str, token: str) -> str:
        return text.replace(token, "<redacted>") if token else text

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"apify:{task.capability.split('.', 1)[1]}"]
        if output.get("actor_id"):
            evidence.append(f"apify:actor:{output['actor_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))

    # ---- the account check (python -m pionir apify-check) -------------------------------------
    def check_account(self) -> tuple[int, dict[str, Any]]:
        """Whose token it is. Reads only (GET /v2/users/me); never shows the token."""
        token = read_token(self.settings.token_file)
        if token is None:
            return 1, {"status": "not_configured", "message": self._not_configured()}
        try:
            user = self._me(token)
        except _Failure as failure:
            code = 2 if failure.output.get("key_rejected") else 1
            return code, {"status": "rejected" if code == 2 else "not_checked",
                          "message": self._scrub(str(failure.output.get("error")), token)}
        return 0, {"status": "ok", "username": user, "api": self._api}


def apify_settings(configured: Any) -> ApifySettings:
    """The ApifySettings for a PionirSettings: the token in the secrets folder, the
    Marketplaces folder, and the same secret sources a client delivery is scanned for."""
    from pionir.adapters.clients import client_settings
    from pionir.crew.marketplaces.paths import default_root

    client = client_settings(configured)
    return ApifySettings(token_file=Path(configured.secrets_path) / "apify-token.txt",
                         root=default_root(), secrets_dir=client.secrets_dir,
                         secret_files=(*client.secret_files, client.token_file),
                         ssh_dir=client.ssh_dir, extra_secrets=tuple(client.secret_values))
