"""The GPU hand-off: Daedalus takes the card under the lease, and gives it back.

Every daemon hook is a fake - nothing here loads or unloads a real model. Each
test fails if the behaviour it names is reverted: Daedalus declaring itself a
CPU tenant again, protected models yielding without the lease, or the release
leaving the coder on the card and the voice's model cold.
"""

import tempfile
import unittest
from pathlib import Path

from pionir.adapters.daedalus import DaedalusAdapter
from pionir.contracts import AgentManifest, Capability, ModelRequirement, Task, TaskResult
from pionir.errors import ResourceUnavailable
from pionir.runtime import Executive
from pionir.scheduler import ModelLeaseScheduler, ResourceBudget
from pionir.shared_gpu import SharedGpuLock

CODER = "qwen3-coder:30b"
VOICE = "gemma3:12b"


class _Card:
    """A fake Ollama + driver: resident models and the free VRAM they leave."""

    SIZES = {VOICE: 7_800, "nomic-embed-text:latest": 320, CODER: 10_000}

    def __init__(self, resident: list[str], free_when_empty: int = 10_450) -> None:
        self.resident = list(resident)
        self.free_when_empty = free_when_empty
        self.unloaded: list[str] = []
        self.warmed: list[str] = []

    def free(self) -> int:
        return self.free_when_empty - sum(self.SIZES.get(m, 0) for m in self.resident)

    def loaded(self) -> list[str]:
        return list(self.resident)

    def unload(self, name: str) -> None:
        self.unloaded.append(name)
        if name in self.resident:
            self.resident.remove(name)

    def warm(self, name: str) -> None:
        self.warmed.append(name)
        if name not in self.resident:
            self.resident.append(name)


def _scheduler(card: _Card, lock: SharedGpuLock | None, **kw) -> ModelLeaseScheduler:
    kw.setdefault("sleep", lambda _s: None)
    return ModelLeaseScheduler(
        ResourceBudget(),  # the real defaults: 12_288 total, 1_830 reserved
        lock,
        vram_probe=card.free,
        residency_probe=lambda model: False,
        evict_to_fit=True,
        evictor=card.unload,
        loaded_probe=card.loaded,
        protected_models=[VOICE],
        rewarmer=card.warm,
        background=lambda work: work(),  # synchronous, so the test can see it
        **kw,
    )


class DaedalusRequirementTests(unittest.TestCase):
    def test_daedalus_is_a_gpu_tenant_with_its_measured_footprint(self) -> None:
        requirement = DaedalusAdapter().manifest.capabilities[0].model
        self.assertTrue(requirement.requires_gpu)
        self.assertEqual(requirement.model_id, CODER)
        self.assertGreaterEqual(requirement.estimated_vram_mb, 9_000)

    def test_daedalus_fits_the_budget_of_this_card(self) -> None:
        requirement = DaedalusAdapter().manifest.capabilities[0].model
        self.assertLessEqual(requirement.total_vram_mb, ResourceBudget().usable_vram_mb)


class ProtectedYieldsToTheLeaseTests(unittest.TestCase):
    def test_under_the_lease_the_voice_model_is_swapped_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _Card([VOICE])
            scheduler = _scheduler(card, SharedGpuLock(Path(tmp) / "gpu.lock"))
            lease = scheduler.acquire(ModelRequirement(CODER, 10_000, 0, exclusive_card=True))
            try:
                self.assertEqual(card.unloaded, [VOICE])
            finally:
                lease.release()

    def test_without_the_lease_protection_stands_and_says_so(self) -> None:
        card = _Card([VOICE])
        scheduler = _scheduler(card, None)
        with self.assertRaises(ResourceUnavailable) as caught:
            scheduler.acquire(ModelRequirement(CODER, 10_000, 0, exclusive_card=True))
        self.assertEqual(card.unloaded, [])
        self.assertIn("protected", str(caught.exception))

    def test_the_holder_record_names_the_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            card = _Card([])
            lease = _scheduler(card, lock).acquire(
                ModelRequirement(CODER, 10_000, 0, exclusive_card=True), purpose=f"daedalus: {CODER}"
            )
            try:
                holder = lock.holder()
                self.assertEqual(holder["owner"], "pionir")
                self.assertEqual(holder["purpose"], f"daedalus: {CODER}")
            finally:
                lease.release()


