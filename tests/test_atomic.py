"""The retried swap every store uses: a destination another process holds for a
moment (an antivirus scan, the search indexer) must not fail the write - on Windows
os.replace raises PermissionError until the holder lets go. Found by an approval
whose job finished while its queue file was held: the record stayed "running"."""
import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pionir import atomic


class ReplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.tmp = self.root / "q.json.tmp"
        self.dest = self.root / "q.json"
        self.tmp.write_text("new", encoding="utf-8")
        self.dest.write_text("old", encoding="utf-8")

    def tearDown(self) -> None:
        self._t.cleanup()

    def test_a_briefly_held_destination_is_retried_until_the_swap_lands(self) -> None:
        real = atomic.os.replace
        held = [PermissionError(13, "Access is denied")] * 3

        def flaky(src, dst):
            if held:
                raise held.pop()
            real(src, dst)

        with mock.patch.object(atomic.os, "replace", side_effect=flaky) as swap, \
                mock.patch.object(atomic.time, "sleep") as sleep:
            atomic.replace(self.tmp, self.dest)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "new")
        self.assertFalse(self.tmp.exists())
        self.assertEqual(swap.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_a_destination_held_past_the_budget_raises_and_keeps_the_old_file(self) -> None:
        with mock.patch.object(atomic.os, "replace",
                               side_effect=PermissionError(13, "Access is denied")) as swap, \
                mock.patch.object(atomic.time, "sleep"), \
                self.assertRaises(PermissionError):
            atomic.replace(self.tmp, self.dest)
        self.assertEqual(swap.call_count, atomic.ATTEMPTS)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "old")

    def test_other_errors_are_not_retried(self) -> None:
        with mock.patch.object(atomic.os, "replace",
                               side_effect=FileNotFoundError(2, "gone")) as swap, \
                self.assertRaises(FileNotFoundError):
            atomic.replace(self.tmp, self.dest)
        self.assertEqual(swap.call_count, 1)

    def test_write_text_lands_whole_and_leaves_no_temp_file(self) -> None:
        atomic.write_text(self.dest, "written")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "written")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["q.json"])


class NoRawSwapTests(unittest.TestCase):
    def test_every_store_swaps_through_atomic_replace(self) -> None:
        # A raw os.replace / tmp.replace in a store is the bug above waiting for a
        # scanner to hold the file. Only pionir.atomic may call os.replace itself.
        src = Path(atomic.__file__).resolve().parent
        offenders = []
        for path in sorted(src.rglob("*.py")):
            if path.name == "atomic.py" and path.parent == src:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "replace" and isinstance(node.func.value, ast.Name)):
                    continue
                owner = node.func.value.id
                if owner == "os" or ("tmp" in owner.lower() and len(node.args) == 1):
                    offenders.append(f"{path.relative_to(src)}:{node.lineno}")
        self.assertEqual(offenders, [], "use pionir.atomic.replace(tmp, dest)")


if __name__ == "__main__":
    unittest.main()
