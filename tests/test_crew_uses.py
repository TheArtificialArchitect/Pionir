"""Each crew worker declares, in the catalogue, the Pionir capabilities it calls (``uses``).

The declaration is what /api/divisions reports per worker, so Pionir Desktop can show
each worker's approval level from Pionir's own /api/capabilities instead of a copied
map. A declaration nobody checks drifts, so this test reads the worker classes
themselves: every ``Job(<capability>, ...)`` their code can build - in their methods,
inherited ones, and the crew functions those call - must be declared, and every
declared capability must be one the code calls. A capability the scan cannot resolve
to a name is a failure too (fail closed): name it plainly or teach the scan.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import types
import unittest

from pionir.crew import workers as workers_module
from pionir.crew.hands import Job
from pionir.crew.registry import build_registry, load_catalogue

_CREW = "pionir.crew"


def _in_crew(obj) -> bool:
    return (getattr(obj, "__module__", "") or "").startswith(_CREW)


class _Scan:
    """Every capability a worker class's code can put in a Job."""

    def __init__(self, cls: type) -> None:
        self.cls = cls
        self.found: set[str] = set()
        self.unresolved: list[str] = []
        self._seen: set = set()
        self._queue: list = []
        for _name, fn in inspect.getmembers(cls, inspect.isfunction):
            self._add(fn)
        while self._queue:
            self._scan(self._queue.pop())

    def _add(self, obj) -> None:
        if inspect.isclass(obj):
            if _in_crew(obj) and obj not in self._seen:
                self._seen.add(obj)
                for _name, fn in inspect.getmembers(obj, inspect.isfunction):
                    self._add(fn)
            return
        fn = inspect.unwrap(obj) if callable(obj) else obj
        if inspect.isfunction(fn) and _in_crew(fn) and fn not in self._seen:
            self._seen.add(fn)
            self._queue.append(fn)

    def _scan(self, fn) -> None:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        except (OSError, TypeError, SyntaxError):
            return
        env = fn.__globals__
        local = self._locals(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = self._value(node.func, env)
            if target is Job:
                arg = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg == "capability"), None)
                where = f"{fn.__module__}.{fn.__qualname__}:{getattr(node, 'lineno', '?')}"
                if arg is None:
                    self.unresolved.append(f"{where}: Job without a capability")
                    continue
                names = self._names(arg, env, local)
                if names is None:
                    self.unresolved.append(f"{where}: {ast.unparse(arg)}")
                else:
                    self.found |= names
            elif target is not None:
                self._add(target)

    @staticmethod
    def _locals(tree) -> dict:
        assigned: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        assigned.setdefault(t.id, []).append(node.value)
        return assigned

    def _value(self, expr, env):
        """The object an expression names, when it is a plain global or module attribute
        (or a method of the worker class through ``self``); None otherwise."""
        if isinstance(expr, ast.Name):
            return env.get(expr.id)
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in ("self", "cls"):
                return getattr(self.cls, expr.attr, None)
            base = self._value(expr.value, env)
            if isinstance(base, (types.ModuleType, type)):
                return getattr(base, expr.attr, None)
        return None

    def _names(self, expr, env, local, depth: int = 0) -> set | None:
        if depth > 8:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.IfExp):
            a = self._names(expr.body, env, local, depth + 1)
            b = self._names(expr.orelse, env, local, depth + 1)
            return None if a is None or b is None else a | b
        if isinstance(expr, ast.Name) and expr.id in local:
            out: set = set()
            for value in local[expr.id]:
                got = self._names(value, env, local, depth + 1)
                if got is None:
                    return None
                out |= got
            return out
        if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "get"):
            table = self._value(expr.func.value, env)
            if isinstance(table, dict) and all(isinstance(v, str) for v in table.values()):
                out = set(table.values())
                if len(expr.args) > 1:
                    got = self._names(expr.args[1], env, local, depth + 1)
                    if got is None:
                        return None
                    out |= got
                return out
            return None
        if isinstance(expr, ast.Subscript):
            table = self._value(expr.value, env)
            if isinstance(table, dict) and all(isinstance(v, str) for v in table.values()):
                return set(table.values())
            return None
        value = self._value(expr, env)
        return {value} if isinstance(value, str) else None


def _worker_class(impl: str) -> type:
    """The class a catalogue ``impl`` builds (a factory function imports it lazily)."""
    factory = workers_module.IMPLS[impl]
    if inspect.isclass(factory):
        return factory
    tree = ast.parse(textwrap.dedent(inspect.getsource(factory)))
    ret = next(n for n in ast.walk(tree) if isinstance(n, ast.Return))
    name = ret.value.func.id  # type: ignore[union-attr]
    imp = next(n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and any(a.name == name for a in n.names))
    module = __import__(f"pionir.crew.{imp.module}", fromlist=[name]) if imp.level else \
        __import__(imp.module, fromlist=[name])
    return getattr(module, name)


class DeclaredUsesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = load_catalogue()
        self.registry = build_registry(self.catalogue)

    def test_every_worker_declares_exactly_the_capabilities_its_code_calls(self) -> None:
        for division in self.catalogue["divisions"]:
            for entry in division["workers"]:
                wid = f"{division['id']}.{entry['name']}"
                with self.subTest(worker=wid):
                    scan = _Scan(_worker_class(entry["impl"]))
                    self.assertEqual(scan.unresolved, [],
                                     f"{wid}: a Job capability the scan cannot name")
                    declared = set(self.registry.uses(wid))
                    self.assertEqual(
                        declared, scan.found,
                        f"{wid}: catalogue 'uses' {sorted(declared)} but its code calls "
                        f"{sorted(scan.found)}")

    def test_the_scan_sees_through_indirection(self) -> None:
        # the cases the catalogue relies on: a method override (blog/instagram _job), a
        # local chosen by a conditional (delivery), a dict lookup with a default (orders)
        # and a module function imported from a sibling (fiverr desk -> gigs)
        self.assertEqual(_Scan(_worker_class("blog_writer")).found, {"content.publish"})
        self.assertEqual(_Scan(_worker_class("delivery_desk")).found >=
                         {"client.deliver", "client.release"}, True)
        self.assertIn("client.quote_reminder", _Scan(_worker_class("order_desk")).found)
        self.assertIn("fiverr.card", _Scan(_worker_class("fiverr_desk")).found)

    def test_uses_is_validated_at_load(self) -> None:
        for bad in ("content.publish", ["content publish"], [""], [3],
                    ["content.publish", "content.publish"]):
            catalogue = load_catalogue()
            catalogue["divisions"][0]["workers"][0]["uses"] = bad
            with self.subTest(uses=bad), self.assertRaises(ValueError):
                build_registry(catalogue)


if __name__ == "__main__":
    unittest.main()
