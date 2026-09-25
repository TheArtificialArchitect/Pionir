"""Adapter for the crew: Moss reads its digest and directs it, through Pionir.

The crew (``pionir.crew``) is its own foreground process with a loopback HTTP API
(``pionir/crew/api.py``, default port 8782). Moss could call that API directly; routing
her through these capabilities instead is what puts every direction change - a goal, a
compute allocation - under Pionir's gates and in its audit ledger, like every other
action. The crew's own record additionally names the Pionir task that asked
(``by: "moss via pionir task <id>"``), so the two records can be joined.

Capabilities, all invoked by name (``routable=False``) and none holding a model:

- ``crew.digest``, ``crew.divisions``, ``crew.compute`` - READ_ONLY.
- ``crew.set_goal``, ``crew.allocate`` - REVERSIBLE_WRITE. The owner lets Moss set goals
  and move COMPUTE between divisions freely; these are not money, and the crew has no
  money lever (``allocate`` takes ``model_calls`` or ``claude_escalations`` only).

The payload is checked with the crew API's own validators before anything is sent, so a
malformed request is refused by Pionir with the crew's words. The crew's answer is
passed through as the output object. A request the crew refuses (an unknown division,
shares over 1) comes back as ``ok: false`` with ``refused`` - the crew doing its job,
not a fault. A crew that is not running comes back at once as ``ok: false`` with
``unavailable: "the crew is not running"``: it is an ordinary state (the crew is started
by hand), so it must not trip the circuit breaker and then hide the plain answer behind
"circuit open" for a while after the crew does start.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pionir.adapters._http import (
    HttpStatusError,
    LoopbackHttpSettings,
    LoopbackJsonClient,
)
from pionir.contracts import (
    AgentManifest,
    Capability,
    RiskLevel,
    Task,
    TaskResult,
)
from pionir.crew.api import parse_allocation, parse_goal, parse_max_chars
from pionir.errors import AdapterProtocolError, AdapterUnavailable

NOT_RUNNING = "the crew is not running"
# Who is asking, as the caller names itself; Pionir adds which task it was.
_BY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")

READS = {
    "crew.digest": "The crew's bounded digest: the newest word from every division, "
                   "most urgent first",
    "crew.divisions": "The crew's divisions: goal, priority, workers and their health",
    "crew.compute": "The crew's compute: model-call and Claude-escalation shares and caps "
                    "per division",
}
WRITES = {
    "crew.set_goal": "Set a crew division's goal and priority (1 most, 5 least)",
    "crew.allocate": "Allocate crew compute (model_calls or claude_escalations) between "
                     "divisions as fractions",
}


@dataclass(frozen=True, slots=True)
class CrewAdapterSettings(LoopbackHttpSettings):
    base_url: str = "http://127.0.0.1:8782"
    # Short on purpose: every crew call is a local SQLite read or one write. A crew that
    # has not answered in this long is not going to, and Moss should hear so at once.
    timeout_seconds: int = 5


class CrewAdapter:
    """The crew's Direction API as gated, audited Pionir capabilities."""

    def __init__(
        self,
        settings: CrewAdapterSettings | None = None,
        *,
        client: LoopbackJsonClient | None = None,
    ) -> None:
        self.settings = settings or CrewAdapterSettings()
        self._client = client or LoopbackJsonClient("the crew", self.settings)
        read = tuple(
            Capability(name=name, description=text, risk=RiskLevel.READ_ONLY,
                       routable=False)
            for name, text in READS.items()
        )
        write = tuple(
            Capability(name=name, description=text, risk=RiskLevel.REVERSIBLE_WRITE,
                       routable=False)
            for name, text in WRITES.items()
        )
        self._manifest = AgentManifest(
            agent_id="crew", version="pionir/crew", capabilities=read + write
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        try:
            document = self._client.get("/api/health")
        except AdapterUnavailable as error:
            raise AdapterUnavailable(self._not_running()) from error
        if document.get("ok") is not True:
            raise AdapterProtocolError("the crew's health response is malformed")
        return document

    # ---- the request ---------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a malformed payload before it is run (or parked), in the crew's words."""
        self._request(task)

    def _request(self, task: Task) -> tuple[str, str, dict[str, Any] | None]:
        """(method, path, body) for a task, or AdapterProtocolError saying what is wrong."""
        payload = task.payload
        try:
            if task.capability == "crew.digest":
                max_chars = parse_max_chars(payload.get("max_chars"))
                return "GET", f"/api/digest?max_chars={max_chars}", None
            if task.capability == "crew.divisions":
                return "GET", "/api/divisions", None
            if task.capability == "crew.compute":
                return "GET", "/api/compute", None
            by = self._by(payload.get("by"), task)
            if task.capability == "crew.set_goal":
                args = parse_goal({**payload, "by": by})
                return "POST", "/api/goal", args
            if task.capability == "crew.allocate":
                args = parse_allocation({**payload, "by": by})
                return "POST", "/api/allocate", args
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability}: {error}") from error
        raise AdapterProtocolError(f"the crew has no capability {task.capability!r}")

    @staticmethod
    def _by(raw: Any, task: Task) -> str:
        who = "moss" if raw is None else raw
        if not isinstance(who, str) or not _BY.match(who.strip()):
            raise ValueError("by is a short name (letters, digits, space, _ . -; at most 40)")
        return f"{who.strip()} via pionir task {task.task_id}"

    def _not_running(self) -> str:
        return (f"{NOT_RUNNING}: nothing answered at {self.settings.base_url} within "
                f"{self.settings.timeout_seconds} s. Start it with `python -m pionir.crew` "
                "(or the Crew pane of pionir.ps1)")

    # ---- the call ------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        method, path, body = self._request(task)
        try:
            if method == "GET":
                document = self._client.get(path)
            else:
                document = self._client.post(path, body or {})
        except HttpStatusError as error:
            if error.status == 400:
                # the crew refused the request (unknown division, shares over 1): an
                # answer, not a fault - it must not count against the crew's circuit
                return self._result(task, {"ok": False, "refused": str(error),
                                           "error": str(error)})
            if error.status == 503:
                return self._result(task, {"ok": False, "unavailable": str(error),
                                           "error": str(error)})
            raise
        except AdapterUnavailable:
            return self._result(task, {"ok": False, "unavailable": NOT_RUNNING,
                                       "error": self._not_running()})
        if document.get("ok") is not True:
            raise AdapterProtocolError(f"the crew answered {path} without ok: true")
        return self._result(task, dict(document))

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"crew:{task.capability.split('.', 1)[1]}"]
        if output.get("ok") is True and task.capability == "crew.set_goal":
            evidence.append(f"crew:goal:{output.get('division')}")
        if output.get("ok") is True and task.capability == "crew.allocate":
            evidence.append(f"crew:allocate:{output.get('resource')}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))
