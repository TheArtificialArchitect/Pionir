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
        offenders = [f"{path.name}:{call.lineno} in {fn.name}()"
                     for path, fn, call in _runtime_settings()
                     if "embed_model" not in _keywords(call)]
        self.assertEqual(offenders, [], "pass embed_model=None (see tests/standins.py)")

    def test_no_runtime_turns_owner_notify_on_without_faking_discord(self) -> None:
        # owner.notify is wired to the REAL bot token file and DISCORD_CHANNEL_ID (the
        # Discord gate's settings, from the environment). The rule: a runtime-shaped
        # PionirSettings whose owner_notify is anything but the literal False must sit in
        # a function that patches the Discord client - a patch(...) naming DiscordRest.
        # quotes.card (quote_cards) is wired to the same bot and channel: the same rule.
        offenders = []
        for path, fn, call in _runtime_settings():
            values = [k.value for k in call.keywords if k.arg in ("owner_notify", "quote_cards")]
            if all(isinstance(v, ast.Constant) and v.value is False for v in values):
                continue
            fakes = [c for name in ("patch", "object") for c in _calls(fn, name)
                     if any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                            and "DiscordRest" in n.value for n in ast.walk(c))]
            if not fakes:
                offenders.append(f"{path.name}:{call.lineno} in {fn.name}()")
        self.assertEqual(offenders, [], "patch pionir.discord_gate.DiscordRest in that test "
                                        "(or leave owner_notify off)")


def _runtime_settings():
    """(path, function, PionirSettings call) for every runtime-shaped settings in the
    tests: built straight into build_runtime(...), assigned to a name that is then passed
    to it, or carrying the specialist wiring (atani_command / galatea_url) that only a
    runtime needs - which is what catches the shared ``_settings()`` helpers. A bare
    defaults check is not one."""
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
                if (any(call is a for a in built) or id(call) in assigned
                        or "atani_command" in kws or "galatea_url" in kws):
                    yield path, fn, call


if __name__ == "__main__":
    unittest.main()
