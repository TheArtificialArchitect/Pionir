"""The suite itself must never reach a live service.

Found the hard way: runtimes built by older tests left ``embed_model`` at its default,
so a failed task's lesson went to the LIVE Ollama embedder, which answered only after
its 30 s timeout while the card was full - an approval test timed out on the owner's
machine and passed on CI. Several pointed Galatea at the live Moss port 8799 too.
These checks read the test sources, so a new test copied from an old pattern fails
here, by name, instead of as a flaky timeout.
"""
import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LIVE_ADDRESSES = ("127.0.0.1:8799", "localhost:8799", ":11434")


def _sources() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"), filename=str(p)))
            for p in sorted(TESTS.glob("*.py")) if p.name != Path(__file__).name]


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def _keywords(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg is not None}


class HermeticSuiteTests(unittest.TestCase):
    def test_no_test_names_a_live_service_address(self) -> None:
        offenders = [
            f"{path.name}:{node.lineno} {node.value!r}"
            for path, tree in _sources() for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and any(addr in node.value for addr in LIVE_ADDRESSES)
            and "\n" not in node.value          # prose in a docstring may name the port
        ]
        self.assertEqual(offenders, [])

    def test_every_runtime_a_test_builds_has_embeddings_off(self) -> None:
        # Runtime-shaped settings: built straight into build_runtime(...), assigned to
        # a name that is then passed to it, or carrying the specialist wiring
        # (atani_command / galatea_url) that only a runtime needs - which is what
        # catches the shared ``_settings()`` helpers. A bare defaults check is not one.
        offenders = []
        for path, tree in _sources():
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                built = [c.args[0] for c in _calls(fn, "build_runtime") if c.args]
                fed = {a.id for a in built if isinstance(a, ast.Name)}
                assigned = {id(n.value) for n in ast.walk(fn) if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id in fed for t in n.targets)}
                for call in _calls(fn, "PionirSettings"):
                    kws = _keywords(call)
                    runtime_shaped = (any(call is a for a in built) or id(call) in assigned
                                      or "atani_command" in kws or "galatea_url" in kws)
                    if runtime_shaped and "embed_model" not in kws:
                        offenders.append(f"{path.name}:{call.lineno} in {fn.name}()")
        self.assertEqual(offenders, [], "pass embed_model=None (see tests/standins.py)")

if __name__ == "__main__":
    unittest.main()
