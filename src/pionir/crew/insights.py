"""What each Instagram post did, from Instagram's own counts, for ``posting.results``.

``posting.instagram`` records every published post with its permalink and (since the adapter
returns it) its media id. At most once per ``REFRESH_SECONDS`` the results worker asks Pionir
``Job("social.instagram_insights", {"media_ids": [...]})`` for those posts - or, when an older
post has no media id, ``{"recent": 25}`` and matches by permalink - and keeps the answer in
``<state_dir>/<worker_id>.instagram.json``, so a run between refreshes reports the last
reading with the time it was measured, and asks nothing.

The ledger's rule, again: honest numbers only.

- Every figure is typed (``count``), names its window (``lifetime``: Meta's Media Insights
  are lifetime totals and the period cannot be changed) and its post (``stream`` = the post's
  draft id); the row is valid at the MEASUREMENT time, not the run time.
- A metric Instagram did not report for a post is listed as unavailable for that post and
  has no figure. It is never a zero, and a total says how many posts it adds up.
- No reading at all (not asked yet, Pionir unreachable, the token rejected, the insights
  permission missing) is said in words - "no Instagram insights yet", or why they are
  unavailable - with no figure at all.
- The "what's working" line names the best post by reach only from a real, non-zero reach.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .blog import _Unreadable, published_posts, read_record
from .figures import Figure
from .hands import Job
from .instagram import media_id
from .log import log
from .worker import make_output

INSTAGRAM_WORKER = "posting.instagram"
CAPABILITY = "social.instagram_insights"
KIND = "traffic.instagram"
REFRESH_SECONDS = 6 * 3600     # at most one insights read per 6 h
MAX_MEDIA = 25                 # the capability's own limit
WINDOW = "lifetime"
NO_INSIGHTS = "no Instagram insights yet"
SOURCE = "Instagram Media Insights (social.instagram_insights), lifetime totals per post"
# Instagram's metric name -> the words a figure measures. The same set the adapter asks for.
METRICS = (("reach", "reach"), ("likes", "likes"), ("comments", "comments"),
           ("saved", "saves"), ("shares", "shares"), ("total_interactions", "interactions"),
           ("views", "views"))
_NAMES = frozenset(name for name, _label in METRICS)


@dataclass
class Insights:
    """One run's view of the Instagram posts' numbers. ``per_post`` maps a draft id to its
    ``{metrics, unavailable}``, or to None when the reading has nothing for that post."""

    posts: list
    per_post: dict = field(default_factory=dict)
    measured_at: float | None = None
    why: str | None = None          # why there is no reading, when there is none
    stale: str | None = None        # the last refresh failed: the reading shown is older
    asked: bool = False             # this run asked Pionir

    def has_numbers(self) -> bool:
        return any(v and v["metrics"] for v in self.per_post.values())


def cache_path(state_dir: Path, worker_id: str) -> Path:
    return Path(state_dir) / f"{worker_id}.instagram.json"


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("results: the Instagram insights cache is unreadable (%s); starting again",
                    exc)
        return {}
    return doc if isinstance(doc, dict) else {}


def _save(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".insights-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _epoch(iso) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        moment = datetime.fromisoformat(iso.strip())
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _link_key(url) -> str | None:
    """A permalink as something to compare: host without www, path without the last /."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}" if host and path else None


def _media(answer) -> list:
    """The answer's media rows, each checked: ``[(media_id, permalink key, metrics,
    unavailable)]``. A value that is not a whole, non-negative count is dropped (and so
    unavailable), never read as zero."""
    rows = answer.get("media") if isinstance(answer, dict) else None
    out = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("media_id"), str):
            continue
        raw = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        metrics = {k: int(v) for k, v in raw.items()
                   if k in _NAMES and not isinstance(v, bool) and isinstance(v, (int, float))
                   and v >= 0 and float(v).is_integer()}
        out.append((item["media_id"], _link_key(item.get("permalink")), metrics,
                    [n for n, _label in METRICS if n not in metrics]))
    return out


def _job(posts: list) -> Job:
    ids = [m for m in (media_id(p) for p in posts) if m is not None]
    if ids and len(ids) == len(posts):
        payload: dict = {"media_ids": ids}
    else:
        # an older post was recorded before the media id was kept: read the account's
        # latest media and match them by permalink
        payload = {"recent": MAX_MEDIA}
    return Job(CAPABILITY, payload, what="read the Instagram posts' insights")


