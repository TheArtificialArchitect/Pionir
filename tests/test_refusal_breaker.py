"""A doer correctly refusing is not a broken doer.

The runtime circuit breaker exists to stop hammering a BROKEN specialist. A
Voodoo/Nyx policy refusal is the doer doing its job, so ``outcome_kind`` splits
it from a fault in the one shared place, and the breaker heeds that: refusals
record ``task.failed`` (honestly, prefixed ``refused:``) but do not count
toward - or reset - the consecutive-failure count, and never trip the circuit.
Real failures still trip it exactly as before, and any shape without an explicit
refusal signal is treated as a fault (fail closed toward counting).
"""

import unittest

from support import offline_scheduler

from pionir.contracts import (
    AgentManifest,
    Capability,
    Task,
    TaskResult,
    outcome_kind,
)
from pionir.errors import CircuitOpen
from pionir.reliability import CircuitBreaker, CircuitState
from pionir.runtime import Executive, InMemoryAuditSink

# The real output shapes an adapter hands back (nyx_status/voodoo_status
# run_action wraps the child's JSON under "output").
NYX_REFUSAL = {"ok": False, "returncode": 1, "output": {"refused": "offensive tools disabled"}}
NYX_UNAVAILABLE = {"ok": False, "returncode": 1, "output": {"unavailable": "no route"}}
VOODOO_REFUSAL = {"ok": False, "returncode": 2, "output": "Refused: no scope for that host"}
REAL_FAILURE = {"ok": False, "error": "boom"}
NONZERO_RC = {"returncode": 2, "output": "traceback..."}


