import unittest

from pionir.errors import CircuitOpen
from pionir.reliability import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ReliabilityTests(unittest.TestCase):
    def test_opens_after_threshold_and_recovers_with_one_probe(self) -> None:
        clock = FakeClock()
        circuit = CircuitBreaker(
            failure_threshold=2,
            recovery_seconds=10,
            clock=clock,
        )
        circuit.before_call()
        circuit.record_failure()
        circuit.before_call()
        circuit.record_failure()
        self.assertEqual(circuit.snapshot().state, CircuitState.OPEN)
        with self.assertRaises(CircuitOpen):
            circuit.before_call()
        clock.now = 10
        circuit.before_call()
        self.assertEqual(circuit.snapshot().state, CircuitState.HALF_OPEN)
        with self.assertRaises(CircuitOpen):
            circuit.before_call()
        circuit.record_success()
        self.assertEqual(circuit.snapshot().state, CircuitState.CLOSED)

    def test_failed_recovery_probe_reopens_circuit(self) -> None:
        clock = FakeClock()
        circuit = CircuitBreaker(failure_threshold=1, recovery_seconds=5, clock=clock)
        circuit.record_failure()
        clock.now = 5
        circuit.before_call()
        circuit.record_failure()
        self.assertEqual(circuit.snapshot().state, CircuitState.OPEN)

    def test_unattempted_recovery_probe_does_not_wedge_the_circuit(self) -> None:
        clock = FakeClock()
        circuit = CircuitBreaker(failure_threshold=1, recovery_seconds=5, clock=clock)
        circuit.record_failure()
        clock.now = 5
        circuit.before_call()
        self.assertEqual(circuit.snapshot().state, CircuitState.HALF_OPEN)

        # The probe never reached the specialist, so it is not a health signal.
        circuit.record_unattempted()
        self.assertEqual(circuit.snapshot().state, CircuitState.OPEN)
        self.assertEqual(circuit.snapshot().consecutive_failures, 1)

        # The recovery window restarts rather than hot-looping on a busy resource.
        with self.assertRaises(CircuitOpen):
            circuit.before_call()
        clock.now = 10
        circuit.before_call()
        circuit.record_success()
        self.assertEqual(circuit.snapshot().state, CircuitState.CLOSED)

    def test_unattempted_call_on_a_closed_circuit_changes_nothing(self) -> None:
        circuit = CircuitBreaker(failure_threshold=2, recovery_seconds=5)
        circuit.before_call()
        circuit.record_unattempted()
        self.assertEqual(circuit.snapshot().state, CircuitState.CLOSED)
        circuit.before_call()


if __name__ == "__main__":
    unittest.main()
