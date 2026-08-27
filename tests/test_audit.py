import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pionir.audit import JsonlAuditSink
from pionir.errors import AuditIntegrityError
from pionir.runtime import AuditEvent


class AuditTests(unittest.TestCase):
    def test_writes_and_verifies_hash_chain_without_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit" / "events.jsonl"
            sink = JsonlAuditSink(path, fsync=False)
            task_id = uuid4()
            sink.record(AuditEvent("task.routed", task_id, "theo", datetime.now(UTC)))
            sink.record(
                AuditEvent("task.completed", task_id, "theo", datetime.now(UTC))
            )
            sequence, digest = sink.verify()
            self.assertEqual(sequence, 2)
            self.assertEqual(len(digest), 64)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("payload", text)
            self.assertNotIn("output", text)

    def test_detects_modified_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlAuditSink(path, fsync=False)
            sink.record(
                AuditEvent("task.routed", uuid4(), "atani", datetime.now(UTC))
            )
            record = json.loads(path.read_text(encoding="utf-8"))
            record["agent_id"] = "tampered"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaises(AuditIntegrityError):
                sink.verify()


if __name__ == "__main__":
    unittest.main()
