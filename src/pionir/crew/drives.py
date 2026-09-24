"""Drives: real values an agent genuinely acts on.

Ported from Hearth's homeostatic mechanism: each drive is a satisfaction level in
[0, 1] with a temperament setpoint and weight; only a deficit motivates
(error = max(0, setpoint - value)); behaviour is chosen by *relief* - how much weighted
error an action would remove - never by the absolute level, so a drive nothing can
currently relieve contributes nothing. Setpoints drift slowly toward how the agent
actually lives, bounded to 0.1 of where it was born.

Hearth had eight drives, most of them bodily (hunger, energy, hygiene) or about the
house (order, solitude). A crew has no body and no house, and a drive nothing relieves
is a number that only ever grows - so only three survive, each relieved by exactly one
real thing:

- ``purpose``      relieved ONLY by real project progress (``relieve_purpose``, called
                   when a project's progress, recomputed from external truth, actually
                   moved). ``apply`` refuses a positive purpose gain: a step that changed
                   nothing relieves nothing, and there is no other door.
- ``social``       relieved by actually talking with another agent (a line said to a
                   colleague, or one said to me).
- ``stimulation``  relieved by taking in genuinely new information in my channels.

All three decay only during working hours; off the clock nothing drains.
"""
from __future__ import annotations

import math

DRIVES = ("purpose", "social", "stimulation")

H = 3600.0


def _rate(hours: float) -> float:
    """Per-real-second change that empties a full drive in ``hours``."""
    return 1.0 / (hours * H)


class Drives:
    def __init__(self, t) -> None:
        self.t = t
        self.setpoint = {
            "purpose": 0.30 + 0.50 * t.initiative,
            "social": 0.30 + 0.50 * t.sociability,
            "stimulation": 0.30 + 0.50 * (t.curiosity + t.restlessness) / 2,
        }
        self.weight = {
            "purpose": 0.25 + 0.85 * t.initiative,
            "social": 0.30 + 1.00 * t.sociability,
            "stimulation": 0.40 + 0.50 * t.restlessness + 0.40 * t.curiosity,
        }
        self.value = {"purpose": 0.62, "social": 0.60, "stimulation": 0.60}
        # decay rates per real second of working time (Hearth's, which were per game second)
        self.decay = {
            "purpose": _rate(20.0 - 9.0 * t.initiative),
            "social": _rate(14.0 - 10.0 * t.sociability),
            "stimulation": _rate(6.0 - 3.0 * (t.curiosity + t.restlessness) / 2),
        }
        self.nonfinite = 0                       # counter: NaN/Inf caught and healed
        self.refused = 0                         # counter: a purpose gain from anywhere but progress
        self.birth_setpoint = dict(self.setpoint)
        self.history: dict = {k: [] for k in DRIVES}   # one sample per real hour, capped
        self.drifted_days = 0

    # ---- per tick -------------------------------------------------------
    def tick(self, seconds: float, working: bool) -> None:
        if not working or seconds <= 0:
            return
        for k in DRIVES:
            self.value[k] -= self.decay[k] * seconds
        self._guard()

    def _guard(self) -> None:
        for k, x in self.value.items():
            if not math.isfinite(x):
                self.value[k] = self.setpoint[k]      # heal to the setpoint, never leave poison
                self.nonfinite += 1
            elif x < 0.0:
                self.value[k] = 0.0
            elif x > 1.0:
                self.value[k] = 1.0

    # ---- reading --------------------------------------------------------
    def error(self, k: str) -> float:
        return max(0.0, self.setpoint[k] - self.value[k])

    def errors(self) -> dict:
        return {k: self.error(k) for k in DRIVES}

    def pressure(self, k: str) -> float:
        """Weighted deficit: what this drive is currently costing."""
        return self.weight[k] * self.error(k)

    def most_pressing(self) -> tuple:
        k = max(DRIVES, key=self.pressure)
        return k, self.pressure(k)

    def relief(self, gains: dict) -> float:
        """Weighted error an action's EXPECTED gains would remove, given current deficits.
        Only for weighing a choice; nothing here changes a value."""
        total = 0.0
        for k, g in gains.items():
            if g <= 0:
                total += self.weight[k] * g * 0.5
                continue
            total += self.weight[k] * min(self.error(k), g)
        return total

    # ---- moving ---------------------------------------------------------
    def apply(self, gains: dict) -> None:
        """Social and stimulation gains, and any cost. Purpose is not relieved here."""
        for k, g in gains.items():
            if k == "purpose" and g > 0:
                self.refused += 1
                raise ValueError("purpose is relieved only by real project progress "
                                 "(relieve_purpose), never by apply()")
            self.value[k] += g
        self._guard()

    def relieve_purpose(self, amount: float) -> None:
        """The one door for purpose: a project step whose progress really moved."""
        if amount <= 0 or not math.isfinite(amount):
            return
        self.value["purpose"] += amount
        self._guard()

    def sample(self) -> None:
        """Hourly sample for drift; bounded history."""
        for k in DRIVES:
            h = self.history[k]
            h.append(self.value[k])
            if len(h) > 24 * 30:
                del h[0]

    def drift(self) -> dict:
        """Once a day: each setpoint moves at most 0.1/30 toward the median of the last 30
        days' samples, never more than 0.1 from birth. A colleague can mellow or sour, not
        become someone else."""
        moved = {}
        for k in DRIVES:
            h = sorted(self.history[k])
            if len(h) < 24:
                continue
            median = h[len(h) // 2]
            step = max(-0.1 / 30, min(0.1 / 30, median - self.setpoint[k]))
            lo, hi = self.birth_setpoint[k] - 0.1, self.birth_setpoint[k] + 0.1
            new = max(lo, min(hi, self.setpoint[k] + step))
            if abs(new - self.setpoint[k]) > 1e-6:
                moved[k] = round(new - self.setpoint[k], 4)
                self.setpoint[k] = new
        self.drifted_days += 1
        return moved

    # ---- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        return {"value": dict(self.value), "nonfinite": self.nonfinite,
                "setpoint": dict(self.setpoint),
                "history": {k: [round(x, 3) for x in v[-720:]] for k, v in self.history.items()},
                "drifted_days": self.drifted_days}

    def load_dict(self, d: dict) -> None:
        for k, x in (d.get("value") or {}).items():
            if k in self.value:
                self.value[k] = float(x)
        for k, x in (d.get("setpoint") or {}).items():
            if k in self.setpoint and math.isfinite(float(x)):
                lo, hi = self.birth_setpoint[k] - 0.1, self.birth_setpoint[k] + 0.1
                self.setpoint[k] = max(lo, min(hi, float(x)))
        for k, v in (d.get("history") or {}).items():
            if k in self.history:
                self.history[k] = [float(x) for x in v][-720:]
        self.drifted_days = int(d.get("drifted_days", 0))
        self.nonfinite = int(d.get("nonfinite", 0))
        self._guard()

    def snapshot(self) -> dict:
        return {k: {"value": round(self.value[k], 3), "setpoint": round(self.setpoint[k], 3),
                    "weight": round(self.weight[k], 2), "pressure": round(self.pressure(k), 3)}
                for k in DRIVES}
