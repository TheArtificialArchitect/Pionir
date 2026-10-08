"""``etsy.scout``: which Etsy keywords to make something for, decided from measured demand.

Once a day it measures up to ``max_probes`` keywords on Etsy's public search, through
Pionir's READ_ONLY ``etsy.search_active`` (the app's key stays in Pionir), and writes the
ranking to ``<state_dir>/etsy-demand.json`` for the two makers.

**Where candidates come from - measured, never a fixed niche list:**

1. **Etsy's own tags.** Every listing the search returns carries its seller's tags; a tag
   that recurs in at least ``TAG_QUORUM`` of the most-favourited results of a keyword already
   measured is a phrase buyers and sellers use, and becomes a candidate (if one of our makers
   can make it: ``sheets.kind_for`` / ``pod_product``).
2. **The crew's own demand ranking** (``products.demand``'s ``demand.json``): any candidate
   name there that one of our makers can make.
3. **Seeds - fallback only.** ``SEEDS`` are probed only while nothing has been measured yet
   (or nothing measured has led anywhere): one phrase per thing we can make, to start the
   measuring. After the first measurements, the tags carry it.

**What a keyword's numbers are.** ``count``: how many active listings match (the
competition). ``favorers``: the average favourites of the top 25 results by relevance (the
demand; UNKNOWN when Etsy gave none - never zero). ``score = favorers / log10(count + 10)``:
demand per unit of competition, used only to order. Also kept, as measured: the median price
of those results (the makers price from it) and the commonest taxonomy (Etsy's category,
which a listing must name).

A keyword is never anyone's brand (``rules.keyword_problems``), and nothing of another
seller's listing is ever copied into ours: only counts, prices, categories and generic tags
are read.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from pathlib import Path

from ...adapters.etsy import PUBLIC_FIELDS, SEARCH, credentials_problem
from ..blog import save_record
from ..figures import Figure
from ..hands import Job
from ..log import log
from ..result import Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import rules, sheets
from .common import SCOUT_FILE, Record, Unreadable

TOP = 25
TAG_QUORUM = 3
STALE_AFTER = 7 * 86400.0
MAX_KEYWORDS = 400                  # the record keeps at most this many measured keywords
# Product words a print-on-demand keyword can name -> what to search Printify's catalog for.
POD_PRODUCTS = {"mug": "mug", "mugs": "mug", "poster": "poster", "posters": "poster",
                "tee": "tee", "tshirt": "tee", "shirt": "tee", "tote": "tote bag"}
SEEDS = ("budget tracker spreadsheet", "habit tracker printable", "savings tracker printable",
         "debt payoff tracker", "motivational poster", "funny coffee mug")


def pod_product(keyword: str) -> str | None:
    words = keyword.lower().replace("t shirt", "tshirt").split()
    for w in reversed(words):
        if w in POD_PRODUCTS:
            return POD_PRODUCTS[w]
    return None


def classify(keyword: str) -> dict | None:
    """Which maker could make this keyword: {"track": "digital", "kind"} or {"track": "pod",
    "product"}; None if neither (or it is a brand)."""
    if rules.keyword_problems(keyword):
        return None
    kind = sheets.kind_for(keyword)
    if kind is not None:
        return {"track": "digital", "kind": kind}
    product = pod_product(keyword)
    if product is not None:
        return {"track": "pod", "product": product}
    return None


def measure(result: dict) -> dict:
    """A search answer -> the keyword's measured inputs. UNKNOWN stays None."""
    rows = [r for r in result.get("results") or [] if isinstance(r, dict)][:TOP]
    favs = [r["num_favorers"] for r in rows if isinstance(r.get("num_favorers"), int)]
    prices = [r["price_cents"] for r in rows if isinstance(r.get("price_cents"), int)
              and r.get("currency") in ("USD", None)]
    taxes = Counter(r["taxonomy_id"] for r in rows if isinstance(r.get("taxonomy_id"), int))
    tags = Counter(t for r in sorted(rows, key=lambda r: -(r.get("num_favorers") or 0))
                   for t in set(r.get("tags") or []) if isinstance(t, str))
    count = result.get("count") if isinstance(result.get("count"), int) else None
    favorers = round(sum(favs) / len(favs), 2) if favs else None
    score = (round(favorers / math.log10(count + 10), 3)
             if favorers is not None and count is not None else None)
    return {"count": count, "favorers": favorers, "favorers_measured": len(favs),
            "median_price_cents": int(statistics.median(prices)) if prices else None,
            "taxonomy_id": taxes.most_common(1)[0][0] if taxes else None,
            "top_tags": [t for t, n in tags.most_common(40) if n >= TAG_QUORUM],
            "score": score}


