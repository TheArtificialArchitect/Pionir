"""The one list of who exists, generated from the catalogue (``catalogue.json``).

There is no second, hand-maintained roster anywhere in this package: the dispatcher's
work list, the health report's cadences, the leaders' divisions and the list Moss is
shown all come from this object, so they cannot drift apart and start rejecting each
other's names on a mismatch rather than on merit (Peter's ADR-0004).

Everything wrong with the catalogue is an error at load time, loudly: a duplicate id
(two workers under one address means one of them silently never runs), a worker in a
division that does not exist, an ``impl`` or ``provider`` nobody defined, and any field
nobody reads (a typo'd ``cadence_second`` would otherwise become the default cadence
without a word). ``resolve`` returning None for an unknown id is deliberately different
from a worker that returned nothing; ``require`` raises.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

CATALOGUE_PATH = Path(__file__).with_name("catalogue.json")

_TOP = frozenset({"_how", "providers", "known_names", "divisions"})
_DIVISION = frozenset({"id", "title", "leader_cadence_seconds", "workers", "brief_quota",
                       "entities", "_note"})
_WORKER = frozenset({"name", "impl", "kind", "cadence_seconds", "provider", "stage",
                     "entities", "note", "params", "_note"})


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    name: str
    division: str
    impl: str
    kind: str
    cadence_seconds: int
    provider: str
    stage: int = 0
    entities: tuple = ()
    note: str = ""
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DivisionSpec:
    division_id: str
    title: str
    leader_cadence_seconds: int
    worker_ids: tuple
    brief_quota: dict = field(default_factory=dict)
    entities: tuple = ()


def _unknown(where: str, d: dict, allowed: frozenset) -> None:
    extra = set(d) - allowed
    if extra:
        raise ValueError(f"{where}: unknown field(s) {', '.join(sorted(extra))}; "
                         f"known: {', '.join(sorted(allowed))}")


def _positive_int(where: str, v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{where} must be a positive whole number of seconds, not {v!r}")
    return v


class Registry:
    def __init__(self, divisions, workers, providers: Mapping[str, float],
                 known_names=()) -> None:
        self._divisions: dict = {}
        for d in divisions:
            if d.division_id in self._divisions:
                raise ValueError(f"duplicate division id {d.division_id!r}")
            self._divisions[d.division_id] = d
        self.providers = dict(providers)
        self._by_id: dict = {}
        for w in workers:
            if w.worker_id in self._by_id:
                raise ValueError(f"duplicate worker id {w.worker_id!r}: two workers under one "
                                 "address means one of them silently never runs")
            if w.division not in self._divisions:
                raise ValueError(f"worker {w.worker_id!r} is in unknown division "
                                 f"{w.division!r}; known: {', '.join(self._divisions)}")
            if w.provider not in self.providers:
                raise ValueError(f"worker {w.worker_id!r} names unknown provider "
                                 f"{w.provider!r}; known: {', '.join(sorted(self.providers))}")
            self._by_id[w.worker_id] = w
        for d in self._divisions.values():
            for wid in d.worker_ids:
                if wid not in self._by_id:
                    raise ValueError(f"division {d.division_id!r} lists unknown worker {wid!r}")
        self.known_names = tuple(known_names)

    def __len__(self) -> int:
        return len(self._by_id)

    def all(self) -> tuple:
        return tuple(self._by_id[k] for k in sorted(self._by_id))

    def ids(self) -> tuple:
        return tuple(sorted(self._by_id))

    def resolve(self, worker_id: str):
        """None means the NAME is wrong - a different thing from a worker with nothing."""
        return self._by_id.get(worker_id)

    def require(self, worker_id: str):
        w = self._by_id.get(worker_id)
        if w is None:
            raise KeyError(f"no such worker {worker_id!r}; known: {', '.join(self.ids())}")
        return w

    def divisions(self) -> tuple:
        return tuple(self._divisions.values())

    def division_ids(self) -> tuple:
        return tuple(self._divisions)

    def division(self, division_id: str) -> DivisionSpec:
        d = self._divisions.get(division_id)
        if d is None:
            raise KeyError(f"no such division {division_id!r}; "
                           f"known: {', '.join(self._divisions)}")
        return d

    def workers_in(self, division_id: str) -> tuple:
        return tuple(self._by_id[w] for w in self.division(division_id).worker_ids)

    def cadences(self, division_id: str | None = None) -> dict:
        """What ``store.health`` needs, so a worker that has never once run is still
        reported rather than absent."""
        ws = self.all() if division_id is None else self.workers_in(division_id)
        return {w.worker_id: w.cadence_seconds for w in ws}

    def provider_interval(self, provider: str) -> float:
        return float(self.providers[provider])


def load_catalogue(path: Path | None = None) -> dict:
    return json.loads((path or CATALOGUE_PATH).read_text(encoding="utf-8"))


def build_registry(catalogue: dict, impls: Mapping | None = None) -> Registry:
    """Every worker the crew runs, built from the catalogue by the factory each names."""
    if impls is None:
        from .workers import IMPLS
        impls = IMPLS
    _unknown("catalogue", catalogue, _TOP)
    providers = catalogue.get("providers") or {}
    for name, interval in providers.items():
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError(f"provider {name!r} interval must be seconds >= 0, not {interval!r}")
    divisions, workers = [], []
    for i, d in enumerate(catalogue.get("divisions") or ()):
        _unknown(f"division #{i}", d, _DIVISION)
        did = str(d.get("id") or "").strip()
        if not did or not did.replace("_", "").isalnum():
            raise ValueError(f"division #{i} needs an id of letters, digits and _: {did!r}")
        ids = []
        for j, w in enumerate(d.get("workers") or ()):
            _unknown(f"worker {did}#{j}", w, _WORKER)
            name = str(w.get("name") or "").strip()
            if not name or not name.replace("_", "").isalnum():
                raise ValueError(f"worker {did}#{j} needs a name of letters, digits and _")
            wid = f"{did}.{name}"
            impl = w.get("impl")
            if impl not in impls:
                raise ValueError(f"worker {wid!r} names unknown impl {impl!r}; "
                                 f"known: {', '.join(sorted(impls))}")
            kind = str(w.get("kind") or "").strip()
            if not kind:
                raise ValueError(f"worker {wid!r} needs a kind")
            spec = WorkerSpec(
                worker_id=wid, name=name, division=did, impl=impl, kind=kind,
                cadence_seconds=_positive_int(f"{wid}.cadence_seconds", w.get("cadence_seconds")),
                provider=str(w.get("provider") or ""), stage=int(w.get("stage", 0)),
                entities=tuple(w.get("entities") or ()), note=str(w.get("note") or ""),
                params=dict(w.get("params") or {}))
            try:
                workers.append(impls[impl](spec, **spec.params))
            except TypeError as exc:
                raise ValueError(f"worker {wid!r}: impl {impl!r} does not take these params "
                                 f"({', '.join(sorted(spec.params)) or 'none'}): {exc}") from exc
            ids.append(wid)
        quota = d.get("brief_quota") or {}
        for k, v in quota.items():
            _positive_int(f"{did}.brief_quota.{k}", v)
        divisions.append(DivisionSpec(
            division_id=did, title=str(d.get("title") or did),
            leader_cadence_seconds=_positive_int(f"{did}.leader_cadence_seconds",
                                                 d.get("leader_cadence_seconds", 3600)),
            worker_ids=tuple(ids), brief_quota=dict(quota),
            entities=tuple(d.get("entities") or ())))
    return Registry(divisions, workers, providers, catalogue.get("known_names") or ())


def default_registry(path: Path | None = None) -> Registry:
    return build_registry(load_catalogue(path))
