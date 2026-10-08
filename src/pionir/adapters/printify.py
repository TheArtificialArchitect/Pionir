"""Adapter for Printify: a print-on-demand product reaches the Etsy shop only after the
owner's yes - twice.

- ``printify.catalog`` - READ_ONLY: blueprints whose title names a product noun, their
  print providers, and each variant's print area in pixels (``GET /v1/catalog/...``). The
  crew picks a blueprint and provider from this, never from a hard-coded list.
- ``printify.create_product`` - PRIVILEGED, ``requires_approval`` (parked on EVERY call; it
  costs nothing - a Printify product is private until published): uploads the staged design
  (``POST /v1/uploads/images.json``) and creates the product (``POST
  /v1/shops/{shop_id}/products.json``). Printify's catalog has no cost before a product
  exists, so the cost-plus rule is enforced HERE, on the costs Printify answers with: every
  enabled variant must keep at least ``min_margin_cents`` after Etsy's fees (6.5% transaction,
  3% + 25c payment processing, the 20c listing fee) and the print cost. A variant below it is
  repriced up to the owner's approved ``max_price_cents``, or disabled; if none is left the
  product is deleted and the answer says so. The design's pixel size must equal the print
  area Printify states for every chosen variant (checked against the catalog at execution).
- ``printify.publish`` - PRIVILEGED, ``spends_money`` (parked on EVERY call, its own card):
  ``POST .../products/{id}/publish.json`` pushes the product to the linked Etsy shop as a
  listing, which costs Etsy's $0.20 listing fee. Before sending, the product is read back
  and every enabled variant's price and cost must be exactly what the card showed, with the
  margin still held - or nothing is published.

Text passes the crew's Etsy rules (``pionir.crew.etsy.rules``, kind ``pod``: the AI
disclosure word for word, no brand, no proper noun, no claim, exactly 13 tags). The design
is read from ``<stage_dir>/<slug>/`` with its pinned SHA-256. A ledger
(``<state_root>/etsy/printify.json``) keeps a slug from being made or published twice, and at
most ``max_new_products_per_day`` products are created per UTC day.

The personal access token is ``~/.pionir/secrets/printify.json`` (``{"token", "shop_id"}``,
written by ``tools\\setup-printify.ps1``), read on every call, sent only as ``Authorization:
Bearer`` to Printify, and never logged or returned.
"""
from __future__ import annotations

import base64
import json
import logging
import math
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

CATALOG = "printify.catalog"
CREATE = "printify.create_product"
PUBLISH = "printify.publish"

DEFAULT_PRINTIFY_URL = "https://api.printify.com/v1"
SETUP_HINT = r"run tools\setup-printify.ps1"
AGENT = "Pionir-POD/0.1"            # Printify requires a User-Agent naming the client
LISTING_FEE = {"amount": "0.20", "currency": "USD"}
MAX_DESIGN_BYTES = 20_000_000
MAX_VARIANTS = 20
PRICE_CENTS = (500, 20_000)
MIN_MARGIN_CENTS = 300            # never less than this, whatever a payload says
# Etsy's cut of a sale, per US listing (2025-2026): 6.5% transaction, 3% + 25c processing,
# and the 20c listing fee (charged per listing and per sale-triggered renewal).
ETSY_PERCENT = 0.065 + 0.03
ETSY_FIXED_CENTS = 25 + 20

CREATE_FIELDS = frozenset({"slug", "title", "description", "tags", "blueprint_id",
                           "print_provider_id", "position", "variants", "design",
                           "min_margin_cents", "max_price_cents", "ai_disclosure"})
PUBLISH_FIELDS = frozenset({"slug", "product_id", "title", "variants", "min_margin_cents",
                            "spend", "spends_money"})
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,59}")
_DESIGN = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\.png")
_PRODUCT = re.compile(r"[0-9a-f]{24}")
_NOUN = re.compile(r"[a-z]+(?: [a-z]+){0,2}")


def net_cents(price_cents: int, cost_cents: int) -> int:
    """What the shop keeps of one sale at this price, after Etsy's fees and the print cost
    (shipping is charged to the buyer at Printify's rate, so it is not in here)."""
    return price_cents - cost_cents - math.ceil(price_cents * ETSY_PERCENT) - ETSY_FIXED_CENTS