def gather(worker, ctx, instagram_worker: str = INSTAGRAM_WORKER) -> Insights | None:
    """The Instagram posts' numbers for this run, or None when the Instagram worker has
    not recorded anything yet (then there is nothing to report on)."""
    if ctx.state_dir is None:
        return None
    try:
        rec = read_record(ctx.state_dir, instagram_worker)
    except _Unreadable as exc:
        log.warning("%s: the Instagram record is unreadable: %s", worker.worker_id, exc)
        return Insights([], why="the Instagram record is unreadable, so its posts are unknown")
    if rec is None:
        return None
    posts = [p for p in published_posts(rec) if isinstance(p.get("draft_id"), str)]
    posts = posts[-MAX_MEDIA:]
    if not posts:
        return Insights([], why="no published Instagram posts yet")
    path = cache_path(ctx.state_dir, worker.worker_id)
    cache = _load(path)
    ins = Insights(posts)
    last = cache.get("attempted_at")
    due = not isinstance(last, (int, float)) or ctx.now - float(last) >= REFRESH_SECONDS
    if due and ctx.job is not None:
        ins.asked = True
        out = ctx.job(_job(posts))
        cache["attempted_at"] = ctx.now
        answer = out.result if out.ran else None
        if out.ran and isinstance(answer, dict) and answer.get("ok") is True \
                and isinstance(answer.get("media"), list):
            cache.update(answer=answer, measured_at=_epoch(answer.get("measured_at"))
                         or ctx.now, error=None)
        else:
            cache["error"] = (out.error or f"Pionir said {out.status}")[:300]
            log.warning("%s: Instagram insights unavailable: %s", worker.worker_id,
                        cache["error"])
        try:
            _save(path, cache)
        except OSError as exc:
            log.warning("%s: the Instagram insights cache could not be saved: %s",
                        worker.worker_id, exc)
    elif due:
        ins.stale = "Pionir is not reachable from this run"
    answer = cache.get("answer")
    if cache.get("error"):
        ins.stale = f"the last read failed: {cache['error']}"
    if not isinstance(answer, dict):
        ins.why = ins.stale or "not read yet"
        ins.stale = None
        return ins
    measured = cache.get("measured_at")
    ins.measured_at = float(measured) if isinstance(measured, (int, float)) else None
    rows = _media(answer)
    by_id = {mid: (m, u) for mid, _k, m, u in rows}
    by_link = {k: (m, u) for _mid, k, m, u in rows if k}
    for p in posts:
        got = by_id.get(p.get("media_id")) or by_link.get(_link_key(p.get("permalink")))
        ins.per_post[p["draft_id"]] = ({"metrics": got[0], "unavailable": got[1]}
                                       if got else None)
    return ins


def _figures(ins: Insights) -> list:
    figs = []
    for did, got in ins.per_post.items():
        for name, label in METRICS:
            if got and name in got["metrics"]:
                figs.append(Figure(got["metrics"][name], "count", f"instagram {label}", did,
                                   WINDOW))
    n = len(ins.per_post)
    for name, label in METRICS:
        values = [got["metrics"][name] for got in ins.per_post.values()
                  if got and name in got["metrics"]]
        if not values:
            continue
        what = f"instagram {label}, total of {len(values)} posts"
        if len(values) < n:
            what += f" (of {n}; the others have no {label} figure)"
        figs.append(Figure(sum(values), "count", what, "instagram", WINDOW))
    return figs


def best_by_reach(ins: Insights | None) -> tuple | None:
    """(draft id, reach) of the post with the highest real, non-zero reach, or None."""
    if ins is None:
        return None
    reached = [(got["metrics"]["reach"], did) for did, got in ins.per_post.items()
               if got and got["metrics"].get("reach")]
    if not reached:
        return None
    reach, did = min(reached, key=lambda r: (-r[0], r[1]))
    return did, reach


def working_line(ins: Insights | None) -> tuple:
    """(the words for the "what's working" summary, the figures behind them), or
    (None, []) when there is nothing Instagram to say."""
    if ins is None:
        return None, []
    if not ins.has_numbers():
        if ins.posts and ins.why and ins.why != "not read yet":
            return f"Instagram insights unavailable: {ins.why[:160]}", []
        return NO_INSIGHTS + (f": {ins.why}" if ins.why else ""), []
    best = best_by_reach(ins)
    if best is None:
        return "no Instagram post has a recorded reach yet", []
    did, reach = best
    return (f"best Instagram post by reach: {did} ({reach})",
            [Figure(reach, "count", "instagram reach", did, WINDOW)])


def row(worker, ctx, ins: Insights):
    """The ``traffic.instagram`` row: every post's figures, the totals, and in words what
    is unavailable. Valid at the measurement time."""
    line, _figs = working_line(ins)
    unavailable = {did: got["unavailable"] for did, got in ins.per_post.items()
                   if got and got["unavailable"]}
    payload = {
        "summary": line,
        "source": SOURCE,
        "window": WINDOW,
        "published_posts": len(ins.posts),
        "measured_at": iso(ins.measured_at) if ins.measured_at else None,
        "metrics_unavailable": unavailable,
        "not_in_reading": [did for did, got in ins.per_post.items() if got is None],
    }
    if ins.why:
        payload["why_no_figures"] = ins.why
    if ins.stale:
        payload["note"] = f"{ins.stale}; the figures are from the last good reading"
    valid = ins.measured_at if ins.measured_at else ctx.now
    return make_output(worker, kind=KIND, valid_at=valid, observed_at=ctx.now,
                       payload=payload, figures=_figures(ins) if ins.has_numbers() else [],
                       entities=("Instagram",),
                       provenance={"source": "real", "provider": "instagram",
                                   "capability": CAPABILITY, "window": WINDOW,
                                   "measured_at": payload["measured_at"],
                                   "asked_this_run": ins.asked})
