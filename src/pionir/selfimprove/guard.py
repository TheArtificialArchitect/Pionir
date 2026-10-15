"""What a self-improvement patch may touch, and what it may never do - checked before Claude
reads it, again before Ian is asked, and again in the owner's repo before it is committed.

The owner's rule (IMPROVEMENT_GATE.md, FUSION_PLAN.md Phase 3): no agent promotes changes to
its own code, its own gate or its own permissions. So a patch is REFUSED, fail closed, without
spending a Claude call, unless EVERY file it touches is on the ALLOWLIST:

- an existing or new Python module under ``src/pionir/`` (never a new one directly in
  ``src/pionir/``, never an ``__init__.py``, a ``*customize.py``, ``bootstrap.py`` or a
  ``.git*`` file), or a test module ``tests/test_*.py``;
- the crew catalogue (``src/pionir/crew/catalogue.json``) - its settings, never its wiring
  (a worker's ``uses``, ``impl`` or ``provider``, or the self-improvement worker's own entry);

and none of them is PROTECTED (below, each with the reason: the approval gate and every surface
that shows or answers it, auth, money and spend rules, secrets handling, content and listing
rules, the build sandbox and everything that runs in it, the GPU lease, CI, Pionir's actuators,
the crew's wiring, the test harness, and the self-improvement worker, its gate, its tests and
EVERY module they import - tests/test_selfimprove_guard.py computes that import closure and
fails if any of it is missing here). It is also refused when it:

- deletes or renames any file, changes a binary, a link, a submodule or a file mode;
- weakens the tests: a test function removed, fewer REAL asserts in any test than before (an
  assertion of a constant or of a thing equal to itself is not one), a skip or
  expected-failure marker added, or test code reaching into the test machinery (results,
  loaders, runners, ``sys.argv``/``sys.modules``/``exit``, monkeypatching unittest or pytest);
- adds a dependency: any import, anywhere in a changed file, of a module that is neither the
  standard library nor already imported somewhere in Pionir, or any new ``__import__`` /
  ``importlib.import_module``;
- is bigger than Ian can read on his phone: ``MAX_CHARS`` characters (the whole diff is on his
  card), ``MAX_FILES`` files, ``MAX_LINES`` changed lines.

``patch_problems`` needs only the diff text (what Pionir's ``improve.apply`` holds);
``tree_problems`` adds what needs both trees (asserts per test, imports and the catalogue read
by parsing) - the worker runs both, and the apply runs both again in the owner's repo. The diff
is read exactly as git writes it: lines split on ``\\n`` only. Every reason is plain English.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field

MAX_FILES = 8
MAX_LINES = 400                     # added + removed lines, over the whole patch
MAX_CHARS = 6_000                   # the whole diff is on Ian's card, which he reads on a phone
CATALOGUE = "src/pionir/crew/catalogue.json"
SELF_WORKER = "builds.improve"
SELF_IMPL = "self_improver"

# What a patch may touch at all (case-folded globs on the repo-relative posix path).
ALLOWED = ("src/pionir/**/*.py", "tests/test_*.py", CATALOGUE)
# ... and never these, wherever they are.
FORBIDDEN_NAMES = ("__init__.py", "*customize.py", "bootstrap.py", ".git*", "conftest.py")

# (glob, why) - read top to bottom by Ian: this IS the list. ``**`` matches any depth.
PROTECTED: tuple = (
    # the approval gate, and every surface that shows or answers it
    ("src/pionir/approvals.py", "the approval gate (the queue of actions waiting on Ian)"),
    ("src/pionir/batching.py", "the approval gate (which approvals may wait for the digest)"),
    ("src/pionir/discord_gate.py", "the approval gate (Ian's Discord approve/deny cards)"),
    ("src/pionir/server.py", "the approval gate and auth (Pionir's HTTP API)"),
    ("src/pionir/contracts.py", "the approval gate (risk levels, requires_approval, spends_money)"),
    ("src/pionir/runtime.py", "the approval gate (the executive every task runs through)"),
    ("src/pionir/registry.py", "the approval gate (which adapter answers which capability)"),
    ("src/pionir/router.py", "the approval gate (what a sentence may be routed to)"),
    ("src/pionir/reliability.py", "the approval gate (the circuit breakers)"),
    ("src/pionir/audit.py", "the approval gate (the audit ledger)"),
    ("src/pionir/desktop.py", "the approval gate (the desktop's approve/deny)"),
    ("src/pionir/web/**", "the approval gate (the dashboard)"),
    ("src/pionir/adapters/**", "Pionir's actuators: what each one does and parks for approval"),
    ("src/pionir/bootstrap.py", "what Pionir registers and how"),
    ("src/pionir/cli.py", "Pionir's command line (the undo command lives there)"),
    ("src/pionir/__main__.py", "Pionir's command line"),
    # auth
    ("src/pionir/auth.py", "auth (client tokens and grants)"),
    ("src/pionir/bridge_auth.py", "auth (the Daedalus and Melete bridge tokens)"),
    ("src/pionir/signin.py", "auth (sign-in)"),
    # money and spend rules
    ("src/pionir/economy/**", "money (the Bolts ledger and payouts)"),
    ("src/pionir/crew/bolts.py", "money (Bolts)"),
    ("src/pionir/crew/budget.py", "spend rules (the crew's model-call budget)"),
    ("src/pionir/crew/escalation.py", "spend rules (the Claude daily and night caps)"),
    ("src/pionir/crew/direction.py", "spend rules (Moss's division of the caps)"),
    ("src/pionir/crew/control.py", "spend rules (the crew's run controls)"),
    ("src/pionir/crew/config.py", "spend rules and secrets paths (the crew's settings)"),
    ("src/pionir/config.py", "secrets paths, auth compat and every service URL (Pionir's settings)"),
    ("src/pionir/worklog.py", "money (the hours and income log)"),
    ("src/pionir/workapi.py", "money (the work log's API)"),
    ("src/pionir/quotes.py", "money (client quotes)"),
    ("src/pionir/fiverr.py", "money (Fiverr orders)"),
    # secrets handling
    ("src/pionir/secretscrub.py", "secrets handling (scrubbing)"),
    ("src/pionir/secret_patterns.json", "secrets handling (the key formats scanned for)"),
    ("src/pionir/crew/fiverr/checks.py", "secrets handling (the secrets and owner-data guard)"),
    ("**/*secret*", "secrets handling (a file named for secrets)"),
    ("**/*token*", "secrets handling (a file named for tokens)"),
    ("**/*cred*", "secrets handling (a file named for credentials)"),
    ("**/.env*", "secrets handling (an environment file)"),
    # content and listing rules: what may be said in public in Ian's name
    ("src/pionir/crew/contentcheck.py", "content rules (what a post or listing may say)"),
    ("src/pionir/crew/content_allowlist.json", "content rules (the names a post may use)"),
    ("src/pionir/crew/etsy/rules.py", "content rules (what an Etsy listing may claim)"),
    ("src/pionir/video/disclosure.py", "content rules (the AI disclosure on every video)"),
    ("src/pionir/crew/words.py", "content rules (the words the crew may use)"),
    ("src/pionir/social/**", "content rules (what is posted socially)"),
    # memory shared by every bot
    ("src/pionir/cortex.py", "the shared memory and its lessons"),
    ("src/pionir/memory.py", "the shared memory"),
    # the build sandbox, everything that runs in it, and the GPU lease
    ("src/pionir/build_sandbox.py", "the build sandbox (the contained pionir-builds user)"),
    ("src/pionir/sandbox_git.py", "the build sandbox (safe git on model-written repos)"),
    ("src/pionir/crew/builds/**", "the build sandbox (the night builds: its repos, its "
                                  "checks, Claude's review and its window)"),
    ("src/pionir/crew/apibuild/**", "the build sandbox (the API builder and its checks)"),
    ("src/pionir/video/tutorial.py", "the build sandbox (the video tutorial runner)"),
    ("src/pionir/shared_gpu.py", "the GPU lease (who has the card; yielding to Moss)"),
    ("src/pionir/scheduler.py", "the GPU lease (admission and handing the card back to Moss)"),
    ("src/pionir/ollama_gate.py", "the GPU lease (other programs' model calls)"),
    ("src/pionir/crew/gpu.py", "the GPU lease (the crew standing down for Moss)"),
    ("src/pionir/benchmark.py", "the GPU lease (what is loaded on the card)"),
    ("tools/**", "the setup scripts"),
    ("scripts/**", "the setup scripts"),
    ("**/*.ps1", "a PowerShell script Ian runs"),
    ("**/*.bat", "a script Ian runs"),
    ("**/*.cmd", "a script Ian runs"),
    # CI
    (".github/**", "CI"),
    # the crew's wiring to Pionir and to Claude
    ("src/pionir/crew/runtime.py", "the crew's wiring (how workers reach Pionir, Claude and the caps)"),
    ("src/pionir/crew/hands.py", "the crew's wiring (how workers act through Pionir's gate)"),
    ("src/pionir/crew/workers.py", "the crew's wiring (which worker implementations exist)"),
    ("src/pionir/crew/registry.py", "the crew's wiring (how the catalogue is loaded)"),
    ("src/pionir/crew/api.py", "the crew's wiring (the crew API and its token check)"),
    ("src/pionir/crew/worker.py", "the crew's wiring (the worker contract)"),
    ("src/pionir/crew/result.py", "the crew's wiring (the worker contract)"),
    ("src/pionir/crew/figures.py", "the crew's wiring (the worker contract)"),
    ("src/pionir/crew/log.py", "the crew's wiring (its log, which the improve worker reads)"),
    ("src/pionir/crew/grounding.py", "the crew's wiring (what a report may state)"),
    ("src/pionir/crew/store.py", "the crew's wiring (its store, which the improve worker reads)"),
    ("src/pionir/atomic.py", "the crew's wiring (atomic file writes every record relies on)"),
    ("src/pionir/errors.py", "the crew's wiring (Pionir's error types)"),
    ("src/pionir/improvement.py", "the self-improvement gate itself (PromotionGate)"),
    # the test harness and the dependency files
    ("tests/conftest.py", "the test harness"),
    ("tests/support.py", "the test harness"),
    ("pyproject.toml", "the dependencies"),
    ("**/requirements*.txt", "the dependencies"),
    ("setup.py", "the dependencies"),
    ("setup.cfg", "the dependencies"),
    ("**/package.json", "the dependencies"),
    ("**/package-lock.json", "the dependencies"),
    # the self-improvement worker, its gate and its tests: never its own promoter
    ("src/pionir/selfimprove/**", "the self-improvement gate itself"),
    ("src/pionir/crew/improve/**", "the self-improvement worker itself"),
    ("src/pionir/improvecli.py", "the self-improvement undo command"),
    ("docs/IMPROVEMENT_GATE.md", "the self-improvement gate's contract"),
    ("docs/SELF_IMPROVEMENT_DESIGN.md", "the self-improvement worker's design"),
    ("tests/test_selfimprove*", "the self-improvement worker's own tests"),
    ("tests/test_improvement*", "the self-improvement gate's own tests"),
    ("tests/test_build_sandbox*", "the build sandbox's own tests"),
    ("tests/test_capability_approval*", "the approval gate's own tests"),
)

# Pionir's declared dependencies: never NEW, whoever imports them.
DECLARED = frozenset({"PIL", "openpyxl", "tzdata", "pytest", "kokoro_onnx"})
OWN = frozenset({"pionir", "tests", "support", "__future__"})

ASSERT_LINE = re.compile(r"\bassert\b|\.assert[A-Z_]\w*\s*\(|\bpytest\.raises\s*\(|"
                         r"\.fail\s*\(|\bassertRaises\b")
SKIP_LINE = re.compile(r"(?i)unittest\.skip\w*|\.skipTest\s*\(|\bSkipTest\b|"
                       r"pytest\.mark\.(?:skip\w*|xfail)|pytest\.(?:skip|xfail|importorskip)\s*\(|"
                       r"expectedFailure|__unittest_skip__|@skip\w*")
# Test code that reaches into the test machinery instead of testing Pionir.
TEST_INTERNALS = re.compile(
    r"\b(?:TestResult|addFailure|addError|addSuccess|addSkip|addExpectedFailure|"
    r"addUnexpectedSuccess|wasSuccessful|load_tests|TextTestRunner|TestLoader|TestProgram|"
    r"defaultTestLoader|_outcome|_feedErrorsToResult|pytest_\w+|monkeypatch|conftest|"
    r"__builtins__|builtins)\b|\bunittest\.main\b|\bsys\.(?:argv|modules|exit|settrace)\b|"
    r"\bos\._exit\b|\batexit\b|\bsetattr\s*\(\s*(?:unittest|pytest)\b|"
    r"\b(?:unittest|pytest)\.[\w.]+\s*=(?!=)|patch(?:\.object)?\(\s*['\"]?(?:unittest|pytest)")
# ... and anywhere at all: nothing changes how unittest or pytest itself behaves.
TEST_MACHINERY = re.compile(r"\bsetattr\s*\(\s*(?:unittest|pytest)\b|"
                            r"\b(?:unittest|pytest)\.[\w.]+\s*=(?!=)|"
                            r"patch(?:\.object)?\(\s*['\"]?(?:unittest|pytest)")
DYNAMIC_IMPORT = re.compile(r"\b__import__\s*\(|\bimport_module\s*\(")
IMPORT_LINE = re.compile(r"^\s*(?:from\s+([A-Za-z_]\w*)[\w.]*\s+import\b|import\s+(.+))")
CATALOGUE_WIRING = re.compile(r'"(?:uses|impl|provider)"\s*:|^\s*"[a-z0-9_]+\.[a-z0-9_.]+"\s*,?\s*$'
                              r"|builds\.improve|self_improver|\"improve\"")
_SAFE_PATH = re.compile(r"[A-Za-z0-9_.@+-][A-Za-z0-9_.@+ -]{0,120}")


@dataclass
class FileChange:
    path: str
    status: str = "modified"            # added | modified | deleted
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    binary: bool = False
    modes: list = field(default_factory=list)
    problem: str | None = None

    @property
    def lines(self) -> int:
        return len(self.added) + len(self.removed)


def _match(path: str, pattern: str) -> bool:
    p, pat = path.casefold(), pattern.casefold()
    if fnmatch.fnmatchcase(p, pat):
        return True
    if "/**/" in pat and fnmatch.fnmatchcase(p, pat.replace("/**/", "/")):
        return True                     # "a/**/b" also matches "a/b"
    if pat.startswith("**/") and fnmatch.fnmatchcase(p, pat[3:]):
        return True                     # "**/x" also matches "x" at the top
    return pat.endswith("/**") and p.startswith(pat[:-2])


def protected_why(path: str, extra: dict | None = None) -> str | None:
    """Why ``path`` (repo-relative, posix) is protected, or None. ``extra``: the protected
    import closure (``protected_closure``) - a module a protected module imports."""
    p = path.replace("\\", "/").lstrip("/")
    for pattern, why in PROTECTED:
        if _match(p, pattern):
            return why
    if in_core(p):
        return "the security core (what the gate, its money, its secrets and its sandbox run)"
    if extra:
        return extra.get(p)
    return None


# ---- the protected import closure ---------------------------------------------------------------
def module_name_of(path: str) -> str | None:
    """``src/pionir/a/b.py`` -> ``pionir.a.b`` (a package's ``__init__.py`` -> the package)."""
    if not (path.startswith("src/") and path.endswith(".py")):
        return None
    parts = path[len("src/"):-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def _path_for(module: str, files) -> str | None:
    base = "src/" + module.replace(".", "/")
    for candidate in (base + ".py", base + "/__init__.py"):
        if candidate in files:
            return candidate
    return None


def _import_nodes(tree, lazy: bool):
    """The import statements of a module: all of them (``lazy``), or only those that run when
    it is imported (not inside a function body)."""
    if lazy:
        yield from ast.walk(tree)
        return
    todo = list(ast.iter_child_nodes(tree))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        todo.extend(ast.iter_child_nodes(node))


def _imported_modules(path: str, source: str, *, lazy: bool = True) -> set:
    """Every pionir module ``path`` imports - with ``lazy``, inside functions too - and the
    packages on the way."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    name = module_name_of(path) or ""
    package = name if path.endswith("__init__.py") else name.rsplit(".", 1)[0]
    found: set = set()
    for node in _import_nodes(tree, lazy):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = package.split(".")
                mod = ".".join(up[:len(up) - node.level + 1] + ([node.module] if node.module
                                                                 else []))
            else:
                mod = node.module or ""
            found.add(mod)
            found |= {f"{mod}.{a.name}" for a in node.names}
    out = set()
    for mod in found:
        if mod == "pionir" or mod.startswith("pionir."):
            parts = mod.split(".")
            out |= {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
    return out


# The SECURITY CORE: the code whose behaviour IS the protection - the approval gate and its
# cards, auth, money and spend rules (the Claude caps), secrets handling, the build sandbox, the
# GPU lease, the executive that enforces permissions, and the self-improvement code itself.
# Everything these import, transitively, is protected too (``protected_closure``). The other
# protected files (content and listing rules, settings, wiring, scripts) are protected as FILES
# only - and no changed module may reach into any of them (``indirect_problems``).
CORE: tuple = (
    "src/pionir/approvals.py", "src/pionir/batching.py", "src/pionir/discord_gate.py",
    "src/pionir/contracts.py", "src/pionir/server.py",
    "src/pionir/auth.py", "src/pionir/bridge_auth.py", "src/pionir/signin.py",
    "src/pionir/economy/**", "src/pionir/crew/bolts.py", "src/pionir/crew/budget.py",
    "src/pionir/crew/escalation.py", "src/pionir/crew/direction.py", "src/pionir/workapi.py",
    "src/pionir/worklog.py",
    "src/pionir/secretscrub.py", "src/pionir/crew/fiverr/checks.py",
    "src/pionir/build_sandbox.py", "src/pionir/sandbox_git.py", "src/pionir/crew/builds/sandbox.py",
    "src/pionir/shared_gpu.py", "src/pionir/scheduler.py", "src/pionir/crew/gpu.py",
    "src/pionir/ollama_gate.py",
    "src/pionir/runtime.py", "src/pionir/registry.py", "src/pionir/reliability.py",
    "src/pionir/adapters/**", "src/pionir/crew/builds/**", "src/pionir/crew/runtime.py",
    "src/pionir/crew/workers.py", "src/pionir/crew/leader.py", "src/pionir/crew/fiverr/desk.py",
    "src/pionir/crew/marketplaces/paths.py", "src/pionir/crew/pool.py", "src/pionir/crew/vitals.py",
    "src/pionir/crew/clock.py", "src/pionir/crew/products.py",
    "src/pionir/selfimprove/**", "src/pionir/crew/improve/**", "src/pionir/adapters/improve.py",
    "src/pionir/improvecli.py", "src/pionir/improvement.py",
)


def in_core(path: str) -> bool:
    return any(_match(path, pattern) for pattern in CORE)


def protected_closure(files: dict) -> dict:
    """{path: why} for every Pionir module a protected module imports, transitively: changing
    one of those changes what protected code runs, so it is protected too. Every protected file
    counts the imports that run when it is LOADED; a SECURITY CORE file counts every import
    (inside its functions too: they run). ``files``: the repo's tree (path -> bytes or text)."""
    start = [p for p in files if p.endswith(".py") and p.startswith("src/") and
             protected_why(p)]
    seen = set(start)
    todo = [(p, p) for p in start]
    out: dict = {}
    while todo:
        path, root = todo.pop()
        for mod in _imported_modules(path, _text(files[path]), lazy=in_core(path)):
            dep = _path_for(mod, files)
            if dep is None or dep in seen:
                continue
            seen.add(dep)
            out[dep] = f"imported by protected code ({root})"
            todo.append((dep, root))
    return out


# ---- changing a protected module from somewhere else -----------------------------------------
# Structural, not a list of tricks. Over a changed Python file (src or tests), counted against the
# file before the change so only ADDED code is judged:
#  a. no reflective or dynamic primitive at all (``REFLECTIVE``; ``getattr`` only with a literal
#     name on something that is not another module's; ``type()`` never on such a thing);
#  b. no NEW import of a protected module, or of a name from one;
#  c. TAINT: a name bound from a protected module (or from an expression rooted at one) is
#     tainted, through assignment, walrus, for/with/comprehension targets, unpacking and default
#     arguments. On a tainted root, code may only READ attributes and CALL them: no store, del or
#     augmented assignment, no mutating method (``MUTATORS``), and no tainted object (not a call
#     result) passed to a call, returned, yielded or put in a container;
#  d. a test patches only unprotected modules, inside a ``with`` or decorator (never ``.start()``).
REFLECTIVE = frozenset({
    "exec", "eval", "compile", "__import__", "importlib", "import_module", "reload", "setattr",
    "delattr", "vars", "globals", "locals", "__dict__", "__class__", "__setattr__",
    "__delattr__", "__setitem__", "__delitem__", "__getattribute__", "__globals__", "__code__",
    "__closure__", "__subclasses__", "__mro__", "__bases__", "builtins", "__builtins__",
    "setitem", "delitem", "attrgetter", "methodcaller", "modules", "__loader__", "__spec__",
})
MUTATORS = frozenset({
    "update", "append", "extend", "add", "insert", "pop", "popitem", "remove", "discard", "clear",
    "setdefault", "sort", "reverse", "__setitem__", "__delitem__", "__setattr__", "__delattr__",
    "difference_update", "intersection_update", "symmetric_difference_update", "write",
})


def _is_patcher(node) -> bool:
    """``patch(...)``, ``mock.patch(...)``, ``patch.object/dict/multiple(...)`` and any
    ``monkeypatch.<x>(...)`` - not an ordinary ``dict()`` or ``object()``."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id.startswith("patch")
    if isinstance(fn, ast.Attribute):
        owner = fn.value
        owner_name = owner.attr if isinstance(owner, ast.Attribute) else owner.id if isinstance(
            owner, ast.Name) else ""
        return fn.attr.startswith("patch") or owner_name == "monkeypatch" or (
            fn.attr in ("object", "dict", "multiple") and owner_name.startswith("patch"))
    return False


def _call_name(node) -> str:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""


def _root(node):
    """The name an expression is rooted at through attributes, subscripts and stars - None for
    a call's result or anything computed."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _names(target) -> set:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _carries(expr, tainted: set) -> bool:
    """Whether ``expr`` holds a tainted object itself (not only a call's result)."""
    todo = [expr]
    while todo:
        node = todo.pop()
        if isinstance(node, ast.Call):
            continue                    # what a call returns is not the module's object
        if isinstance(node, ast.Name) and node.id in tainted:
            return True
        todo.extend(ast.iter_child_nodes(node))
    return False


def _reach(source: str, *, path: str, module_protected) -> dict | None:
    """Counts of every way the file could reach into ANOTHER (protected) module - the rules
    above. None when it does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    test = path.startswith("tests/")
    name = module_name_of(path) or ""
    package = name if path.endswith("__init__.py") else name.rsplit(".", 1)[0]
    counts: dict = {}

    def bump(kind: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1

    tainted: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in REFLECTIVE or a.name in REFLECTIVE:
                    bump(f"uses a reflective primitive ({a.name})")
                if module_protected(a.name):
                    bump(f"imports a protected module ({a.name})")
                    tainted.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = package.split(".")
                mod = ".".join(up[:len(up) - node.level + 1] + ([node.module] if node.module
                                                                 else []))
            else:
                mod = node.module or ""
            if mod.split(".")[0] in REFLECTIVE:
                bump(f"uses a reflective primitive ({mod})")
            for a in node.names:
                if a.name in REFLECTIVE:
                    bump(f"uses a reflective primitive ({a.name})")
                full = f"{mod}.{a.name}"
                if module_protected(full):
                    bump(f"imports from a protected module ({full})")
                    tainted.add(a.asname or a.name)
    # taint flows through every binding, to a fixed point
    for _ in range(8):
        before = len(tainted)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _carries(node.value, tainted):
                for t in node.targets:
                    tainted |= _names(t)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None \
                    and _carries(node.value, tainted):
                tainted |= _names(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and \
                    _carries(node.iter, tainted):
                tainted |= _names(node.target)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None and _carries(item.context_expr, tainted):
                        tainted |= _names(item.optional_vars)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                args = node.args
                positional = args.posonlyargs + args.args
                for arg, default in zip(positional[len(positional) - len(args.defaults):],
                                        args.defaults):
                    if _carries(default, tainted):
                        tainted.add(arg.arg)
                for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                    if default is not None and _carries(default, tainted):
                        tainted.add(arg.arg)
        if len(tainted) == before:
            break

    def store(target) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for e in target.elts:
                store(e)
        elif isinstance(target, (ast.Attribute, ast.Subscript, ast.Starred)) and \
                _root(target) in tainted:
            bump("stores into or deletes from a protected module's data")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in REFLECTIVE and node.id != "modules":
            bump(f"uses a reflective primitive ({node.id})")
        elif isinstance(node, ast.Attribute) and node.attr in REFLECTIVE and \
                node.attr != "compile":                     # re.compile is not compile()
            bump(f"uses a reflective primitive ({node.attr})")
        if isinstance(node, ast.Assign):
            for t in node.targets:
                store(t)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            store(node.target)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                store(t)
        elif isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)) and \
                node.value is not None and _carries(node.value, tainted):
            bump("hands out a protected module's object")
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)) and any(
                _carries(e, tainted) for e in ast.iter_child_nodes(node)
                if not isinstance(e, ast.expr_context)):
            bump("puts a protected module's object in a container")
        elif isinstance(node, ast.Call):
            fn = node.func
            call = _call_name(node)
            if isinstance(fn, ast.Attribute) and _root(fn) in tainted and (
                    fn.attr in MUTATORS or (fn.attr.startswith("__i") and fn.attr.endswith("__"))):
                bump(f"mutates a protected module's data (.{fn.attr})")
            for arg in [*node.args, *[k.value for k in node.keywords]]:
                if _carries(arg, tainted):
                    bump("passes a protected module's object to a call")
            first = node.args[0] if node.args else None
            if call == "getattr":
                literal = len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                if not literal or (first is not None and _carries(first, tainted)):
                    bump("uses getattr with a computed name or on a protected module")
            elif call == "type" and len(node.args) == 1 and _carries(first, tainted):
                bump("calls type() on a protected module's object")
            elif _is_patcher(node):
                if not test:
                    bump("patches another module")
                elif isinstance(first, ast.Constant) and isinstance(first.value, str) and \
                        module_protected(first.value.split(":")[0]):
                    bump("patches a protected module")
            elif call == "start" and isinstance(fn, ast.Attribute) and \
                    isinstance(fn.value, ast.Call) and _is_patcher(fn.value):
                bump("starts a patcher outside a with/decorator scope")
    return counts


def indirect_problems(path: str, old, new, *, protected=None, files=()) -> list:
    """What a changed Python file would do to OTHER modules (``_reach``), counted against the
    file before the change - any new way is refused."""
    extra = protected or {}

    def module_protected(dotted: str) -> bool:
        """Whether the module that owns ``dotted`` (a module, or an attribute of one) is
        protected - or is a package's ``__init__`` (``import pionir`` reaches everything)."""
        mod = dotted
        while mod and (mod.startswith("pionir.") or mod == "pionir"):
            dep = _path_for(mod, files) if files else None
            if dep is None and not files:
                dep = "src/" + mod.replace(".", "/") + ".py"
                if protected_why(dep, extra):
                    return True
                dep = None
            if dep is not None:
                return bool(protected_why(dep, extra)) or dep.endswith("__init__.py")
            if "." not in mod:
                return False
            mod = mod.rpartition(".")[0]
        return False

    after = _reach(_text(new), path=path, module_protected=module_protected)
    if after is None:
        return []
    reasons = whole_file_problems(path, _text(new), module_protected=module_protected)
    before = _reach(_text(old), path=path, module_protected=module_protected) if old else {}
    before = before or {}
    return reasons + [f"{path} {kind}: a change here never reaches into another module"
                      for kind, n in sorted(after.items()) if n > before.get(kind, 0)
                      and not kind.startswith(WHOLE_FILE_KINDS)]


# The guarantee, by construction: a file a patch leaves behind - ALL of it, not only what the
# patch added - imports nothing protected (at any level: module, function, class, branch, try)
# and uses no reflective or dynamic primitive. Code that cannot name a protected module cannot
# change one. A test file imports only the standard library, Pionir's declared dependencies and
# patchable modules.
WHOLE_FILE_KINDS = ("uses a reflective primitive", "imports a protected module",
                    "imports from a protected module", "uses getattr")


def whole_file_problems(path: str, source: str, *, module_protected) -> list:
    """Every reason the WHOLE file is not one a patch may leave: a protected import anywhere in
    it, a reflective or dynamic primitive anywhere in it, and - for a test - any import that is
    not the standard library, a declared dependency or a patchable Pionir module."""
    counts = _reach(source, path=path, module_protected=module_protected)
    if counts is None:
        return [f"{path} does not parse as Python"]
    reasons = [f"{path} {kind}: a patchable file never does, anywhere in it"
               for kind in sorted(counts) if kind.startswith(WHOLE_FILE_KINDS)]
    if path.startswith("tests/"):
        allowed = set(sys.stdlib_module_names) | DECLARED | {"__future__"}
        tree = ast.parse(source)
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [f"{node.module}.{a.name}" for a in node.names] \
                if isinstance(node, ast.ImportFrom) and not node.level and node.module \
                else ["(relative)"] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                top = name.split(".")[0]
                if top in allowed or top == "pionir":  # a protected one is refused above
                    continue
                reasons.append(f"{path} imports {name!r}: a test a patch leaves imports only "
                               "the standard library, declared dependencies and patchable "
                               "modules")
    return reasons


def allowed_why_not(path: str, status: str = "modified") -> str | None:
    """Why a patch may not touch ``path`` at all (outside the allowlist), or None."""
    name = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatchcase(name.casefold(), pat) for pat in FORBIDDEN_NAMES):
        return f"{path} is never changed by a self-improvement ({name})"
    if not any(_match(path, pat) for pat in ALLOWED):
        return (f"{path} is outside what a self-improvement may change (Python modules under "
                "src/pionir, tests/test_*.py and the catalogue's settings)")
    parts = path.split("/")
    if parts[0] == "tests" and len(parts) != 2:
        return f"{path} is outside what a self-improvement may change (tests/test_*.py only)"
    if status == "added" and parts[0] == "src" and len(parts) <= 3:
        return f"{path} would be a new top-level module of Pionir"
    return None


def _path_problem(path: str) -> str | None:
    if not path or path.startswith(("/", '"')) or "\\" in path or ":" in path:
        return f"{path[:80]!r} is not a plain relative path"
    parts = path.split("/")
    if any(p in ("", ".", "..") or not _SAFE_PATH.fullmatch(p) for p in parts):
        return f"{path[:80]!r} is not a plain relative path"
    if any(p.casefold().startswith(".git") for p in parts):
        return f"{path[:80]!r} is a git file or inside .git"
    return None


def parse_patch(text: str) -> list:
    """``git diff-tree -p --no-renames --binary --full-index`` text -> FileChanges, split into
    lines exactly as git writes them (on ``\\n`` only - a ``\\r`` or U+2028 is content).
    Anything it cannot read is a ``problem``, never skipped."""
    files: list = []
    cur: FileChange | None = None
    in_hunk = False
    skip_binary = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            n = (len(rest) - 5) // 2
            path = rest[2:2 + n] if n > 0 else ""
            cur = FileChange(path)
            if rest != f"a/{path} b/{path}":
                cur.problem = f"{rest[:80]!r} is a rename or a quoted path"
            else:
                cur.problem = _path_problem(path)
            files.append(cur)
            in_hunk = skip_binary = False
            continue
        if cur is None:
            if line.strip():
                return [FileChange("(patch)", problem="the patch has text before its first file")]
            continue
        if skip_binary:
            continue
        if not in_hunk:
            if line.startswith("new file mode "):
                cur.status = "added"
                cur.modes.append(line.rsplit(" ", 1)[-1])
            elif line.startswith("deleted file mode "):
                cur.status = "deleted"
                cur.modes.append(line.rsplit(" ", 1)[-1])
            elif line.startswith(("old mode ", "new mode ")):
                cur.problem = cur.problem or "changes a file mode"
            elif line.startswith(("rename ", "copy ", "similarity ", "dissimilarity ")):
                cur.problem = cur.problem or "is a rename or copy"
            elif line.startswith(("GIT binary patch", "Binary files ")):
                cur.binary = True
                skip_binary = True
            elif line.startswith("@@"):
                in_hunk = True
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


def _top_imports(lines) -> set:
    found = set()
    for text in lines:
        m = IMPORT_LINE.match(text)
        if not m:
            continue
        if m.group(1):
            found.add(m.group(1))
        else:
            for part in m.group(2).split(","):
                name = part.strip().split(" ")[0].split(".")[0]
                if re.fullmatch(r"[A-Za-z_]\w*", name or ""):
                    found.add(name)
    return found


def _count(regex, lines) -> int:
    return sum(len(regex.findall(t)) for t in lines)


def patch_problems(text: str, *, local_modules=frozenset(), known_imports=frozenset(),
                   protected: dict | None = None) -> list:
    """Every reason the diff alone refuses the patch (empty: none). ``local_modules`` are the
    tests folder's helper modules; ``known_imports`` every top-level name Pionir imports
    somewhere already (neither is a new dependency); ``protected`` the protected import
    closure of the tree the patch applies to (``protected_closure``)."""
    if not isinstance(text, str) or not text.strip():
        return ["the patch is empty: it changes nothing"]
    if len(text) > MAX_CHARS:
        return [f"the patch is {len(text):,} characters - more than Ian's card shows in full "
                f"({MAX_CHARS:,}); a fix here is small"]
    files = parse_patch(text)
    if not files:
        return ["the patch could not be read as a git diff"]
    reasons: list = []
    known = set(sys.stdlib_module_names) | OWN | DECLARED | set(local_modules) | \
        set(known_imports)
    if len(files) > MAX_FILES:
        reasons.append(f"it changes {len(files)} files; at most {MAX_FILES} fit one review")
    total = sum(f.lines for f in files)
    if total > MAX_LINES:
        reasons.append(f"it changes {total} lines; at most {MAX_LINES} fit one review")
    for f in files:
        why = protected_why(f.path, protected) if f.path else None
        if why:
            reasons.append(f"{f.path} is protected ({why}): no agent changes it")
            continue
        if f.problem:
            reasons.append(f"{f.path or '(a file)'}: {f.problem}")
            continue
        outside = allowed_why_not(f.path, f.status)
        if outside:
            reasons.append(outside)
            continue
        if f.status == "deleted":
            reasons.append(f"it deletes {f.path}"
                           + (" - a test file" if f.path.startswith("tests/") else "")
                           + "; a fix here never deletes a file")
        if f.binary:
            reasons.append(f"{f.path} is a binary change, which no review can read")
        if any(m not in ("100644", "100755") for m in f.modes):
            reasons.append(f"{f.path} is a link or a submodule")
        if f.path.endswith(".py"):
            for name in sorted(_top_imports(f.added) - _top_imports(f.removed) - known):
                reasons.append(f"{f.path} imports {name!r}, which is neither the standard "
                               "library nor anything Pionir imports already: it adds a "
                               "dependency")
            if _count(DYNAMIC_IMPORT, f.added) > _count(DYNAMIC_IMPORT, f.removed):
                reasons.append(f"{f.path} adds a dynamic import (__import__ / import_module)")
            if any(TEST_MACHINERY.search(t) for t in f.added):
                reasons.append(f"{f.path} changes how unittest or pytest itself behaves")
        if f.path.startswith("tests/"):
            lost = (sum(1 for t in f.removed if ASSERT_LINE.search(t))
                    - sum(1 for t in f.added if ASSERT_LINE.search(t)))
            if lost > 0:
                reasons.append(f"{f.path} loses {lost} assert(s): a fix never weakens a test")
            skips = (sum(1 for t in f.added if SKIP_LINE.search(t))
                     - sum(1 for t in f.removed if SKIP_LINE.search(t)))
            if skips > 0:
                reasons.append(f"{f.path} adds a skip or expected-failure marker: a fix never "
                               "silences a test")
            inner = sorted({m.group(0) for t in f.added for m in TEST_INTERNALS.finditer(t)})
            if inner:
                reasons.append(f"{f.path} reaches into the test machinery ({', '.join(inner[:4])}): "
                               "a test tests Pionir, never the runner")
        if f.path == CATALOGUE and any(CATALOGUE_WIRING.search(t)
                                       for t in f.added + f.removed):
            reasons.append("it changes the catalogue's wiring (a worker's uses, impl or provider, "
                           "or the self-improvement worker's own entry), not a setting")
    return reasons


# ---- what needs both trees --------------------------------------------------------------------
def _const(node) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_const(e) for e in node.elts)
    return False


