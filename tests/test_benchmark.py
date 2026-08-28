"""Tests for the Phase 0 measurement module.

These exercise the honesty guarantees rather than the arithmetic: the module's
value is that it refuses to report a contended or warm measurement as a clean
cold one, so those refusals are what is asserted here.
"""

import unittest
from unittest import mock

from pionir.benchmark import (
    BenchmarkError,
    ContextMeasurement,
    GpuMemory,
    LoadedModel,
    _context_slope,
    _residency,
    await_eviction,
    benchmark_model,
    unload_all,
)


def _model(name: str, size: int, vram: int, ctx: int = 4096) -> LoadedModel:
    return LoadedModel(name=name, size_bytes=size, size_vram_bytes=vram, context_length=ctx)


class LoadedModelTests(unittest.TestCase):
    def test_reports_full_gpu_residency_only_when_nothing_spilled(self) -> None:
        self.assertTrue(_model("a", 1000, 1000).fully_on_gpu)
        self.assertFalse(_model("a", 1000, 999).fully_on_gpu)

    def test_a_model_with_no_size_is_never_called_fully_resident(self) -> None:
        # An empty inventory entry must not read as "entirely on the GPU".
        self.assertFalse(_model("a", 0, 0).fully_on_gpu)

    def test_converts_bytes_to_whole_megabytes(self) -> None:
        self.assertEqual(_model("a", 0, 4 * 1024 * 1024).size_vram_mb, 4)


class ContextSlopeTests(unittest.TestCase):
    def _measure(self, num_ctx: int, vram: int, fully: bool = True) -> ContextMeasurement:
        return ContextMeasurement(
            num_ctx=num_ctx, size_vram_mb=vram, driver_delta_mb=0, fully_on_gpu=fully
        )

    def test_computes_megabytes_per_thousand_context_tokens(self) -> None:
        slope = _context_slope([self._measure(4096, 4423), self._measure(16384, 4792)])
        self.assertAlmostEqual(slope, 30.75, places=2)

    def test_excludes_points_where_the_model_spilled_to_system_memory(self) -> None:
        # A spilled model's VRAM figure is capped by the card, not by its KV
        # growth, so differencing it would invent a slope.
        slope = _context_slope(
            [self._measure(4096, 4423), self._measure(16384, 4423, fully=False)]
        )
        self.assertIsNone(slope)

    def test_declines_to_guess_from_a_single_point(self) -> None:
        self.assertIsNone(_context_slope([self._measure(4096, 4423)]))


class ResidencyTests(unittest.TestCase):
    def test_separates_our_model_from_models_a_third_party_loaded(self) -> None:
        with mock.patch(
            "pionir.benchmark.read_loaded_models",
            return_value=[_model("mine", 10, 10), _model("theirs", 10, 10)],
        ):
            mine, foreign = _residency("mine", "http://x")
        self.assertIsNotNone(mine)
        self.assertEqual(foreign, ("theirs",))

    def test_reports_absence_without_raising(self) -> None:
        with mock.patch(
            "pionir.benchmark.read_loaded_models", return_value=[_model("theirs", 10, 10)]
        ):
            mine, foreign = _residency("mine", "http://x")
        self.assertIsNone(mine)
        self.assertEqual(foreign, ("theirs",))


class AwaitEvictionTests(unittest.TestCase):
    def test_refuses_to_proceed_while_a_model_is_still_resident(self) -> None:
        # Proceeding here would time a warm start and label it a cold one.
        with mock.patch(
            "pionir.benchmark.read_loaded_models", return_value=[_model("stuck", 10, 10)]
        ):
            with self.assertRaises(BenchmarkError) as caught:
                await_eviction("http://x", timeout_seconds=0.1, settle_seconds=0)
        self.assertIn("stuck", str(caught.exception))

    def test_returns_a_driver_sample_once_the_daemon_is_empty(self) -> None:
        sample = GpuMemory(total_mb=12288, used_mb=1830, free_mb=10458)
        with mock.patch("pionir.benchmark.read_loaded_models", return_value=[]):
            with mock.patch("pionir.benchmark.read_gpu_memory", return_value=sample):
                self.assertEqual(await_eviction("http://x", settle_seconds=0), sample)


class UnloadAllTests(unittest.TestCase):
    def test_evicts_every_resident_model_and_names_them(self) -> None:
        with mock.patch(
            "pionir.benchmark.read_loaded_models",
            return_value=[_model("a", 10, 10), _model("b", 10, 10)],
        ):
            with mock.patch("pionir.benchmark.unload") as unload_one:
                evicted = unload_all("http://x")
        self.assertEqual(evicted, ["a", "b"])
        self.assertEqual([call.args[0] for call in unload_one.call_args_list], ["a", "b"])


class BenchmarkModelTests(unittest.TestCase):
    def test_records_contention_when_our_model_is_evicted_after_answering(self) -> None:
        sample = GpuMemory(total_mb=12288, used_mb=1830, free_mb=10458)
        with mock.patch("pionir.benchmark.unload_all", return_value=[]), mock.patch(
            "pionir.benchmark.await_eviction", return_value=sample
        ), mock.patch("pionir.benchmark._generate", return_value=1.0), mock.patch(
            "pionir.benchmark._residency", return_value=(None, ("squatter",))
        ):
            result = benchmark_model("big", contexts=(4096,), base_url="http://x")

        self.assertTrue(result.evicted_by_contention)
        self.assertEqual(result.foreign_resident, ("squatter",))
        self.assertIn("squatter", result.error or "")
        # It must not pass off a zero as a measured footprint.
        self.assertEqual(result.resident_vram_mb, 0)

    def test_records_the_third_party_alongside_a_successful_measurement(self) -> None:
        sample = GpuMemory(total_mb=12288, used_mb=1830, free_mb=10458)
        loaded = GpuMemory(total_mb=12288, used_mb=6253, free_mb=6035)
        with mock.patch("pionir.benchmark.unload_all", return_value=[]), mock.patch(
            "pionir.benchmark.await_eviction", return_value=sample
        ), mock.patch("pionir.benchmark._generate", return_value=1.0), mock.patch(
            "pionir.benchmark.read_gpu_memory", return_value=loaded
        ), mock.patch(
            "pionir.benchmark._residency",
            return_value=(_model("mine", 4423 * 1024 * 1024, 4423 * 1024 * 1024), ("other",)),
        ):
            result = benchmark_model("mine", contexts=(4096,), base_url="http://x")

        self.assertIsNone(result.error)
        self.assertEqual(result.resident_vram_mb, 4423)
        self.assertEqual(result.foreign_resident, ("other",))
        self.assertTrue(result.contexts[0].contended)


if __name__ == "__main__":
    unittest.main()
