"""Fakes for the Etsy streams' tests: Etsy's Open API v3 and Printify's API, at the HTTP
opener, with the real opener's signature ``(request, timeout=None)``.

Not a test module. Each fake checks what the real API checks (the ``x-api-key`` of
``keystring:shared_secret``, the OAuth bearer, Printify's bearer and User-Agent), parses the
request body the way the API reads it (form-urlencoded, multipart, JSON), records every call,
and answers in the API's documented shape. Nothing touches the network.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from typing import Any, Self

from test_instagram_post import parse_multipart

KEYSTRING = "etsykeystringabc123def456"
SHARED_SECRET = "etsysharedSECRET789xyz"
SHOP_ID = 31415926
ACCESS = "55555.accessTOKENetsy0123456789abcdef"
REFRESH = "55555.refreshTOKENetsy0123456789abcdef"
NEW_ACCESS = "55555.freshACCESStoken9876543210zyx"
NEW_REFRESH = "55555.freshREFRESHtoken9876543210zyx"
PRINTIFY_TOKEN = "printifyPERSONALaccessTOKEN0123456789abcdef"
PRINTIFY_SHOP = 777
TAXONOMY = 6844


class Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _error(url: str, status: int, payload: Any):
    raise urllib.error.HTTPError(url, status, "error", {},  # type: ignore[arg-type]
                                 io.BytesIO(json.dumps(payload).encode("utf-8")))


def _headers(request) -> dict:
    return {k.lower(): v for k, v in request.header_items()}


def search_result(keyword: str, n: int = 30, *, base_fav: int = 40, tags=()) -> dict:
    """An Etsy search answer: ``n`` listings with favourites, prices and tags."""
    rows = []
    for i in range(n):
        rows.append({"listing_id": 1000 + i, "title": f"someone else's {keyword} {i}",
                     "num_favorers": base_fav + (i % 7) * 3, "views": 500 + i,
                     "price": {"amount": 650 + (i % 3) * 50, "divisor": 100,
                               "currency_code": "USD"},
                     "taxonomy_id": TAXONOMY if i % 4 else 1234,
                     "tags": list(tags) + [f"other tag {i}"],
                     "creation_timestamp": 1_700_000_000 + i, "shop_id": 99 + i})
    return {"count": 1200, "results": rows}


class FakeEtsy:
    """Etsy's Open API v3, in memory."""

    def __init__(self, base: str = "https://etsy.test/v3") -> None:
        self.base = base.rstrip("/")
        self.calls: list[dict[str, Any]] = []
        self.access = ACCESS
        self.listings: dict[int, dict[str, Any]] = {}
        self.next_id = 900_001
        self.search: dict[str, dict] = {}
        self.default_search = None
        self.receipts: list[dict] = []
        self.fail: dict[str, tuple[int, Any]] = {}

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def __call__(self, request, timeout: float | None = None) -> Response:
        url = request.full_url
        parts = urllib.parse.urlsplit(url)
        assert url.startswith(self.base), url
        path = parts.path[len(urllib.parse.urlsplit(self.base).path):]
        headers = _headers(request)
        ctype = headers.get("content-type", "")
        call: dict[str, Any] = {"method": request.get_method(), "path": path,
                                "query": urllib.parse.parse_qs(parts.query),
                                "api_key": headers.get("x-api-key"),
                                "auth": headers.get("authorization"),
                                "content_type": ctype, "timeout": timeout}
        if request.data is not None and ctype.startswith("application/x-www-form-urlencoded"):
            call["form"] = urllib.parse.parse_qs(request.data.decode("utf-8"),
                                                 keep_blank_values=True)
        elif request.data is not None and ctype.startswith("multipart/form-data"):
            call["parts"] = parse_multipart(request.data, ctype)
        self.calls.append(call)
        return self._route(call, url)

    def _route(self, call: dict, url: str) -> Response:
        m, path = call["method"], call["path"]
        if path == "/public/oauth/token" and m == "POST":
            call["step"] = "token"
            form = call["form"]
            if form.get("grant_type") != ["refresh_token"] or form.get("client_id") != \
                    [KEYSTRING] or form.get("refresh_token") != [REFRESH]:
                _error(url, 400, {"error": "invalid_grant"})
            self.access = NEW_ACCESS
            return Response(200, {"access_token": NEW_ACCESS, "token_type": "Bearer",
                                  "expires_in": 3600, "refresh_token": NEW_REFRESH})
        if call["api_key"] != f"{KEYSTRING}:{SHARED_SECRET}":
            _error(url, 403, {"error": "Shared secret is required in x-api-key header."})
        if path == "/application/listings/active" and m == "GET":
            call["step"] = "search"
            kw = call["query"]["keywords"][0]
            doc = self.search.get(kw) or (self.default_search(kw) if self.default_search
                                          else {"count": 0, "results": []})
            return Response(200, doc)
        if call["auth"] != f"Bearer {self.access}":
            _error(url, 401, {"error": "invalid_token"})
        shop = f"/application/shops/{SHOP_ID}"
        if path == f"{shop}/listings" and m == "POST":
            call["step"] = "create"
            if "create" in self.fail:
                _error(url, *self.fail["create"])
            form = call["form"]
            for key in ("quantity", "title", "description", "price", "who_made", "when_made",
                        "taxonomy_id"):
                if key not in form:
                    _error(url, 400, {"error": f"{key} is required"})
            lid = self.next_id
            self.next_id += 1
            self.listings[lid] = {"listing_id": lid, "shop_id": SHOP_ID, "state": "draft",
                                  "form": form, "files": [], "images": []}
            return Response(201, {"listing_id": lid, "shop_id": SHOP_ID, "state": "draft",
                                  "title": form["title"][0]})
        if path.startswith(f"{shop}/listings/") and m == "POST":
            lid = int(path.split("/")[5])
            what = path.split("/")[6]
            call["step"] = what
            if what in self.fail:
                _error(url, *self.fail[what])
            names = {p["name"]: p for p in call["parts"]}
            self.listings[lid][what].append(names)
            return Response(201, {"listing_id": lid, "rank": int(names["rank"]["data"])})
        if path.startswith("/application/listings/") and m == "GET":
            call["step"] = "read"
            lid = int(path.rsplit("/", 1)[1])
            row = self.listings.get(lid)
            if row is None:
                _error(url, 404, {"error": "not found"})
            return Response(200, {"listing_id": lid, "shop_id": row["shop_id"],
                                  "state": row["state"]})
        if path.startswith(f"{shop}/listings/") and m == "PATCH":
            call["step"] = "activate"
            lid = int(path.rsplit("/", 1)[1])
            self.listings[lid]["state"] = call["form"]["state"][0]
            return Response(200, {"listing_id": lid, "state": self.listings[lid]["state"],
                                  "url": f"https://www.etsy.com/listing/{lid}/a-listing"})
        if path == f"{shop}/receipts" and m == "GET":
            call["step"] = "receipts"
            off = int(call["query"].get("offset", ["0"])[0])
            return Response(200, {"count": len(self.receipts),
                                  "results": self.receipts[off:off + 100]})
        _error(url, 404, {"error": f"no route {m} {path}"})
        raise AssertionError