def _real_assert(node) -> bool | None:
    """True for an assertion that can fail, False for a tautology, None for not an assert."""
    if isinstance(node, ast.Assert):
        return not _const(node.test)
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(
        fn, ast.Name) else ""
    if not (name.startswith("assert") or name in ("fail", "raises")):
        return None
    args = list(node.args)
    if name in ("fail", "raises") or name.startswith("assertRaises") or not args:
        return True
    if all(_const(a) for a in args):
        return False                    # assertTrue(True), assertEqual(1, 1)
    if len(args) >= 2 and ast.dump(args[0]) == ast.dump(args[1]):
        return False                    # assertEqual(x, x)
    return True


def _tests_of(source: str) -> dict | None:
    """{test name: real asserts in it} of one test file, and ``"__skips__"`` - or None when it
    does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    out: dict = {"__skips__": 0}

    def visit(node, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    child.name.startswith("test"):
                out[f"{prefix}{child.name}"] = sum(
                    1 for n in ast.walk(child) if _real_assert(n) is True)
    visit(tree, "")
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        if name in ("skip", "skipIf", "skipUnless", "skipTest", "SkipTest", "xfail",
                    "importorskip", "expectedFailure"):
            out["__skips__"] += 1
    return out


def imports_of(source: str) -> tuple | None:
    """(top-level names imported anywhere in the file, dynamic imports) - or None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    names, dynamic = set(), 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(
                fn, ast.Name) else ""
            if name in ("__import__", "import_module"):
                dynamic += 1
    return names, dynamic


