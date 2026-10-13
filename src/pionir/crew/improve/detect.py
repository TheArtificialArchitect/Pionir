"""What keeps failing the same way: the sources the self-improvement worker reads, and how it
clusters them. No model, no network, nothing written - every reader here only reads.

A **sighting** is one recorded refusal or failure: which worker, the reason in its own words,
when. A **cluster** is one worker and one NORMALISED reason (``normalise``: digits become ``N``,
long hex ids ``<id>``, case and spacing folded - so "title: 15-140 characters (this is 241)" and
"(this is 257)" are one reason, while ``('medical')`` and ``('bill')`` stay apart) seen at least
``threshold`` times since ``since``. The Etsy maker blocking 26 drafts on three false alarms
while every run reported ok is the case this exists for.

Sources (each optional; a missing one is simply not read):

- ``outputs`` - the crew store's rows whose kind ends in a refusal (``BLOCK_KINDS``: an
  ``etsy.blocked`` draft and its ``reasons``, a ``build.rejected``, ``*.shelved``...);
- ``runs`` - the crew store's failed attempts (not NOT CONFIGURED, not wired, not "the shared
  model did not answer": setup and capacity, not code);
- ``vitals`` - the ``VITALS:`` lines of ``crew.log`` (dated from the file's own time, walking
  back over midnight; "is stale" only echoes the failed runs, which are counted already);
- ``builds`` - ``builds.daedalus.json``: failed attempts ("Daedalus reported a pass but
  committed nothing") and rejected reviews ("larger than one review can read");
- ``approvals`` - Ian's own denials in the approvals queue (an expiry or an interruption is not
  his no);
- ``lessons`` - a lesson in the shared ``lessons`` namespace that keeps recurring (not a policy
  refusal Pionir itself calls "not a fault").

The worker's own failures and its own capability are never evidence: no agent proposes changes
to itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pionir.selfimprove.guard import allowed_why_not, protected_closure, protected_why

SELF = "builds.improve"
SELF_CAPABILITY = "improve.apply"
BLOCK_KINDS = ("blocked", "rejected", "shelved", "not_listed", "attempt_failed",
               "review_failed", "refused")
# Not code: a setting or secret missing, a placeholder, or the shared model not answering in
# time (the GPU busy with someone else) - none of these is fixed by a patch.
SETUP_KINDS = ("not_configured", "not_wired", "no_words")
# VITALS lines that only echo what the runs already say (a stale worker's cause is its failed
# runs, counted there) or that are setup, not code.
VITALS_ECHO = ("is NOT CONFIGURED", "is a placeholder", "is stale:")
# A lesson Pionir records when an adapter's own policy refused a task: by its own words, not a
# fault - and the failure itself is recorded as its own lesson.
NOT_A_FAULT = "not a fault"
# ... nor one about a service passing through a bad moment (the GPU leased, a circuit open).
PASSING = re.compile(r"(?i)resourceunavailable|circuitopen|bodydeferred")
NOT_HIS_NO = ("expired", "interrupted")
FAILED_ATTEMPTS = ("gate_failed", "timed_out", "lost", "failed", "refused")
VITALS = re.compile(r"^(\d\d):(\d\d):(\d\d)\s+\w+\s+VITALS:\s+(\S+)\s+(.+)$")
_TIME = re.compile(r"^(\d\d):(\d\d):(\d\d)\s")
LOG_TAIL_BYTES = 2_000_000
MAX_SAMPLES = 3


def normalise(reason) -> str:
    """One reason, as clustered: digits -> N, long hex ids -> <id>, case and spaces folded."""
    text = " ".join(str(reason or "").split()).lower()
    text = re.sub(r"\b[0-9a-f]{12,64}\b", "<id>", text)
    text = re.sub(r"\d+(?:\.\d+)?", "N", text)
    return text[:240]


def iso(t: float) -> str:
    try:
        return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def _clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3] + "..."


@dataclass(frozen=True)
class Sighting:
    source: str
    worker: str
    reason: str             # in its own words
    at: float
    line: str               # how it is shown as a sample
    weight: int = 1         # a recurring lesson counts its recurrences
    first: float | None = None


@dataclass
class Cluster:
    source: str
    worker: str
    reason: str             # normalised
    count: int
    first_seen: float
    last_seen: float
    samples: list = field(default_factory=list)
    example: str = ""       # one reason in its own words (targets are found from it)
    also: list = field(default_factory=list)    # keys of the same failure seen elsewhere

    @property
    def key(self) -> str:
        return f"{self.source}|{self.worker}|{self.reason}"

    @property
    def key_hash(self) -> str:
        return hashlib.sha1(self.key.encode("utf-8")).hexdigest()[:12]

    def evidence(self, days: int) -> dict:
        return {"key": self.key, "source": self.source, "worker": self.worker,
                "reason": self.reason, "example": _clip(self.example, 300),
                "count": self.count, "window_days": days,
                "first_seen": iso(self.first_seen), "last_seen": iso(self.last_seen),
                "first_seen_at": self.first_seen, "last_seen_at": self.last_seen,
                "samples": [_clip(s, 300) for s in self.samples[:MAX_SAMPLES]],
                "also": list(self.also)}


def cluster(sightings, *, threshold: int, since: float) -> list:
    """Every (source, worker, normalised reason) seen ``threshold`` or more times since
    ``since``, the most frequent first."""
    groups: dict = {}
    for s in sightings:
        if s.at < since or s.worker == SELF or SELF_CAPABILITY in s.reason:
            continue
        key = (s.source, s.worker, normalise(s.reason))
        groups.setdefault(key, []).append(s)
    out = []
    for (source, worker, reason), group in groups.items():
        count = sum(max(1, int(s.weight)) for s in group)
        if count < threshold:
            continue
        group.sort(key=lambda s: s.at, reverse=True)
        samples, seen = [], set()
        for s in group:
            if s.line not in seen:
                seen.add(s.line)
                samples.append(s.line)
            if len(samples) >= MAX_SAMPLES:
                break
        first = min((s.first if s.first is not None else s.at) for s in group)
        out.append(Cluster(source, worker, reason, count, first, group[0].at, samples,
                           group[0].reason))
    out.sort(key=lambda c: (-c.count, -c.last_seen, c.key))
    return out


# ---- the sources ----------------------------------------------------------------------------
def _get(row, name, default=None):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def from_outputs(rows) -> list:
    """Refusal rows (``BLOCK_KINDS``) and each of their reasons."""
    out = []
    for row in rows or []:
        kind = str(_get(row, "kind") or "")
        if kind.rsplit(".", 1)[-1] not in BLOCK_KINDS:
            continue
        worker = str(_get(row, "worker_id") or "?")
        payload = _get(row, "payload") or {}
        at = float(_get(row, "observed_at") or 0)
        reasons = payload.get("reasons") if isinstance(payload, dict) else None
        if not isinstance(reasons, list):
            one = payload.get("why") or payload.get("reason") if isinstance(payload, dict) \
                else None
            reasons = [one] if one else []
        about = ""
        if isinstance(payload, dict):
            for key in ("keyword", "slug", "id", "kind"):
                if isinstance(payload.get(key), str) and payload[key]:
                    about = f" [{key}: {_clip(payload[key], 60)}]"
                    break
        for reason in reasons:
            if isinstance(reason, str) and reason.strip():
                out.append(Sighting("outputs", worker, reason, at,
                                    f"{iso(at)} {worker} {kind}: {_clip(reason, 200)}{about}"))
    return out


def from_runs(rows) -> list:
    out = []
    for row in rows or []:
        kind = str(row.get("error_kind") or "")
        if kind in SETUP_KINDS:
            continue
        worker = str(row.get("worker_id") or "?")
        at = float(row.get("finished_at") or 0)
        reason = f"{kind}: {row.get('error_message') or ''}"
        out.append(Sighting("runs", worker, reason, at,
                            f"{iso(at)} {worker} failed: {_clip(reason, 220)}"))
    return out


def from_vitals(text: str, mtime: float) -> list:
    """``VITALS:`` lines of crew.log. The log stamps only the time of day, so each line is
    dated by walking back from the file's last write: a time later than the line after it
    means the day before."""
    day = datetime.fromtimestamp(mtime).date()
    after = datetime.fromtimestamp(mtime)
    later = after.hour * 3600 + after.minute * 60 + after.second + 1
    out = []
    for line in reversed(text.splitlines()):
        t = _TIME.match(line)
        if not t:
            continue
        secs = int(t.group(1)) * 3600 + int(t.group(2)) * 60 + int(t.group(3))
        if secs > later:
            day = day.fromordinal(day.toordinal() - 1)
        later = secs
        m = VITALS.match(line)
        if not m:
            continue
        worker, says = m.group(4), m.group(5)
        if says.startswith(VITALS_ECHO):
            continue
        at = datetime.combine(day, datetime.min.time()).timestamp() + secs
        out.append(Sighting("vitals", worker, says, at, f"{iso(at)} VITALS: {worker} "
                                                        f"{_clip(says, 220)}"))
    return out


def read_vitals(log_path) -> list:
    if not log_path:
        return []
    path = Path(log_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > LOG_TAIL_BYTES:
                fh.seek(size - LOG_TAIL_BYTES)
            data = fh.read()
        mtime = path.stat().st_mtime
    except OSError:
        return []
    return from_vitals(data.decode("utf-8", "replace"), mtime)


def from_builds(rec) -> list:
    """The night builds' failed attempts and rejected reviews (``builds.daedalus.json``)."""
    out = []
    products = (rec or {}).get("products") if isinstance(rec, dict) else None
    for slug, p in (products or {}).items():
        if not isinstance(p, dict):
            continue
        for a in p.get("attempts") or []:
            if not isinstance(a, dict) or a.get("outcome") not in FAILED_ATTEMPTS \
                    or not a.get("why"):
                continue
            at = float(a.get("finished_at") or a.get("submitted_at") or 0)
            out.append(Sighting("builds", "builds.daedalus", str(a["why"]), at,
                                f"{iso(at)} builds.daedalus {slug} {a.get('kind', 'build')} "
                                f"#{a.get('n')} {a['outcome']}: {_clip(a['why'], 200)}"))
        for r in p.get("reviews") or []:
            if not isinstance(r, dict) or r.get("approved") is not False:
                continue
            at = float(r.get("at") or 0)
            for reason in r.get("reasons") or []:
                if isinstance(reason, str) and reason.strip():
                    out.append(Sighting("builds", "builds.daedalus", reason, at,
                                        f"{iso(at)} builds.daedalus {slug} rejected by "
                                        f"{r.get('by')}: {_clip(reason, 200)}"))
    return out