class HandbackTests(unittest.TestCase):
    def test_release_unloads_the_job_model_and_rewarms_the_voice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _Card([VOICE])
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            scheduler = _scheduler(card, lock)
            lease = scheduler.acquire(ModelRequirement(CODER, 10_000, 0, exclusive_card=True))
            card.resident.append(CODER)  # Daedalus loads it while working
            lease.release()
            self.assertEqual(card.unloaded, [VOICE, CODER])
            self.assertEqual(card.warmed, [VOICE])
            self.assertEqual(card.resident, [VOICE])
            # and the lock is free again for the next tenant
            again = lock.try_acquire(owner="test", purpose="after")
            self.assertIsNotNone(again)
            again.release()

    def test_unload_happens_before_the_lock_is_let_go(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            card = _Card([])
            seen: list[bool] = []

            def unload(name: str) -> None:
                probe = lock.try_acquire(owner="voice-probe", purpose="check")
                seen.append(probe is None)  # None: still held while unloading
                if probe is not None:
                    probe.release()
                card.unload(name)

            scheduler = _scheduler(card, lock)
            scheduler.evictor = unload
            lease = scheduler.acquire(ModelRequirement(CODER, 10_000, 0, exclusive_card=True))
            lease.release()
            self.assertEqual(seen, [True])

    def test_a_failing_handback_is_logged_never_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _Card([VOICE])
            scheduler = _scheduler(card, SharedGpuLock(Path(tmp) / "gpu.lock"))
            lease = scheduler.acquire(ModelRequirement(CODER, 10_000, 0, exclusive_card=True))

            def boom(_name: str) -> None:
                raise OSError("ollama went away")

            scheduler.evictor = boom
            scheduler.rewarmer = boom
            with self.assertLogs("pionir.scheduler", level="WARNING"):
                lease.release()
            self.assertEqual(scheduler.active_requirements, ())

    def test_a_model_already_resident_is_not_unloaded_and_nothing_to_rewarm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _Card(["qwen2.5:7b-instruct"])  # someone else's warm copy
            scheduler = _scheduler(card, SharedGpuLock(Path(tmp) / "gpu.lock"))
            lease = scheduler.acquire(ModelRequirement("qwen2.5:7b-instruct", 4_500, 0))
            lease.release()
            self.assertEqual(card.unloaded, [])
            self.assertEqual(card.warmed, [])

    def test_the_executive_takes_the_lease_for_a_daedalus_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            card = _Card([VOICE])
            scheduler = _scheduler(card, lock)
            held_during: list[dict | None] = []

            class Coder:
                manifest = AgentManifest(
                    "daedalus",
                    "test",
                    (Capability("coding.daedalus_solve", "code",
                                model=DaedalusAdapter().manifest.capabilities[0].model),),
                )

                def execute(self, task: Task) -> TaskResult:
                    held_during.append(lock.holder())
                    card.resident.append(CODER)
                    return TaskResult(task.task_id, "daedalus", {"ok": True})

            executive = Executive(scheduler=scheduler)
            executive.register(Coder())
            executive.execute(Task("coding.daedalus_solve", {"content": "x"}))
            self.assertEqual(held_during[0]["purpose"], f"daedalus: {CODER}")
            self.assertEqual(card.unloaded, [VOICE, CODER])
            self.assertEqual(card.warmed, [VOICE])


class ExclusiveCardTests(unittest.TestCase):
    """Only a capability that declares it needs the whole card (exclusive_card)
    may evict a protected model, even under the lease. An ordinary lease-holding
    GPU tenant must fit beside the protected model or be refused."""

    def test_a_non_exclusive_lease_holder_does_not_evict_the_protected_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _Card([VOICE])  # ~2650 MB free with the voice resident
            scheduler = _scheduler(card, SharedGpuLock(Path(tmp) / "gpu.lock"))
            with self.assertRaises(ResourceUnavailable) as caught:
                # A big tenant that does NOT claim the whole card: it needs room
                # the protected model is holding, but must not take it.
                scheduler.acquire(ModelRequirement("some:big-model", 10_000, 0))
            self.assertEqual(card.unloaded, [])          # the voice model kept
            self.assertIn("protected", str(caught.exception))

    def test_an_exclusive_lease_holder_may_swap_the_protected_model_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _Card([VOICE])
            scheduler = _scheduler(card, SharedGpuLock(Path(tmp) / "gpu.lock"))
            lease = scheduler.acquire(
                ModelRequirement(CODER, 10_000, 0, exclusive_card=True)
            )
            try:
                self.assertEqual(card.unloaded, [VOICE])  # the swap the fix allows
            finally:
                lease.release()

    def test_daedalus_declares_it_needs_the_whole_card(self) -> None:
        requirement = DaedalusAdapter().manifest.capabilities[0].model
        self.assertTrue(requirement.exclusive_card)


class LockWaitTests(unittest.TestCase):
    """When another process holds the shared GPU lease, Pionir waits for it up to
    a bounded time rather than refusing at once, and audits that it waited."""

    def test_waits_for_a_held_lock_then_acquires_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            holder = [lock.try_acquire(owner="bryo", purpose="governor")]  # someone else holds it
            self.assertIsNotNone(holder[0])
            card = _Card([])
            clock = [0.0]
            waited: list[str] = []

            def sleep(_s: float) -> None:
                clock[0] += 5.0
                if holder[0] is not None:      # the holder lets go while we wait
                    holder[0].release()
                    holder[0] = None

            scheduler = _scheduler(
                card, lock, lock_wait_seconds=60.0, lock_poll_seconds=5.0,
                on_wait=waited.append, sleep=sleep, monotonic=lambda: clock[0],
            )
            lease = scheduler.acquire(ModelRequirement(CODER, 10_000, 0, exclusive_card=True))
            try:
                self.assertEqual(len(waited), 1)                 # the wait was audited
                self.assertIn("waiting for the card", waited[0])
            finally:
                lease.release()

    def test_refuses_after_the_wait_runs_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            held = lock.try_acquire(owner="bryo", purpose="governor")  # never released
            self.assertIsNotNone(held)
            card = _Card([])
            clock = [0.0]

            def sleep(_s: float) -> None:
                clock[0] += 5.0

            scheduler = _scheduler(
                card, lock, lock_wait_seconds=10.0, lock_poll_seconds=5.0,
                sleep=sleep, monotonic=lambda: clock[0],
            )
            with self.assertRaises(ResourceUnavailable):
                scheduler.acquire(ModelRequirement(CODER, 10_000, 0, exclusive_card=True))
            held.release()


if __name__ == "__main__":
    unittest.main()
