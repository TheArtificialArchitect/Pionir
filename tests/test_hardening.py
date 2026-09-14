"""The smaller fixes, each verified against its artifact."""

import argparse
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pionir import bryofeed, cli
from pionir.approvals import ApprovalQueue
from pionir.config import PionirSettings
from pionir.contracts import ModelRequirement
from pionir.cortex import Cortex
from pionir.errors import ResourceUnavailable
from pionir.scheduler import ModelLeaseScheduler, ResourceBudget
from pionir.shared_gpu import SharedGpuLock


class ApprovalExpiryTests(unittest.TestCase):
    def _rewrite(self, path: Path, **changes) -> None:
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            row.update(changes)
        path.write_text(json.dumps(rows), encoding="utf-8")

    def test_new_rows_carry_a_24h_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            q = ApprovalQueue(Path(tmp) / "q.json")
            aid = q.enqueue("x.y", {}, ["p"], "s")
            row = q.get(aid)
            delta = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
            self.assertEqual(delta, timedelta(hours=24))

    def test_expired_pending_rows_are_auto_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            q = ApprovalQueue(path)
            aid = q.enqueue("x.y", {}, ["p"], "s")
            self._rewrite(path, expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
            self.assertEqual(q.pending(), [])
            row = q.get(aid)
            self.assertEqual(row["status"], "denied")
            self.assertEqual(row["reason"], "expired")
            self.assertIsNone(q.claim(aid))   # cannot be run any more

    def test_rows_without_expiry_expire_from_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            q = ApprovalQueue(path)
            aid = q.enqueue("x.y", {}, ["p"], "s")
            self._rewrite(path, expires_at=None,
                          created_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
            self.assertEqual(q.pending(), [])
            self.assertEqual(q.get(aid)["status"], "denied")

    def test_old_resolved_rows_are_dropped_on_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            q = ApprovalQueue(path)
            old = q.enqueue("x.y", {}, ["p"], "old")
            q.resolve(old, "denied")
            self._rewrite(path, resolved_at=(datetime.now(UTC) - timedelta(days=8)).isoformat())
            fresh = q.enqueue("x.y", {}, ["p"], "fresh")   # a write
            self.assertIsNone(q.get(old))
            self.assertIsNotNone(q.get(fresh))

    def test_claim_is_exclusive_and_resolve_needs_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            q = ApprovalQueue(Path(tmp) / "q.json")
            aid = q.enqueue("x.y", {}, ["p"], "s")
            self.assertIsNotNone(q.claim(aid, task_id="t1"))
            self.assertIsNone(q.claim(aid))
            self.assertFalse(q.resolve(aid, "approved"))                          # not pending
            self.assertTrue(q.resolve(aid, "approved", {"ok": True}, from_status="running"))
            self.assertEqual(q.get(aid)["task_id"], "t1")

    def test_running_row_is_failed_closed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            q = ApprovalQueue(path)
            aid = q.enqueue("x.y", {}, ["p"], "s")
            self.assertIsNotNone(q.claim(aid, task_id="t1"))
            restarted = ApprovalQueue(path)
            row = restarted.get(aid)
            self.assertEqual(row["status"], "denied")
            self.assertEqual(row["reason"], "interrupted")


class ProtectedModelTests(unittest.TestCase):
    def _budget(self) -> ResourceBudget:
        return ResourceBudget(total_vram_mb=12_288, reserved_vram_mb=1_830, max_gpu_leases=1)

    def test_a_protected_model_is_never_evicted_and_the_lease_is_refused(self) -> None:
        evicted: list[str] = []
        scheduler = ModelLeaseScheduler(
            self._budget(),
            vram_probe=lambda: 1_000,
            residency_probe=lambda model: False,
            evict_to_fit=True,
            evictor=lambda name: evicted.append(name),
            loaded_probe=lambda: ["gemma3:12b"],
            sleep=lambda _s: None,
            protected_models=["gemma3:12b"],
        )
        with self.assertRaises(ResourceUnavailable) as caught:
            scheduler.acquire(ModelRequirement("qwen2.5:7b-instruct", 4_500, 345))
        self.assertEqual(evicted, [])
        self.assertIn("protected", str(caught.exception))
        self.assertIn("gemma3:12b", str(caught.exception))

    def test_room_is_made_from_unprotected_models_only(self) -> None:
        evicted: list[str] = []
        state = {"free": 1_000}

        def evict(name: str) -> None:
            evicted.append(name)
            state["free"] = 8_000

        scheduler = ModelLeaseScheduler(
            self._budget(),
            vram_probe=lambda: state["free"],
            residency_probe=lambda model: False,
            evict_to_fit=True,
            evictor=evict,
            loaded_probe=lambda: ["gemma3:12b", "other:7b"],
            sleep=lambda _s: None,
            protected_models=["gemma3:12b"],
        )
        lease = scheduler.acquire(ModelRequirement("qwen2.5:7b-instruct", 4_500, 345))
        self.assertEqual(evicted, ["other:7b"])
        lease.release()


class SharedGpuHolderTests(unittest.TestCase):
    def test_refusal_names_the_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            self.assertIsNone(lock.holder())
            lease = lock.try_acquire(owner="bryo", purpose="dreaming")
            try:
                held = lock.holder()
                self.assertEqual(held["owner"], "bryo")
                self.assertEqual(held["pid"], os.getpid())
                self.assertIn("purpose=dreaming", lock.describe_holder())
                scheduler = ModelLeaseScheduler(
                    ResourceBudget(), lock, vram_probe=None, residency_probe=None
                )
                with self.assertRaises(ResourceUnavailable) as caught:
                    scheduler.acquire(ModelRequirement("m", 100))
                self.assertIn("owner=bryo", str(caught.exception))
            finally:
                lease.release()


class FoldTests(unittest.TestCase):
    def test_fold_writes_episode_and_retires_turns_together(self) -> None:
        c = Cortex(":memory:")
        ids = [c.remember("message", f"turn {i} about the deploy", namespace="chat") for i in range(6)]
        episode_id, fact_ids = c.fold("chat", ids, "they settled the deploy", ["deploy is Friday"],
                                      meta={"folded_turns": 6})
        self.assertEqual(len(c.memories("chat", kind="message")), 0)
        self.assertEqual(c.get(episode_id).meta["folded_turns"], 6)
        self.assertEqual(len(fact_ids), 1)
        self.assertEqual(c.get(fact_ids[0]).kind, "fact")

    def test_fold_is_all_or_nothing(self) -> None:
        c = Cortex(":memory:")
        ids = [c.remember("message", f"turn {i}", namespace="chat") for i in range(3)]
        with self.assertRaises(Exception):
            c.fold("chat", [*ids, object()], "an episode")   # the UPDATE cannot bind
        self.assertEqual(len(c.memories("chat", kind="message")), 3)   # turns still live
        self.assertEqual(len(c.memories("chat", kind="episode")), 0)   # no orphan episode

    def test_fold_cannot_retire_another_namespace(self) -> None:
        c = Cortex(":memory:")
        own = c.remember("message", "own turn", namespace="chat")
        other = c.remember("message", "private turn", namespace="private")
        c.fold("chat", [own, other], "chat episode")
        self.assertEqual(len(c.memories("chat", kind="message")), 0)
        self.assertEqual(len(c.memories("private", kind="message")), 1)

    def test_memories_limit_none_lists_everything(self) -> None:
        c = Cortex(":memory:")
        for i in range(1_205):
            c.remember("message", f"turn {i}", namespace="long")
        self.assertEqual(len(c.memories("long", kind="message")), 1_000)
        self.assertEqual(len(c.memories("long", kind="message", limit=None)), 1_205)


class DistilModelTests(unittest.TestCase):
    def test_settings_read_distil_and_protected_models_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "PIONIR_STATE_ROOT": tmp,
            "PIONIR_DISTIL_MODEL": "llama3:8b",
            "PIONIR_PROTECTED_MODELS": "gemma3:12b, theo-local:latest",
        }):
            settings = PionirSettings.from_environment()
        self.assertEqual(settings.distil_model, "llama3:8b")
        self.assertEqual(settings.protected_models, ("gemma3:12b", "theo-local:latest"))
        self.assertEqual(PionirSettings(state_root=Path(tmp)).distil_model, "qwen3:4b-instruct-2507-q4_K_M")
        self.assertNotEqual(PionirSettings(state_root=Path(tmp)).distil_model,
                            PionirSettings(state_root=Path(tmp)).embed_model)

    def test_consolidate_refuses_the_embed_model(self) -> None:
        runtime = SimpleNamespace(
            settings=SimpleNamespace(distil_model="nomic-embed-text", embed_model="nomic-embed-text"),
            cortex=None,
        )
        args = argparse.Namespace(command="consolidate", model=None, namespace="chat", min_turns=None)
        with self.assertRaises(ValueError) as caught:
            cli._execute(args, runtime)
        self.assertIn("cannot chat", str(caught.exception))


class BryofeedUrlTests(unittest.TestCase):
    def test_audit_is_asked_with_n_not_limit(self) -> None:
        seen: list[str] = []

        def fake_get(url, timeout):
            seen.append(url)
            if url.endswith("/api/state"):
                return {"gpu": {}, "roster": []}
            return {"events_total": 3}

        with mock.patch.object(bryofeed, "_get_json", fake_get):
            snap, total = bryofeed.snapshot("http://127.0.0.1:8780", prev_events_total=1)
        self.assertEqual(total, 3)
        self.assertIn("http://127.0.0.1:8780/api/audit?n=1", seen)
        self.assertFalse(any("limit=" in u for u in seen))


if __name__ == "__main__":
    unittest.main()
