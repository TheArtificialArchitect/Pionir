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


def protected_why(path: str) -> str | None:
    """Why ``path`` (repo-relative, posix) is protected, or None."""
    p = path.replace("\\", "/").lstrip("/")
    for pattern, why in PROTECTED:
        if _match(p, pattern):
            return why
    return None


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


def patch_problems(text: str, *, local_modules=frozenset(), known_imports=frozenset()) -> list:
    """Every reason the diff alone refuses the patch (empty: none). ``local_modules`` are the
    tests folder's helper modules; ``known_imports`` every top-level name Pionir imports
    somewhere already (neither is a new dependency)."""
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
        why = protected_why(f.path) if f.path else None
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


def tree_problems(changed: list, base: dict, head: dict, *, known=frozenset()) -> list:
    """What the two trees (repo-relative path -> bytes) show about the changed paths: a test
    with fewer real asserts, a removed test, a new skip marker, a file that no longer parses,
    an import of anything Pionir does not import already, a new dynamic import, and the
    catalogue's wiring changed. ``known``: top-level names Pionir imports already (default:
    computed from ``base``)."""
    reasons: list = []
    known = set(known or known_imports(base)) | set(sys.stdlib_module_names) | OWN | DECLARED
    known |= {p.split("/")[-1][:-3] for p in base if p.startswith("tests/") and p.endswith(".py")}
    for path in changed:
        old, new = base.get(path), head.get(path)
        if new is None:
            continue
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