class Adapter:
    """A voodoo specialist returning whatever output it is handed, per call."""

    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls = 0
        self._manifest = AgentManifest(
            "voodoo", "test", (Capability("security.voodoo_run", "Run a Voodoo action"),)
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.calls += 1
        return TaskResult(task.task_id, "voodoo", self.output)


def _executive(output: dict, lessons: list[str] | None = None, threshold: int = 3):
    ex = Executive(
        scheduler=offline_scheduler(),
        audit_sink=InMemoryAuditSink(),
        circuit_factory=lambda: CircuitBreaker(failure_threshold=threshold, recovery_seconds=60),
        on_lesson=(lessons.append if lessons is not None else None),
    )
    adapter = Adapter(output)
    ex.register(adapter)
    return ex, adapter


def _run(ex) -> None:
    ex.execute(Task("security.voodoo_run", {}))


def _failures(ex) -> int:
    return ex.circuit("voodoo").snapshot().consecutive_failures


def _state(ex) -> CircuitState:
    return ex.circuit("voodoo").snapshot().state


class ClassifierTests(unittest.TestCase):
    def test_explicit_signals_are_refused(self) -> None:
        for output in (NYX_REFUSAL, NYX_UNAVAILABLE, VOODOO_REFUSAL):
            with self.subTest(output=output):
                self.assertEqual(outcome_kind(output), "refused")

    def test_success_is_ok(self) -> None:
        self.assertEqual(outcome_kind({"ok": True, "returncode": 0}), "ok")

    def test_unknown_failing_shape_is_failure(self) -> None:
        # No explicit refusal signal - fail closed toward counting. "refused" in a
        # value (not a key) is NOT a signal; nor is rc 2 without the "Refused:"
        # text; nor a Daedalus-style ok=false with a reason string.
        for output in (
            REAL_FAILURE,
            NONZERO_RC,
            {"ok": False, "error": "the job was refused by the gate"},
            {"ok": False, "returncode": 1, "output": {"error": "policy denied"}},
            {"ok": False, "gate": {"passed": False, "reason": "germline forbids it"}},
            {"returncode": 3, "output": "Refused: text present but exit code is not 2"},
        ):
            with self.subTest(output=output):
                self.assertEqual(outcome_kind(output), "failed")


class BreakerTests(unittest.TestCase):
    def test_three_refusals_do_not_open_the_circuit(self) -> None:
        ex, adapter = _executive(VOODOO_REFUSAL)
        for _ in range(5):
            _run(ex)
        self.assertEqual(adapter.calls, 5)  # never blocked
        self.assertEqual(_failures(ex), 0)
        self.assertIs(_state(ex), CircuitState.CLOSED)

    def test_three_real_failures_still_open_the_circuit(self) -> None:
        ex, adapter = _executive(REAL_FAILURE)
        for count in range(1, 4):
            _run(ex)
            self.assertEqual(_failures(ex), count)
        self.assertIs(_state(ex), CircuitState.OPEN)
        with self.assertRaises(CircuitOpen):
            _run(ex)
        self.assertEqual(adapter.calls, 3)

    def test_a_refusal_between_failures_does_not_reset_the_count(self) -> None:
        ex, adapter = _executive(REAL_FAILURE)
        _run(ex)
        self.assertEqual(_failures(ex), 1)
        adapter.output = VOODOO_REFUSAL
        _run(ex)  # a refusal in the middle
        self.assertEqual(_failures(ex), 1, "a refusal must neither count nor reset")
        adapter.output = REAL_FAILURE
        _run(ex)
        self.assertEqual(_failures(ex), 2, "the second real failure resumes the count")

    def test_a_success_still_resets_the_count(self) -> None:
        ex, adapter = _executive(REAL_FAILURE)
        _run(ex)
        adapter.output = {"ok": True}
        _run(ex)
        self.assertEqual(_failures(ex), 0)


class LedgerAndLessonTests(unittest.TestCase):
    def test_ledger_marks_a_refusal_and_the_result_still_fails(self) -> None:
        ex, _ = _executive(VOODOO_REFUSAL)
        _run(ex)
        last = ex.audit_sink.events[-1]
        self.assertEqual(last.event_type, "task.failed")
        self.assertTrue(last.detail.startswith("refused:"), last.detail)

    def test_a_real_failure_ledger_reason_is_not_marked_refused(self) -> None:
        ex, _ = _executive(REAL_FAILURE)
        _run(ex)
        self.assertFalse(ex.audit_sink.events[-1].detail.startswith("refused:"))

    def test_a_refusal_records_a_refusal_lesson_not_a_circuit_trip_lesson(self) -> None:
        lessons: list[str] = []
        ex, _ = _executive(VOODOO_REFUSAL, lessons=lessons)
        for _ in range(4):
            _run(ex)
        self.assertTrue(lessons, "a refusal should be teachable")
        self.assertTrue(all("refused a task" in text for text in lessons))
        self.assertFalse(any("circuit opened" in text for text in lessons))

    def test_a_half_open_probe_that_refuses_does_not_wedge_the_circuit(self) -> None:
        # Trip the circuit with real failures, wait out recovery, then let the
        # probe return a refusal: the probe slot must be handed back (not left in
        # flight) and the failure count must be preserved, not reset.
        clock = [1000.0]
        ex = Executive(
            scheduler=offline_scheduler(),
            audit_sink=InMemoryAuditSink(),
            circuit_factory=lambda: CircuitBreaker(
                failure_threshold=3, recovery_seconds=30, clock=lambda: clock[0]
            ),
        )
        adapter = Adapter(REAL_FAILURE)
        ex.register(adapter)
        for _ in range(3):
            _run(ex)
        self.assertIs(_state(ex), CircuitState.OPEN)
        clock[0] += 31  # recovery window elapses -> next call is the half-open probe
        adapter.output = VOODOO_REFUSAL
        _run(ex)  # the probe refuses
        self.assertEqual(_failures(ex), 3, "a refusal must not reset the count")
        # The slot was returned: the circuit reopened cleanly rather than staying
        # half-open with a probe stuck in flight. After the window it probes again.
        clock[0] += 31
        adapter.output = {"ok": True}
        _run(ex)
        self.assertIs(_state(ex), CircuitState.CLOSED)


if __name__ == "__main__":
    unittest.main()
