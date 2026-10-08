"""Chrome Web Store: a new version of an extension the owner created by hand, uploaded and
submitted for review only on his yes; and its status, read only.

The Chrome Web Store API cannot create an item or edit its listing text: the owner creates
each item ONCE by hand in the developer dashboard (the $5 developer fee is his), pastes the
listing pack the packager drafted, and records its item id in
``<marketplaces>/chrome-items.json`` (``{slug: item_id}``). From then on:

- ``chrome.publish_update`` - PRIVILEGED, ``requires_approval=True`` (parked on EVERY call;
  the Discord card shows the item, the version and the listing's name and summary),
  ``routable=False``. On approval, with the API **v2** (v1.1 shuts down on 15 Oct 2026):

  1. ``POST https://oauth2.googleapis.com/token`` - a short-lived access token from the
     owner's OAuth client and refresh token (``grant_type=refresh_token``);
  2. ``POST /upload/v2/publishers/<publisher>/items/<item>:upload`` - the drafted zip, after
     its SHA-256 and a secrets scan (marketplace_listing.py) and a check that its
     manifest's version is the one approved; ``uploadState`` IN_PROGRESS is followed with
     ``GET /v2/publishers/<publisher>/items/<item>:fetchStatus`` (``lastAsyncUploadState``);
  3. ``POST /v2/publishers/<publisher>/items/<item>:publish`` ``{publishType}`` - never
     ``skipReview``. The item goes to Google's review; the answer says so (``submitted``), it
     never claims the version is live.

  The item must be the one ``chrome-items.json`` maps the slug to: a payload cannot aim an
  upload at any other item.
- ``chrome.status`` - READ_ONLY: ``:fetchStatus`` for the named items (the published and
  submitted revisions, the last upload, taken down, warned).

The credentials file (``~/.pionir/secrets/chrome-webstore.json``: ``client_id``,
``client_secret``, ``refresh_token``, ``publisher_id``; written by
``tools\\setup-chrome-webstore.ps1``) is read on every call; no secret in it is ever logged
or returned.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

_log = logging.getLogger(__name__)

PUBLISH = "chrome.publish_update"
STATUS = "chrome.status"
DEFAULT_API = "https://chromewebstore.googleapis.com"
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
SETUP_HINT = r"run tools\setup-chrome-webstore.ps1"
PACKAGE_NAME = "extension.zip"
PUBLISH_KEYS = frozenset({"slug", "item_id", "name", "summary", "version", "package_name",
                          "package_sha256", "publish_type"})
PUBLISH_TYPES = ("DEFAULT_PUBLISH", "STAGED_PUBLISH")
CRED_KEYS = ("client_id", "client_secret", "refresh_token", "publisher_id")
UPLOAD_DONE = ("SUCCEEDED", "FAILED", "NOT_FOUND")
MAX_STATUS_ITEMS = 20
MAX_RESPONSE_BYTES = 2_000_000
_ITEM = re.compile(r"[a-p]{32}")
_VERSION = re.compile(r"\d+(?:\.\d+){0,3}")
_LINE_BAD = re.compile(r"[\x00-\x1f\x7f<>]")
_PUBLISHER = re.compile(r"[A-Za-z0-9._@-]{1,120}")

Opener = Callable[..., Any]


def check_publish(payload: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(payload)
    if keys != PUBLISH_KEYS:
        extra, missing = sorted(keys - PUBLISH_KEYS), sorted(PUBLISH_KEYS - keys)
        raise ValueError(f"{(extra or missing)[0]}: " + ("not a field" if extra else "required"))
    p = dict(payload)
    try:
        check_slug(p["slug"])
        check_sha(p["package_sha256"], "package_sha256")
    except ListingProblem as exc:
        raise ValueError(str(exc)) from None
    if not isinstance(p["item_id"], str) or not _ITEM.fullmatch(p["item_id"]):
        raise ValueError("item_id: the 32-letter Chrome Web Store item id")
    for key, lo, hi in (("name", 3, 75), ("summary", 10, 132)):
        v = p[key]
        if not isinstance(v, str) or not lo <= len(v) <= hi or _LINE_BAD.search(v):
            raise ValueError(f"{key}: one line of {lo}-{hi} characters")
    if not isinstance(p["version"], str) or not _VERSION.fullmatch(p["version"]):
        raise ValueError("version: 1-4 dot-separated numbers")
    if p["package_name"] != PACKAGE_NAME:
        raise ValueError(f"package_name: {PACKAGE_NAME}")
    if p["publish_type"] not in PUBLISH_TYPES:
        raise ValueError(f"publish_type: one of {', '.join(PUBLISH_TYPES)}")
    problems = claim_problems({"name": p["name"], "summary": p["summary"]})
    if problems:
        raise ValueError(problems[0])
    return p


@dataclass(frozen=True, slots=True)
class ChromeSettings:
    api_url: str = DEFAULT_API
    token_url: str = DEFAULT_TOKEN_URL
    credentials_file: Path = Path("~/.pionir/secrets/chrome-webstore.json")
    root: Path = Path("~/.pionir/marketplaces")
    secrets_dir: Path | None = None
    secret_files: tuple = ()
    ssh_dir: Path | None = None
    timeout_seconds: int = 120
    upload_wait_seconds: int = 300
    scan_secrets: bool = True
    extra_secrets: tuple = field(default=(), repr=False)

    def __post_init__(self) -> None:
        for url in (self.api_url, self.token_url):
            parsed = urllib.parse.urlparse(url)
            loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
                raise ValueError("the Chrome Web Store URLs must be https: (or http: on "
                                 "loopback)")
        object.__setattr__(self, "credentials_file", Path(self.credentials_file).expanduser())
        object.__setattr__(self, "root", Path(self.root).expanduser())


class _Failure(Exception):
    def __init__(self, output: dict[str, Any]) -> None:
        super().__init__(output.get("error"))
        self.output = output


def _refused(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "refused": why, "error": why, **extra})


def _unavailable(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "unavailable": why, "error": why, **extra})


def read_credentials(path: Path) -> dict[str, str] | None:
    """The OAuth client, refresh token and publisher id; None when any is missing."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        # said once per call, never the content: the file is there but unusable
        _log.warning("chrome_webstore: %s cannot be read (%s); treated as not configured - "
                     "%s", path, type(exc).__name__, SETUP_HINT)
        return None
    if not isinstance(doc, dict):
        return None
    out = {k: str(doc.get(k) or "").strip() for k in CRED_KEYS}
    if not all(out.values()) or not _PUBLISHER.fullmatch(out["publisher_id"]):
        return None
    return out


