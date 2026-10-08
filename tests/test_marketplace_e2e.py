"""Marketplaces, end to end: a gap on the Apify Store to an Actor live on it - on one yes.

Real: the scout, the Builds backlog and the Builds worker (git in temp folders, our checks,
the staging hook), the packager and watcher, the crew's hands and Pionir client, and Pionir
itself serving HTTP on a throwaway loopback port with the Apify adapter registered. Fake: the
Apify Store's public API and the Apify REST API (at the HTTP boundary), the shared brain,
Daedalus (a test commits the build) and Claude's review (it approves).

The chain: the scout reads the Store and queues a spec -> the Builds worker builds it at night
and, Claude having approved, stages it for the Marketplaces packager, NOT the Gumroad shelf ->
the packager drafts the Actor and its listing and submits ``apify.publish`` -> Pionir PARKS it
(nothing reaches Apify) -> the owner approves -> Apify receives exactly the documented calls
-> the packager records it published -> the watcher reads its figures through Pionir. It fails
if any link is cut or the gate is skipped.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from marketplace_support import APIFY_WORDS, FakeHttp, Recorder, isolate
from test_crew_builds import _Case as BuildsCase
from test_crew_builds import approve, at, commit, product
from test_marketplace_adapters import API, TOKEN, FakeApify

from pionir.adapters.apify import ApifyAdapter, ApifySettings
from pionir.auth import token_path
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.crew.builds import backlog as bl
from pionir.crew.hands import Hands, PionirClient
from pionir.crew.marketplaces import paths, providers
from pionir.crew.registry import default_registry
from pionir.crew.result import Ok
from pionir.crew.worker import WorkContext
from pionir.server import PionirApp, _make_handler

STORE = f"{providers.APIFY_STORE}?limit=500&offset=0&sortBy=popularity"
ITEMS = [{"username": f"dev{i}", "name": f"sitemap-{i}", "title": title,
          "categories": ["SEO_TOOLS"], "stats": {"totalUsers30Days": users},
          "actorReviewRating": rating, "actorReviewCount": 5}
         for i, (title, users, rating) in enumerate((("Sitemap Audit Tool", 3000, 2.9),
                                                     ("Sitemap Audit Pro", 1800, 3.2)))]


class EndToEnd(BuildsCase):
    def setUp(self) -> None:
        super().setUp()
        isolate(self)
        self.root = self.builds.parent / "marketplaces"
        (self.secrets / "apify-token.txt").write_text(TOKEN, encoding="utf-8")
        # Pionir: its own state, serving HTTP on a free loopback port
        runtime = build_runtime(PionirSettings(
            state_root=self.builds.parent / "pionir", atani_command=("pionir-test-no-such",),
            daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
            bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
            embed_model=None, evict_to_fit=False, content_url=None))
        self.apify = FakeApify()
        self.apify_http = Recorder(self.apify)
        runtime.executive.registry.unregister("apify")
        runtime.register(ApifyAdapter(ApifySettings(
            api_url=API, token_file=self.secrets / "apify-token.txt", root=self.root,
            secrets_dir=self.secrets), opener=self.apify_http, sleep=lambda s: None))
        self.app = PionirApp(runtime)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)
        client = PionirClient(f"http://127.0.0.1:{self.httpd.server_address[1]}",
                              token_file=token_path(self.builds.parent / "pionir" / "secrets",
                                                    "crew"))
        crew = SimpleNamespace(lock=threading.Lock(), paused_reason=None)
        self.hands = Hands(SimpleNamespace(job_follow_seconds=30.0, job_poll_seconds=1.0),
                           crew, client)
        self.hands.start()
        self.addCleanup(self.hands.stop)
        self.store = FakeHttp({STORE: (200, {"data": {"items": ITEMS}})})

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.runtime.cortex.close()

    def crew_step(self, worker_id: str, now: float):
        worker = default_registry().require(worker_id)
        worker.sleep = lambda s: None

        def words(purpose, system, user, schema):
            return Ok(dict(APIFY_WORDS))

        ctx = WorkContext(now=now, http=self.store, secrets_dir=self.secrets, words=words,
                          job=lambda job: self.hands.run_sync(worker_id, job),
                          approval=self.hands.approval, state_dir=self.state,
                          builds_dir=self.builds)
        got = worker.run(ctx)
        self.assertIsInstance(got, Ok, got)
        return got

    def test_from_a_store_gap_to_a_live_actor_on_one_approval(self) -> None:
        # 1. the scout: the Store's public data -> a spec in the Builds backlog
        got = self.crew_step("marketplaces.scout_apify", at(1, 0, 10))
        self.assertIn("market.spec_queued", [o.kind for o in got.value])
        entry = next(e for e in bl.load(self.builds)["products"] if e["slug"] == "sitemap-audit")
        self.assertEqual(entry["product_type"], "apify_actor")

        # 2. the night builds: Daedalus builds it (Moss's goal names it), Claude approves
        self.goal = "build sitemap-audit next"
        self.answers.append(approve())
        self.run_at(at(1, 1, 30))
        job = self.pionir.builds()[-1]
        self.assertIn("process(record)", job.payload["intent"])       # the core's contract
        head = commit(Path(job.payload["repo"]), product(entry))
        self.pionir.finish(f"t-build-{len(self.pionir.builds()) - 1}", commit=head,
                           branch="main")
        self.run_at(at(1, 1, 50))
        self.assertEqual(self.product_state("sitemap-audit")["state"], "staged")
        self.assertTrue((paths.staged_dir(self.root, "sitemap-audit") / "entry.json").is_file())
        self.assertFalse((self.shelf / "sitemap-audit").exists())     # never the Gumroad shelf
        self.assertNotIn("builds:staged:sitemap-audit", self.pionir.cards)

        # 3. the packager: a listing draft, submitted - and Pionir parks it
        got = self.crew_step("marketplaces.packager", at(1, 2, 0))
        self.assertIn("market.submitted_for_approval", [o.kind for o in got.value])
        (pending,) = self.app.approvals.pending()
        self.assertEqual(pending["capability"], "apify.publish")
        self.assertIn("Sitemap Audit", pending["summary"])
        self.assertEqual(self.apify_http.calls, [])                   # nothing reached Apify

        # 4. the owner's yes -> exactly the documented Apify calls
        res = self.app.approve(pending["id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        calls = [(c["method"], c["url"][len(API):].split("?")[0]) for c in self.apify_http.calls]
        self.assertEqual(calls, [
            ("GET", "/v2/users/me"), ("GET", "/v2/acts/dokaz~sitemap-audit"),
            ("POST", "/v2/acts"), ("POST", "/v2/acts/act-sitemap-audit/builds"),
            ("GET", "/v2/actor-builds/build-1"), ("PUT", "/v2/acts/act-sitemap-audit")])
        created = self.apify_http.json_body(2)
        names = {f["name"] for f in created["versions"][0]["sourceFiles"]}
        self.assertTrue({".actor/actor.json", ".actor/input_schema.json", ".actor/Dockerfile",
                         "src/main.py", "src/sitemap_audit/core.py", "README.md"} <= names)
        main = next(f["content"] for f in created["versions"][0]["sourceFiles"]
                    if f["name"] == "src/main.py")
        self.assertIn('await Actor.charge(event_name=EVENT)', main)
        final = self.apify_http.json_body(5)
        self.assertIs(final["isPublic"], True)
        self.assertEqual(final["pricingInfos"][0]["pricingModel"], "PAY_PER_EVENT")

        # 5. the packager hears it is live; the watcher reads it through Pionir
        got = self.crew_step("marketplaces.packager", at(1, 2, 30))
        self.assertIn("market.published", [o.kind for o in got.value])
        rec = json.loads((self.state / "marketplaces.packager.json").read_text("utf-8"))
        self.assertEqual(rec["drafts"]["sitemap-audit"]["url"],
                         "https://apify.com/dokaz/sitemap-audit")
        self.apify.actors["sitemap-audit"]["stats"] = {"totalUsers": 3, "totalUsers30Days": 3,
                                                       "totalRuns": 7}
        got = self.crew_step("marketplaces.watcher", at(1, 3, 0))
        stats = next(o for o in got.value if o.kind == "market.stats")
        self.assertEqual(stats.payload["item"], "sitemap-audit")
        self.assertEqual(self.app.approvals.pending(), [])            # a read never parks


if __name__ == "__main__":
    unittest.main()
