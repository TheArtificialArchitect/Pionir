"""A crew member: drives, mood, skills, private memory, choice - and real work.

Ported from Hearth's agent. Everything here is deterministic Python; the model is
consulted only through the brain's queue (an intention's wording, a day's reflection,
a line in a conversation) and never inside the tick. The agent chooses by relief
utility over its drives, works on a project whose progress is read from the world,
drives Pionir through ``Job`` commands, and writes all of it into its own store. It
never reads another agent's memory and never records a thing that did not happen.

Dropped from Hearth with the house: walking (``_step_goto``), rooms and doors, sight,
sound and noticing, the order drive, sleeping in a bed, notes on the table. Working
hours replace sleep: off the clock an agent does nothing and nothing drains.

The founding rule, carried over: everything recorded is real. In particular
``_on_job`` is the ONE place a job's outcome enters the agent's record:

    done              a ``did`` episode (evidence the critic accepts for "I sent...")
                      and a ``result`` episode (source ``seen``) holding what Pionir
                      reported, figures included - the only figures the agent may state
    pending_approval  a ``job_pending`` episode that says it has NOT run
    running           a ``job_running`` episode: handed off, no result yet
    failed/unreachable a ``tried`` episode - an attempt, never a deed
"""
from __future__ import annotations

import json
import math
import random
import re
from datetime import datetime
from pathlib import Path

from . import projects
from .actions import LIBRARY, Job, Until, Wait
from .affect import Affect
from .drives import DRIVES, Drives
from .grounding import entities_in
from .hands import JobOutcome
from .log import log, safe
from .memory import Memory
from .skills import Skills
from .thinking import think as think_thought

# All intervals are real (wall-clock) seconds; Hearth's were game seconds at one tick
# each, so the numbers carry over as they were and are flagged for retuning after the
# crew has been watched for a while.
EVAL_EVERY = 30            # seconds between choices when idle
THINK_EVERY = 600
FOLD_EVERY = 6 * 3600
LEDGER_EVERY = 600
SAMPLE_EVERY = 3600
MAX_OPEN_INTENTIONS = 4
INTEND_COOLDOWN = 3600
INTENTION_EXPIRES = 2 * 86400
# a job whose outcome never came back (the hands stopped mid-job) is written off after this;
# long past Hands' own follow window, so a real answer is never overtaken
JOB_GIVE_UP = 3600
REFLECT_HOUR = 23          # an always-on agent reflects once, at this local hour
REFLECT_RETRY = 600        # a day with too little in it is looked at again this often, not every tick