class ChromeWebStoreAdapter:
    """``chrome.publish_update`` (always parked for the owner) and ``chrome.status``."""

    def __init__(self, settings: ChromeSettings | None = None, *, opener: Opener | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings or ChromeSettings()
        self._api = self.settings.api_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._sleep = sleep
        self._manifest = AgentManifest(
            agent_id="chrome_webstore", version="pionir/chrome_webstore",
            capabilities=(
                Capability(name=PUBLISH,
                           description="Upload a new version of an extension the owner created "
                                       "on the Chrome Web Store and submit it for review (only "
                                       "after the owner approves it)",
                           risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({PUBLISH}),
                           requires_approval=True, routable=False),
                Capability(name=STATUS,
                           description="Read the Chrome Web Store status of the owner's "
                                       "extensions (reads only)",
                           risk=RiskLevel.READ_ONLY, routable=False),
            ))

    def __repr__(self) -> str:
        return "ChromeWebStoreAdapter(credentials=<read at call time>)"

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def _not_configured(self) -> str:
        return (f"not configured: no complete Chrome Web Store credentials at "
                f"{self.settings.credentials_file} - {SETUP_HINT}")

    def status(self) -> Mapping[str, Any]:
        if read_credentials(self.settings.credentials_file) is None:
            raise AdapterUnavailable(self._not_configured())
        return {"api": self._api, "credentials": "configured"}

    def _items(self) -> dict[str, str]:
        path = self.settings.root / "chrome-items.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise _unavailable(f"{path} cannot be read ({type(exc).__name__})") from None
        return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(doc, dict) else {}

    def _secrets(self):
        if not self.settings.scan_secrets:
            return None
        try:
            return load_secrets(self.settings.secrets_dir,
                                (*self.settings.secret_files, self.settings.credentials_file),
                                self.settings.ssh_dir, self.settings.extra_secrets)
        except DeliveryProblem as exc:
            raise _unavailable(f"the secrets check could not run ({exc}); nothing was "
                               "sent") from None

    # ---- the request ---------------------------------------------------------------------
    def validate(self, task: Task) -> None:
        if task.capability == STATUS:
            self._status_items(task.payload)
            return
        p = self._check(task)
        if read_credentials(self.settings.credentials_file) is None:
            raise AdapterUnavailable(self._not_configured())
        try:
            self._owned(p)
            self._package(p)
        except _Failure as failure:
            if failure.output.get("refused"):
                raise AdapterProtocolError(f"{PUBLISH} refused by Pionir - "
                                           f"{failure.output['refused']}") from None
            raise AdapterUnavailable(str(failure.output.get("error"))) from None

    @staticmethod
    def _check(task: Task) -> dict[str, Any]:
        if task.capability != PUBLISH:
            raise AdapterProtocolError(f"chrome_webstore has no capability {task.capability!r}")
        try:
            return check_publish(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{PUBLISH} refused by Pionir - {error}") from error

    @staticmethod
    def _status_items(payload: Mapping[str, Any]) -> list[str]:
        items = payload.get("items")
        if set(payload) != {"items"} or not isinstance(items, list) \
                or len(items) > MAX_STATUS_ITEMS or not all(
                    isinstance(i, str) and _ITEM.fullmatch(i) for i in items):
            raise AdapterProtocolError(f"{STATUS}: the payload is {{items: [up to "
                                       f"{MAX_STATUS_ITEMS} item ids]}}")
        return list(items)

    def _owned(self, p: Mapping[str, Any]) -> None:
        if self._items().get(p["slug"]) != p["item_id"]:
            raise _refused(f"item_id: {p['item_id']} is not the item chrome-items.json names "
                           f"for {p['slug']} (create the item by hand once and record it)")

    def _package(self, p: Mapping[str, Any]) -> bytes:
        try:
            data = read_package(self.settings.root, p["slug"], p["package_name"],
                                p["package_sha256"], self._secrets())
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except ListingProblem as exc:
            raise _refused(str(exc)) from None
        except (KeyError, ValueError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
            raise _refused(f"the package has no readable manifest.json ({exc})") from None
        if not isinstance(manifest, dict) or manifest.get("version") != p["version"]:
            raise _refused("version: the package's manifest.json is not the approved version")
        return data

    # ---- the call ------------------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        if task.capability == STATUS:
            items = self._status_items(task.payload)
        else:
            p = self._check(task)
        creds = read_credentials(self.settings.credentials_file)
        if creds is None:
            why = self._not_configured()
            return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                       "not_configured": True})
        try:
            if task.capability == STATUS:
                output = self._status(items, creds)
            else:
                self._owned(p)
                output = self._publish(p, self._package(p), creds)
        except _Failure as failure:
            output = failure.output
        text = json.dumps(output)
        for secret in (creds["client_secret"], creds["refresh_token"]):
            text = text.replace(secret, "<redacted>")
        return self._result(task, json.loads(text))

    def _access_token(self, creds: Mapping[str, str]) -> str:
        form = urllib.parse.urlencode({"client_id": creds["client_id"],
                                       "client_secret": creds["client_secret"],
                                       "refresh_token": creds["refresh_token"],
                                       "grant_type": "refresh_token"}).encode()
        status, doc = self._http("POST", self.settings.token_url, form,
                                 {"Content-Type": "application/x-www-form-urlencoded"})
        token = doc.get("access_token") if isinstance(doc, dict) else None
        if status == 200 and isinstance(token, str) and token:
            return token
        if status in (400, 401):
            raise _unavailable(f"Google refused the OAuth refresh token - {SETUP_HINT}",
                               status=status, key_rejected=True)
        raise _unavailable(f"Google's token endpoint answered HTTP {status}", status=status)

    def _item_path(self, creds: Mapping[str, str], item: str) -> str:
        return (f"publishers/{urllib.parse.quote(creds['publisher_id'], safe='')}/items/"
                f"{item}")

    def _publish(self, p: Mapping[str, Any], package: bytes,
                 creds: Mapping[str, str]) -> dict[str, Any]:
        token = self._access_token(creds)
        auth = {"Authorization": f"Bearer {token}"}
        name = self._item_path(creds, p["item_id"])
        status, doc = self._http("POST", f"{self._api}/upload/v2/{name}:upload", package,
                                 {**auth, "Content-Type": "application/zip"})
        if status != 200 or not isinstance(doc, dict):
            raise self._failure(status, doc, "uploading the package", token)
        state = str(doc.get("uploadState") or "")
        deadline = time.monotonic() + self.settings.upload_wait_seconds
        while state not in UPLOAD_DONE:
            if time.monotonic() >= deadline:
                raise _unavailable("the upload was still being processed when Pionir stopped "
                                   "waiting; nothing was submitted - approve again later")
            self._sleep(5.0)
            status, sdoc = self._http("GET", f"{self._api}/v2/{name}:fetchStatus", None, auth)
            if status != 200 or not isinstance(sdoc, dict):
                raise self._failure(status, sdoc, "following the upload", token)
            state = str(sdoc.get("lastAsyncUploadState") or "")
        if state != "SUCCEEDED":
            raise _refused(f"the Chrome Web Store did not accept the package (upload "
                           f"{state}); nothing was submitted", upload_state=state)
        status, pdoc = self._http("POST", f"{self._api}/v2/{name}:publish",
                                  json.dumps({"publishType": p["publish_type"]}).encode(),
                                  {**auth, "Content-Type": "application/json"})
        if status != 200 or not isinstance(pdoc, dict):
            raise self._failure(status, pdoc, "submitting it for review", token,
                                uploaded=True)
        return {"ok": True, "submitted": True, "published": False,
                "state": pdoc.get("state"), "warnings": pdoc.get("warningInfo"),
                "item_id": p["item_id"], "version": p["version"],
                "crx_version": doc.get("crxVersion"),
                "url": f"https://chromewebstore.google.com/detail/{p['item_id']}",
                "note": "submitted for Google's review; it is live only once the review "
                        "passes"}

    def _status(self, items: list[str], creds: Mapping[str, str]) -> dict[str, Any]:
        token = self._access_token(creds)
        out = []
        for item in items:
            status, doc = self._http("GET", f"{self._api}/v2/"
                                     f"{self._item_path(creds, item)}:fetchStatus", None,
                                     {"Authorization": f"Bearer {token}"})
            if status == 404:
                out.append({"item_id": item, "found": False})
                continue
            if status != 200 or not isinstance(doc, dict):
                raise self._failure(status, doc, f"reading {item}", token)
            out.append({"item_id": item, "found": True,
                        "published": _revision(doc.get("publishedItemRevisionStatus")),
                        "submitted": _revision(doc.get("submittedItemRevisionStatus")),
                        "last_upload": doc.get("lastAsyncUploadState"),
                        "taken_down": bool(doc.get("takenDown")),
                        "warned": bool(doc.get("warned"))})
        return {"ok": True, "items": out}

    # ---- transport --------------------------------------------------------------------------
    def _http(self, method: str, url: str, data: bytes | None,
              headers: Mapping[str, str]) -> tuple[int, Any]:
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"User-Agent": "pionir-chrome/0.1",
                                                  "Accept": "application/json", **headers})
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

    def _failure(self, status: int, doc: Any, step: str, token: str, **extra: Any) -> _Failure:
        err = doc.get("error") if isinstance(doc, dict) else None
        said = str(err.get("message") or "")[:300] if isinstance(err, dict) else ""
        said = said.replace(token, "<redacted>")
        if status in (401, 403):
            return _unavailable(f"the Chrome Web Store refused the account ({step}, HTTP "
                                f"{status}){': ' + said if said else ''} - {SETUP_HINT}",
                                status=status, **extra)
        if status == 0:
            return _unavailable(f"the Chrome Web Store did not answer ({step})", **extra)
        if 400 <= status < 500 and status != 429:
            return _refused(f"the Chrome Web Store refused {step} (HTTP {status})"
                            + (f": {said}" if said else ""), status=status, **extra)
        return _unavailable(f"the Chrome Web Store answered HTTP {status} ({step})"
                            + (f": {said}" if said else ""), status=status, **extra)

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"chrome:{task.capability.split('.', 1)[1]}"]
        if output.get("item_id"):
            evidence.append(f"chrome:item:{output['item_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))


def _revision(rev: Any) -> dict[str, Any] | None:
    if not isinstance(rev, dict):
        return None
    channels = rev.get("distributionChannels") if isinstance(rev.get("distributionChannels"),
                                                             list) else []
    first = channels[0] if channels and isinstance(channels[0], dict) else {}
    return {"state": rev.get("state"), "crx_version": first.get("crxVersion"),
            "deploy_percentage": first.get("deployPercentage")}


def chrome_settings(configured: Any) -> ChromeSettings:
    from pionir.adapters.clients import client_settings
    from pionir.crew.marketplaces.paths import default_root

    client = client_settings(configured)
    return ChromeSettings(credentials_file=Path(configured.secrets_path) / "chrome-webstore.json",
                          root=default_root(), secrets_dir=client.secrets_dir,
                          secret_files=(*client.secret_files, client.token_file),
                          ssh_dir=client.ssh_dir, extra_secrets=tuple(client.secret_values))
