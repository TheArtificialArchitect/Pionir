import tempfile
import unittest
from pathlib import Path

from pionir.contracts import ModelRequirement
from pionir.errors import ResourceUnavailable
from pionir.scheduler import ModelLeaseScheduler, ResourceBudget
from pionir.shared_gpu import SharedGpuLock


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = ModelLeaseScheduler(
            ResourceBudget(total_vram_mb=12_288, reserved_vram_mb=1_024, max_gpu_leases=1)
        )

    def test_only_one_gpu_model_is_admitted(self) -> None:
        first = self.scheduler.acquire(ModelRequirement("theo", 4_700, 1_500))
        with self.assertRaises(ResourceUnavailable):
            self.scheduler.acquire(ModelRequirement("qwen", 4_700, 1_500))
        first.release()
        second = self.scheduler.acquire(ModelRequirement("qwen", 4_700, 1_500))
        second.release()

    def test_context_overhead_participates_in_admission(self) -> None:
        with self.assertRaises(ResourceUnavailable):
            self.scheduler.acquire(ModelRequirement("too-large", 10_000, 2_000))

    def test_context_manager_releases_lease(self) -> None:
        with self.scheduler.acquire(ModelRequirement("theo", 4_700)):
            self.assertEqual(len(self.scheduler.active_requirements), 1)
        self.assertEqual(self.scheduler.active_requirements, ())

    def test_cpu_leases_do_not_consume_gpu_slots(self) -> None:
        cpu = self.scheduler.acquire(ModelRequirement("bryo-cpu", 0, requires_gpu=False))
        gpu = self.scheduler.acquire(ModelRequirement("theo", 4_700))
        self.assertEqual(len(self.scheduler.active_requirements), 2)
        cpu.release()
        gpu.release()

    def test_shared_lock_excludes_another_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shared_lock = SharedGpuLock(Path(directory) / "gpu.lock")
            first = ModelLeaseScheduler(self.scheduler.budget, shared_lock)
            second = ModelLeaseScheduler(self.scheduler.budget, shared_lock)
            lease = first.acquire(ModelRequirement("atani", 4_700))
            with self.assertRaises(ResourceUnavailable):
                second.acquire(ModelRequirement("theo", 4_700))
            lease.release()
            second.acquire(ModelRequirement("theo", 4_700)).release()


if __name__ == "__main__":
    unittest.main()
