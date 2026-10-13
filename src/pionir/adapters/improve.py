"""``improve.apply``: apply ONE approved self-improvement to the Pionir repo, on Ian's yes only.

The crew's ``builds.improve`` worker (crew/improve) finds a failure that keeps recurring, has
Daedalus write a fix in the build sandbox, verifies it there and has Claude review it. What it
then asks for is this capability - PRIVILEGED with ``requires_approval=True``, so it is parked on
EVERY call, never batched into the digest, and never routed from a sentence. Its card shows the
evidence, the WHOLE diff (the guard caps a patch at what fits on Ian's phone), Claude's verdict,
how it was verified, and the undo command.

The payload is ``{"id", "digest"}`` and nothing else. The patch, its base commit and its tests
are read ONCE from THIS process's own improve folder (selfimprove/store.py: the owner's
profile) and must hash to exactly the digest the card showed - a patch, base or test list
changed after the card is refused, never applied - and those same bytes are what is applied.
The guard (selfimprove/guard.py) runs again here, before parking and before applying, and once
more on the trees in Ian's own index: the worker's word that it passed is not taken.

On the yes: selfimprove/apply.py - one commit on a clean ``main`` that still contains the base,
nothing of it run as Ian (its tests ran contained before he was asked), or "stale, will re-propose". Never a push,
never a restart. The outcome is written to ``<id>/outcome.json`` for the worker and for
``python -m pionir improve status``.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.selfimprove import apply as applier
from pionir.selfimprove import guard, store

APPLY = "improve.apply"
PAYLOAD_KEYS = frozenset({"id", "digest"})


@dataclass(frozen=True, slots=True)
class ImproveSettings:
    """Pionir's repository (what an approved change is committed to) and the improve folder."""

    pionir_repo: Path = Path("C:/src/Pionir")
    improve_dir: Path = Path("~/.pionir/improve")

    def __post_init__(self) -> None:
        object.__setattr__(self, "pionir_repo", Path(self.pionir_repo).expanduser())
        object.__setattr__(self, "improve_dir", Path(self.improve_dir).expanduser())


def improve_settings(configured: Any) -> ImproveSettings:
    return ImproveSettings(pionir_repo=configured.pionir_repo_path,
                           improve_dir=configured.improve_path)


def local_modules(repo: Path) -> frozenset:
    """The test folder's own helper modules (``support``, ``video_support``...): importing one
    is not a new dependency."""
    try:
        return frozenset(p.stem for p in (Path(repo) / "tests").glob("*.py"))
    except OSError:
        return frozenset()


def repo_files(repo: Path) -> dict:
    """Pionir's own modules, read from the owner's repo (path -> bytes)."""
    files = {}
    root = Path(repo) / "src" / "pionir"
    try:
        for path in root.rglob("*.py"):
            if "__pycache__" not in path.parts:
                files[path.relative_to(repo).as_posix()] = path.read_bytes()
    except OSError:
        return {}
    return files


def repo_imports(repo: Path) -> frozenset:
    """Every top-level name Pionir's own modules import (read from the owner's repo)."""
    return guard.known_imports(repo_files(repo)) | local_modules(repo)


