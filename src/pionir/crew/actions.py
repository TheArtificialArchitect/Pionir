"""The action library, and the commands an action yields.

Each action is a generator the agent drives one tick at a time. It yields commands:

    outcome = yield Job(capability, payload, what)  ask Pionir to do something real;
                                                    the generator gets a JobOutcome back
    ok = yield Wait(seconds, label)                 stay on this for ``seconds`` of real time
    ok = yield Until(pred, max_seconds, label)      wait until pred(agent, crew) or max_seconds

Hearth's ``GoTo(tile)`` is gone with the grid. Its place is taken by ``Job``: where a
resident walked to a thing and used it, a crew member asks Pionir to run a capability.
What a Job's outcome means for the agent's own record is decided in ONE place
(``Agent._on_job``), not by each action, so no action can write "did" for a job that
did not run: done becomes a ``did`` episode, ``pending_approval`` is recorded as
exactly that, and a failure is an attempt, not a deed.

``feasible(agent, crew)`` returns the drive gains the action is EXPECTED to deliver, or
None. Expected gains only weigh the choice; relief is applied when it really happens
(for purpose: only when a project's progress, recomputed from external truth, moved).
Hearth's twenty house actions are gone. What remains is getting on with real work, and
honest idleness.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import projects
from .log import log

# ---- commands ---------------------------------------------------------------


@dataclass
class Job:
    capability: str                 # a Pionir capability name, e.g. "reasoning.atani_answer"
    payload: dict = field(default_factory=dict)
    what: str = ""                  # in words, as the agent would say it: "send the follow-ups"
    permissions: tuple = ()         # anything privileged without these is parked for Ian
    wait: float = 30.0              # seconds Pionir may hold the call before handing back an id

    def __post_init__(self) -> None:
        if not self.capability or not isinstance(self.capability, str):
            raise ValueError("a Job names the capability it asks Pionir to run")
        if not isinstance(self.payload, dict):
            raise TypeError("a Job's payload is an object")
        if not self.what:
            self.what = f"run {self.capability}"


@dataclass
class Wait:
    seconds: float
    label: str
    skilled: bool = False      # True only where the time IS the agent working


@dataclass
class Until:
    pred: object
    max_seconds: float
    label: str


# ---- actions ----------------------------------------------------------------

class Action:
    name = "base"
    interruptible = True

    def feasible(self, agent, crew):
        return None

    def run(self, agent, crew):
        yield Wait(1, "idle")
        return "nothing"


class Stay(Action):
    """Nothing worth doing: stay put. Honest idleness, not an animation - and counted as
    idleness (``idled``), never as something done."""
    name = "stay"

    def feasible(self, agent, crew):
        return {}

    def run(self, agent, crew):
        yield Wait(120, "idle")
        return "stayed"


class WorkOnProject(Action):
    """Get on with whatever you have taken on.

    Holds no logic of its own: it reads the project's progress from external truth,
    lets the Kind take its next real step (Jobs through Pionir, waits), reads progress
    again, and hands both readings to ``note_project_step``. Only the difference between
    two readings of the world can relieve purpose.
    """
    name = "work_on"

    def feasible(self, agent, crew):
        p = getattr(agent, "project", None)
        if p is None:
            return None
        kind = p.kind(crew)
        if kind is None:
            return None
        try:
            if kind.done(agent, crew):
                return None
        except Exception as exc:  # noqa: BLE001 - logged: an unreadable project is news
            log.warning("%s: cannot tell whether %s is done: %s", agent.name, p.key, exc)
            return None
        return {"purpose": projects.STEP_RELIEF}

    def run(self, agent, crew):
        p = getattr(agent, "project", None)
        kind = p.kind(crew) if p is not None else None
        if kind is None:
            return "nothing on"
        before = agent.project_progress(crew)
        result = yield from kind.step(agent, crew)
        after = agent.project_progress(crew)
        agent.note_project_step(crew, before, after, str(result or "worked on it"))
        return result or "worked on it"


LIBRARY = [WorkOnProject(), Stay()]
BY_NAME = {a.name: a for a in LIBRARY}


def repertoire() -> list:
    """What every crew is expected to do sometimes (for the vitals' dead-action check)."""
    return [a.name for a in LIBRARY if a.name != "stay"]
