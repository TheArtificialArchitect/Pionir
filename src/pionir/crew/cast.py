"""Who is on the crew, as data.

Hearth kept its cast as a dict literal in ``temperament.py``; adding a resident meant
editing code. Here the cast is ``cast.json`` beside this module (or any file a caller
names), and adding an agent is adding an entry. Each entry is a ``Member``: the
agent's ``Temperament`` (every number read by code), the one-line ``role`` the prompt
shows, and the ``channels`` - the workstreams it belongs to. An agent hears, and can
be heard, only in its own channels.

A bad entry is refused at load with the reason, never half-loaded: a cast that loads
"mostly" would give an agent silently different behaviour from the file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .temperament import Temperament

CAST_PATH = Path(__file__).resolve().parent / "cast.json"

_TEMPERAMENT_NUMBERS = (
    "sociability", "curiosity", "restlessness", "initiative", "order_sensitivity",
    "memory_fidelity", "people_weight", "perception", "impulsivity", "reticence",
    "speech_cap", "resilience", "dwell", "work_start", "work_end",
)


@dataclass(frozen=True)
class Member:
    temperament: Temperament
    role: str
    channels: tuple            # workstream names, e.g. ("outreach", "ops")

    @property
    def id(self) -> str:
        return self.temperament.id


def member_from(entry: dict) -> Member:
    """One cast entry -> a Member. Raises ValueError naming what is wrong."""
    if not isinstance(entry, dict):
        raise TypeError("a cast entry must be an object")
    aid = str(entry.get("id") or "").strip()
    where = aid or "<entry without an id>"
    nums = entry.get("temperament")
    if not isinstance(nums, dict):
        raise TypeError(f"{where}: 'temperament' must be an object")
    missing = [k for k in _TEMPERAMENT_NUMBERS if k not in nums]
    if missing:
        raise ValueError(f"{where}: temperament is missing {', '.join(missing)}")
    unknown = sorted(set(nums) - set(_TEMPERAMENT_NUMBERS) - {"palette", "rest"})
    if unknown:
        # a number no code reads is a number somebody will tune and nothing will change
        raise ValueError(f"{where}: temperament has numbers no code reads: {', '.join(unknown)}")
    channels = entry.get("channels")
    if not isinstance(channels, list) or not channels or \
            not all(isinstance(c, str) and c.strip() for c in channels):
        raise ValueError(f"{where}: 'channels' must be a non-empty list of workstream names")
    role = str(entry.get("role") or "").strip()
    if not role:
        raise ValueError(f"{where}: 'role' is required - the prompt has nothing else to say "
                         "about the job")
    pronouns = entry.get("pronouns") or ["they", "them", "their"]
    t = Temperament(
        id=aid,
        name=str(entry.get("name") or "").strip(),
        pronouns=tuple(str(p) for p in pronouns),
        disposition=str(entry.get("disposition") or "").strip(),
        colour=str(entry.get("colour") or "#888888"),
        sociability=float(nums["sociability"]),
        curiosity=float(nums["curiosity"]),
        restlessness=float(nums["restlessness"]),
        initiative=float(nums["initiative"]),
        order_sensitivity=float(nums["order_sensitivity"]),
        memory_fidelity=float(nums["memory_fidelity"]),
        people_weight=float(nums["people_weight"]),
        perception=float(nums["perception"]),
        impulsivity=float(nums["impulsivity"]),
        reticence=float(nums["reticence"]),
        speech_cap=int(nums["speech_cap"]),
        resilience=float(nums["resilience"]),
        dwell=float(nums["dwell"]),
        work_start=float(nums["work_start"]),
        work_end=float(nums["work_end"]),
        palette=tuple(nums.get("palette") or ()),
        rest=dict(nums.get("rest") or {}),
    )
    return Member(temperament=t, role=role,
                  channels=tuple(dict.fromkeys(c.strip().lower() for c in channels)))


def load_cast(path: Path | None = None) -> list:
    """The crew, in file order. Refuses duplicates and an empty cast."""
    path = path or CAST_PATH
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = doc.get("agents") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: no 'agents' in the cast")
    members = [member_from(e) for e in entries]
    seen_ids: set = set()
    seen_names: set = set()
    for m in members:
        if m.id in seen_ids or m.temperament.name in seen_names:
            raise ValueError(f"{path}: {m.id} / {m.temperament.name} appears twice")
        seen_ids.add(m.id)
        seen_names.add(m.temperament.name)
    return members
