"""Read-only window on the owner's work log for Moss and Atani: aggregates only.

``work.summary`` answers "how many hours this week" and lets Moss notice a timer left running
or a slow week. It returns hours and estimated earnings per job for today / week / month and
whether a timer is running - and NOTHING else. No session note, no rubric tip, no log text and
no payout ever leaves the work log through this door (WorkLog.aggregates builds the answer
from numbers and job names alone; a test plants canary text in every text column and asserts
it never appears). Moss cannot start or stop a timer or write an entry: this adapter has one
capability and it only reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.errors import AdapterProtocolError
from pionir.worklog import WorkLog, zone_or_local

CAPABILITY = "work.summary"


@dataclass(frozen=True, slots=True)
class WorkSummarySettings:
    path: Path
    zone: str | None = None
    version: str = "pionir/work-summary"


class WorkSummaryAdapter:
    def __init__(self, settings: WorkSummarySettings, *, clock: Any = None) -> None:
        self.settings = settings
        zone, self._zone_error = zone_or_local(settings.zone)
        self._log = WorkLog(settings.path, clock=clock, zone=zone)
        self._manifest = AgentManifest(
            agent_id="work",
            version=settings.version,
            capabilities=(
                Capability(
                    name=CAPABILITY,
                    description=("Read the owner's hours worked and estimated earnings per job "
                                 "(today, this week, this month) and whether a work timer is "
                                 "running - totals only, never notes"),
                    routing_hints=frozenset({"hours", "worked", "timer", "shift", "workday",
                                             "annotation", "clocked", "timesheet"}),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> dict[str, Any]:
        status = self._log.status()
        return {**status, "zone_error": self._zone_error} if self._zone_error else status

    def execute(self, task: Task) -> TaskResult:
        if task.capability != CAPABILITY:
            raise AdapterProtocolError(f"unsupported work capability: {task.capability}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output={"work": self._log.aggregates()},
            evidence=("work:read-only-aggregates",),
        )