class Agent:
    def __init__(self, member, sim, state_dir: Path) -> None:
        t = member.temperament
        self.t = t
        self.id = t.id
        self.name = t.name
        self.role = member.role
        self.channels = tuple(member.channels)
        self.sim = sim
        self.mem = Memory(Path(state_dir) / "agents" / f"{t.id}.db", t.id)
        # Built once, here, and never replaced. Hearth built Skills and sixteen lines later
        # overwrote it with None; every use sat behind a guard that the object existed, so the
        # whole module was dead for the life of the house. There is no such guard here.
        self.skills = Skills(self.mem, t.id)
        self.project = projects.Project.from_dict(self.mem.get("project", None))
        self.drives = Drives(t)
        self.affect = Affect(t)
        self.rng = random.Random(f"{t.id}:{sim.store.get('born_real', 0)}")
        self.doing = "idle"
        self.action = None
        self.action_name: str | None = None
        self.action_channel = self.channels[0]
        self.action_relief_before: dict = {}
        self.action_started_t = 0
        self.gen = None
        self.cmd = None
        self.cmd_result = None
        self.wait_left = 0.0
        self.job_token = 0                 # which Job the outstanding callback belongs to
        self.job_waiting_since = 0
        self.conversation = None
        self.speech_shown: tuple | None = None
        self.last_eval_t = 0
        self.last_think_t = 0
        self.last_fold_t = 0
        self.last_ledger_t = 0
        self.last_sample_t = 0
        self.last_intend_t = 0
        self.last_failure: str | None = None
        self.last_failure_t = -10 ** 9
        self.was_working = False
        self.intend_pending = False
        self.reflection_pending = False
        self.worked_day = 0                # local midnight of the last day this agent worked
        self.last_reflect_try_t = -10 ** 9
        self._restore()

    # ---- persistence ----------------------------------------------------
    CLOCKS = ("last_intend_t", "last_think_t", "last_ledger_t", "last_sample_t",
              "last_fold_t", "last_failure_t")

    def _restore(self) -> None:
        st = self.mem.get("state")
        if not st:
            self.mem.set("born_t", self.sim.clock.t)
            log.info("%s joins the crew (%s)", self.name, ", ".join("#" + c for c in self.channels))
        else:
            self.drives.load_dict(st.get("drives", {}))
            self.affect.load_dict(st.get("affect", {}))
            for k, v in (st.get("clocks") or {}).items():
                if k in self.CLOCKS:
                    setattr(self, k, int(v))
            log.info("%s resumes", self.name)
        if self.project is not None and self.project.kind(self.sim) is None:
            # the Kind it was on is no longer on this crew: said, not silently dropped
            log.warning("%s was on %r, which this crew no longer offers; letting it go",
                        self.name, self.project.key)
            self._note("project", f"stopped working on {self.project.key}: it is no longer "
                                  f"offered", salience=1.0, detail={"project": self.project.key})
            self.project = None
            self.mem.set("project", None)

    def checkpoint(self) -> None:
        self.mem.set("state", {
            "drives": self.drives.to_dict(), "affect": self.affect.to_dict(),
            "clocks": {k: getattr(self, k) for k in self.CLOCKS},
            "saved_t": self.sim.clock.t,
        })
        self.skills.age_all(self.sim.clock.t)      # unpractised things fade even unchosen
        self.skills.save()
        self.mem.set("project", self.project.to_dict() if self.project else None)
        self.mem.flush()

    def close(self) -> None:
        self.checkpoint()
        self.mem.close()

    # ---- small helpers ----------------------------------------------------
    def working(self, sim=None) -> bool:
        return self.t.working((sim or self.sim).clock)

    def _note(self, kind: str, text: str, *, source: str = "did", salience: float = 0.5,
              detail: dict | None = None, people: list | None = None,
              channel: str | None = None) -> int:
        return self.mem.add_episode(self.sim.clock.t, channel or self.action_channel, kind, text,
                                    actor=self.id, salience=salience, source=source,
                                    detail=detail, people=people)

    def meet(self, other, channel: str) -> None:
        """Face to face in a channel: from now on this colleague is someone I know."""
        p = self.mem.person(other.id)
        p["last_seen_t"] = self.sim.clock.t
        p["last_seen_channel"] = channel
        self.mem.save_person(p)

    def note_trust(self, other: str, delta: float, why: str) -> None:
        """Trust moves when an account of theirs meets my own eyes - up as well as down."""
        p = self.mem.person(other)
        p["trust"] = max(-1.0, min(1.0, p["trust"] + delta))
        self.mem.save_person(p)
        self.mem.bump("trust_up" if delta > 0 else "trust_down")
        who = self.sim.agent(other)
        self.mem.add_episode(self.sim.clock.t, None, "verified",
                             f"{who.name if who else other} {why}", people=[other],
                             salience=0.9 * self.t.memory_fidelity, source="noticed",
                             detail={"trust": round(delta, 3)})

    # ---- projects -------------------------------------------------------
    def project_progress(self, sim) -> float:
        """Always recomputed from external truth, never stored, so it cannot drift."""
        if self.project is None:
            return 0.0
        kind = self.project.kind(sim)
        if kind is None:
            return 0.0
        try:
            return max(0.0, min(1.0, float(kind.progress(self, sim))))
        except Exception as exc:  # noqa: BLE001 - logged; unreadable progress is not progress
            log.warning("%s: could not measure %s: %s: %s", self.name, self.project.key,
                        type(exc).__name__, exc)
            return 0.0

    def note_project_step(self, sim, before: float, after: float, result: str) -> None:
        """One step taken. Purpose is relieved ONLY by real movement between two readings of
        the world, so a step that achieved nothing is felt as nothing - which is how giving
        up begins."""
        p = self.project
        if p is None:
            return
        title = p.title(sim)
        p.steps += 1
        moved = after - before
        detail = {"project": p.key, "before": round(before, 4), "after": round(after, 4)}
        if moved > 0.001:
            p.last_progress_t = sim.clock.t
            p.real_steps += 1
            p.best = max(p.best, after)
            self.drives.relieve_purpose(projects.STEP_RELIEF * min(1.0, moved * 4 + 0.35))
            self.affect.push("contentment", 0.6, 0.15)
            self.mem.bump("project_steps")
            self._note("did_own", f"you {result} ({title})", salience=0.7 + 1.2 * moved,
                       detail=detail)
        else:
            self.mem.bump("project_steps_empty")
            self._note("project", f"worked on {title}: {result} - nothing changed",
                       salience=0.5, detail=detail)
        kind = p.kind(sim)
        if kind is not None and safe(f"agent.{self.id}.done", lambda: kind.done(self, sim),
                                     default=False):
            self.finish_project(sim)

    def finish_project(self, sim) -> None:
        p = self.project
        if p is None:
            return
        title = p.title(sim)
        # paid for the work actually done: progress is read from the shared world, so whoever
        # holds the project when it completes is not necessarily who did it
        share = min(1.0, 0.2 + 0.16 * p.real_steps)
        mine = p.real_steps > 0
        self.drives.relieve_purpose(projects.FINISH_RELIEF * share)
        self.affect.push("contentment", 0.5 + 0.4 * share, 0.2 + 0.3 * share)
        self._note("project", f"finished: {title}" if mine
                   else f"it got done without me: {title}",
                   salience=1.4 + 1.2 * share, detail={"project": p.key})
        self.mem.bump("projects_finished")
        log.info("%s finished a project: %s", self.name, title)
        self.project = None
        self.mem.set("project", None)

    def maybe_take_something_on(self, sim) -> None:
        """Start something, or let go of something that is not happening. Looking and
        finding nothing achievable is counted as a streak the vitals read, so a crew with
        no real work to do says so instead of idling quietly."""
        p = self.project
        stale = float(getattr(sim.cfg, "project_stale_seconds", projects.DEFAULT_STALE_SECONDS))
        if p is not None:
            if sim.clock.t - p.last_progress_t > stale:
                title = p.title(sim)
                self._note("project", f"gave up on it: {title}", salience=2.0,
                           detail={"project": p.key})
                self.mem.bump("projects_abandoned")
                self.mem.set("gave_up." + p.key, sim.clock.t)
                log.info("%s gave up on: %s (no progress for %.1f h)", self.name, title,
                         (sim.clock.t - p.last_progress_t) / 3600.0)
                self.affect.push("irritation", 0.45, 0.10 + 0.3 * self.t.order_sensitivity)
                self.project = None
                self.mem.set("project", None)
            return
        if self.drives.error("purpose") < 0.10:
            return
        picked = safe(f"agent.{self.id}.choose_project", lambda: projects.choose(self, sim))
        if picked is None:
            streak = int(self.mem.get("no_project_streak", 0)) + 1
            self.mem.set("no_project_streak", streak)
            self.mem.bump("project_searches_empty")
            return
        self.mem.set("no_project_streak", 0)
        self.project = picked
        self.mem.set("project", picked.to_dict())
        self.mem.bump("projects_started")
        self._note("project", f"decided to take on: {picked.title(sim)}", salience=1.8,
                   detail={"project": picked.key})
        log.info("%s has taken something on: %s", self.name, picked.title(sim))

    # ---- the tick -------------------------------------------------------
    def act(self, sim) -> None:
        t = sim.clock.t
        working = self.working(sim)
        dt = max(0.0, float(getattr(sim, "dt", 1.0)))
        self.drives.tick(dt, working)
        self.affect.decay(dt)
        if not working:
            if self.was_working:
                self.was_working = False
                self._off_the_clock(sim)
            if self.action is None:
                self.doing = "off"
            elif self.cmd is not None and isinstance(self.cmd, Job):
                self._drive(sim)          # an outstanding job still settles off the clock
            safe(f"agent.{self.id}.reflect", lambda: self.maybe_reflect(sim))
            self._housekeeping(sim, t)
            return
        self.was_working = True
        self.worked_day = sim.clock.day_start_t()
        if self.action is None and (t - self.last_eval_t >= EVAL_EVERY or self.last_eval_t == 0):
            self.last_eval_t = t
            self._choose(sim)
        if self.action is not None:
            self._drive(sim)
        if t - self.last_think_t >= THINK_EVERY / (0.6 + 0.8 * self.t.curiosity):
            self.last_think_t = t
            safe(f"agent.{self.id}.think", lambda: self._think(sim))
            safe(f"agent.{self.id}.project", lambda: self.maybe_take_something_on(sim))
        if self.t.always_on and sim.clock.hour == REFLECT_HOUR:
            safe(f"agent.{self.id}.reflect", lambda: self.maybe_reflect(sim))
        if t - self.last_sample_t >= SAMPLE_EVERY:
            self.last_sample_t = t
            self.drives.sample()
            lonely = min(1.0, self.drives.error("social") * 2.5)
            if lonely > 0.15:
                self.affect.push("loneliness", lonely, 0.12 + 0.35 * self.t.sociability)
            elif self.drives.error("purpose") > 0.2:
                self.affect.push("edge", 0.4, 0.10 + 0.20 * self.t.initiative)
            else:
                self.affect.push("contentment", 0.6, 0.10)
            day = sim.clock.day_start_t()
            if self.mem.get("drift_day", 0) != day:
                self.mem.set("drift_day", day)
                moved = self.drives.drift()
                if moved:
                    log.info("%s setpoints drifted: %s", self.name, moved)
                    self.mem.bump("setpoint_drifts")
        self._housekeeping(sim, t)

    def _housekeeping(self, sim, t: int) -> None:
        if t - self.last_ledger_t >= LEDGER_EVERY:
            safe(f"agent.{self.id}.ledger", lambda: self._decay_ledger(sim))
        if t - self.last_fold_t >= FOLD_EVERY:
            self.last_fold_t = t
            removed = self.mem.fold(t)
            if removed:
                log.info("%s folded %d old episodes", self.name, removed)

    def _off_the_clock(self, sim) -> None:
        """Working hours are over. A conversation cannot go on; work in hand stops (an
        outstanding Job still settles when its answer comes)."""
        if self.action is not None and not isinstance(self.cmd, Job):
            self._finish(sim, "stopped: the working day was over", failed=False,
                         interrupted=True)

    # ---- choosing -------------------------------------------------------
    def _choose(self, sim) -> None:
        cands = []
        hour = sim.clock.hour
        for act in LIBRARY:
            gains = safe(f"feasible.{act.name}", lambda act=act: act.feasible(self, sim))
            if gains is None:
                continue
            u = self.drives.relief(gains)
            u += self.mem.habit_bonus(act.name, self.action_channel, hour, sim.clock.t)
            # what they have come to like, earned only by having done it
            u += 0.09 * self.skills.affinity(act.name)
            if act.name == "stay":
                u = 0.02 - 0.03 * self.t.restlessness      # the restless cannot sit idle
            cands.append((u, act))
        if not cands:
            return
        self.mem.bump("choices")
        temp = 0.04 + 0.25 * self.t.impulsivity
        m = max(u for u, _ in cands)
        weights = [math.exp((u - m) / temp) for u, _ in cands]
        pick = self.rng.choices(cands, weights=weights, k=1)[0][1]
        self._start(pick, sim)

    def _start(self, act, sim) -> None:
        self.action = act
        self.action_name = act.name
        self.action_started_t = sim.clock.t
        self.action_relief_before = self.drives.errors()
        self.gen = act.run(self, sim)
        self.cmd = None
        self.cmd_result = None
        self.doing = "working" if act.name != "stay" else "idle"
        self.mem.bump("actions_started")
        self.mem.bump(f"action.{act.name}")

    def _finish(self, sim, result: str, failed: bool, interrupted: bool = False) -> None:
        name = self.action_name or "?"
        after = self.drives.errors()
        before = self.action_relief_before or after
        relieved = sum(self.drives.weight[k] * (before[k] - after[k]) for k in DRIVES) > 0.01
        if interrupted:
            self.mem.bump("interrupted")
        elif failed:
            self.mem.bump("actions_failed")
            self.mem.bump(f"failed.{name}")
            if name != "stay":
                self.skills.did(name, sim.clock.t, helped=False, failed=True)
            key = f"tried to {name}: {result}"
            if key != self.last_failure or sim.clock.t - self.last_failure_t > 1800:
                self._note("tried", key, salience=0.9)
            else:
                self.mem.bump("failures_repeated")
            self.last_failure, self.last_failure_t = key, sim.clock.t
        elif name == "stay":
            self.mem.bump("idled")               # honest idleness is not a thing done
        else:
            self.mem.bump("actions_done")
            if relieved:
                self.affect.push("contentment", 0.65, 0.12 + 0.18 * self.t.resilience)
            self.skills.did(name, sim.clock.t, helped=relieved, failed=False)
            self.mem.habit_hit(name, self.action_channel,
                               (self.action_started_t % 86400) // 3600, sim.clock.t, relieved)
        self.action = None
        self.action_name = None
        self.gen = None
        self.cmd = None
        self.cmd_result = None
        self.doing = "idle"
        self.last_eval_t = sim.clock.t - EVAL_EVERY      # choose again next tick
        self.last_doing_result = result

    # ---- driving the generator -----------------------------------------
    def _drive(self, sim) -> None:
        if self.gen is None:
            return
        for _ in range(3):          # a few instant commands per tick
            if self.cmd is None:
                try:
                    self.cmd = self.gen.send(self.cmd_result)
                except StopIteration as done:
                    result = str(done.value or "done")
                    self._finish(sim, result, failed=result.startswith("failed"))
                    return
                self.cmd_result = None
                if isinstance(self.cmd, Job):
                    self._submit_job(sim, self.cmd)
                elif isinstance(self.cmd, Wait):
                    secs = float(self.cmd.seconds)
                    if self.cmd.skilled:
                        secs = max(1.0, secs * self.skills.speed(self.action_name))
                    self.wait_left = secs
                    self.doing = self.cmd.label
                elif isinstance(self.cmd, Until):
                    self.wait_left = float(self.cmd.max_seconds)
                    self.doing = self.cmd.label
                else:
                    log.warning("%s: %s yielded %r, which is not a command; stopping it",
                                self.name, self.action_name, self.cmd)
                    self._finish(sim, "failed: yielded something that is not a command",
                                 failed=True)
                    return
            if isinstance(self.cmd, Job):
                if sim.clock.t - self.job_waiting_since > JOB_GIVE_UP:
                    self._on_job(self.job_token, self.cmd, JobOutcome(
                        "unreachable", self.cmd.capability,
                        error=f"no answer came back in {JOB_GIVE_UP // 60} minutes"))
                    continue
                return                                   # the outcome arrives by callback
            dt = max(0.0, float(getattr(sim, "dt", 1.0)))
            if isinstance(self.cmd, Wait):
                self.wait_left -= dt
                if self.wait_left <= 0:
                    self.cmd = None
                    self.cmd_result = True
                    continue
                return
            if isinstance(self.cmd, Until):
                self.wait_left -= dt
                ok = bool(safe(f"agent.{self.id}.until", lambda: self.cmd.pred(self, sim),
                               default=False))
                if ok or self.wait_left <= 0:
                    self.cmd = None
                    self.cmd_result = ok
                    continue
                return
            return

    # ---- jobs: the only way an agent touches the world ---------------------
    def _submit_job(self, sim, job: Job) -> None:
        self.job_token += 1
        token = self.job_token
        self.job_waiting_since = sim.clock.t
        self.doing = f"waiting on Pionir: {job.what}"
        self.mem.bump("jobs_asked")
        hands = getattr(sim, "hands", None)
        if hands is None:
            self._on_job(token, job, JobOutcome(
                "unreachable", job.capability, error="this crew has no hands (no Pionir client)"))
            return
        hands.submit(self.id, job, lambda out, token=token, job=job: self._on_job(token, job, out))

    def _on_job(self, token: int, job: Job, out: JobOutcome) -> None:
        """Record what really happened, then hand it back to the action. The ONE place a
        job's outcome enters this agent's record."""
        if token != self.job_token or self.cmd is not job:
            self.mem.bump("jobs_late_answer")
            log.warning("%s: an answer for %s came back after the agent had moved on (%s)",
                        self.name, job.capability, out.status)
            self._record_job(job, out, late=True)
            return
        self._record_job(job, out)
        self.cmd = None
        self.cmd_result = out

    def _record_job(self, job: Job, out: JobOutcome, late: bool = False) -> None:
        t = self.sim.clock.t
        base = {"capability": job.capability, "status": out.status, "task_id": out.task_id,
                "project": self.project.key if self.project else None}
        what = job.what
        if out.status == "done":
            self.mem.bump("jobs_done")
            eid = self._note("did", f"you had Pionir {what} ({job.capability}) and it ran",
                             salience=1.0, detail=base)
            answer = re.sub(r"\s+", " ", out.answer or "").strip()[:400]
            text = (f"Pionir reported for {what}: {answer}" if answer
                    else f"Pionir reported {what} ran, with nothing to show")
            self.mem.add_episode(t, self.action_channel, "result", text, source="seen",
                                 salience=1.2, detail={**base, "did_episode": eid,
                                                       "figures": list(out.figures),
                                                       "entities": sorted(entities_in(answer))})
            self.skills.did("job:" + job.capability, t, helped=True, failed=False)
            self.affect.push("contentment", 0.55, 0.10)
        elif out.status == "pending_approval":
            self.mem.bump("jobs_pending")
            self._note("job_pending", f"asked Pionir to {what} ({job.capability}); it is "
                                      f"waiting on Ian's approval and has NOT run",
                       salience=1.0, detail={**base, "approval_id": out.approval_id})
        elif out.status == "running":
            self.mem.bump("jobs_unfinished")
            self._note("job_running", f"asked Pionir to {what} ({job.capability}); it is "
                                      f"still running and nothing has come back yet",
                       salience=0.8, detail=base)
        else:
            self.mem.bump("jobs_failed")
            reason = (out.error or "no reason given")[:200]
            verb = "it failed" if out.status == "failed" else "Pionir did not answer"
            self._note("tried", f"tried to {what} ({job.capability}) but {verb}: {reason}",
                       salience=0.9 + 0.4 * self.t.order_sensitivity,
                       detail={**base, "error": reason})
            self.skills.did("job:" + job.capability, t, helped=False, failed=True)
            self.affect.push("irritation", 0.5, 0.08 + 0.25 * self.t.order_sensitivity)
        if late:
            self.mem.bump("jobs_recorded_late")

    # ---- thinking (deterministic) ---------------------------------------
    def _think(self, sim) -> None:
        t = sim.clock.t
        th = think_thought(self, sim)
        if th is None:
            return
        thought_about = set(self.mem.get("thought_about", []))
        thought_about.update(th.sources)
        self.mem.set("thought_about", sorted(thought_about)[-300:])
        tid = self.mem.add_thought(t, th.kind, th.text, th.about, th.urge, th.sources,
                                   th.topic, th.say)
        self.mem.bump("thoughts")
        self.mem.bump(f"thought.{th.kind}")
        if th.kind in ("doubt", "news"):
            self.affect.push("curiosity", 0.55 + 0.3 * self.t.curiosity,
                             0.15 + 0.3 * self.t.curiosity)
        elif th.kind in ("stalled", "trouble"):
            self.affect.push("irritation", 0.5, 0.06 + 0.3 * self.t.order_sensitivity)
        elif th.kind == "quiet":
            self.affect.push("loneliness", 0.5, 0.08 + 0.3 * self.t.sociability)
        self._maybe_intend(sim, tid, th)
        self._expire_intentions(sim)

    # ---- volition -------------------------------------------------------
    def _colleagues(self, sim) -> list:
        """Colleagues I have met and share a channel with - the ones I could take it to."""
        out = []
        for p in self.mem.people():
            other = sim.agent(p["other"])
            if other is not None and set(other.channels) & set(self.channels):
                out.append(other)
        return out

    def _maybe_intend(self, sim, thought_id: int, th) -> None:
        """A thought worth saying may become an intention: something to take up with a
        colleague. The brain phrases the want (temperature 0, JSON schema); everything
        gating it is arithmetic, and the answer is validated before anything is kept."""
        t = sim.clock.t
        if not th.say or th.urge < 0.2 or self.intend_pending or sim.brain is None:
            return
        if t - self.last_intend_t < INTEND_COOLDOWN:
            return
        if len(self.mem.open_intentions()) >= MAX_OPEN_INTENTIONS:
            self.mem.bump("intend_capped")
            return
        if self.rng.random() > 0.25 + 0.75 * self.t.initiative:
            return
        known = self._colleagues(sim)
        if not known:
            return
        self.last_intend_t = t
        about = sim.agent(th.about) if th.about else None
        system = (
            f"You are {self.name}. {self.t.disposition}. You work on a small business team; "
            f"your job: {self.role}. You know only what is stated here. Decide, as {self.name}, "
            f"whether this thought is worth taking up with one of your colleagues. Answer only "
            f"in JSON."
        )
        user = (
            f"Your thought: {th.text}\n"
            + (f"It concerns {about.name}.\n" if about else "")
            + f"Colleagues you could take it to: {', '.join(o.name for o in known)}.\n"
            f"You feel {self.affect.describe()}.\n"
            'Reply with {"intend": true|false, "want": "<one short sentence naming the specific '
            'thing you want to ask or say, or empty>", "target": "<one colleague name, or '
            'empty>", "kind": "ask|tell|raise|share"}'
        )
        schema = {"type": "object",
                  "properties": {"intend": {"type": "boolean"}, "want": {"type": "string"},
                                 "target": {"type": "string"}, "kind": {"type": "string"}},
                  "required": ["intend", "want", "target", "kind"]}
        self.intend_pending = True
        self.mem.bump("model_calls")
        rid = sim.brain.request(self.id, "intend", system, user,
                                {"temperature": 0.0, "num_predict": 80, "seed": 7},
                                lambda text_, meta, err, tid=thought_id:
                                    self._on_intend(sim, tid, text_, err),
                                priority=1, fmt=schema)
        if rid is None:
            self.intend_pending = False

    def _on_intend(self, sim, thought_id: int, text, err) -> None:
        self.intend_pending = False
        if err or not text:
            if err:
                self.mem.bump("intend_no_words")   # expired or failed: the want never formed
            return
        try:
            d = json.loads(text)
        except ValueError:
            self.mem.bump("intend_unparsed")
            log.info("%s: the intention came back unparsable: %r", self.name, str(text)[:120])
            return
        if not isinstance(d, dict) or not d.get("intend") \
                or len(str(d.get("want") or "").strip()) < 4:
            self.mem.bump("intend_declined")
            return
        target = None
        wanted = str(d.get("target") or "").strip().lower()
        for other in self._colleagues(sim):
            if other.name.lower() == wanted:
                target = other
        if target is None:
            self.mem.bump("intend_no_target")
            return
        want = str(d["want"]).strip()[:160]
        # the want is the model's words: it goes through the same critic as speech, or a
        # confabulated figure or name would be kept in the store as a plan
        talk = getattr(sim, "talk", None)
        if talk is not None and talk.critic(self, want) is None:
            self.mem.bump("confab_dropped")
            return
        kind = str(d.get("kind") or "ask").strip().lower()
        if kind not in ("ask", "tell", "raise", "share"):
            kind = "ask"
        iid = self.mem.add_intention(sim.clock.t, want, kind, target.id, thought_id, kind)
        if iid is not None:
            self.mem.bump("intentions_formed")
            self.mem.set("last_intention_t", sim.clock.t)
            self.mem.add_episode(sim.clock.t, None, "intended",
                                 f"decided: {want} ({target.name})", people=[target.id],
                                 salience=0.7, source="did", detail={"intention": iid})

    def _expire_intentions(self, sim) -> None:
        for it in self.mem.open_intentions():
            if sim.clock.t - it["t"] > INTENTION_EXPIRES:
                self.mem.resolve_intention(it["id"], "let_go",
                                           "it stopped mattering after two days", sim.clock.t)
                self.mem.bump("intentions_let_go")

    def pursuable_intention(self, sim) -> dict | None:
        for it in self.mem.open_intentions():
            if it["next_try_t"] <= sim.clock.t and it["target"]:
                return it
        return None

    # ---- reflection: once a working day, over the day's own episodes -----
    def maybe_reflect(self, sim) -> None:
        day = sim.clock.day_start_t()
        if sim.brain is None or self.reflection_pending or self.worked_day != day:
            return
        if sim.clock.t - self.last_reflect_try_t < REFLECT_RETRY:
            return
        self.last_reflect_try_t = sim.clock.t
        if self.mem.get("reflected_day", 0) == day:
            return
        eps = [e for e in self.mem.recent(300, since_t=day)
               if e["kind"] not in ("said", "heard_say", "intended", "reflection")]
        if len(eps) < 3:
            return
        eps.sort(key=lambda e: -e["salience"])
        eps = sorted(eps[:22], key=lambda e: e["t"])
        lines = [f"- {datetime.fromtimestamp(e['t']).astimezone().strftime('%H:%M')}: {e['text']}"
                 for e in eps]
        known = ", ".join(o.name for o in self._colleagues(sim)) or "nobody"
        system = (
            f"You are {self.name} ({self.t.they}/{self.t.them}). {self.t.disposition}. You work "
            f"on a small business team; your job: {self.role}. Colleagues you have met: {known}. "
            f"Your working day is over. Think back over it in two or three plain sentences, "
            f"first person, only about things in the list. Do not invent anything: no figure, "
            f"no name and no deed that is not in the list. Output the sentences only."
        )
        user = ("What happened to you today:\n" + "\n".join(lines)
                + f"\n\nYou feel {self.affect.describe()}.")
        self.reflection_pending = True
        self.mem.bump("model_calls")
        ids = [e["id"] for e in eps]
        rid = sim.brain.request(self.id, "reflect", system, user,
                                {"temperature": 0.6, "num_predict": 120,
                                 "seed": self.rng.randrange(1, 2 ** 31)},
                                lambda text, meta, err, ids=ids, day=day:
                                    self._on_reflect(sim, day, ids, text, err),
                                priority=1)
        if rid is None:
            self.reflection_pending = False

    def _on_reflect(self, sim, day: int, ids: list, text, err) -> None:
        self.reflection_pending = False
        self.mem.set("reflected_day", day)          # one attempt per day, whatever came of it
        if err or not text:
            return
        talk = getattr(sim, "talk", None)
        clean = talk.critic(self, text) if talk is not None else str(text).strip()
        if clean is None:
            self.mem.bump("confab_dropped")
            return
        # it must cite something real: some content word from the day's own episodes
        eps = {e["id"]: e for e in self.mem.recent(300, since_t=day)}
        words: set = set()
        for i in ids:
            if i in eps:
                words |= set(re.findall(r"[a-z]{4,}", eps[i]["text"].lower()))
        words -= {"there", "that", "with", "from", "into", "came", "left", "went", "them",
                  "then", "have", "were", "been", "your", "this"}
        if not (words & set(re.findall(r"[a-z]{4,}", clean.lower()))):
            self.mem.bump("confab_dropped")
            log.info("%s's reflection cited nothing real; dropped: %r", self.name, clean[:100])
            return
        self.mem.add_reflection(sim.clock.t, day, clean, ids)
        self.mem.add_episode(sim.clock.t, None, "reflection", clean, salience=1.5, source="did",
                             detail={"day": day, "episode_ids": ids})
        self.mem.bump("reflections")

    # ---- relationships ----------------------------------------------------
    def _decay_ledger(self, sim) -> None:
        """Every relationship value drifts back toward neutral. Nothing here is permanent."""
        dt = sim.clock.t - self.last_ledger_t
        self.last_ledger_t = sim.clock.t
        if dt <= 0:
            return
        f_g = math.exp(-dt / (3600.0 * 12.0 * (1.6 - self.t.resilience)))
        f_w = math.exp(-dt / (3600.0 * 72.0))
        for p in self.mem.people():
            changed = False
            for k, f in (("grievance", f_g), ("warmth", f_w), ("trust", f_w)):
                if abs(p[k]) > 1e-4:
                    p[k] *= f
                    changed = True
            if changed:
                self.mem.save_person(p)

    # ---- for the viewer -------------------------------------------------
    def vitals(self) -> dict:
        c = self.mem.counters()
        keys = ("choices", "actions_started", "actions_done", "actions_failed", "idled",
                "interrupted", "thoughts", "intentions_formed", "intentions_pursued",
                "utterances", "model_calls", "confab_dropped", "false_own_claim",
                "unbacked_figure", "unknown_entity", "unmet_name", "reflections",
                "jobs_asked", "jobs_done", "jobs_pending", "jobs_failed", "jobs_unfinished",
                "project_steps", "project_steps_empty", "projects_started",
                "projects_finished", "projects_abandoned", "project_searches_empty",
                "news_taken_in")
        out = {k: c.get(k, 0) for k in keys}
        out.update({"episodes": self.mem.episode_count(),
                    "drive_nonfinite": self.drives.nonfinite,
                    "affect_nonfinite": self.affect.nonfinite,
                    "store_bytes": self.mem.size_bytes()})
        return out

    def snapshot(self, sim) -> dict:
        last = self.mem.recent(1)
        its = self.mem.open_intentions()
        return {
            "id": self.id, "name": self.name, "role": self.role, "colour": self.t.colour,
            "pronouns": self.t.pronouns, "channels": list(self.channels),
            "working": self.working(sim), "doing": self.doing, "action": self.action_name,
            "project": ({"key": self.project.key, "title": self.project.title(sim),
                         "progress": round(self.project_progress(sim), 3),
                         "steps": self.project.steps} if self.project else None),
            "drives": self.drives.snapshot(), "mood": self.affect.describe(),
            "last_memory": last[0]["text"] if last else None,
            "intention": ({"want": its[0]["want"], "target": its[0]["target"],
                           "attempts": its[0]["attempts"]} if its else None),
            "vitals": self.vitals(),
            "skills": self.skills.to_dict(), "good_at": self.skills.best(),
            "talking_to": self.conversation.other(self.id) if self.conversation else None,
            "speech": ({"text": self.speech_shown[0], "t": self.speech_shown[1]}
                       if self.speech_shown and sim.clock.t - self.speech_shown[1] < 40
                       else None),
        }
