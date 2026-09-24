"""Personality as parameters. Every number here is read by code, none by a prompt
(the prompt sees only the name, pronouns, role and the disposition line).

Ported from Hearth's temperament. The house is gone, so are the bed, the bedroom and
how deeply somebody sleeps; the sleep window has become WORKING HOURS - the stretch
of the local day an agent is on. The cast itself no longer lives here: it is data
(``cast.json``, loaded by ``cast.py``), so adding an agent is adding an entry.

Who reads each number (if a number is not in this list, it must not be in the class):

sociability        social drive setpoint/weight/decay; odds of starting a conversation;
                   the urge to reply; resting warmth and loneliness
curiosity          stimulation setpoint/weight; how much a genuinely new line relieves
                   stimulation; how often the agent thinks; resting curiosity
restlessness       stimulation decay; how little appeal sitting idle has
initiative         purpose setpoint/weight/decay; odds of turning a thought into an
                   intention; odds of starting a conversation; resting resolve
order_sensitivity  urge and weight of the "my work has stalled" thought; how hard a
                   failed job or a given-up project lands (irritation)
memory_fidelity    whether a heard line is kept verbatim; recall half-life in the prompt;
                   weight of the "that contradicts what I saw" thought
people_weight      salience of episodes that involve a colleague
perception         the chance of taking in a line said in one of my channels that was
                   not addressed to me (addressed lines are always taken in)
impulsivity        softmax temperature for choosing an action; speech temperature
reticence          the urge a line (or a reply) must clear to be said at all
speech_cap         tokens per utterance, and the critic's word cap
resilience         mood half-lives; how fast a grievance fades
dwell              resting unease; how long a failure is carried as a thought
work_start/end     working hours, local clock hours [start, end), wrapping midnight;
                   start == end means always on
palette            the emotional vocabulary (affect channels); empty -> the base six
rest               resting levels for that vocabulary, where known
"""
from __future__ import annotations

from dataclasses import dataclass, field

UNIT_FIELDS = (
    "sociability", "curiosity", "restlessness", "initiative", "order_sensitivity",
    "memory_fidelity", "perception", "impulsivity", "reticence", "resilience", "dwell",
)


@dataclass(frozen=True)
class Temperament:
    id: str
    name: str
    pronouns: tuple            # (subject, object, possessive)
    disposition: str
    colour: str
    sociability: float
    curiosity: float
    restlessness: float
    initiative: float
    order_sensitivity: float
    memory_fidelity: float
    people_weight: float
    perception: float
    impulsivity: float
    reticence: float
    speech_cap: int
    resilience: float
    dwell: float
    work_start: float
    work_end: float
    palette: tuple = ()                         # empty -> affect's base six
    rest: dict = field(default_factory=dict)    # resting levels, where known

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("a temperament needs an id and a name")
        if len(self.pronouns) != 3:
            raise ValueError(f"{self.id}: pronouns are (subject, object, possessive)")
        for k in UNIT_FIELDS:
            v = getattr(self, k)
            if not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
                raise ValueError(f"{self.id}: {k} must be in [0, 1], got {v!r}")
        if not 0.1 <= float(self.people_weight) <= 3.0:
            raise ValueError(f"{self.id}: people_weight must be in [0.1, 3], got {self.people_weight!r}")
        if int(self.speech_cap) < 8:
            raise ValueError(f"{self.id}: speech_cap must be at least 8 tokens")
        for k in ("work_start", "work_end"):
            v = getattr(self, k)
            if not 0.0 <= float(v) < 24.0:
                raise ValueError(f"{self.id}: {k} must be a local hour in [0, 24), got {v!r}")

    @property
    def they(self) -> str:
        return self.pronouns[0]

    @property
    def them(self) -> str:
        return self.pronouns[1]

    @property
    def their(self) -> str:
        return self.pronouns[2]

    @property
    def always_on(self) -> bool:
        return float(self.work_start) == float(self.work_end)

    def working(self, clock) -> bool:
        """Inside working hours on the wall clock right now."""
        if self.always_on:
            return True
        return clock.in_window(float(self.work_start), float(self.work_end))
