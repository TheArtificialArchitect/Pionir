"""The API builder: a product written by Claude, checked, staged, and run only on the owner's yes.

Claude, Pionir and the Scrooge repository are all fakes: ``build_site`` answers scripted
files, ``FakePionir`` parks or refuses the verify job, the repository is a temporary git repo,
and the adapter's runner is injected, so nothing here runs npm, Claude, the network, the real
Scrooge or ``~/.pionir``. Each test fails if the rule it names is reverted: a banned construct
staged, a product built over one still waiting on the owner, a build staged on ``main``, the
checks run without the owner's yes, a moved branch verified, a red check called green, a
rejected build retried for ever, or a latch with no way back.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pionir.adapters.apibuild import VERIFY, ApiBuildAdapter, ApiBuildSettings
from pionir.contracts import Task
from pionir.crew.apibuild import backlog as backlog_mod
from pionir.crew.apibuild.backlog import check_entry, load_backlog
from pionir.crew.apibuild.checks import build_prompt, check_build, parse_build
from pionir.crew.apibuild.stage import StageError, branch_for, git, registry_edit, smoke_edit, stage
from pionir.crew.apibuild.worker import MAX_ATTEMPTS
from pionir.crew.escalation import ClaudeRefusal
from pionir.crew.hands import JobOutcome
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext
from pionir.errors import AdapterProtocolError

T0 = 1_790_000_000.0
HAVE_GIT = shutil.which("git") is not None


def entry_of(pid="colour") -> dict:
    return next(e for e in backlog_mod._seed() if e["id"] == pid)


def good_files(entry: dict) -> dict:
    pid = entry["id"]
    eps = entry["endpoints"]
    product = ("import type { Product } from '../openapi';\n"
               f"export const {pid}: Product = {{\n  id: '{pid}',\n  name: '{entry['name']}',\n"
               f"  summary: '{entry['summary']}',\n  endpoints: [\n"
               + "".join(f"    {{ method: '{e['method']}', path: '{e['path']}' }},\n"
                         for e in eps)
               + "  ],\n};\n")
    cases = "".join(
        f"  it('answers {e['path']}', () => {{\n    expect({pid}.id).toBe('{pid}');\n"
        f"    expect({pid}.endpoints.length).toBeGreaterThan(0);\n"
        f"    expect({pid}.name.length).toBeGreaterThan(0);\n  }});\n"
        f"  it('declares {e['path']}', () => {{\n"
        f"    expect({pid}.summary.length).toBeGreaterThan(0);\n  }});\n" for e in eps)
    test = (f"import {{ describe, it, expect }} from 'vitest';\n"
            f"import {{ {pid} }} from '../src/products/{pid}';\n"
            f"describe('{pid}', () => {{\n{cases}}});\n")
    smoke = "".join(f'Step "{e["method"]} {e["path"]}" {{ Hit "{e["method"]} {e["path"]}" 200 }}\n'
                    for e in eps)
    return {f"src/products/{pid}.ts": product, f"test/{pid}.test.ts": test,
            "smoke.lines": smoke}


def answer(files, problems=()) -> Ok:
    return Ok(json.dumps({"files": files, "problems": list(problems)}))


def with_file(entry, name, text) -> dict:
    files = good_files(entry)
    key = next(k for k in files if k.endswith(name))
    files[key] = text
    return files


# ---- the backlog ------------------------------------------------------------------------------
class BacklogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_the_seed_is_written_once_and_marked_unvalidated(self) -> None:
        first = load_backlog(self.dir)
        self.assertTrue(first.seeded)
        self.assertGreaterEqual(len(first.entries), 3)
        self.assertTrue(all(e["validated"] is False for e in first.entries))
        doc = json.loads((self.dir / "backlog.json").read_text(encoding="utf-8"))
        doc["entries"][0]["priority"] = 5
        (self.dir / "backlog.json").write_text(json.dumps(doc), encoding="utf-8")
        second = load_backlog(self.dir)
        self.assertFalse(second.seeded)
        self.assertEqual(second.entries[0]["id"], doc["entries"][0]["id"])   # his order stands

    def test_the_owner_can_remove_and_add_and_a_bad_entry_is_reported_not_built(self) -> None:
        load_backlog(self.dir)
        doc = json.loads((self.dir / "backlog.json").read_text(encoding="utf-8"))
        doc["entries"][0]["state"] = "removed"
        bad = dict(entry_of("cron"), id="UPPER")
        doc["entries"].append(bad)
        (self.dir / "backlog.json").write_text(json.dumps(doc), encoding="utf-8")
        got = load_backlog(self.dir)
        ids = [e["id"] for e in got.entries]
        self.assertNotIn(doc["entries"][0]["id"], ids)
        self.assertNotIn("UPPER", ids)
        self.assertEqual([i for i, _r in got.invalid], ["UPPER"])

    def test_an_entry_naming_a_model_or_reusing_a_reserved_id_is_refused(self) -> None:
        self.assertTrue(check_entry(dict(entry_of(), summary="Powered by Claude, an AI model.")))
        self.assertTrue(check_entry(dict(entry_of(), id="health")))

    def test_an_unreadable_file_raises_and_is_never_overwritten(self) -> None:
        path = self.dir / "backlog.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_backlog(self.dir)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


# ---- the static checks ------------------------------------------------------------------------
class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entry = entry_of()
        self.files = good_files(self.entry)

    def test_a_proper_build_passes(self) -> None:
        self.assertEqual(check_build(self.entry, self.files), [])

    def assertRefused(self, files, word) -> None:
        reasons = check_build(self.entry, files)
        self.assertTrue(any(word in r for r in reasons), reasons)

    def bad(self, name, add) -> dict:
        files = good_files(self.entry)
        key = next(k for k in files if k.endswith(name))
        files[key] = files[key] + add
        return files

    def test_each_banned_construct_is_refused(self) -> None:
        for add, word in (("\nawait fetch('x');\n", "fetch"), ("\neval('1');\n", "eval"),
                          ("\nconst a = process;\n", "process"),
                          ("\nconst a = await import('x');\n", "dynamic import"),
                          ("\nconst k = env.SECRET;\n", "env"),
                          ("\nconst s = 'sk_live_abcdefgh12345678';\n", "secret"),
                          ("\n// C:\\Users\\Ian\\x\n", "secret or a local path"),
                          ("\nconst z = '\u202e';\n", "hidden"),
                          ("\nconst m = 'a Claude model';\n", "model")):
            self.assertRefused(self.bad(self.entry["id"] + ".ts", add), word)

    def test_an_unlisted_import_and_extra_files_are_refused(self) -> None:
        self.assertRefused(self.bad(self.entry["id"] + ".ts", "\nimport x from 'left-pad';\n"),
                           "left-pad")
        files = dict(self.files, **{"worker/wrangler.toml": "x"})
        self.assertRefused(files, "not allowed")
        files = dict(self.files)
        del files["smoke.lines"]
        self.assertRefused(files, "not written")

    def test_tests_that_cannot_fail_are_refused(self) -> None:
        pid = self.entry["id"]
        self.assertRefused(self.bad(f"{pid}.test.ts", "\nit.skip('x', () => {});\n"),
                           "narrows tests")
        files = with_file(self.entry, f"{pid}.test.ts",
                          "import { it, expect } from 'vitest';\n"
                          f"import {{ {pid} }} from '../src/products/{pid}';\n"
                          "it('a', () => { expect(1).toBe(1); });\n")
        self.assertRefused(files, "cases")

    def test_the_product_must_carry_the_owners_words_and_every_endpoint(self) -> None:
        pid = self.entry["id"]
        files = with_file(self.entry, f"{pid}.ts", good_files(self.entry)[f"src/products/{pid}.ts"]
                          .replace(self.entry["summary"], "something else"))
        self.assertRefused(files, "summary")
        files = with_file(self.entry, f"{pid}.ts", good_files(self.entry)[f"src/products/{pid}.ts"]
                          .replace(self.entry["endpoints"][0]["path"], "/v1/x"))
        self.assertRefused(files, "does not declare")

    def test_smoke_lines_are_held_to_the_few_things_they_may_do(self) -> None:
        ep = self.entry["endpoints"][0]
        base = f'Step "{ep["method"]} {ep["path"]}" {{ Hit "{ep["method"]} {ep["path"]}" 200 }}\n'
        for line, word in (
                (base + 'Step "GET /v1/colour/other" { Hit "GET /v1/colour/other" 200 }\n', "not in"),
                (base.replace("200 }", "200; Invoke-Expression 'x' }"), "may not"),
                (base.replace("200 }", "200 | Out-File x }"), "may not"),
                (base.replace("Hit", "Hit $env:X; Hit"), "may not"),
                (base.replace("200 }", "200 https://evil.example }"), "host")):
            files = with_file(self.entry, "smoke.lines", line)
            reasons = check_build(self.entry, files)
            self.assertTrue(any(word in r for r in reasons), (line, reasons))
        files = with_file(self.entry, "smoke.lines", base)
        self.assertTrue(any("no Hit line" in r for r in check_build(self.entry, files)))

    def test_the_prompt_carries_the_spec_as_data_and_the_reasons_on_a_retry(self) -> None:
        prompt = build_prompt(self.entry, ["the test has 1 cases"])
        self.assertIn(self.entry["name"], prompt)
        self.assertIn("the test has 1 cases", prompt)
        self.assertNotIn("the test has 1 cases", build_prompt(self.entry))

    def test_parse_build_needs_a_files_object(self) -> None:
        self.assertEqual(parse_build("nope")[0], None)
        files, problems = parse_build(json.dumps({"files": {"a": "b", "c": 1}, "problems": ["p"]}))
        self.assertEqual((files, problems), ({"a": "b"}, ["p"]))


# ---- staging ----------------------------------------------------------------------------------
REGISTRY = ("import type { Product } from '../openapi';\r\nimport { alpha } from './alpha';\r\n\r\n"
            "export const products: Product[] = [alpha];\r\n")
SMOKE = 'Step "GET /health" { Hit "GET /health" 200 }\r\n$dash = try { 1 } catch { 2 }\r\n'


def make_repo(root: Path) -> Path:
    repo = root / "scrooge"
    (repo / "worker/src/products").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "worker/src/products/index.ts").write_bytes(REGISTRY.encode())
    (repo / "tools/smoke.ps1").write_bytes(SMOKE.encode())
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        rc, _o, err = git(repo, *args)
        assert rc == 0, err
    return repo


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = make_repo(self.root)
        self.wt = self.root / "wt"
        self.entry = entry_of()

    def test_a_build_lands_on_its_own_branch_in_a_worktree_and_main_is_untouched(self) -> None:
        main_before = git(self.repo, "rev-parse", "main")[1]
        staged = stage(self.repo, self.wt, self.entry, good_files(self.entry))
        self.assertEqual(staged.branch, "api/colour")
        self.assertEqual(git(self.repo, "rev-parse", "main")[1], main_before)
        self.assertEqual(git(self.wt / "colour", "rev-parse", "--abbrev-ref", "HEAD")[1],
                         "api/colour")
        self.assertEqual(git(self.wt / "colour", "rev-parse", "HEAD")[1], staged.commit)
        self.assertEqual(git(self.wt / "colour", "status", "--porcelain")[1], "")
        self.assertFalse((self.repo / "worker/src/products/colour.ts").exists())  # not his tree
        names = git(self.repo, "diff", "--name-only", f"main..{staged.commit}")[1].split()
        self.assertEqual(sorted(names), ["tools/smoke.ps1", "worker/src/products/colour.ts",
                                         "worker/src/products/index.ts",
                                         "worker/test/colour.test.ts"])
        reg = (self.wt / "colour/worker/src/products/index.ts").read_bytes().decode()
        self.assertIn("import { colour } from './colour';\r\n", reg)
        self.assertIn("[alpha, colour];", reg)
        smoke = (self.wt / "colour/tools/smoke.ps1").read_bytes().decode()
        self.assertLess(smoke.index('Step "GET /v1/colour/contrast"'), smoke.index("$dash = try"))
        self.assertNotIn("\n\n\n", smoke.replace("\r\n", "\n"))

    def test_nothing_is_pushed_and_no_remote_is_needed(self) -> None:
        stage(self.repo, self.wt, self.entry, good_files(self.entry))
        self.assertEqual(git(self.repo, "remote")[1], "")

    def test_an_existing_branch_or_worktree_is_never_overwritten(self) -> None:
        stage(self.repo, self.wt, self.entry, good_files(self.entry))
        with self.assertRaises(StageError):
            stage(self.repo, self.wt, self.entry, good_files(self.entry))
        git(self.repo, "worktree", "remove", "--force", str(self.wt / "colour"))
        with self.assertRaises(StageError) as ctx:
            stage(self.repo, self.wt, self.entry, good_files(self.entry))
        self.assertIn("already exists", str(ctx.exception))

    def test_a_failure_after_the_worktree_exists_leaves_nothing_behind(self) -> None:
        (self.repo / "worker/src/products/index.ts").write_bytes(b"export const x = 1;\n")
        git(self.repo, "commit", "-qam", "odd registry")
        with self.assertRaises(StageError):
            stage(self.repo, self.wt, self.entry, good_files(self.entry))
        self.assertFalse((self.wt / "colour").exists())
        self.assertNotEqual(git(self.repo, "show-ref", "--verify", "--quiet",
                                "refs/heads/api/colour")[0], 0)

    def test_it_refuses_a_missing_repo_and_never_stages_on_main(self) -> None:
        with self.assertRaises(StageError):
            stage(self.root / "nowhere", self.wt, self.entry, good_files(self.entry))
        with self.assertRaises(StageError):
            stage(self.repo, self.wt, self.entry, good_files(self.entry), base="nope")
        self.assertEqual(branch_for("colour"), "api/colour")

    def test_the_registry_and_smoke_edits_refuse_what_they_do_not_know(self) -> None:
        with self.assertRaises(StageError):
            registry_edit(REGISTRY, "alpha")
        with self.assertRaises(StageError):
            smoke_edit("no marker here\n", "Step x\n")
        self.assertIn("[alpha, beta]", registry_edit(REGISTRY.replace("\r\n", "\n"), "beta"))


# ---- the worker -------------------------------------------------------------------------------
class FakePionir:
    def __init__(self) -> None:
        self.jobs: list = []
        self.approvals: dict = {}
        self.outcome = None

    def job(self, job):
        self.jobs.append(job)
        assert job.capability == VERIFY, job.capability
        if self.outcome is not None:
            return self.outcome
        n = len(self.jobs)
        return JobOutcome("pending_approval", VERIFY, task_id=f"t-{n}", approval_id=f"ap-{n}")

    def approval(self, approval_id):
        return dict(self.approvals.get(approval_id, {"status": "pending"}))

    def approve(self, approval_id, inner) -> None:
        self.approvals[approval_id] = {"id": approval_id, "status": "approved", "result": {
            "ok": True, "agent_id": "apibuild", "result": inner}}


def rows(green=True) -> list:
    return [{"check": "tsc --noEmit", "ok": True, "exit": 0, "detail": ""},
            {"check": "vitest run", "ok": green, "exit": 0 if green else 1,
             "detail": "" if green else "FAIL colour.test.ts"}]


GREEN = {"ok": True, "green": True, "id": "colour", "rows": rows(True)}
RED = {"ok": True, "green": False, "id": "colour", "rows": rows(False)}


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.builds = self.root / "apibuilds"
        self.repo = make_repo(self.root)
        self.worker = default_registry().require("products.api_builder")
        self.pionir = FakePionir()
        self.answers: list = []
        self.prompts: list = []

    def build_site(self, prompt, timeout=None):
        self.prompts.append(prompt)
        got = self.answers.pop(0) if self.answers else Err(ClaudeRefusal("off", "no runner"))
        return got(prompt) if callable(got) else got

    def run_at(self, now=T0, **over):
        kw = dict(now=now, http=None, secrets_dir=self.state, job=self.pionir.job,
                  approval=self.pionir.approval, state_dir=self.state,
                  build_site=self.build_site, apibuilds_dir=self.builds,
                  scrooge_repo=self.repo)
        kw.update(over)
        return self.worker.run(WorkContext(**kw))

    def ok(self, **over):
        result = self.run_at(**over)
        self.assertIsInstance(result, Ok, result)
        return result

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))

    @staticmethod
    def kinds(result) -> list:
        return [o.kind for o in result.value]

    def first_id(self) -> str:
        return load_backlog(self.builds).entries[0]["id"]

    def test_it_is_a_live_worker_that_declares_the_one_capability_it_calls(self) -> None:
        self.assertTrue(self.worker.live)
        self.assertEqual(tuple(default_registry().uses("products.api_builder")), (VERIFY,))

    def test_it_will_not_run_without_the_things_it_needs(self) -> None:
        for field in ("state_dir", "job", "build_site", "apibuilds_dir", "scrooge_repo"):
            result = self.run_at(**{field: None})
            self.assertIsInstance(result, Err, field)
            self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)

    def test_an_unreadable_record_or_backlog_refuses_to_build(self) -> None:
        self.state.mkdir(parents=True)
        self.worker.record_path(self.state).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_at().error.kind, ErrorKind.MALFORMED)
        self.assertEqual(self.prompts, [])
        self.worker.record_path(self.state).unlink()
        self.builds.mkdir(parents=True)
        (self.builds / "backlog.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(self.run_at().error.kind, ErrorKind.MALFORMED)
        self.assertEqual(self.prompts, [])

    def test_a_good_build_is_staged_and_parked_for_the_owner_and_nothing_else(self) -> None:
        pid = self.first_id()
        entry = next(e for e in load_backlog(self.builds).entries if e["id"] == pid)
        self.answers.append(answer(good_files(entry)))
        result = self.ok()
        self.assertEqual(self.kinds(result)[:2], ["api.staged", "api.pending"])
        (job,) = self.pionir.jobs
        commit = git(self.repo, "rev-parse", f"api/{pid}")[1]
        self.assertEqual(job.payload, {"id": pid, "commit": commit})
        self.assertEqual(self.record()["products"][pid]["state"], "pending")
        self.assertEqual(git(self.repo, "rev-parse", "main")[1],
                         git(self.repo, "rev-list", "--max-parents=0", "main")[1])

    def test_a_second_product_is_not_built_while_one_waits_on_the_owner(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        self.answers.append(answer(good_files(entry)))
        self.ok()
        calls = len(self.prompts)
        self.ok(now=T0 + 6 * 3600)
        self.assertEqual(len(self.prompts), calls)
        self.assertEqual(len(self.pionir.jobs), 1)

    def test_green_after_his_yes_is_verified_and_tells_him_what_to_run(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        self.answers.append(answer(good_files(entry)))
        self.ok()
        self.pionir.approve("ap-1", GREEN)
        self.answers.append(answer(good_files(load_backlog(self.builds).entries[1])))
        result = self.ok(now=T0 + 3600)
        (verified,) = [o for o in result.value if o.kind == "api.verified"]
        self.assertIn("deploy.ps1", verified.payload["next"])
        self.assertIn("nothing was deployed", verified.payload["next"])
        self.assertEqual(self.record()["products"][entry["id"]]["state"], "verified")
        # once his product is settled the next one is built, in the same run
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("api.staged", self.kinds(result))

    def test_red_is_reported_red_never_green(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        self.answers.append(answer(good_files(entry)))
        self.ok()
        self.pionir.approve("ap-1", RED)
        result = self.ok(now=T0 + 3600)
        self.assertIn("api.red", self.kinds(result))
        self.assertNotIn("api.verified", self.kinds(result))
        self.assertIn("FAIL colour.test.ts",
                      self.record()["products"][entry["id"]]["rows"][1]["detail"])

    def test_denied_is_left_alone_and_a_checks_that_could_not_run_are_offered_again(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        self.answers.append(answer(good_files(entry)))
        self.ok()
        self.pionir.approvals["ap-1"] = {"status": "denied", "reason": "no"}
        self.assertIn("api.denied", self.kinds(self.ok(now=T0 + 3600)))
        self.assertEqual(len(self.pionir.jobs), 1)

    def test_a_check_that_could_not_run_is_offered_again_not_called_red(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        self.answers.append(answer(good_files(entry)))
        self.ok()
        self.pionir.approve("ap-1", {"ok": False, "unavailable": "npm missing",
                                     "error": "npm missing", "rows": []})
        result = self.ok(now=T0 + 3600)
        self.assertNotIn("api.red", self.kinds(result))
        self.assertNotIn("api.verified", self.kinds(result))
        self.ok(now=T0 + 7200)
        self.assertEqual(len(self.pionir.jobs), 2)       # offered to him again

    def test_a_job_that_ran_without_being_parked_is_logged_as_a_broken_gate(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        self.pionir.outcome = JobOutcome("done", VERIFY, task_id="t", result=GREEN)
        self.answers.append(answer(good_files(entry)))
        with self.assertLogs("pionir.crew", level="ERROR") as logs:
            self.ok()
        self.assertTrue(any("WITHOUT the owner's approval" in line for line in logs.output))
        self.assertFalse(self.record()["products"][entry["id"]].get("approved_by_owner", True))

    def test_when_pionir_lacks_the_capability_the_branch_waits_and_is_not_rebuilt(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        self.pionir.outcome = JobOutcome("failed", VERIFY, error="CapabilityNotFound",
                                         error_type="CapabilityNotFound")
        self.answers.append(answer(good_files(entry)))
        result = self.ok()
        self.assertIn("api.not_set_up", self.kinds(result))
        self.pionir.outcome = None
        self.ok(now=T0 + 3600)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.record()["products"][entry["id"]]["state"], "pending")

    def test_a_cap_or_usage_window_waits_and_costs_no_attempt(self) -> None:
        self.answers.append(Err(ClaudeRefusal("budget", "the daily cap is spent")))
        result = self.ok()
        self.assertIn("api.waiting", self.kinds(result))
        pid = self.first_id()
        self.assertEqual(self.record()["products"][pid]["attempts"], [])

    def test_a_rejected_build_is_retried_with_the_reasons_then_shelved(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        pid = entry["id"]
        evil = with_file(entry, f"{pid}.ts", "await fetch('x');\n")
        self.answers.extend([answer(evil)] * 5)
        first = self.ok()
        self.assertEqual(len(self.prompts), 2)                   # two tries in one run
        self.assertIn("fetch", self.prompts[1])                  # the second carries the reasons
        self.assertEqual(self.pionir.jobs, [])
        self.assertNotIn("api.shelved", self.kinds(first))
        second = self.ok(now=T0 + 3600)
        self.assertIn("api.shelved", self.kinds(second))
        self.assertEqual(len(self.prompts), MAX_ATTEMPTS)
        calls = len(self.prompts)
        self.ok(now=T0 + 7200)                                   # the next product, not this again
        self.assertEqual(self.record()["products"][pid]["state"], "shelved")
        self.assertEqual(len(self.record()["products"][pid]["attempts"]), MAX_ATTEMPTS)
        self.assertGreater(len(self.prompts), calls)
        self.assertEqual(self.pionir.jobs, [])
        self.assertNotEqual(git(self.repo, "show-ref", "--verify", "--quiet",
                                f"refs/heads/api/{pid}")[0], 0)

    def test_retry_reopens_a_shelved_product_and_a_leftover_branch_is_reported_not_deleted(self) -> None:
        entry = load_backlog(self.builds).entries[0]
        pid = entry["id"]
        evil = with_file(entry, f"{pid}.ts", "await fetch('x');\n")
        self.answers.extend([answer(evil)] * 3)
        self.ok()
        self.ok(now=T0 + 3600)
        self.assertEqual(self.record()["products"][pid]["state"], "shelved")
        doc = json.loads((self.builds / "backlog.json").read_text(encoding="utf-8"))
        next(e for e in doc["entries"] if e["id"] == pid)["retry"] = 1
        (self.builds / "backlog.json").write_text(json.dumps(doc), encoding="utf-8")
        self.answers.append(answer(good_files(entry)))
        self.ok(now=T0 + 7200)
        self.assertEqual(self.record()["products"][pid]["state"], "pending")
        # a second retry on a product that still has its branch: nothing is deleted
        self.pionir.approve("ap-1", RED)
        self.ok(now=T0 + 8000)
        doc = json.loads((self.builds / "backlog.json").read_text(encoding="utf-8"))
        next(e for e in doc["entries"] if e["id"] == pid)["retry"] = 2
        (self.builds / "backlog.json").write_text(json.dumps(doc), encoding="utf-8")
        result = self.ok(now=T0 + 9000)
        (tally,) = [o for o in result.value if o.kind == "api.tally"]
        (clean,) = tally.payload["needs_cleanup_before_retry"]
        self.assertEqual(clean["id"], pid)
        self.assertTrue(any("branch -D" in c for c in clean["commands"]))
        self.assertEqual(git(self.repo, "show-ref", "--verify", "--quiet",
                             f"refs/heads/api/{pid}")[0], 0)

    def test_an_invalid_backlog_entry_is_reported_and_the_rest_still_build(self) -> None:
        load_backlog(self.builds)
        doc = json.loads((self.builds / "backlog.json").read_text(encoding="utf-8"))
        doc["entries"].insert(0, dict(entry_of("cron"), id="BAD"))
        (self.builds / "backlog.json").write_text(json.dumps(doc), encoding="utf-8")
        self.answers.append(answer(good_files(load_backlog(self.builds).entries[0])))
        result = self.ok()
        self.assertIn("api.backlog_invalid", self.kinds(result))
        self.assertIn("api.staged", self.kinds(result))

    def test_the_tally_carries_counts_and_no_model_word_reaches_a_row(self) -> None:
        entry = load_backlog(self.builds, write_seed=False).entries[0]
        self.answers.append(answer(good_files(entry)))
        result = self.ok()
        (tally,) = [o for o in result.value if o.kind == "api.tally"]
        self.assertTrue(tally.figures)
        self.assertTrue(tally.payload["backlog_seeded"])
        self.assertGreater(tally.payload["unvalidated_ideas"], 0)
        for o in result.value:
            self.assertIsNone(backlog_mod.MODEL_WORDS.search(json.dumps(o.payload)), o.kind)


# ---- the adapter ------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_GIT, "git is not installed")
class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = make_repo(self.root)
        self.entry = entry_of()
        self.builds = self.root / "apibuilds"
        self.staged = stage(self.repo, self.builds / "worktrees", self.entry,
                            good_files(self.entry))
        self.calls: list = []
        self.codes: dict = {}
        self.adapter = ApiBuildAdapter(ApiBuildSettings(self.repo, self.builds),
                                       runner=self.runner)

    def runner(self, argv, cwd, timeout):
        self.calls.append((list(argv), Path(cwd)))
        key = " ".join(argv[:3])
        got = self.codes.get(key, (0, "ok"))
        if isinstance(got, Exception):
            raise got
        return got

    def task(self, **payload) -> Task:
        body = {"id": "colour", "commit": self.staged.commit}
        body.update(payload)
        return Task(capability=VERIFY, payload=body)

    def test_it_is_privileged_parked_every_time_and_not_routable(self) -> None:
        (cap,) = self.adapter.manifest.capabilities
        self.assertTrue(cap.requires_approval)
        self.assertFalse(cap.routable)
        self.assertEqual(cap.name, VERIFY)

    def test_the_payload_is_exactly_an_id_and_a_full_commit(self) -> None:
        for payload in ({"id": "colour"}, {"id": "colour", "commit": "abc"},
                        {"id": "../x", "commit": self.staged.commit},
                        {"id": "colour", "commit": self.staged.commit, "cmd": "rm"}):
            with self.assertRaises(AdapterProtocolError):
                self.adapter.validate(Task(capability=VERIFY, payload=payload))
        self.adapter.validate(self.task())

    def test_a_branch_that_moved_or_a_dirty_worktree_is_refused_not_run(self) -> None:
        wt = self.builds / "worktrees" / "colour"
        (wt / "worker/src/products/colour.ts").write_text("// changed\n", encoding="utf-8")
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(self.task())
        git(wt, "commit", "-qam", "more")
        with self.assertRaises(AdapterProtocolError) as ctx:
            self.adapter.validate(self.task())
        self.assertIn("moved", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_the_card_shows_the_branch_the_commit_and_the_files(self) -> None:
        card = self.adapter.park_context(self.task())
        self.assertEqual(card["branch"], "api/colour")
        self.assertEqual(card["commit"], self.staged.commit)
        self.assertIn("worker/src/products/colour.ts", card["files"])

    def test_green_runs_install_tsc_and_vitest_in_the_worktree_only(self) -> None:
        out = self.adapter.execute(self.task()).output
        self.assertTrue(out["ok"] and out["green"])
        wt = self.builds / "worktrees" / "colour" / "worker"
        self.assertEqual([c[0][:3] for c in self.calls],
                         [["npm", "ci", "--ignore-scripts"], ["npx", "--no-install", "tsc"],
                          ["npx", "--no-install", "vitest"]])
        self.assertTrue(all(cwd == wt for _a, cwd in self.calls))
        self.assertIn("deploy.ps1", out["next"])
        self.assertNotIn("returncode", out)

    def test_install_is_skipped_when_node_modules_exists(self) -> None:
        (self.builds / "worktrees" / "colour" / "worker" / "node_modules").mkdir()
        self.adapter.execute(self.task())
        self.assertEqual([c[0][0] for c in self.calls], ["npx", "npx"])

    def test_a_failing_check_is_an_answer_red_not_a_fault(self) -> None:
        self.codes["npx --no-install vitest"] = (1, "FAIL colour.test.ts")
        out = self.adapter.execute(self.task()).output
        self.assertTrue(out["ok"])
        self.assertFalse(out["green"])
        self.assertIn("FAIL", out["rows"][-1]["detail"])
        self.assertIn("RED", out["next"])

    def test_a_failed_install_stops_the_run_and_a_tool_that_cannot_run_is_unavailable(self) -> None:
        self.codes["npm ci --ignore-scripts"] = (1, "ERESOLVE")
        out = self.adapter.execute(self.task()).output
        self.assertFalse(out["green"])
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.codes["npm ci --ignore-scripts"] = OSError("npm is not installed")
        out = self.adapter.execute(self.task()).output
        self.assertFalse(out["ok"])
        self.assertIn("unavailable", out)

    def test_status_is_local_and_honest_when_the_repo_is_missing(self) -> None:
        self.assertIn("scrooge_repo", self.adapter.status())
        from pionir.errors import AdapterUnavailable
        gone = ApiBuildAdapter(ApiBuildSettings(self.root / "nowhere", self.builds))
        with self.assertRaises(AdapterUnavailable):
            gone.status()


class CardTests(unittest.TestCase):
    ROW = {"id": "ap-1", "capability": VERIFY, "summary": "verify colour", "requester": "x",
           "payload": {"id": "colour", "commit": "a" * 40},
           "context": {"branch": "api/colour", "commit": "a" * 40,
                       "files": ["worker/src/products/colour.ts"],
                       "diff_stat": " 4 files changed", "checks": "static checks passed",
                       "runs": "tsc and vitest"}}

    def test_the_card_shows_the_branch_the_files_and_that_nothing_is_deployed(self) -> None:
        from pionir.discord_gate import render_request
        text = render_request(self.ROW, "123")
        for want in ("api/colour", "worker/src/products/colour.ts", "4 files changed",
                     "Nothing is deployed", "tsc and vitest"):
            self.assertIn(want, text)

    def test_a_card_with_no_context_still_says_what_approving_does(self) -> None:
        from pionir.discord_gate import render_request
        text = render_request({**self.ROW, "context": None}, "123")
        self.assertIn("Nothing is deployed", text)


if __name__ == "__main__":
    unittest.main()