def _epoch(stamp) -> float:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def from_approvals(rows) -> list:
    """Ian's denials (an expiry or an interruption is not his no; this worker's own
    capability is never evidence)."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("status") != "denied":
            continue
        reason = str(row.get("reason") or "").strip()
        cap = str(row.get("capability") or "?")
        if reason in NOT_HIS_NO or cap == SELF_CAPABILITY:
            continue
        at = _epoch(row.get("resolved_at") or row.get("created_at"))
        said = reason or "denied without a reason"
        out.append(Sighting("approvals", cap, f"Ian denied it: {said}", at,
                            f"{iso(at)} {cap} denied by Ian ({_clip(said, 120)}): "
                            f"{_clip(row.get('summary'), 140)}"))
    return out


def read_approvals(path) -> list:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return from_approvals(doc if isinstance(doc, list) else [])


def read_lessons(db_path, since: float) -> list:
    """Lessons in the shared namespace still recurring since ``since``: one sighting each,
    weighted by how often it has recurred."""
    if not db_path or not Path(db_path).is_file():
        return []
    out = []
    try:
        con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT ts, text, meta FROM memories WHERE namespace='lessons' "
                               "AND kind='lesson' AND active=1").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    for ts, text, meta in rows:
        try:
            m = json.loads(meta or "{}")
        except ValueError:
            m = {}
        last = float(m.get("last_seen") or ts or 0)
        seen = int(m.get("seen") or 1)
        if last < since or NOT_A_FAULT in str(text) or PASSING.search(str(text)):
            continue
        worker = str(text).split(" \u00b7 ", 1)[0].strip()[:60] if " \u00b7 " in str(text) \
            else "lessons"
        out.append(Sighting("lessons", worker, str(text), last,
                            f"{iso(last)} lesson (seen {seen} times since {iso(ts)}): "
                            f"{_clip(text, 200)}", weight=seen, first=float(ts or last)))
    return out


# ---- where a fix would go, and why it is proposed --------------------------------------------
def _fragments(raw: str) -> list:
    parts = re.split(r"'[^']*'|\"[^\"]*\"|\d+|[\[\]():;,]", raw or "")
    frags = sorted({p.strip(" .-") for p in parts if len(p.strip(" .-")) >= 14},
                   key=len, reverse=True)
    return frags[:4]


def locate(repo, c: Cluster, *, limit: int = 4) -> dict:
    """Where a fix would go, read from the owner's repo: ``targets`` - the unprotected source
    files that hold this reason's own words (the check that says it) or name the worker, and
    the catalogue where a crew worker's settings live - and ``protected`` - the PROTECTED files
    that hold its words. When the words are found only in protected files (the Etsy listing
    rules, the content check...), no agent may fix it: ``needs_human`` says so before anything
    is cloned or written."""
    root = Path(repo) / "src" / "pionir" if repo else None
    if root is None or not root.is_dir():
        return {"targets": [], "protected": [], "needs_human": None}
    frags = _fragments(c.example)
    texts = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            texts[path.relative_to(Path(repo)).as_posix()] = path.read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
    extra = protected_closure(texts)        # what protected code imports is protected too
    scored, guarded = [], []
    for rel, text in texts.items():
        words = sum(3 for f in frags if f in text)
        if protected_why(rel, extra) or allowed_why_not(rel):
            if words:
                guarded.append(rel)
            continue
        score = words + (1 if c.worker in text else 0)
        if score:
            scored.append((-score, -words, rel))
    ranked = sorted(scored)
    found = [rel for _score, _words, rel in ranked[:limit]]
    worded = [rel for _score, words, rel in ranked if words]
    needs = None
    if guarded and not worded:
        needs = (f"needs a human: it touches a protected rule ({', '.join(guarded[:3])} - "
                 f"{protected_why(guarded[0], extra) or allowed_why_not(guarded[0])})")
    if c.source in ("outputs", "runs", "vitals") and "." in c.worker:
        found.append("src/pionir/crew/catalogue.json")
    return {"targets": found, "protected": guarded[:4], "needs_human": needs}


def find_targets(repo, c: Cluster, *, limit: int = 4) -> list:
    return locate(repo, c, limit=limit)["targets"]


def _agent(c: Cluster) -> str:
    """What a cluster is about, as one word: a lesson's agent ("printify" of "printify ·
    ..."), else the worker's last part."""
    return c.worker.split(".")[0].lower()


