"""A Shopify App Store submission pack, from the spec alone (no build, no publish yet).

The owner registers as a Shopify partner ($19, once) and submits apps by hand; review takes
weeks. Until the night builds can build an embedded app, the pipeline prepares everything
that does not need code, in the store's own limits: the app name (30 characters), the
introduction (100), the details (500), up to five features (80 each), the plans, the
category, each scope with why it is needed (read scopes only - no customer or order data),
the app icon (1200 x 1200) and a feature card (1600 x 900), and what only the owner can
supply (a privacy policy URL, a support email, a demo screencast, a test store).
"""
from __future__ import annotations

import hashlib
import json

from pionir.adapters.marketplace_listing import claim_problems

from . import images

PACK = "submission-pack.md"
DISCLOSURE = ("Built with the help of an AI coding system; its code must be reviewed and "
              "tested before submission.")
OWNER_SUPPLIES = ("a Shopify Partner account ($19 one-time registration)",
                  "a privacy policy URL", "a support email address",
                  "a demo screencast of the working app", "a development store for review")


def _fit(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3].rsplit(" ", 1)[0] + "..."


def draft(spec: dict) -> tuple:
    lst = spec["listing"]
    listing = {
        "slug": spec["slug"], "app_name": _fit(spec["name"].split(":", 1)[0], 30),
        "introduction": _fit(spec["summary"], 100), "details": _fit(spec["brief"], 500),
        "features": [_fit(f, 80) for f in spec["features"][:5]],
        "category": lst.get("category"), "plans": lst.get("plans"),
        "scopes": {s: (lst.get("justifications") or {}).get(s) for s in lst.get("scopes") or []},
        "limits": spec["limits"], "owner_supplies": list(OWNER_SUPPLIES),
        "disclosure": DISCLOSURE, "evidence": spec.get("evidence")}
    icon = images.icon(spec["name"], 1200)
    card = images.feature_card(spec["name"], spec["summary"], spec["features"], (1600, 900),
                               label="SHOPIFY APP")
    plans = "\n".join(f"- {p.get('name')}: ${float(p.get('price_usd_month') or 0):.2f}/month"
                      for p in lst.get("plans") or [])
    scopes = "\n".join(f"- `{s}` - {why}" for s, why in listing["scopes"].items())
    feats = "\n".join(f"- {f}" for f in listing["features"])
    needs = "\n".join(f"- {x}" for x in OWNER_SUPPLIES)
    text = (f"# Shopify App Store submission pack: {listing['app_name']}\n\n"
            "NOT submitted. Nothing here was built or sent to Shopify.\n\n"
            f"## App name (30)\n\n{listing['app_name']}\n\n## Introduction (100)\n\n"
            f"{listing['introduction']}\n\n## Details (500)\n\n{listing['details']}\n\n"
            f"## Features (80 each)\n\n{feats}\n\n## Plans\n\n{plans}\n\n## Category\n\n"
            f"{listing['category']}\n\n## Access scopes (read only)\n\n{scopes}\n\n"
            f"## Limits\n\n{spec['limits']}\n\n## You supply\n\n{needs}\n\n## How it was made\n\n"
            f"{DISCLOSURE}\n\n## Why this app (measured)\n\n```json\n"
            f"{json.dumps(spec.get('evidence'), indent=1, ensure_ascii=False)}\n```\n")
    files = {PACK: text.encode(), "app-icon.png": icon, "feature-card.png": card,
             "spec.json": (json.dumps(spec, indent=1, ensure_ascii=False) + "\n").encode()}
    listing["files"] = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
    problems = claim_problems({"introduction": listing["introduction"],
                               "details": listing["details"], "features": listing["features"]})
    return listing, files, problems