class FakePrintify:
    """Printify's API v1, in memory."""

    def __init__(self, base: str = "https://printify.test/v1") -> None:
        self.base = base.rstrip("/")
        self.calls: list[dict[str, Any]] = []
        self.costs = {101: 700, 102: 700, 103: 700}
        self.products: dict[str, dict] = {}
        self.uploads: list[dict] = []
        self.published: list[str] = []
        self.next = 1

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def __call__(self, request, timeout: float | None = None) -> Response:
        url = request.full_url
        parts = urllib.parse.urlsplit(url)
        path = parts.path[len(urllib.parse.urlsplit(self.base).path):]
        headers = _headers(request)
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        call = {"method": request.get_method(), "path": path, "body": body,
                "auth": headers.get("authorization"), "agent": headers.get("user-agent"),
                "content_type": headers.get("content-type")}
        self.calls.append(call)
        if call["auth"] != f"Bearer {PRINTIFY_TOKEN}":
            _error(url, 401, {"message": "Unauthenticated"})
        if not call["agent"]:
            _error(url, 400, {"message": "User-Agent is required"})
        m = call["method"]
        if path == "/catalog/blueprints.json":
            call["step"] = "blueprints"
            return Response(200, [{"id": 9, "title": "Unisex Jersey Short Sleeve Tee"},
                                  {"id": 5, "title": "Ceramic Mug 11oz"},
                                  {"id": 6, "title": "Ceramic Mug Gift Box Edition"}])
        if path.endswith("/print_providers.json"):
            call["step"] = "providers"
            return Response(200, [{"id": 1, "title": "Provider One"}])
        if path.endswith("/variants.json"):
            call["step"] = "variants"
            return Response(200, {"id": 5, "variants": [
                {"id": 101, "title": "11oz / White", "options": {"color": "White",
                                                                  "size": "11oz"},
                 "placeholders": [{"position": "front", "width": 2700, "height": 1050}]},
                {"id": 102, "title": "15oz / White", "options": {"color": "White",
                                                                  "size": "15oz"},
                 "placeholders": [{"position": "front", "width": 2700, "height": 1050}]},
                {"id": 103, "title": "11oz / Black", "options": {"color": "Black",
                                                                  "size": "11oz"},
                 "placeholders": [{"position": "front", "width": 2700, "height": 1050}]}]})
        if path == "/uploads/images.json" and m == "POST":
            call["step"] = "upload"
            self.uploads.append(body)
            return Response(200, {"id": "img0001", "file_name": body["file_name"],
                                  "width": 2700, "height": 1050})
        shop = f"/shops/{PRINTIFY_SHOP}/products"
        if path == f"{shop}.json" and m == "POST":
            call["step"] = "create"
            pid = f"{self.next:024x}"
            self.next += 1
            variants = [{"id": v["id"], "price": v["price"], "cost": self.costs[v["id"]],
                         "is_enabled": v["is_enabled"]} for v in body["variants"]]
            self.products[pid] = {"id": pid, "title": body["title"], "variants": variants,
                                  "body": body}
            return Response(200, {"id": pid, "variants": variants})
        if path.startswith(f"{shop}/") and path.endswith("/publish.json"):
            call["step"] = "publish"
            self.published.append(path.split("/")[4])
            return Response(200, {})
        if path.startswith(f"{shop}/"):
            pid = path.split("/")[4].removesuffix(".json")
            if m == "GET":
                call["step"] = "read"
                return Response(200, self.products[pid])
            if m == "PUT":
                call["step"] = "update"
                for v in body["variants"]:
                    for live in self.products[pid]["variants"]:
                        if live["id"] == v["id"]:
                            live.update(price=v["price"], is_enabled=v["is_enabled"])
                return Response(200, self.products[pid])
            if m == "DELETE":
                call["step"] = "delete"
                self.products.pop(pid)
                return Response(200, {})
        _error(url, 404, {"message": f"no route {m} {path}"})
        raise AssertionError