class EtsyScout(_Base):
    """``etsy.scout``: measures Etsy demand for candidate keywords; writes the ranking."""

    def __init__(self, spec, *, max_probes: int = 8, demand_file: str = "demand.json") -> None:
        super().__init__(spec)
        if isinstance(max_probes, bool) or not isinstance(max_probes, int) \
                or not 1 <= max_probes <= 30:
            raise ValueError("max_probes: 1-30 searches a run")
        self.max_probes = max_probes
        self.demand_file = demand_file

    def readiness(self, secrets_dir) -> str | None:
        return credentials_problem(Path(secrets_dir) / "etsy.json", PUBLIC_FIELDS)

    # ---- where candidates come from ------------------------------------------------------
    def candidates(self, rec: dict, state_dir: Path) -> list[tuple[str, str]]:
        """(keyword, source) to consider, measured sources first; seeds only as fallback."""
        out: dict[str, str] = {}
        measured = rec.get("keywords") or {}
        for kw, m in sorted(measured.items(), key=lambda kv: -(kv[1].get("score") or 0)):
            for tag in m.get("top_tags") or []:
                if tag not in out and classify(tag) is not None:
                    out[tag] = f"tag of {kw!r}"
        for name in self._demand_names(state_dir):
            if name not in out and classify(name) is not None:
                out[name] = "the crew's demand ranking"
        useful = [k for k in measured if classify(k) is not None]
        if not out and not useful:
            for seed in SEEDS:
                out.setdefault(seed, "seed (fallback: nothing measured yet)")
        return list(out.items())

    def _demand_names(self, state_dir: Path) -> list[str]:
        path = Path(state_dir) / self.demand_file
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("%s: %s is unreadable (%s); not used", self.worker_id, path, exc)
            return []
        names = []
        for row in (doc.get("ranking") or []) if isinstance(doc, dict) else []:
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str):
                names.append(" ".join(name.lower().split())[:40])
        return names

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir", retryable=False)
        problem = self.readiness(ctx.secrets_dir)
        if problem:
            return self._err(ErrorKind.NOT_CONFIGURED, problem, retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: Etsy is read through Pionir",
                             retryable=False)
        try:
            record = Record(ctx.state_dir, self.worker_id, {"keywords": {}, "counts": {}})
        except Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc})",
                             retryable=False)
        rec = record.doc
        measured = rec["keywords"]
        cands = self.candidates(rec, ctx.state_dir)
        fresh = [(k, s) for k, s in cands if k not in measured]
        stale = sorted((k for k, m in measured.items()
                        if ctx.now - float(m.get("measured_at") or 0) > STALE_AFTER
                        and classify(k) is not None),
                       key=lambda k: measured[k].get("measured_at") or 0)
        todo = [(k, s) for k, s in fresh][:self.max_probes]
        todo += [(k, measured[k].get("source", "re-measure")) for k in stale][
            :self.max_probes - len(todo)]
        probed, failed = 0, []
        for kw, source in todo:
            out = ctx.job(Job(SEARCH, {"keywords": kw, "limit": 100},
                              what=f"measure Etsy demand for {kw!r}"))
            if not out.ran or not isinstance(out.result, dict):
                failed.append(f"{kw}: {out.status} {out.error}"[:200])
                if out.status == "unreachable" or "not configured" in (out.error or ""):
                    break                    # Pionir or Etsy is not there: stop asking
                continue
            measured[kw] = {**measure(out.result), "measured_at": ctx.now, "source": source,
                            **(classify(kw) or {})}
            probed += 1
        if len(measured) > MAX_KEYWORDS:
            for k in sorted(measured, key=lambda k: measured[k].get("measured_at") or 0)[
                    :len(measured) - MAX_KEYWORDS]:
                del measured[k]
        record.count("probes", probed)
        record.save()
        ranking = self.ranking(measured)
        save_record(Path(ctx.state_dir) / SCOUT_FILE, {"made_at": ctx.now, "ranking": ranking})
        if failed and not probed:
            log.warning("%s: no keyword measured: %s", self.worker_id, "; ".join(failed[:3]))
            kind = ErrorKind.NOT_CONFIGURED if any("not configured" in f for f in failed) \
                else ErrorKind.UNAVAILABLE
            return self._err(kind, "no keyword could be measured: " + failed[0])
        best = ranking[0] if ranking else None
        figures = [
            Figure(probed, "count", "Etsy keywords measured this run", window="run"),
            Figure(len(ranking), "count", "Etsy keywords ranked", window="now"),
            Figure(sum(1 for r in ranking if r["track"] == "digital"), "count",
                   "ranked keywords a digital maker can make", window="now"),
            Figure(sum(1 for r in ranking if r["track"] == "pod"), "count",
                   "ranked keywords the print-on-demand maker can make", window="now"),
        ]
        payload = {"top": ranking[:5], "failed": failed[:3], "file": SCOUT_FILE,
                   "note": "score = average favourites of the top 25 results / log10(listings "
                           "+ 10); UNKNOWN inputs have no score, never zero"}
        if best is not None:
            payload["best"] = best["keyword"]
        return Ok((make_output(self, kind="etsy.demand", valid_at=ctx.now, observed_at=ctx.now,
                               payload=payload, figures=figures, entities=self.entities,
                               provenance={"source": "real", "provider": self.provider,
                                           "record": "Etsy's public search, via Pionir"}),))

    @staticmethod
    def ranking(measured: dict) -> list[dict]:
        rows = []
        for kw, m in measured.items():
            c = classify(kw)
            if c is None:
                continue
            rows.append({"keyword": kw, **c, "score": m.get("score"),
                         "count": m.get("count"), "favorers": m.get("favorers"),
                         "median_price_cents": m.get("median_price_cents"),
                         "taxonomy_id": m.get("taxonomy_id"),
                         "measured_at": m.get("measured_at"), "source": m.get("source")})
        # scored first (best first); a keyword with no score ranks after every scored one
        return sorted(rows, key=lambda r: (r["score"] is None, -(r["score"] or 0),
                                           r["keyword"]))