def price_for(cost_cents: int, margin_cents: int) -> int:
    """The lowest price ending in 99 cents that keeps ``margin_cents`` after fees and cost."""
    p = math.ceil((cost_cents + ETSY_FIXED_CENTS + margin_cents) / (1 - ETSY_PERCENT))
    p = (p // 100) * 100 + 99 if p % 100 != 99 else p
    while net_cents(p, cost_cents) < margin_cents:
        p += 100
    return p


def credentials_problem(path: Path) -> str | None:
    """The exact missing credential, or None (for the crew's readiness and doctor)."""
    try:
        doc = shop.read_json_file(Path(path))
    except shop.Failure as failure:
        return f"{failure.output.get('error')} - {SETUP_HINT}"
    if doc is None:
        return f"no Printify credentials at {path} - {SETUP_HINT} (after connecting Printify " \
               "to the Etsy shop)"
    if not isinstance(doc.get("token"), str) or len(doc["token"]) < 20:
        return f"{path} has no token - {SETUP_HINT}"
    if not str(doc.get("shop_id") or "").isdigit():
        return f"{path} has no shop_id - {SETUP_HINT}"
    return None


def _int(p: Mapping[str, Any], key: str, low: int, high: int) -> int:
    v = p.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise ValueError(f"{key}: a whole number {low}-{high}")
    return v


def check_create(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - CREATE_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a product field")
    missing = sorted(CREATE_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    if not isinstance(payload["slug"], str) or not _SLUG.fullmatch(payload["slug"]):
        raise ValueError("slug: 3-60 of a-z, 0-9 and '-'")
    if payload["ai_disclosure"] != rules.AI_DISCLOSURE:
        raise ValueError(f"ai_disclosure: must be exactly {rules.AI_DISCLOSURE!r}")
    rules.check_listing(payload["title"], payload["description"], payload["tags"], kind="pod")
    if payload["position"] != "front":
        raise ValueError("position: front (the one print area designs are drawn for)")
    margin = _int(payload, "min_margin_cents", MIN_MARGIN_CENTS, 10_000)
    ceiling = _int(payload, "max_price_cents", *PRICE_CENTS)
    variants = payload["variants"]
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise ValueError(f"variants: 1-{MAX_VARIANTS}")
    out_v = []
    for i, v in enumerate(variants):
        if not isinstance(v, Mapping) or set(v) != {"id", "price_cents"}:
            raise ValueError(f"variants[{i}]: {{id, price_cents}}")
        vid = _int(v, "id", 1, 10**9)
        price = _int(v, "price_cents", *PRICE_CENTS)
        if price > ceiling:
            raise ValueError(f"variants[{i}].price_cents: above max_price_cents")
        out_v.append({"id": vid, "price_cents": price})
    if len({v["id"] for v in out_v}) != len(out_v):
        raise ValueError("variants: each variant once")
    d = payload["design"]
    if not isinstance(d, Mapping) or set(d) != {"name", "sha256", "width", "height"} \
            or not isinstance(d["name"], str) or not _DESIGN.fullmatch(d["name"]):
        raise ValueError("design: {name (.png), sha256, width, height}")
    return {"slug": payload["slug"], "title": payload["title"],
            "description": payload["description"], "tags": list(payload["tags"]),
            "blueprint_id": _int(payload, "blueprint_id", 1, 10**9),
            "print_provider_id": _int(payload, "print_provider_id", 1, 10**9),
            "position": "front", "variants": out_v, "min_margin_cents": margin,
            "max_price_cents": ceiling,
            "design": {"name": d["name"], "sha256": d["sha256"],
                       "width": _int(d, "width", 500, 20_000),
                       "height": _int(d, "height", 500, 20_000)}}


def check_publish(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - PUBLISH_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a publish field")
    missing = sorted(PUBLISH_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    if payload["spends_money"] is not True or payload["spend"] != LISTING_FEE:
        raise ValueError(f"spend/spends_money: publishing to Etsy costs its listing fee "
                         f"{LISTING_FEE}; state it exactly, with spends_money true")
    if not isinstance(payload["slug"], str) or not _SLUG.fullmatch(payload["slug"]):
        raise ValueError("slug: the slug the product was created with")
    if not isinstance(payload["product_id"], str) or not _PRODUCT.fullmatch(
            payload["product_id"]):
        raise ValueError("product_id: Printify's 24-hex product id")
    margin = _int(payload, "min_margin_cents", MIN_MARGIN_CENTS, 10_000)
    variants = payload["variants"]
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise ValueError(f"variants: 1-{MAX_VARIANTS}")
    out = []
    for i, v in enumerate(variants):
        if not isinstance(v, Mapping) or set(v) != {"id", "price_cents", "cost_cents"}:
            raise ValueError(f"variants[{i}]: {{id, price_cents, cost_cents}}")
        row = {"id": _int(v, "id", 1, 10**9), "price_cents": _int(v, "price_cents",
                                                                  *PRICE_CENTS),
               "cost_cents": _int(v, "cost_cents", 1, 100_000)}
        if net_cents(row["price_cents"], row["cost_cents"]) < margin:
            raise ValueError(f"variants[{i}]: keeps less than {margin} cents after Etsy's fees "
                             "and the print cost")
        out.append(row)
    if not isinstance(payload["title"], str) or not payload["title"].strip():
        raise ValueError("title: the product's title, for the card")
    return {"slug": payload["slug"], "product_id": payload["product_id"],
            "title": payload["title"], "variants": out, "min_margin_cents": margin}


def check_catalog(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != {"product"} or not isinstance(payload["product"], str) \
            or not _NOUN.fullmatch(payload["product"]):
        raise ValueError("product: 1-3 lower-case words naming a product (e.g. 'mug')")
    problem = rules.brand_problem(payload["product"])
    if problem:
        raise ValueError(f"product: {problem}")
    return {"product": payload["product"]}


@dataclass(frozen=True, slots=True)
class PrintifySettings:
    api_url: str = DEFAULT_PRINTIFY_URL
    credentials_file: Path = Path("~/.pionir/secrets/printify.json")
    stage_dir: Path = Path("~/.pionir/etsy/pod")
    ledger_file: Path = Path("~/.pionir/etsy/printify.json")
    secrets_dir: Path | None = Path("~/.pionir/secrets")
    max_new_products_per_day: int = 1
    timeout_seconds: int = 30
    upload_timeout_seconds: int = 180

    def __post_init__(self) -> None:
        shop.check_url(self.api_url, "Printify API")
        if self.timeout_seconds < 5 or self.upload_timeout_seconds < self.timeout_seconds:
            raise ValueError("Printify timeouts: at least 5 s, uploads no shorter than calls")
        if not 0 <= self.max_new_products_per_day <= 20:
            raise ValueError("max_new_products_per_day: 0-20")
        for name in ("credentials_file", "stage_dir", "ledger_file"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser())
        if self.secrets_dir is not None:
            object.__setattr__(self, "secrets_dir", Path(self.secrets_dir).expanduser())


def printify_settings(configured: Any) -> PrintifySettings:
    return PrintifySettings(api_url=configured.printify_url or DEFAULT_PRINTIFY_URL,
                            credentials_file=configured.printify_credentials_path,
                            stage_dir=configured.etsy_path / "pod",
                            ledger_file=configured.etsy_path / "printify.json",
                            secrets_dir=configured.secrets_path)


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


class PrintifyAdapter:
    def __init__(self, settings: PrintifySettings | None = None, *,
                 opener: shop.Opener | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.settings = settings or PrintifySettings()
        self._api = self.settings.api_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._clock = clock or time.time
        self._manifest = AgentManifest(
            agent_id="printify", version="pionir/printify",
            capabilities=(
                Capability(name=CATALOG,
                           description="Read Printify's catalog: blueprints naming a product, "
                                       "their print providers and print-area sizes",
                           risk=RiskLevel.READ_ONLY, routable=False),
                Capability(name=CREATE,
                           description="Create a print-on-demand product in Printify from a "
                                       "staged design, priced cost-plus (only after the owner "
                                       "approves it; private until published)",
                           risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({CREATE}),
                           requires_approval=True, routable=False),
                Capability(name=PUBLISH,
                           description="Publish a Printify product to the linked Etsy shop "
                                       "(only after the owner approves it; Etsy's listing fee)",
                           risk=RiskLevel.PRIVILEGED,
                           required_permissions=frozenset({PUBLISH}),
                           spends_money=True, routable=False),
            ))

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        problem = credentials_problem(self.settings.credentials_file)
        if problem:
            raise AdapterUnavailable(f"not configured: {problem}")
        return {"api": self._api, "credentials": "configured",
                "stage_dir": str(self.settings.stage_dir)}

    # ---- the request -----------------------------------------------------------------
    def _check(self, task: Task) -> dict[str, Any]:
        checker = {CATALOG: check_catalog, CREATE: check_create,
                   PUBLISH: check_publish}.get(task.capability)
        if checker is None:
            raise AdapterProtocolError(f"printify has no capability {task.capability!r}")
        try:
            return checker(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") \
                from error

    def validate(self, task: Task) -> None:
        item = self._check(task)
        problem = credentials_problem(self.settings.credentials_file)
        if problem:
            raise AdapterUnavailable(f"not configured: {problem}")
        try:
            if task.capability == CREATE:
                self._design(item)
                self._may_create(item["slug"])
            elif task.capability == PUBLISH:
                self._may_publish(item)
        except DeliveryProblem as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") \
                from error
        except shop.Failure as failure:
            why = str(failure.output.get("error"))
            if "refused" in failure.output:
                raise AdapterProtocolError(f"{task.capability} refused by Pionir - {why}") \
                    from None
            raise AdapterUnavailable(why) from None

    def _design(self, item: Mapping[str, Any]) -> shop.StagedFile:
        d = item["design"]
        root = self.settings.stage_dir
        staged = shop.stage_file(root / item["slug"], root, d["name"], d["sha256"],
                                 max_bytes=MAX_DESIGN_BYTES,
                                 secrets=shop.secrets_for(self.settings.secrets_dir))
        if (staged.width, staged.height) != (d["width"], d["height"]):
            raise DeliveryProblem(f"{d['name']}: {staged.width}x{staged.height}, not the "
                                  f"{d['width']}x{d['height']} the payload states")
        return staged

    def design_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The design as it is on disk, for the Discord card (which attaches it)."""
        try:
            staged = self._design(check_create(payload))
        except (ValueError, DeliveryProblem, shop.Failure) as error:
            return {"ok": False, "problem": str(error)}
        return {"ok": True, "name": staged.name, "size": staged.size, "width": staged.width,
                "height": staged.height, "cover_name": staged.name,
                "cover_bytes": staged.data}

    # ---- the ledger --------------------------------------------------------------------
    def _ledger(self) -> dict[str, Any]:
        doc = shop.read_json_file(self.settings.ledger_file) or {}
        doc.setdefault("products", {})
        if not isinstance(doc["products"], dict):
            raise shop.unavailable(f"{self.settings.ledger_file}: products is not an object")
        return doc

    def _may_create(self, slug: str) -> None:
        ledger = self._ledger()
        if slug in ledger["products"]:
            raise shop.refused(f"slug {slug!r} already has a Printify product "
                               f"({ledger['products'][slug].get('product_id')}); never twice")
        today = _utc_day(self._clock())
        made = sum(1 for e in ledger["products"].values() if isinstance(e, dict)
                   and _utc_day(float(e.get("created_at") or 0)) == today)
        if made >= self.settings.max_new_products_per_day:
            raise shop.refused(f"the daily cap of {self.settings.max_new_products_per_day} new "
                               f"Printify products is reached ({made} today, UTC)")

    def _may_publish(self, item: Mapping[str, Any]) -> dict[str, Any]:
        entry = self._ledger()["products"].get(item["slug"])
        if not isinstance(entry, dict) or entry.get("product_id") != item["product_id"]:
            raise shop.refused(f"product {item['product_id']} was not created by Pionir for "
                               f"{item['slug']!r}; only its own products are published")
        if entry.get("state") == "published":
            raise shop.refused(f"product {item['product_id']} is already published")
        return entry

    def _record(self, slug: str, entry: Mapping[str, Any]) -> str | None:
        try:
            ledger = self._ledger()
        except shop.Failure:
            ledger = {"products": {}}
        ledger["products"][slug] = {**ledger["products"].get(slug, {}), **entry}
        try:
            shop.write_json_file(self.settings.ledger_file, ledger)
        except OSError as error:
            _log.warning("printify: the ledger write for %s failed (%s)", slug,
                         type(error).__name__)
            return f"the ledger could not be written; do not submit {slug} again"
        return None

    # ---- transport ------------------------------------------------------------------------
    def _creds(self) -> dict[str, Any]:
        doc = shop.read_json_file(self.settings.credentials_file)
        if doc is None or credentials_problem(self.settings.credentials_file):
            raise shop.unavailable(f"not configured: "
                                   f"{credentials_problem(self.settings.credentials_file)}",
                                   not_configured=True)
        return doc

    def _call(self, method: str, path: str, token: str, *, body: Any = None, step: str,
              upload: bool = False) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json;charset=utf-8"
        timeout = self.settings.upload_timeout_seconds if upload else \
            self.settings.timeout_seconds
        status, doc = shop.http(self._open, method, f"{self._api}{path}", data, headers,
                                timeout, AGENT)
        if 200 <= status < 300:
            return doc
        said = ""
        if isinstance(doc, Mapping):
            said = str(doc.get("message") or doc.get("error") or "")[:300]
        said = shop.scrub(said, [token])
        if status in (401, 403):
            raise shop.unavailable(f"{step}: Printify refused the token (HTTP {status}) - "
                                   f"{SETUP_HINT}", status=status, key_rejected=True)
        if status in (400, 404, 409, 422):
            raise shop.refused(f"{step}: Printify said no (HTTP {status}"
                               f"{': ' + said if said else ''})", status=status)
        if status == 429:
            raise shop.unavailable(f"{step}: Printify is rate limiting - try later", status=429)
        if status == 0:
            raise shop.unavailable(f"{step}: Printify did not answer", status=0)
        raise shop.unavailable(f"{step}: Printify answered HTTP {status}", status=status)

    # ---- the call ------------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        item = self._check(task)
        token = ""
        try:
            creds = self._creds()
            token = creds["token"]
            shop_id = str(creds["shop_id"])
            if task.capability == CATALOG:
                output = self._catalog(item, token)
            elif task.capability == CREATE:
                with shop.lock_for(self.settings.ledger_file):
                    output = self._create(item, token, shop_id)
            else:
                with shop.lock_for(self.settings.ledger_file):
                    output = self._publish(item, token, shop_id)
        except shop.Failure as failure:
            output = failure.output
        output = json.loads(shop.scrub(json.dumps(output), [token]))
        evidence = [f"printify:{task.capability.split('.', 1)[1]}"]
        if output.get("ok") is True and output.get("product_id"):
            evidence.append(f"printify:product:{output['product_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))

    def _variants(self, blueprint: int, provider: int, token: str) -> list:
        doc = self._call("GET", f"/catalog/blueprints/{blueprint}/print_providers/{provider}/"
                         "variants.json", token, step="read the variants")
        rows = doc.get("variants") if isinstance(doc, Mapping) else doc
        return [v for v in (rows or []) if isinstance(v, Mapping)]

    def _catalog(self, item: Mapping[str, Any], token: str) -> dict[str, Any]:
        doc = self._call("GET", "/catalog/blueprints.json", token, step="read the blueprints")
        if not isinstance(doc, list):
            raise shop.unavailable("Printify's blueprints answer is not a list")
        noun = item["product"]
        pattern = re.compile(r"(?<![a-z])" + re.escape(noun) + r"s?(?![a-z])", re.IGNORECASE)
        found = sorted((b for b in doc if isinstance(b, Mapping)
                        and isinstance(b.get("title"), str) and pattern.search(b["title"])),
                       key=lambda b: (len(b["title"]), b.get("id") or 0))[:3]
        out = []
        for b in found:
            provs = self._call("GET", f"/catalog/blueprints/{b['id']}/print_providers.json",
                               token, step="read the print providers")
            rows = []
            for p in (provs if isinstance(provs, list) else [])[:3]:
                if not isinstance(p, Mapping) or not isinstance(p.get("id"), int):
                    continue
                variants = []
                for v in self._variants(b["id"], p["id"], token)[:200]:
                    places = [{"position": ph.get("position"), "width": ph.get("width"),
                               "height": ph.get("height")}
                              for ph in (v.get("placeholders") or []) if isinstance(ph, Mapping)]
                    variants.append({"id": v.get("id"), "title": v.get("title"),
                                     "options": v.get("options") or {},
                                     "placeholders": places})
                rows.append({"print_provider_id": p["id"], "title": p.get("title"),
                             "variants": variants})
            out.append({"blueprint_id": b["id"], "title": b["title"], "providers": rows})
        return {"ok": True, "product": noun, "blueprints": out}

    def _create(self, item: Mapping[str, Any], token: str, shop_id: str) -> dict[str, Any]:
        try:
            design = self._design(item)
        except DeliveryProblem as error:
            raise shop.refused(f"{error} - nothing was created") from None
        self._may_create(item["slug"])
        # the print area for every chosen variant must be the design's exact size
        sizes = {}
        for v in self._variants(item["blueprint_id"], item["print_provider_id"], token):
            for ph in v.get("placeholders") or []:
                if isinstance(ph, Mapping) and ph.get("position") == item["position"]:
                    sizes[v.get("id")] = (ph.get("width"), ph.get("height"))
        want = (item["design"]["width"], item["design"]["height"])
        for v in item["variants"]:
            if sizes.get(v["id"]) != want:
                raise shop.refused(f"variant {v['id']}: its {item['position']} print area is "
                                   f"{sizes.get(v['id'])}, not the design's {want}; nothing "
                                   "was created")
        up = self._call("POST", "/uploads/images.json", token, upload=True,
                        body={"file_name": design.name,
                              "contents": base64.b64encode(design.data).decode("ascii")},
                        step="upload the design")
        image_id = up.get("id") if isinstance(up, Mapping) else None
        if not isinstance(image_id, str) or not image_id:
            raise shop.unavailable("Printify took the design but gave no image id")
        ids = [v["id"] for v in item["variants"]]
        body = {"title": item["title"], "description": item["description"],
                "tags": item["tags"], "blueprint_id": item["blueprint_id"],
                "print_provider_id": item["print_provider_id"],
                "variants": [{"id": v["id"], "price": v["price_cents"], "is_enabled": True}
                             for v in item["variants"]],
                "print_areas": [{"variant_ids": ids, "placeholders": [{
                    "position": item["position"],
                    "images": [{"id": image_id, "x": 0.5, "y": 0.5, "scale": 1,
                                "angle": 0}]}]}]}
        doc = self._call("POST", f"/shops/{shop_id}/products.json", token, body=body,
                         step="create the product")
        pid = doc.get("id") if isinstance(doc, Mapping) else None
        if not isinstance(pid, str) or not _PRODUCT.fullmatch(pid):
            raise shop.unavailable("Printify accepted the product but gave no id; check the "
                                   "Printify store before trying again")
        warning = self._record(item["slug"], {"product_id": pid, "state": "pricing",
                                              "created_at": self._clock()})
        costs = {v.get("id"): v.get("cost") for v in (doc.get("variants") or [])
                 if isinstance(v, Mapping)}
        final, repriced, disabled = [], [], []
        for v in item["variants"]:
            cost = costs.get(v["id"])
            if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
                disabled.append(v["id"])          # no cost: never sold blind
                continue
            price = v["price_cents"]
            if net_cents(price, cost) < item["min_margin_cents"]:
                needed = price_for(cost, item["min_margin_cents"])
                if needed > item["max_price_cents"]:
                    disabled.append(v["id"])
                    continue
                price = needed
                repriced.append(v["id"])
            final.append({"id": v["id"], "price_cents": price, "cost_cents": cost,
                          "net_cents": net_cents(price, cost)})
        if not final:
            self._call("DELETE", f"/shops/{shop_id}/products/{pid}.json", token,
                       step="delete the unprofitable product")
            self._record(item["slug"], {"state": "deleted",
                                        "why": "no variant keeps the minimum margin"})
            raise shop.refused(f"no variant keeps {item['min_margin_cents']} cents at or under "
                               f"{item['max_price_cents']} cents after Etsy's fees and the "
                               "print cost; the product was deleted", product_id=pid)
        if repriced or disabled:
            keep = {v["id"]: v["price_cents"] for v in final}
            self._call("PUT", f"/shops/{shop_id}/products/{pid}.json", token, body={
                "variants": [{"id": v["id"], "price": keep.get(v["id"], v["price_cents"]),
                              "is_enabled": v["id"] in keep} for v in item["variants"]]},
                step="set the cost-plus prices")
        warning = self._record(item["slug"], {"state": "created", "variants": final}) or warning
        out = {"ok": True, "product_id": pid, "variants": final, "repriced": repriced,
               "disabled": disabled, "min_margin_cents": item["min_margin_cents"]}
        if warning:
            out["ledger_error"] = warning
        return out

    def _publish(self, item: Mapping[str, Any], token: str, shop_id: str) -> dict[str, Any]:
        self._may_publish(item)
        pid = item["product_id"]
        doc = self._call("GET", f"/shops/{shop_id}/products/{pid}.json", token,
                         step="read the product back")
        live = {v.get("id"): v for v in (doc.get("variants") if isinstance(doc, Mapping)
                                         else None) or [] if isinstance(v, Mapping)}
        want = {v["id"]: v for v in item["variants"]}
        for vid, v in live.items():
            if v.get("is_enabled") and vid not in want:
                raise shop.refused(f"variant {vid} is enabled in Printify but not on the card; "
                                   "nothing published")
        for vid, v in want.items():
            got = live.get(vid)
            if not got or not got.get("is_enabled") or got.get("price") != v["price_cents"] \
                    or got.get("cost") != v["cost_cents"]:
                raise shop.refused(f"variant {vid} is not priced as approved (price or print "
                                   "cost changed since); nothing published")
            if net_cents(v["price_cents"], v["cost_cents"]) < item["min_margin_cents"]:
                raise shop.refused(f"variant {vid} no longer keeps the minimum margin")
        self._call("POST", f"/shops/{shop_id}/products/{pid}/publish.json", token, body={
            "title": True, "description": True, "images": True, "variants": True,
            "tags": True, "keyFeatures": True, "shipping_template": True},
            step="publish to Etsy")
        warning = self._record(item["slug"], {"state": "published",
                                              "published_at": self._clock()})
        _log.info("printify: published product %s to the linked Etsy shop", pid)
        out = {"ok": True, "product_id": pid, "state": "published",
               "note": "Printify is pushing it to the linked Etsy shop"}
        if warning:
            out["ledger_error"] = warning
        return out


# ---- what the Discord card says --------------------------------------------------------------
CREATE_LINE = ("\U0001f455 **MAKES A PRINT-ON-DEMAND PRODUCT IN PRINTIFY** (private; nothing "
               "is listed or charged until a separate publish card) - look at the design")
PUBLISH_LINE = ("\U0001f6cd️ **PUBLISHES TO THE ETSY SHOP VIA PRINTIFY - Etsy charges a "
                "$0.20 listing fee**")


def card_lines(row: Mapping[str, Any], preview: Mapping[str, Any] | None) -> list[str]:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    cap = row.get("capability")
    if cap == PUBLISH:
        lines = [PUBLISH_LINE, f"**Product:** {payload.get('product_id')} - "
                               f"{str(payload.get('title'))[:140]}"]
        for v in payload.get("variants") or []:
            if isinstance(v, Mapping) and isinstance(v.get("price_cents"), int) \
                    and isinstance(v.get("cost_cents"), int):
                lines.append(f"- variant {v.get('id')}: price {v['price_cents'] / 100:.2f}, "
                             f"print cost {v['cost_cents'] / 100:.2f}, keeps "
                             f"{net_cents(v['price_cents'], v['cost_cents']) / 100:.2f} USD "
                             "after Etsy's fees")
        return lines
    if cap != CREATE:
        return []
    lines = [CREATE_LINE, f"**Title:** {str(payload.get('title'))[:140]}",
             "**AI disclosure in the description:** "
             + ("yes, word for word" if rules.AI_DISCLOSURE in str(payload.get("description"))
                else "**NO - do not approve**"),
             f"**Cost-plus rule:** at least {payload.get('min_margin_cents')} cents kept per "
             f"sale, never above {payload.get('max_price_cents')} cents"]
    if preview is None or not preview.get("ok"):
        why = (preview or {}).get("problem") or "the design could not be inspected"
        lines.append(f"⚠️ **DO NOT APPROVE: {str(why)[:300]}**")
    else:
        lines.append(f"**Design:** `{preview['name']}` {preview['width']}x{preview['height']} "
                     "(attached)")
    tags = payload.get("tags")
    if isinstance(tags, list):
        lines.append(f"**Tags ({len(tags)}):** " + ", ".join(f"`{t}`" for t in tags))
    return lines


def card_body(row: Mapping[str, Any]) -> list[str]:
    """The whole description, verbatim, below the card's header (split, never cut)."""
    if row.get("capability") != CREATE:
        return []
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return ["**Description, in full, exactly as it will be listed:**", "```",
            str(payload.get("description")).replace("```", "'''"), "```"]
