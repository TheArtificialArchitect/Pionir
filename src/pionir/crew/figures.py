"""A figure is a number that says what it is: ``{value, unit, measures}``.

A bare number is the raw material of confabulation. "12" backs "$12" and "12 replies"
and "12 %" equally well, so a check that compares values alone lets a report claim
revenue it never saw on the strength of a reply count. Every figure a worker records
therefore carries its UNIT (from a closed list) and what it MEASURES, and the grounding
check (grounding.py) matches value AND unit.

Money is ``usd_cents``, an integer, exactly as Scrooge's ledger keeps it: no float
dollars anywhere, so $12.00 is 1200 and cannot drift to 1199.9999.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Closed on purpose: a unit not listed here is a proposal to change what the crew can
# say, and must be made in one place or the grounding check will silently ignore it.
UNITS = frozenset({
    "usd_cents",     # money, as integer cents
    "count",         # how many of something (``measures`` says of what)
    "ms",            # a duration in milliseconds (latency)
    "seconds",       # a duration in seconds
    "percent",       # 0..100
    "boolean",       # 1 or 0 (up / down)
    "http_status",   # an HTTP status code
})

_FIELDS = frozenset({"value", "unit", "measures", "stream", "window"})


@dataclass(frozen=True, slots=True)
class Figure:
    value: float
    unit: str
    measures: str
    stream: str = ""        # which stream / product / subject it belongs to, if any
    window: str = ""        # e.g. "last30", "mtd", "all_time", "now"

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError(f"a figure's value is a number, not {self.value!r}")
        if not math.isfinite(float(self.value)):
            raise ValueError(f"a figure's value must be finite, not {self.value!r}")
        if self.unit not in UNITS:
            raise ValueError(f"unknown unit {self.unit!r}; known: {', '.join(sorted(UNITS))}")
        if not isinstance(self.measures, str) or not self.measures.strip():
            raise ValueError("a figure says what it measures")
        if self.unit == "usd_cents" and not float(self.value).is_integer():
            raise ValueError(f"money is whole cents, not {self.value!r}")

    def to_dict(self) -> dict:
        d = {"value": self.value, "unit": self.unit, "measures": self.measures}
        if self.stream:
            d["stream"] = self.stream
        if self.window:
            d["window"] = self.window
        return d

    @staticmethod
    def from_dict(d) -> Figure:
        """Strict: an unknown key or a missing field is an error, never a default. Raises
        TypeError or ValueError."""
        if not isinstance(d, dict):
            raise TypeError(f"a figure is an object, not {type(d).__name__}")
        extra = set(d) - _FIELDS
        if extra:
            raise ValueError(f"unknown figure field(s): {', '.join(sorted(extra))}")
        for key in ("value", "unit", "measures"):
            if key not in d:
                raise ValueError(f"a figure needs {key!r}")
        value = d["value"]
        if d["unit"] == "usd_cents" and isinstance(value, float) and value.is_integer():
            value = int(value)
        return Figure(value, str(d["unit"]), str(d["measures"]),
                      str(d.get("stream") or ""), str(d.get("window") or ""))

    @property
    def shown(self) -> float:
        """The value as a person would write it: dollars for cents, else the value."""
        return self.value / 100.0 if self.unit == "usd_cents" else float(self.value)

    def display(self) -> str:
        """How the brief writes it - the form a report is expected to repeat."""
        if self.unit == "usd_cents":
            text = f"${self.value / 100:,.2f} {self.measures}"
        elif self.unit == "percent":
            text = f"{_num(self.value)}% {self.measures}"
        elif self.unit == "ms":
            text = f"{self.measures} {_num(self.value)} ms"
        elif self.unit == "seconds":
            text = f"{self.measures} {_num(self.value)} seconds"
        elif self.unit == "boolean":
            text = f"{self.measures}: {'yes' if self.value else 'no'}"
        elif self.unit == "http_status":
            text = f"{self.measures} HTTP {_num(self.value)}"
        else:
            text = f"{_num(self.value)} {self.measures}"
        tags = ", ".join(t for t in (self.stream and f"stream {self.stream}", self.window) if t)
        return f"{text} ({tags})" if tags else text


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"