def _same_failure(a: Cluster, b: Cluster) -> bool:
    """Two clusters that are one failure seen twice: the same worker with one reason inside
    the other ("committed nothing" / "committed nothing new"), or a lesson about an agent and a
    worker's own record naming that agent over the same days (the Printify catalog failure is
    both a failed run of etsy.pod and a recurring lesson)."""
    overlap = a.first_seen <= b.last_seen and b.first_seen <= a.last_seen
    if a.worker == b.worker and (a.reason in b.reason or b.reason in a.reason):
        return True
    for lesson, other in ((a, b), (b, a)):
        if lesson.source == "lessons" and other.source != "lessons" and overlap:
            agent = _agent(lesson)
            if agent and agent != "lessons" and (agent in other.reason or
                                                 agent in other.worker.lower()):
                return True
    return False


def merge_duplicates(clusters: list) -> list:
    """One cluster per failure: a duplicate (``_same_failure``) is folded into the cluster
    seen most - the count is the larger of the two (the same events, not more of them), the
    first and last sightings the widest, and its key kept in ``also`` so dedupe sees both."""
    out: list = []
    for c in sorted(clusters, key=lambda c: (-c.count, -c.last_seen, c.key)):
        into = next((k for k in out if _same_failure(k, c)), None)
        if into is None:
            c.also = []
            out.append(c)
            continue
        into.first_seen = min(into.first_seen, c.first_seen)
        into.last_seen = max(into.last_seen, c.last_seen)
        into.also.append(c.key)
    return out


def hypothesis(c: Cluster, targets: list, days: int) -> str:
    who = {"approvals": f"Ian denied {c.worker}", "lessons": f"The lesson about {c.worker} "
           "recurred", "builds": "Night builds failed"}.get(c.source, f"{c.worker} was stopped")
    where = f" Likely places: {', '.join(targets)}." if targets else ""
    return (f"{who} {c.count} times in {days} days for the same reason (\"{_clip(c.example, 160)}"
            f"\"), from {iso(c.first_seen)} to {iso(c.last_seen)}. The same refusal repeating "
            "identically means either the rule is a false alarm on legitimate work, or the "
            "worker keeps producing what the rule rightly refuses without adapting (a prompt, a "
            "setting or a code path that never learns from the refusal). A fix makes it stop "
            "recurring without weakening any protection that is right." + where)
