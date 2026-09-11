import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import offline_scheduler

from pionir.benchmark import LoadedModel
from pionir.contracts import ModelRequirement
from pionir.errors import ResourceUnavailable
from pionir.scheduler import (
    KV_CACHE_MB_PER_1K_TOKENS,
    ModelLeaseScheduler,
    ResourceBudget,
    canonical_model,
    kv_cache_vram_mb,
    model_already_resident,
)
from pionir.shared_gpu import SharedGpuLock


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        # These exercise lease accounting against the static budget. The probes
        # are switched off so the assertions do not depend on whatever the
        # developer's card happens to be doing; the observed-VRAM gate has its
        # own tests below, with deterministic probes.
        self.scheduler = ModelLeaseScheduler(
            ResourceBudget(total_vram_mb=12_288, reserved_vram_mb=1_024, max_gpu_leases=1),
            vram_probe=None,
            residency_probe=None,
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
            first = offline_scheduler(self.scheduler.budget, shared_lock)
            second = offline_scheduler(self.scheduler.budget, shared_lock)
            lease = first.acquire(ModelRequirement("atani", 4_700))
            with self.assertRaises(ResourceUnavailable):
                second.acquire(ModelRequirement("theo", 4_700))
            lease.release()
            second.acquire(ModelRequirement("theo", 4_700)).release()


class ObservedVramTests(unittest.TestCase):
    """The budget says what Pionir allows itself; the card says what is free.

    Processes outside Pionir hold VRAM without taking the shared lock, so lease
    accounting alone cannot tell whether a model will fit. Overcommitting does
    not raise: Ollama silently runs the model on the CPU and answers correctly
    at a seventh of the speed, so this gate is the only place it can be caught.
    """

    def _scheduler(self, free_mb, resident=()):
        return ModelLeaseScheduler(
            ResourceBudget(total_vram_mb=12_288, reserved_vram_mb=1_830, max_gpu_leases=1),
            vram_probe=lambda: free_mb,
            residency_probe=lambda model_id: model_id in resident,
        )

    def test_refuses_a_model_the_budget_allows_but_the_card_cannot_hold(self) -> None:
        requirement = ModelRequirement("theo", 4_500, 690)
        self.assertLess(requirement.total_vram_mb, 10_458)  # the budget would allow it
        with self.assertRaises(ResourceUnavailable) as caught:
            self._scheduler(free_mb=3_000).acquire(requirement)
        self.assertIn("3000 MB free", str(caught.exception))

    def test_admits_when_the_card_has_room(self) -> None:
        lease = self._scheduler(free_mb=8_000).acquire(ModelRequirement("theo", 4_500, 690))
        lease.release()

    def test_falls_back_to_the_budget_when_vram_cannot_be_measured(self) -> None:
        # None means unmeasurable, not zero. Refusing everything on a machine
        # with no NVIDIA tooling would make Pionir unusable there and would fail
        # CI, for no gain: the static budget check still applies.
        scheduler = ModelLeaseScheduler(
            ResourceBudget(), vram_probe=lambda: None, residency_probe=lambda _: False
        )
        lease = scheduler.acquire(ModelRequirement("theo", 4_500, 690))
        lease.release()

    def test_a_resident_model_costs_only_its_context(self) -> None:
        # Genesis keeps qwen2.5:7b-instruct hot, which is also Atani's model.
        # Reusing the resident copy costs the KV cache, not the weights again,
        # so this must be admitted where a cold load of the same model is not.
        requirement = ModelRequirement("qwen2.5:7b-instruct", 4_500, 690)
        with self.assertRaises(ResourceUnavailable):
            self._scheduler(free_mb=1_000).acquire(requirement)
        lease = self._scheduler(
            free_mb=1_000, resident=("qwen2.5:7b-instruct",)
        ).acquire(requirement)
        lease.release()

    def test_a_cpu_model_is_not_measured_against_the_card_at_all(self) -> None:
        # nemotron measures at zero VRAM: Ollama runs it on the CPU and it still
        # returns 30 tok/s because only ~3B parameters are active. It must not
        # be refused for card space it never takes.
        probed = []

        def probe():
            probed.append(True)
            return 0

        scheduler = ModelLeaseScheduler(
            ResourceBudget(), vram_probe=probe, residency_probe=lambda _: False
        )
        lease = scheduler.acquire(
            ModelRequirement("nemotron", 0, 0, requires_gpu=False)
        )
        lease.release()
        self.assertEqual(probed, [])

    def test_a_cpu_model_does_not_consume_the_single_gpu_lease(self) -> None:
        scheduler = self._scheduler(free_mb=8_000)
        depth = scheduler.acquire(ModelRequirement("nemotron", 0, 0, requires_gpu=False))
        chat = scheduler.acquire(ModelRequirement("theo", 4_500, 690))
        chat.release()
        depth.release()


class ContextCostTests(unittest.TestCase):
    def test_context_cost_comes_from_the_measured_rate(self) -> None:
        self.assertEqual(kv_cache_vram_mb(16_384), math.ceil(16 * KV_CACHE_MB_PER_1K_TOKENS))
        self.assertEqual(kv_cache_vram_mb(0), 0)

    def test_a_wider_context_always_costs_more(self) -> None:
        self.assertGreater(kv_cache_vram_mb(32_768), kv_cache_vram_mb(4_096))

    def test_a_negative_context_is_refused_rather_than_returning_a_credit(self) -> None:
        with self.assertRaises(ValueError):
            kv_cache_vram_mb(-1)


if __name__ == "__main__":
    unittest.main()


class ModelNameTests(unittest.TestCase):
    """Ollama reports `name:tag`; Theo's promotion writes the name untagged.

    Comparing the two verbatim never matches, so the residency discount would
    silently never apply and every turn would be charged full weights.
    """

    def test_an_untagged_name_means_latest(self) -> None:
        self.assertEqual(canonical_model("theo-local-v25-q4"), "theo-local-v25-q4:latest")

    def test_an_explicit_tag_is_left_alone(self) -> None:
        self.assertEqual(canonical_model("theo-local-v25-q4:latest"), "theo-local-v25-q4:latest")
        self.assertEqual(canonical_model("qwen2.5:7b-instruct"), "qwen2.5:7b-instruct")

    def test_residency_matches_across_the_tag_difference(self) -> None:
        loaded = LoadedModel(
            name="theo-local-v25-q4:latest",
            size_bytes=100,
            size_vram_bytes=100,
            context_length=16384,
        )
        with patch("pionir.scheduler.read_loaded_models", return_value=[loaded]):
            # What the promotion mechanism writes, without a tag.
            self.assertTrue(model_already_resident("theo-local-v25-q4"))
            # And a different build must still not match.
            self.assertFalse(model_already_resident("theo-local-v17-q4"))


class EvictionTests(unittest.TestCase):
    """Sidelining an idle resident model to make room for an on-demand doer.

    Everything is injected - no real daemon is touched - so the suite can prove
    the policy without ever unloading a live model.
    """

    def _budget(self) -> ResourceBudget:
        return ResourceBudget(total_vram_mb=12_288, reserved_vram_mb=1_830, max_gpu_leases=1)

    def test_sidelines_an_idle_model_to_make_room(self) -> None:
        evicted: list[str] = []
        state = {"free": 1_000}  # the card is full of the voice's big model

        def evict(name: str) -> None:
            evicted.append(name)
            state["free"] = 8_000  # freeing it opens the room

        scheduler = ModelLeaseScheduler(
            self._budget(),
            vram_probe=lambda: state["free"],
            residency_probe=lambda model: False,
            evict_to_fit=True,
            evictor=evict,
            loaded_probe=lambda: ["gemma3:12b", "qwen2.5:7b-instruct"],
            sleep=lambda _s: None,
        )
        lease = scheduler.acquire(ModelRequirement("qwen2.5:7b-instruct", 4_500, 345))
        # The target model is never evicted; the idle voice model is.
        self.assertEqual(evicted, ["gemma3:12b"])
        lease.release()

    def test_refuses_when_sidelining_cannot_free_enough(self) -> None:
        scheduler = ModelLeaseScheduler(
            self._budget(),
            vram_probe=lambda: 1_000,
            residency_probe=lambda model: False,
            evict_to_fit=True,
            evictor=lambda name: None,  # frees nothing
            loaded_probe=lambda: ["something-small"],
            sleep=lambda _s: None,
        )
        with self.assertRaises(ResourceUnavailable):
            scheduler.acquire(ModelRequirement("qwen2.5:7b-instruct", 4_500, 345))

    def test_never_evicts_when_the_flag_is_off(self) -> None:
        evicted: list[str] = []
        scheduler = ModelLeaseScheduler(
            self._budget(),
            vram_probe=lambda: 1_000,
            residency_probe=lambda model: False,
            evict_to_fit=False,
            evictor=lambda name: evicted.append(name),
            loaded_probe=lambda: ["gemma3:12b"],
            sleep=lambda _s: None,
        )
        with self.assertRaises(ResourceUnavailable):
            scheduler.acquire(ModelRequirement("qwen2.5:7b-instruct", 4_500, 345))
        self.assertEqual(evicted, [])
