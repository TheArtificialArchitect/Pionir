"""The ledger under more than one writer, and after a crash mid-write.

The server and a CLI command each hold their own sink on the one file. Before
this, each cached the chain head at construction, so the second writer appended
with a stale previous_sha256, and the next start-up's full verify refused to run
until the file was hand-edited.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pionir.audit import JsonlAuditSink
from pionir.errors import AuditIntegrityError
from pionir.runtime import AuditEvent


def _event(agent: str = "atani") -> AuditEvent:
    return AuditEvent("task.routed", uuid4(), agent, datetime.now(UTC), "outcome=route")


class MultiWriterTests(unittest.TestCase):
    def test_two_sinks_on_one_file_keep_a_valid_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            a = JsonlAuditSink(path, fsync=False)
            b = JsonlAuditSink(path, fsync=False)   # built when the file is still empty
            for i in range(6):
                (a if i % 2 == 0 else b).record(_event(f"writer{i % 2}"))
            count, head = JsonlAuditSink(path, fsync=False).verify()
            self.assertEqual(count, 6)
            self.assertEqual(len(head), 64)
            # each sink individually agrees, from its own view
            self.assertEqual(a.verify()[0], 6)
            self.assertEqual(b.verify()[0], 6)

    def test_construction_does_not_run_a_full_verify(self) -> None:
        # A tampered line in the middle: verify() must still catch it, but
        # building a sink (every CLI command) must not refuse to start.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlAuditSink(path, fsync=False)
            for _ in range(3):
                sink.record(_event())
            lines = path.read_text(encoding="utf-8").splitlines()
            middle = json.loads(lines[1])
            middle["agent_id"] = "tampered"
            lines[1] = json.dumps(middle, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            again = JsonlAuditSink(path, fsync=False)   # must not raise
            with self.assertRaises(AuditIntegrityError):
                again.verify()
            # and it can still append, chaining from the (intact) last line
            again.record(_event())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["sequence"], 4)

    def test_a_truncated_tail_is_reported_then_healed_by_the_next_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlAuditSink(path, fsync=False)
            sink.record(_event())
            sink.record(_event())
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write('{"sequence": 3, "previous_sha256": "abc", "half')  # crash mid-write
            with self.assertRaises(AuditIntegrityError) as caught:
                sink.verify()
            self.assertIn("partial line", str(caught.exception))
            fresh = JsonlAuditSink(path, fsync=False)   # start-up still works
            fresh.record(_event())
            count, _ = fresh.verify()
            self.assertEqual(count, 3)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"half', text)
            self.assertTrue(text.endswith("\n"))
            self.assertEqual(sink.verify()[0], 3)   # the other sink sees the healed file

    def test_recent_reads_the_tail_and_verify_caches_by_stat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlAuditSink(path, fsync=False)
            for i in range(40):
                sink.record(_event(f"a{i}"))
            recent = sink.recent(5)
            self.assertEqual([e["agent_id"] for e in recent], ["a39", "a38", "a37", "a36", "a35"])
            first = sink.verify()
            self.assertIs(sink.verify(), first)       # cached: the same tuple object
            other = JsonlAuditSink(path, fsync=False)
            other.record(_event("b"))
            self.assertEqual(sink.verify()[0], 41)    # invalidated by the file changing
            self.assertEqual(sink.recent(1)[0]["agent_id"], "b")

    def test_recent_is_honest_about_a_partial_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlAuditSink(path, fsync=False)
            sink.record(_event("whole"))
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write('{"sequence": 2, "agent_id": "torn"')
            self.assertEqual([e["agent_id"] for e in sink.recent(5)], ["whole"])


if __name__ == "__main__":
    unittest.main()
