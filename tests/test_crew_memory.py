"""One crew agent's private memory: what it keeps, how it recalls, how it forgets.

Each test fails if the behaviour it names is reverted: the ``room`` columns
coming back instead of ``channel``, provenance lost or loosened (the
anti-confabulation critic reads it), an intention closed without an outcome
and a reason, or fold eating the episodes that mattered.
"""

import unittest
from pathlib import Path

from crew_support import temp_dir

from pionir.crew.memory import DAY, FOLD_AFTER_DAYS, SOURCES, Memory

NOW = 1_800_000_000


class _MemoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "agents" / "ada.db"
        self.mem = Memory(self.path, "ada")
        # a lambda, so it closes whichever Memory is current after a reopen
        self.addCleanup(lambda: self.mem.close())

    def reopen(self) -> Memory:
        self.mem.close()
        self.mem = Memory(self.path, "ada")
        return self.mem


class ChannelRenameTests(_MemoryCase):
    def _columns(self, table: str) -> set:
        return {r[1] for r in self.mem.conn.execute(f"PRAGMA table_info({table})")}

    def test_every_room_column_is_now_a_channel(self) -> None:
        for table, col in (("episodes", "channel"), ("beliefs", "channel"),
                           ("people", "last_seen_channel"), ("habits", "channel"),
                           ("summaries", "channel")):
            cols = self._columns(table)
            self.assertIn(col, cols, table)
            self.assertFalse({c for c in cols if "room" in c}, table)

    def test_channel_round_trips_through_every_read(self) -> None:
        self.mem.add_episode(NOW, "#billing", "said", "the invoice is late", source="heard")
        self.assertEqual(self.mem.recent(1)[0]["channel"], "#billing")
        self.mem.believe("invoice-42", "status", "late", NOW, "#billing")
        self.assertEqual(self.mem.beliefs_in_channel("#billing"),
                         {"invoice-42": {"status": ("late", NOW)}})
        self.assertEqual(self.mem.all_beliefs()["invoice-42"]["status"][2], "#billing")
        p = self.mem.person("ian")
        p.update(last_seen_t=NOW, last_seen_channel="#billing", channel_seconds=12.5)
        self.mem.save_person(p)
        self.reopen()
        back = self.mem.person("ian")
        self.assertEqual(back["last_seen_channel"], "#billing")
        self.assertEqual(back["channel_seconds"], 12.5)
        for _ in range(3):
            self.mem.habit_hit("triage", "#billing", 9, NOW, relieved=True)
        self.assertEqual(self.mem.habits()[0]["channel"], "#billing")


class ProvenanceTests(_MemoryCase):
    def test_every_source_is_kept_exactly_as_written(self) -> None:
        for source in sorted(SOURCES):
            self.mem.add_episode(NOW, "#ops", "event", f"a {source} thing", source=source,
                                 told_by="ian" if source == "told" else None)
        self.reopen()
        back = {ep["text"]: ep for ep in self.mem.recent(20)}
        for source in SOURCES:
            self.assertEqual(back[f"a {source} thing"]["source"], source)
        self.assertEqual(back["a told thing"]["told_by"], "ian")

    def test_the_seven_sources_are_the_hearth_seven(self) -> None:
        self.assertEqual(SOURCES, {"seen", "did", "told", "heard", "inferred", "felt", "noticed"})

    def test_an_unknown_source_is_refused_and_nothing_is_written(self) -> None:
        with self.assertRaises(ValueError):
            self.mem.add_episode(NOW, "#ops", "event", "I remember this", source="imagined")
        self.assertEqual(self.mem.episode_count(), 0)


class RecallTests(_MemoryCase):
    def test_recall_ranks_the_matching_episode_first(self) -> None:
        self.mem.add_episode(NOW - 60, "#ops", "event", "the backup job failed overnight",
                             source="seen")
        self.mem.add_episode(NOW - 60, "#ops", "event", "someone made coffee", source="seen")
        top = self.mem.recall(NOW, query="backup failed", limit=1)
        self.assertEqual(top[0]["text"], "the backup job failed overnight")

    def test_recall_prefers_the_same_channel_and_the_same_people(self) -> None:
        self.mem.add_episode(NOW, "#sales", "event", "a quote went out", source="did")
        self.mem.add_episode(NOW, "#ops", "event", "a quote went out", source="did",
                             people=["ian"])
        self.assertEqual(self.mem.recall(NOW, channel="#sales", limit=1)[0]["channel"], "#sales")
        self.assertEqual(self.mem.recall(NOW, people=["ian"], limit=1)[0]["channel"], "#ops")

    def test_recall_prefers_recent_over_old(self) -> None:
        self.mem.add_episode(NOW - 10 * DAY, "#ops", "event", "old news", source="seen")
        self.mem.add_episode(NOW - 60, "#ops", "event", "new news", source="seen")
        self.assertEqual(self.mem.recall(NOW, query="news", limit=1)[0]["text"], "new news")


class FoldTests(_MemoryCase):
    def test_fold_summarises_old_trivia_and_keeps_what_mattered(self) -> None:
        old = NOW - (FOLD_AFTER_DAYS + 1) * DAY
        for i in range(3):
            self.mem.add_episode(old + i, "#ops", "tick", f"routine {i}", salience=0.5,
                                 source="noticed")
        self.mem.add_episode(old, "#ops", "incident", "the database was lost", salience=4.0,
                             source="seen")
        self.mem.add_episode(NOW, "#ops", "tick", "routine today", salience=0.5, source="noticed")
        removed = self.mem.fold(NOW)
        self.assertEqual(removed, 3)
        kept = {ep["text"]: ep for ep in self.mem.recent(10)}
        self.assertEqual(set(kept), {"the database was lost", "routine today"})
        self.assertEqual(kept["the database was lost"]["source"], "seen")
        summary = self.mem.summaries()
        self.assertEqual(len(summary), 1)
        self.assertEqual((summary[0]["kind"], summary[0]["channel"], summary[0]["count"]),
                         ("tick", "#ops", 3))
        self.assertIn("in #ops", summary[0]["text"])


class IntentionTests(_MemoryCase):
    def _open(self) -> int:
        iid = self.mem.add_intention(NOW, "chase the late invoice", "task", "invoice-42", None, None)
        self.assertIsNotNone(iid)
        return iid

    def test_an_intention_will_not_close_without_a_reason(self) -> None:
        iid = self._open()
        for bad in ("", "  ", "ok"):
            with self.assertRaises(ValueError):
                self.mem.resolve_intention(iid, "done", bad, NOW)
        self.assertEqual([it["id"] for it in self.mem.open_intentions()], [iid])

    def test_an_intention_will_not_close_without_an_outcome(self) -> None:
        iid = self._open()
        with self.assertRaises(ValueError):
            self.mem.resolve_intention(iid, "closed", "it was paid on Tuesday", NOW)
        self.assertEqual(len(self.mem.open_intentions()), 1)

    def test_with_an_outcome_and_a_reason_it_closes(self) -> None:
        iid = self._open()
        self.mem.resolve_intention(iid, "done", "it was paid on Tuesday", NOW)
        self.assertEqual(self.mem.open_intentions(), [])
        self.assertEqual(self.mem.recent_intentions(1)[0]["result"], "it was paid on Tuesday")

    def test_the_same_want_is_not_opened_twice(self) -> None:
        self._open()
        self.assertIsNone(self.mem.add_intention(NOW, "Chase the late invoice ", "task", None,
                                                 None, None))


if __name__ == "__main__":
    unittest.main()
