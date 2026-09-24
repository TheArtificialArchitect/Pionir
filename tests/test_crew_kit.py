"""Shared fakes for the crew's behaviour tests. It holds no tests itself.

(It is named ``test_crew_*`` only because this phase may add no other test files;
``crew_support.py`` is Phase 1a's and is left as it was.)

Nothing here reaches a model, a network or the real state root: the model is
``FakeOllama``, Pionir is ``FakePionir`` (scripted answers in Pionir's real shapes),
both clocks are one ``FakeTime`` hand, and every crew lives in a temporary directory.
"""

from __future__ import annotations

from crew_support import FakeOllama, FakeTime, settings, temp_dir

from pionir.crew.actions import BY_NAME, Job
from pionir.crew.cast import Member
from pionir.crew.clock import WallClock
from pionir.crew.crew import build
from pionir.crew.gpu import CardWatch
from pionir.crew.projects import Kind, Project
from pionir.crew.temperament import Temperament

BASE = {"sociability": 0.5, "curiosity": 0.5, "restlessness": 0.3, "initiative": 0.6,
        "order_sensitivity": 0.5, "memory_fidelity": 1.0, "people_weight": 1.0,
        "perception": 1.0, "impulsivity": 0.3, "reticence": 0.2, "speech_cap": 90,
        "resilience": 0.6, "dwell": 0.4,
        "work_start": 0.0, "work_end": 0.0}       # start == end: always at work


def member(aid: str, channels, **nums) -> Member:
    t = Temperament(id=aid, name=aid.capitalize(), pronouns=("they", "them", "their"),
                    disposition="does the work", colour="#888", **{**BASE, **nums})
    return Member(temperament=t, role=f"{aid}'s job", channels=tuple(channels))


# Pionir's real response shapes (server.py run_task / _execute_task)
def done(result, task_id="t-done") -> dict:
    return {"ok": True, "agent_id": "worker", "result": result, "evidence": [],
            "task_id": task_id}


PENDING = {"ok": False, "status": "pending_approval", "approval_id": "ap-1",
           "summary": "outreach · outreach.send", "note": "held for Ian's approval - it has not run",
           "task_id": "t-pending"}
FAILED = {"ok": False, "agent_id": "worker",
          "error": {"type": "PionirError", "message": "the mail relay refused the login"},
          "task_id": "t-failed"}


class FakePionir:
    """Answers ``run_task`` from a script keyed by capability. An answer may be a dict,
    an exception to raise, or a callable(payload) -> dict (for side effects on the fake
    world). ``records`` answers ``task(id)`` for jobs that report running."""

    def __init__(self, answers: dict | None = None) -> None:
        self.answers = dict(answers or {})
        self.calls: list = []
        self.polls: list = []
        self.records: dict = {}

    def run_task(self, capability, payload, *, permissions=(), wait=30.0):
        self.calls.append((capability, dict(payload), tuple(permissions)))
        ans = self.answers[capability]
        if isinstance(ans, Exception):
            raise ans
        if callable(ans):
            ans = ans(payload)
        return dict(ans)

    def task(self, task_id, *, wait=0.0):
        self.polls.append(task_id)
        return dict(self.records[task_id])


class World:
    """External truth a test Kind reads: what has really been sent. Only the fake Pionir
    changes it, and only when it reports the job ran."""

    def __init__(self, target: int = 10) -> None:
        self.sent = 0
        self.target = target


class SendKind(Kind):
    """A test Kind: progress is read from ``World`` on every call; a step asks Pionir."""
    key = "send_followups"
    title = "send the follow-ups"
    capability = "outreach.send"

    def __init__(self, world: World, suits_fn=None) -> None:
        self.world = world
        self._suits = suits_fn or (lambda agent: 0.5)

    def suits(self, agent) -> float:
        return self._suits(agent)

    def progress(self, agent, crew) -> float:
        return min(1.0, self.world.sent / self.world.target)

    def step(self, agent, crew):
        out = yield Job(self.capability, {"n": 1}, what="send one follow-up email")
        if out.ran:
            return "sent one follow-up"
        return f"failed: {out.status}"


class Crew:
    """A real, unstarted crew on fakes, driven by hand."""

    def __init__(self, members, *, kinds=(), answers=None, **cfg) -> None:
        self._tmp = temp_dir()
        self.time = FakeTime()
        self.cfg = settings(self._tmp.name, **cfg)
        self.ollama = FakeOllama()
        self.pionir = FakePionir(answers)
        self.sim = build(self.cfg, cast=list(members), kinds=kinds, post=self.ollama,
                         client=self.pionir,
                         card=CardWatch(self.cfg.gpu_lock_path, probe=lambda: None,
                                        poll_seconds=0.01),
                         clock=WallClock(self.time.now), monotonic=self.time.monotonic,
                         now=self.time.now)

    def __getitem__(self, aid: str):
        return self.sim.agent(aid)

    @property
    def t(self) -> int:
        return self.sim.clock.t

    def tick(self, n: int = 1, seconds: float = 1.0) -> None:
        for _ in range(n):
            self.time.advance(seconds)
            with self.sim.lock:
                self.sim.tick()

    def run_hands(self) -> int:
        n = 0
        while self.sim.hands is not None and self.sim.hands.waiting():
            self.sim.hands.step()
            n += 1
        return n

    def work_one_step(self, agent) -> None:
        """Put the agent to work on its project and drive one whole step to the end:
        the Job goes out, Pionir answers, the step finishes."""
        agent.last_think_t = self.t          # keep the thinking cadence out of it
        agent._start(BY_NAME["work_on"], self.sim)
        for _ in range(20):
            self.tick()
            self.run_hands()
            if agent.action is None:
                return
        raise AssertionError("the work step never finished")

    def give_project(self, agent, kind) -> None:
        agent.project = Project(kind.key, self.t)

    def close(self) -> None:
        self.sim.stop()
        self._tmp.cleanup()
