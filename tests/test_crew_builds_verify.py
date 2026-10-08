"""The test command Daedalus is told to pass is the one our review accepts on.

2026-10-05: json-to-xlsx's tests imported ``from src.json_to_xlsx...``. Daedalus's gate
(pytest from the repo root) and the command the brief named (``$env:PYTHONPATH='src';
python -m unittest``, which also puts the current folder on the path) both passed it; our
review runs ``python -I -S`` with only src/ on the path and REJECTED it, and the night's
build was shelved. Three spellings of "the tests pass" disagreed (HEAD 3.8). Now the brief
and Daedalus's verify run the review's own code, so the build fails where the review would.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pionir.crew.builds import review
from pionir.crew.builds.worker import BuildsWorker


class VerifyIsTheReviewsCommandTests(unittest.TestCase):
    def verify(self) -> str:
        return BuildsWorker._verify(None, {"language": "python"})

    def test_it_runs_the_reviews_exact_suite_code_isolated(self) -> None:
        cmd = self.verify()
        self.assertIn(" -I -S -c ", cmd)
        self.assertIn(review.SUITE_RUNNER, cmd)
        self.assertNotIn("PYTHONPATH", cmd)            # -I ignores it: it promised nothing

    def test_a_src_dot_import_fails_it_and_a_package_import_passes_it(self) -> None:
        cmd = self.verify()
        code = cmd.split(" -c ", 1)[1].strip().strip('"')
        self.assertEqual(code, review.SUITE_RUNNER)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "pkg").mkdir(parents=True)
            (root / "src" / "pkg" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
            (root / "tests").mkdir()
            test = root / "tests" / "test_x.py"

            def run(line: str) -> int:
                test.write_text(f"import unittest\n{line}\n"
                                "class T(unittest.TestCase):\n"
                                "    def test_x(self):\n        self.assertEqual(X, 1)\n",
                                encoding="utf-8")
                env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
                return subprocess.run([sys.executable, "-I", "-S", "-c", code], cwd=root,
                                      env=env, capture_output=True, timeout=60).returncode

            self.assertEqual(run("from pkg import X"), 0)
            self.assertNotEqual(run("from src.pkg import X"), 0)


if __name__ == "__main__":
    unittest.main()
