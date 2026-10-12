"""What a self-improvement patch may never do - checked before Claude reads it, and again
before Ian is asked and before it is applied.

The owner's rule (IMPROVEMENT_GATE.md, FUSION_PLAN.md Phase 3): no agent promotes changes to
its own code, its own gate or its own permissions. So a patch the ``builds.improve`` worker
gets from Daedalus is REFUSED, fail closed, without spending a Claude call, when it:

- touches anything in ``PROTECTED`` (below, each with the reason - the approval gate and every
  surface that shows or answers it, auth, money and spend rules (the Claude caps included),
  secrets handling, the build sandbox and the setup scripts, CI, Pionir's actuators, the crew's
  wiring to Pionir and to Claude, the test harness, the dependency files, and the
  self-improvement worker, its gate and its tests);
- deletes any file (a renamed file is a deleted one: diffs are read with ``--no-renames``);
- weakens the tests: fewer asserts in a test file than before, a test function removed, or a
  skip / expected-failure marker added;
- adds a dependency: a new import of a module that is not the standard library, Pionir's own
  code, the test helpers or one of Pionir's declared dependencies;
- changes a binary file, a link, a submodule or a file mode, or uses a path it cannot read;
- is bigger than one review can read (``MAX_FILES`` files, ``MAX_LINES`` changed lines,
  ``MAX_CHARS`` characters);
- changes the crew catalogue's wiring rather than its settings: a worker's ``uses``, ``impl``,
  ``provider`` or its own entry (``builds.improve`` / ``self_improver``).

``patch_problems`` needs only the diff text (what Pionir's ``improve.apply`` adapter holds);
``tree_problems`` adds what needs both trees (asserts and test functions counted by parsing, the
catalogue compared as JSON) - the worker runs both. Every reason is plain English for the card.
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
MAX_CHARS = 60_000                  # the patch text, so the review prompt is never cut
CATALOGUE = "src/pionir/crew/catalogue.json"
SELF_WORKER = "builds.improve"
SELF_IMPL = "self_improver"

# (glob on the repo-relative posix path, case-folded; why it is protected). ``**`` matches any
# depth. Read top to bottom by Ian: this IS the list.
PROTECTED: tuple = (
    # the approval gate, and every surface that shows or answers it
    ("src/pionir/approvals.py", "the approval gate (the queue of actions waiting on Ian)"),
    ("src/pionir/batching.py", "the approval gate (which approvals may wait for the digest)"),
    ("src/pionir/discord_gate.py", "the approval gate (Ian's Discord approve/deny cards)"),
    ("src/pionir/server.py", "the approval gate and auth (Pionir's HTTP API)"),
    ("src/pionir/contracts.py", "the approval gate (risk levels, requires_approval, spends_money)"),
    ("src/pionir/runtime.py", "the approval gate (the executive every task runs through)"),
    ("src/pionir/registry.py", "the approval gate (which adapter answers which capability)"),
    ("src/pionir/desktop.py", "the approval gate (the desktop's approve/deny)"),
    ("src/pionir/web/**", "the approval gate (the dashboard)"),
    ("src/pionir/adapters/**", "Pionir's actuators: what each one does and parks for approval"),
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
    ("src/pionir/crew/config.py", "spend rules and secrets paths (the crew's settings)"),
    ("src/pionir/config.py", "secrets paths, auth compat and every service URL (Pionir's settings)"),
    ("src/pionir/worklog.py", "money (the hours and income log)"),
    # secrets handling
    ("src/pionir/secretscrub.py", "secrets handling (scrubbing)"),
    ("src/pionir/secret_patterns.json", "secrets handling (the key formats scanned for)"),
    ("src/pionir/crew/fiverr/checks.py", "secrets handling (the secrets and owner-data guard)"),
    ("**/*secret*", "secrets handling (a file named for secrets)"),
    ("**/*token*", "secrets handling (a file named for tokens)"),
    ("**/*cred*", "secrets handling (a file named for credentials)"),
    ("**/.env*", "secrets handling (an environment file)"),
    # the build sandbox, the GPU lease and the setup scripts
    ("src/pionir/build_sandbox.py", "the build sandbox (the contained pionir-builds user)"),
    ("src/pionir/sandbox_git.py", "the build sandbox (safe git on model-written repos)"),
    ("src/pionir/crew/builds/sandbox.py", "the build sandbox (the night builds' repos)"),
    ("src/pionir/crew/builds/review.py", "the night builds' own checks and Claude's review"),
    ("src/pionir/crew/apibuild/checks.py", "the API builder's own checks"),
    ("src/pionir/shared_gpu.py", "the GPU lease (who has the card; yielding to Moss)"),
    ("src/pionir/scheduler.py", "the GPU lease (admission and handing the card back to Moss)"),
    ("src/pionir/ollama_gate.py", "the GPU lease (other programs' model calls)"),
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
    ("src/pionir/improvement.py", "the self-improvement gate itself (PromotionGate)"),
    ("src/pionir/improvecli.py", "the self-improvement undo command"),
    ("docs/IMPROVEMENT_GATE.md", "the self-improvement gate's contract"),
    ("docs/SELF_IMPROVEMENT_DESIGN.md", "the self-improvement worker's design"),
    ("tests/test_selfimprove*", "the self-improvement worker's own tests"),
    ("tests/test_improvement*", "the self-improvement gate's own tests"),
)

# Pionir's declared dependencies (pyproject.toml) and what the tests already use: importing one
# of these where it was not imported before is not a NEW dependency.
DECLARED = frozenset({"PIL", "openpyxl", "tzdata", "pytest", "kokoro_onnx"})
OWN = frozenset({"pionir", "tests", "support", "__future__"})

ASSERT_LINE = re.compile(r"\bassert\b|\.assert[A-Z_]\w*\s*\(|\bpytest\.raises\s*\(|"
                         r"\.fail\s*\(|\bassertRaises\b")
SKIP_LINE = re.compile(r"(?i)unittest\.skip\w*|\.skipTest\s*\(|\bSkipTest\b|"
                       r"pytest\.mark\.(?:skip\w*|xfail)|pytest\.(?:skip|xfail|importorskip)\s*\(|"
                       r"expectedFailure|__unittest_skip__|@skip\w*")
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


def protected_why(path: str) -> str | None:
    """Why ``path`` (repo-relative, posix) is protected, or None."""
    p = path.replace("\\", "/").lstrip("/").casefold()
    for pattern, why in PROTECTED:
        pat = pattern.casefold()
        if fnmatch.fnmatchcase(p, pat):
            return why
        if pat.startswith("**/") and fnmatch.fnmatchcase(p, pat[3:]):
            return why                  # "**/x" also matches "x" at the top
        if pat.endswith("/**") and p.startswith(pat[:-2]):
            return why
    return None


def _path_problem(path: str) -> str | None:
    if not path or path.startswith(("/", '"')) or "\\" in path or ":" in path:
        return f"{path[:80]!r} is not a plain relative path"
    parts = path.split("/")
    if any(p in ("", ".", "..") or not _SAFE_PATH.fullmatch(p) for p in parts):
        return f"{path[:80]!r} is not a plain relative path"
    if parts[0] == ".git" or ".git" in parts:
        return f"{path[:80]!r} is inside .git"
    return None


def parse_patch(text: str) -> list:
    """``git diff-tree -p --no-renames --binary --full-index`` text -> FileChanges. Anything it
    cannot read is a ``problem`` on that file (or a whole-patch problem), never skipped."""
    files: list = []
    cur: FileChange | None = None
    in_hunk = False
    skip_binary = False
    for line in text.splitlines():
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
            elif line.startswith("GIT binary patch") or line.startswith("Binary files "):
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


def patch_problems(text: str, *, local_modules=frozenset()) -> list:
    """Every reason the diff alone refuses the patch (empty: none). ``local_modules`` are the
    repo's own importable names besides ``pionir`` (the tests folder's helper modules)."""
    if not isinstance(text, str) or not text.strip():
        return ["the patch is empty: it changes nothing"]
    if len(text) > MAX_CHARS:
        return [f"the patch is {len(text):,} characters - larger than one review can read "
                f"({MAX_CHARS:,})"]
    files = parse_patch(text)
    if not files:
        return ["the patch could not be read as a git diff"]
    reasons: list = []
    std = set(sys.stdlib_module_names) | OWN | DECLARED | set(local_modules)
    if len(files) > MAX_FILES:
        reasons.append(f"it changes {len(files)} files; at most {MAX_FILES} fit one review")
    total = sum(f.lines for f in files)
    if total > MAX_LINES:
        reasons.append(f"it changes {total} lines; at most {MAX_LINES} fit one review")
    for f in files:
        if f.problem:
            reasons.append(f"{f.path or '(a file)'}: {f.problem}")
            continue
        why = protected_why(f.path)
        if why:
            reasons.append(f"{f.path} is protected ({why}): no agent changes it")
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
            new = _top_imports(f.added) - _top_imports(f.removed) - std
            for name in sorted(new):
                reasons.append(f"{f.path} imports {name!r}, which is not the standard library, "
                               "Pionir or one of its declared dependencies: it adds a dependency")
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
        if f.path == CATALOGUE and any(CATALOGUE_WIRING.search(t)
                                       for t in f.added + f.removed):
            reasons.append("it changes the catalogue's wiring (a worker's uses, impl or provider, "
                           "or the self-improvement worker's own entry), not a setting")
    return reasons


# ---- what needs both trees --------------------------------------------------------------------
def _asserts_and_tests(source: str) -> tuple | None:
    """(asserts, skip markers, test function names) of one Python test file, or None when it
    does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    asserts = skips = 0
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            asserts += 1
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(
                fn, ast.Name) else ""
            if name.startswith("assert") or name in ("fail", "raises"):
                asserts += 1
            if name.startswith("skip") or name in ("xfail", "importorskip", "SkipTest",
                                                   "expectedFailure"):
                skips += 1
        elif isinstance(node, (ast.Attribute, ast.Name)):
            name = node.attr if isinstance(node, ast.Attribute) else node.id
            if name in ("expectedFailure", "skip", "skipIf", "skipUnless", "xfail") and \
                    not isinstance(getattr(node, "ctx", None), ast.Store):
                skips += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name.startswith("test"):
            names.add(node.name)
    return asserts, skips, names