def known_imports(files: dict) -> frozenset:
    """Every top-level name any module under src/pionir imports (path -> bytes or text)."""
    found: set = set()
    for path, data in files.items():
        if not (path.startswith("src/pionir/") and path.endswith(".py")):
            continue
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        got = imports_of(text)
        if got:
            found |= got[0]
    return frozenset(found)


def _catalogue_wiring(text: str) -> dict | None:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    out = {"providers": doc.get("providers")}
    for division in doc.get("divisions") or []:
        for w in division.get("workers") or []:
            wid = f"{division.get('id')}.{w.get('name')}"
            if wid == SELF_WORKER or w.get("impl") == SELF_IMPL:
                out[wid] = w
            else:
                out[wid] = {k: w.get(k) for k in ("uses", "impl", "provider")}
    return out


def _text(data) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)


def tree_problems(changed: list, base: dict, head: dict, *, known=frozenset(),
                  protected: dict | None = None, files: dict | None = None) -> list:
    """What the two trees (repo-relative path -> bytes) show about the changed paths: a file
    in the protected import closure, a change that reaches into another module
    (``indirect_problems``), a test with fewer real asserts, a removed test, a new skip marker,
    a file that no longer parses, an import of anything Pionir does not import already, a new
    dynamic import, and the catalogue's wiring changed. ``known``: top-level names Pionir
    imports already; ``protected`` / ``files``: the protected closure and the whole tree
    (default: computed from ``base``, which must then be the whole tree)."""
    reasons: list = []
    files = files if files is not None else base
    extra = protected if protected is not None else protected_closure(files)
    known = set(known or known_imports(base)) | set(sys.stdlib_module_names) | OWN | DECLARED
    known |= {p.split("/")[-1][:-3] for p in base if p.startswith("tests/") and p.endswith(".py")}
    for path in changed:
        old, new = base.get(path), head.get(path)
        why = protected_why(path, extra)
        if why:
            reasons.append(f"{path} is protected ({why}): no agent changes it")
            continue
        if new is None:
            continue
        if path.endswith(".py"):
            reasons += indirect_problems(path, old, new, protected=extra, files=files)
        if path == CATALOGUE:
            before = _catalogue_wiring(_text(old or b"{}"))
            after = _catalogue_wiring(_text(new))
            if after is None:
                reasons.append("the catalogue is no longer valid JSON")
            elif before != after:
                reasons.append("it changes the catalogue's wiring (a worker's uses, impl or "
                               "provider, or the self-improvement worker's own entry), not a "
                               "setting")
            continue
        if not path.endswith(".py"):
            continue
        now = imports_of(_text(new))
        if now is None:
            reasons.append(f"{path} does not parse as Python")
            continue
        was = imports_of(_text(old)) if old is not None else (set(), 0)
        was = was or (set(), 0)
        for name in sorted(now[0] - was[0] - known):
            reasons.append(f"{path} imports {name!r}, which is neither the standard library nor "
                           "anything Pionir imports already: it adds a dependency")
        if now[1] > was[1]:
            reasons.append(f"{path} adds a dynamic import (__import__ / import_module)")
        if not path.startswith("tests/") or old is None:
            continue
        after = _tests_of(_text(new))
        before = _tests_of(_text(old))
        if after is None or before is None:
            continue
        if after["__skips__"] > before["__skips__"]:
            reasons.append(f"{path} has a new skip or expected-failure marker: a fix never "
                           "silences a test")
        for name, count in sorted(before.items()):
            if name == "__skips__":
                continue
            if name not in after:
                reasons.append(f"{path} no longer has {name}: a fix never removes a test")
            elif after[name] < count:
                reasons.append(f"{path}: {name} has {count - after[name]} fewer real assert(s) "
                               "(an assert of a constant, or of a thing equal to itself, is "
                               "not one): a fix never weakens a test")
    return reasons


def changed_paths(text: str) -> list:
    return [f.path for f in parse_patch(text or "")]


PROTECTED_FILE_PROBLEM = " is protected ("


def only_protected_files(reasons) -> bool:
    """True when every refusal (and there is at least one) is 'a protected file is edited'. That
    is the ONE thing a human-applied DRAFT may do that the bot's own apply never will: touch
    protected code, because a person - not the bot - commits it. Every other refusal (size, a
    reflective primitive, a new dependency, a weakened or deleted test, the catalogue's wiring, a
    PATCHABLE file reaching protected code through the whole-file import rule) disqualifies even a
    draft, so it is still shelved or repaired."""
    reasons = list(reasons)
    return bool(reasons) and all(PROTECTED_FILE_PROBLEM in r for r in reasons)
