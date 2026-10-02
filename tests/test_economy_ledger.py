"""The Bolts ledger: an append-only hash chain whose balances are derived, never stored.

Each test fails if its rule is reverted: an edit, deletion, reorder or tail cut that verified
clean; a paid event paid twice; a torn final line dropped without a word; a broken chain
that still accepted rows. Everything lives in a temp dir; the real ~/.pionir is never read.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pionir.economy.ledger import (GENESIS, InsufficientBolts, Ledger, LedgerBroken,
                                   LedgerError, row_hash)

SRC = str(Path(__file__).resolve().parents[1] / "src")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ledger = Ledger.in_dir(self.dir, now=Clock())

    def fill(self, n: int = 4) -> None:
        for i in range(n):
            self.ledger.append(f"acct.{i % 2}", 10 + i, "work", f"ev:{i}")

    def lines(self) -> list[str]:
        return self.ledger.path.read_text(encoding="ascii").splitlines()

    def write_lines(self, lines: list[str]) -> None:
        self.ledger.path.write_text("\n".join(lines) + "\n", encoding="ascii")


class BalanceTests(LedgerCase):
    def test_balances_are_derived_by_replay_and_accounts_appear_on_first_credit(self) -> None:
        self.assertEqual(self.ledger.balances(), {})
        self.ledger.append("posting.blog", 10, "post", "e1")
        self.ledger.append("leader.posting", 4, "lead", "e2")
        self.ledger.append("posting.blog", 5, "post", "e3")
        self.assertEqual(self.ledger.balances(), {"posting.blog": 15, "leader.posting": 4})
        self.assertEqual(self.ledger.balance("never.paid"), 0)
        self.assertFalse(any(p.name.startswith("balances") for p in self.dir.iterdir()))

    def test_rows_chain_from_genesis_and_carry_every_field(self) -> None:
        self.fill(2)
        a, b = self.ledger.rows()
        self.assertEqual((a.seq, a.prev_hash), (0, GENESIS))
        self.assertEqual((b.seq, b.prev_hash), (1, a.hash))
        self.assertEqual(a.hash, row_hash(a.seq, a.ts, a.account, a.delta, a.reason,
                                         a.event_id, a.prev_hash))
        self.assertEqual(set(json.loads(self.lines()[0])),
                         {"seq", "ts", "account", "delta", "reason", "event_id",
                          "prev_hash", "hash"})

    def test_bad_input_is_refused(self) -> None:
        for args in (("Bad Account", 1, "r", "e"), ("ok", 1.5, "r", "e"),
                     ("ok", True, "r", "e"), ("ok", 1, "r", " ")):
            with self.assertRaises(LedgerError, msg=str(args)):
                self.ledger.append(*args)
        self.assertEqual(self.ledger.rows(), [])

    def test_a_debit_cannot_overdraw(self) -> None:
        self.ledger.append("a", 5, "r", "e1")
        with self.assertRaises(InsufficientBolts):
            self.ledger.append("a", -6, "spend", "e2")
        self.assertEqual(self.ledger.append("a", -5, "spend", "e3").row.delta, -5)
        self.assertEqual(self.ledger.balance("a"), 0)


class IdempotenceTests(LedgerCase):
    def test_the_same_event_is_refused_and_returns_the_original_row(self) -> None:
        first = self.ledger.append("a", 7, "r", "approval:1")
        again = self.ledger.append("a", 99, "other reason", "approval:1")
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(again.row, first.row)
        self.assertEqual(len(self.ledger.rows()), 1)
        self.assertEqual(self.ledger.balance("a"), 7)


class TamperTests(LedgerCase):
    def assertBrokenAt(self, seq: int) -> None:
        st = self.ledger.verify_chain()
        self.assertFalse(st.ok, st)
        self.assertEqual(st.first_bad_seq, seq, st)
        self.assertEqual(st.label, f"BROKEN at seq {seq}")

    def test_a_clean_chain_verifies(self) -> None:
        self.fill(5)
        st = self.ledger.verify_chain()
        self.assertTrue(st.ok)
        self.assertEqual((st.rows, st.label), (5, "OK"))

    def test_an_edited_amount_is_caught_at_that_row(self) -> None:
        self.fill(5)
        lines = self.lines()
        row = json.loads(lines[2])
        row["delta"] = 9999
        lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.write_lines(lines)
        self.assertBrokenAt(2)

    def test_a_recomputed_hash_still_breaks_the_next_row(self) -> None:
        self.fill(5)
        lines = self.lines()
        row = json.loads(lines[1])
        row["delta"] = 500
        row["hash"] = row_hash(row["seq"], row["ts"], row["account"], row["delta"],
                               row["reason"], row["event_id"], row["prev_hash"])
        lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.write_lines(lines)
        self.assertBrokenAt(2)

    def test_a_deleted_middle_row_is_caught(self) -> None:
        self.fill(5)
        lines = self.lines()
        del lines[2]
        self.write_lines(lines)
        self.assertBrokenAt(2)

    def test_reordered_rows_are_caught(self) -> None:
        self.fill(5)
        lines = self.lines()
        lines[1], lines[3] = lines[3], lines[1]
        self.write_lines(lines)
        self.assertBrokenAt(1)

    def test_deleting_the_last_row_is_caught_by_the_head_anchor(self) -> None:
        self.fill(5)
        self.write_lines(self.lines()[:-1])
        st = self.ledger.verify_chain()
        self.assertFalse(st.ok)
        self.assertIn("cut off", st.problem)

    def test_deleting_the_whole_ledger_file_is_caught(self) -> None:
        self.fill(3)
        self.ledger.path.unlink()
        self.assertFalse(self.ledger.verify_chain().ok)

    def test_rewriting_the_last_row_with_a_valid_hash_is_caught_by_the_anchor(self) -> None:
        self.fill(3)
        lines = self.lines()
        row = json.loads(lines[-1])
        row["delta"] = 1
        row["hash"] = row_hash(row["seq"], row["ts"], row["account"], row["delta"],
                               row["reason"], row["event_id"], row["prev_hash"])
        lines[-1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.write_lines(lines)
        self.assertBrokenAt(2)

    def test_a_garbage_line_is_caught(self) -> None:
        self.fill(3)
        lines = self.lines()
        lines.insert(1, "not json at all")
        self.write_lines(lines)
        self.assertBrokenAt(1)

    def test_the_first_bad_seq_is_the_earliest_one(self) -> None:
        self.fill(6)
        lines = self.lines()
        for i in (4, 2):
            row = json.loads(lines[i])
            row["account"] = "someone.else"
            lines[i] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.write_lines(lines)
        self.assertBrokenAt(2)

    def test_a_broken_chain_accepts_nothing_more(self) -> None:
        self.fill(3)
        lines = self.lines()
        del lines[1]
        self.write_lines(lines)
        with self.assertRaises(LedgerBroken):
            self.ledger.append("a", 1, "r", "new")
        self.assertEqual(len(self.lines()), 2)

    def test_a_crash_between_row_and_anchor_is_tolerated_and_healed(self) -> None:
        self.fill(3)
        row = self.ledger.rows()[1]
        self.ledger.head_path.write_text(json.dumps({"seq": row.seq, "hash": row.hash}))
        self.assertTrue(self.ledger.verify_chain().ok)
        self.ledger.append("a", 1, "r", "after-crash")
        self.assertTrue(self.ledger.verify_chain().ok)
        self.assertEqual(json.loads(self.ledger.head_path.read_text())["seq"], 3)


class TornLineTests(LedgerCase):
    def tear(self) -> bytes:
        torn = b'{"seq":3,"ts":18000'
        with self.ledger.path.open("ab") as fh:
            fh.write(torn)
        return torn

    def test_a_torn_final_line_is_reported_not_dropped(self) -> None:
        self.fill(3)
        before = self.ledger.path.read_bytes()
        self.tear()
        st = self.ledger.verify_chain()
        self.assertTrue(st.torn)
        self.assertEqual(st.torn_bytes, 19)
        self.assertEqual(st.label, "TORN TAIL")
        self.assertEqual(len(self.ledger.rows()), 3)
        self.assertEqual(self.ledger.path.read_bytes(), before + b'{"seq":3,"ts":18000')

    def test_the_next_append_sets_it_aside_loudly_and_carries_on(self) -> None:
        self.fill(3)
        torn = self.tear()
        with self.assertLogs("pionir.economy", level="ERROR") as logs:
            done = self.ledger.append("a", 3, "r", "after-torn")
        self.assertTrue(done.created)
        self.assertIn("torn", logs.output[0])
        st = self.ledger.verify_chain()
        self.assertTrue(st.ok and not st.torn, st)
        self.assertEqual(st.quarantined, 1)
        kept = list(self.dir.glob("bolts.jsonl.torn-*"))
        self.assertEqual([p.read_bytes() for p in kept], [torn])
        self.assertEqual(len(self.ledger.rows()), 4)


class ConcurrencyTests(LedgerCase):
    def test_processes_appending_at_once_never_corrupt_the_chain(self) -> None:
        code = ("import sys\n"
                "from pathlib import Path\n"
                "from pionir.economy.ledger import Ledger\n"
                "led = Ledger.in_dir(Path(sys.argv[1]))\n"
                "for i in range(8):\n"
                "    led.append('worker.' + sys.argv[2], 1, 'x', 'p' + sys.argv[2] + ':' + str(i))\n")
        env = dict(os.environ, PYTHONPATH=SRC)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(self.dir), str(n)], env=env)
                 for n in range(4)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        st = self.ledger.verify_chain()
        self.assertTrue(st.ok, st)
        self.assertEqual(st.rows, 32)
        self.assertEqual(sum(self.ledger.balances().values()), 32)


if __name__ == "__main__":
    unittest.main()
