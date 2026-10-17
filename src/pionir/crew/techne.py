"""The Techne watch: a site preview is ready, so the owner is told - once.

Techne is Scrooge's site-delivery pipeline. A job the owner (or the doers) moves along
reaches ``preview_ready`` when a preview site is up and needs the owner's APPROVAL before it
can go live. This worker is the nudge: each run it reads the jobs through Pionir
(``techne.jobs``, PII-redacted and read-only) and, for each job newly in ``preview_ready``,
sends the owner ONE alert (``owner.notify``, kind ``alert``) so he knows a preview is
waiting on his Desk.

It only ever READS and NOTIFIES. It never moves a job and never approves a preview: the
approval is the owner's, and this worker holds no ``techne.run``. The approve/reject steps
are not even reachable (Scrooge 403s the ops token for them), so nothing here can take
them.

**Once, never twice.** Every job this worker has alerted on is in its record
(``delivery.techne.json``); a job already pinged is never pinged again, even as it moves on
and (rarely) returns to ``preview_ready``. A job that is still ``preview_ready`` on the next
run but was already pinged gets nothing. A genuinely NEW ``preview_ready`` job pings.
"""
from __future__ import annotations

from .blog import _Unreadable, read_record, record_path, save_record
from .hands import Job
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base

TECHNE_JOBS = "techne.jobs"
OWNER_NOTIFY = "owner.notify"
PREVIEW_READY = "preview_ready"
MAX_PINGS_PER_RUN = 4          # owner.notify is itself rate-limited; this caps a burst


def _jobs_of(result) -> list | None:
    """The job list from Pionir's answer to techne.jobs, or None when it holds none."""
    for doc in (result, result.get("result") if isinstance(result, dict) else None):
        if isinstance(doc, dict) and isinstance(doc.get("jobs"), list):
            return None if doc.get("ok") is False else doc["jobs"]
    return None


def _label(job: dict) -> str:
    for key in ("slug", "domain", "id"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return "a Techne job"


def _alert(job: dict) -> dict:
    """The owner.notify payload for a preview that is ready. Fixed text: no model, nothing
    a client wrote (the brief and the client's details are never read here)."""
    label = _label(job)
    return {
        "kind": "alert",
        "title": "Techne: a site preview is ready",
        "body_text": f"Techne: a site preview is ready for your approval - {label}",
    }


class TechneWatchWorker(_Base):
    """``delivery.techne``: a preview is ready, so the owner is alerted once."""

    record_what = "the Techne watch's own record of which previews it has alerted on"

    def record_path(self, state_dir):
        return record_path(state_dir, self.worker_id)

    @staticmethod
    def _blank() -> dict:
        return {"pinged": []}

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        return self._blank() if doc is None else {**self._blank(), **doc}

    def save(self, state_dir, rec: dict) -> None:
        save_record(self.record_path(state_dir), rec)

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the watch cannot keep "
                             "its record, and without it could alert the owner twice",
                             retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: the jobs are read and the "
                             "owner alerted only through Pionir", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); refusing "
                             "to alert, since it could alert the owner twice", retryable=False)
        got = self._read_jobs(ctx)
        if isinstance(got, Err):
            return got          # nothing seen, nothing done: never "no previews ready"
        jobs = got.value
        pinged = set(rec["pinged"])
        events: list = []
        sent = 0
        for job in jobs:
            if job.get("status") != PREVIEW_READY:
                continue
            jid = job.get("id")
            if not isinstance(jid, str) or not jid.strip() or jid in pinged:
                continue
            if sent >= MAX_PINGS_PER_RUN:
                continue
            out = ctx.job(Job(OWNER_NOTIFY, _alert(job),
                              what=f"alert the owner that Techne job {jid} has a preview ready"))
            sent += 1
            if out.status == "done":
                pinged.add(jid)
                rec["pinged"].append(jid)
                log.info("%s: alerted the owner that job %s has a preview ready",
                         self.worker_id, jid)
                events.append(self._event(ctx, "techne.preview_ready",
                                          {"job_id": jid, "label": _label(job)}))
            else:
                # not sent (Discord down, owner.notify rate-limited, unreachable): NOT recorded,
                # so it is offered again next run rather than lost
                log.warning("%s: could not alert the owner about job %s (%s: %s)",
                            self.worker_id, jid, out.status, out.error)
                events.append(self._event(ctx, "techne.not_alerted",
                                          {"job_id": jid, "status": out.status,
                                           "why": (out.error or "")[:160]}))
        self.save(ctx.state_dir, rec)
        ready = sum(1 for j in jobs if j.get("status") == PREVIEW_READY)
        return Ok((*events, make_output(
            self, valid_at=ctx.now, observed_at=ctx.now,
            kind="techne.tally",
            payload={"jobs_seen": len(jobs), "preview_ready": ready, "alerted": sent},
            entities=self.entities,
            provenance={"source": "real"},
        )))

    def _read_jobs(self, ctx: WorkContext) -> Result:
        out = ctx.job(Job(TECHNE_JOBS, {}, what="read the Techne jobs"))
        if out.status == "done":
            listed = _jobs_of(out.result)
            if listed is None:
                return self._err(ErrorKind.MALFORMED, "Pionir answered techne.jobs without a "
                                 "job list; the jobs are UNKNOWN, not none")
            return Ok([j for j in listed if isinstance(j, dict)])
        why = out.error or f"Pionir said {out.status}"
        if out.status == "failed" and ("not configured" in why.lower()
                                       or "no capability" in why.lower()
                                       or "not set up" in why.lower()):
            return self._err(ErrorKind.NOT_CONFIGURED, f"techne.jobs is not set up in Pionir "
                             f"({why[:160]}); the jobs are UNKNOWN, not none", retryable=False)
        return self._err(ErrorKind.UNAVAILABLE, f"could not read the Techne jobs ({out.status}: "
                         f"{why[:160]}); they are UNKNOWN, not none")

    def _event(self, ctx: WorkContext, kind: str, payload: dict):
        return make_output(self, valid_at=ctx.now, observed_at=ctx.now, kind=kind,
                           payload=payload, entities=self.entities,
                           provenance={"source": "real"})