class ImproveAdapter:
    """One approved self-improvement, applied as a gated, audited Pionir capability."""

    def __init__(self, settings: ImproveSettings | None = None, *, git=None, run=None,
                 clock=time.time) -> None:
        self.settings = settings or ImproveSettings()
        self._git = git or applier.real_git
        self._run = run or applier.real_run
        self._clock = clock
        self._manifest = AgentManifest(
            agent_id="improve",
            version="pionir/improve",
            capabilities=(
                Capability(
                    name=APPLY,
                    description="Apply one self-improvement the crew proposed (Daedalus wrote "
                                "it, Claude reviewed it) as one commit on main of the Pionir "
                                "repo - only after the owner approves it; never pushes",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({APPLY}),
                    requires_approval=True,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Local only: the repo is a git repository. Nothing is run."""
        repo = self.settings.pionir_repo
        if not (repo / ".git").exists():
            raise AdapterUnavailable(f"not configured: no Pionir repository at {repo} "
                                     "(PIONIR_REPO_DIR)")
        return {"pionir_repo": str(repo), "improve_dir": str(self.settings.improve_dir)}

    # ---- the request ----------------------------------------------------------------------
    def _checked(self, task: Task) -> tuple:
        """``(id, patch bytes, card, known imports)`` - read once - or AdapterProtocolError: a
        payload that is not exactly ``{id, digest}``, a missing patch, a patch, base or test
        list that changed since the card, or a patch the guard refuses."""
        if task.capability != APPLY:
            raise AdapterProtocolError(f"improve has no capability {task.capability!r}")
        payload = task.payload
        if not isinstance(payload, Mapping) or set(payload) != PAYLOAD_KEYS:
            raise AdapterProtocolError(f"{APPLY} refused by Pionir - the payload is exactly "
                                       "{id, digest}")
        try:
            cid = store.check_id(payload.get("id"))
            data, card, digest = store.read_candidate(self.settings.improve_dir, cid)
        except store.StoreError as exc:
            raise AdapterProtocolError(f"{APPLY} refused by Pionir - {exc}") from exc
        if payload.get("digest") != digest:
            raise AdapterProtocolError(f"{APPLY} refused by Pionir - {cid}'s patch, base or "
                                       "tests are not the ones the card showed (its digest "
                                       "differs)")
        if len(card["base"]) < 40:
            raise AdapterProtocolError(f"{APPLY} refused by Pionir - {cid}'s card names no "
                                       "base commit")
        files = repo_files(self.settings.pionir_repo)
        known = guard.known_imports(files) | local_modules(self.settings.pionir_repo)
        closure = guard.protected_closure(files)
        problems = guard.patch_problems(data.decode("utf-8", "replace"), known_imports=known,
                                        protected=closure)
        if problems:
            raise AdapterProtocolError(f"{APPLY} refused by Pionir - the protected list "
                                       f"refuses it: {'; '.join(problems[:4])}")
        return cid, data, card, (known, closure, files)

    def validate(self, task: Task) -> None:
        self._checked(task)

    def park_context(self, task: Task) -> dict[str, Any] | None:
        """What Ian's card shows: the evidence, the hypothesis, the WHOLE diff, Claude's verdict,
        the verification and the undo command. Never blocks parking."""
        if task.capability != APPLY:
            return None
        cid, data, card, _known = self._checked(task)
        return {"id": cid, "title": str(card.get("title") or "")[:160],
                "evidence": card.get("evidence") if isinstance(card.get("evidence"), dict)
                else {}, "hypothesis": str(card.get("hypothesis") or "")[:800],
                "verdict": card.get("verdict") if isinstance(card.get("verdict"), dict) else {},
                "verification": str(card.get("verification") or "")[:600],
                "diff_stat": str(card.get("diff_stat") or "")[:1200],
                "diff": data.decode("utf-8", "replace"),
                "diff_file": str(store.folder(self.settings.improve_dir, cid) / store.PATCH),
                "base": card.get("base"), "tests": list(card.get("tests") or []),
                "repo": str(self.settings.pionir_repo),
                "undo": f"python -m pionir improve undo {cid}",
                "runs": "one commit on main of the Pionir repo (if main is clean and still "
                        "contains its base), nothing of it is run as you (its tests ran contained in the sandbox). "
                        "Nothing is pushed or restarted."}

    # ---- the call ---------------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        cid, data, card, (known, closure, files) = self._checked(task)
        done = applier.apply_patch(
            self.settings.pionir_repo, data, cid, card["base"],
            title=str(card.get("title") or cid), tests=[str(t) for t in card.get("tests") or []],
            approval=str(task.task_id), known=known, protected=closure, files=files,
            git=self._git)
        outcome = {**done, "id": cid, "at": self._clock(), "digest": task.payload["digest"]}
        store.write_json(store.folder(self.settings.improve_dir, cid) / store.OUTCOME, outcome)
        ok = done.get("applied") is True or done.get("stale") is True
        output = {"ok": ok, **outcome}
        if not ok:
            output["error"] = str(done.get("error") or "it could not be applied")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=(f"improve:id:{cid}",))