def _catalogue_wiring(text: str) -> dict | None:
    """Everything of the catalogue a patch may not change: each worker's uses, impl and
    provider, the self-improvement worker's whole entry, and the providers."""
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


def tree_problems(changed: list, base: dict, head: dict) -> list:
    """What the two trees (repo-relative path -> bytes) show about the changed paths: a test
    file with fewer asserts, a removed test function or more skip markers than before, a test
    file that no longer parses, and the catalogue's wiring changed."""
    reasons: list = []
    for path in changed:
        old, new = base.get(path), head.get(path)
        if path == CATALOGUE and new is not None:
            before = _catalogue_wiring((old or b"{}").decode("utf-8", "replace"))
            after = _catalogue_wiring(new.decode("utf-8", "replace"))
            if after is None:
                reasons.append("the catalogue is no longer valid JSON")
            elif before != after:
                reasons.append("it changes the catalogue's wiring (a worker's uses, impl or "
                               "provider, or the self-improvement worker's own entry), not a "
                               "setting")
        if not (path.startswith("tests/") and path.endswith(".py")) or new is None:
            continue
        after = _asserts_and_tests(new.decode("utf-8", "replace"))
        if after is None:
            reasons.append(f"{path} does not parse as Python")
            continue
        if old is None:
            continue
        before = _asserts_and_tests(old.decode("utf-8", "replace"))
        if before is None:
            continue
        if after[0] < before[0]:
            reasons.append(f"{path} has {before[0] - after[0]} fewer assert(s) than before: a "
                           "fix never weakens a test")
        if after[1] > before[1]:
            reasons.append(f"{path} has a new skip or expected-failure marker: a fix never "
                           "silences a test")
        gone = sorted(before[2] - after[2])
        if gone:
            reasons.append(f"{path} no longer has {', '.join(gone[:5])}: a fix never removes "
                           "a test")
    return reasons


def changed_paths(text: str) -> list:
    return [f.path for f in parse_patch(text or "")]
