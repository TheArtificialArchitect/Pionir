"""The Library: the owner's surfaces read Pionir's memory - and nothing else can, and
nothing can write through it.

Real HTTP against the real handler (as Pionir Desktop speaks to it), over a temporary
cortex: the reads answer only a signed owner client, every argument is checked, the
link graph stays inside a namespace, and the store's bytes are the same after every
read as before.
"""

import hashlib
import http.client
import json
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from standins import down_url

from pionir import auth, library
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.server import PionirApp, _make_handler


def _runtime(tmp: str):
    return build_runtime(PionirSettings(
        state_root=Path(tmp),
        atani_command=("pionir-test-no-such-binary",),
        galatea_url=down_url(),
        galatea_model_id="stub-model",
        embed_model=None,  # never reach the live Ollama embedder from a test
        daedalus_url=down_url(),
        melete_url=down_url(),
        crew_url=None,
        bryo_status_command=None,
        nyx_status_command=None,
        voodoo_status_command=None,
        evict_to_fit=False,
    ))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LibraryOverHttp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        runtime = _runtime(self._tmp.name)
        c = runtime.cortex
        self.ids = {
            "lesson_a": c.record_lesson("fiverr.ack failed: Scrooge answered 400 - send the numeric id",
                                        slug="failure:fiverr.ack", links=["failure:scrooge"]),
            "lesson_b": c.record_lesson("scrooge refused a call: check its id field names",
                                        slug="failure:scrooge"),
            "lesson_c": c.record_lesson("fiverr.ack failed again: 100% of acks bounce",
                                        slug="failure:fiverr.ack"),
            "private": c.remember("fact", "Ian prefers the moss colour", namespace="galatea",
                                  slug="failure:scrooge"),
            "episode": c.remember("episode", "we talked about the Fiverr stall and pricing",
                                  namespace="galatea", links=["failure:fiverr.ack"]),
            "note": c.remember("note", "a_b literal underscore check", namespace="crew"),
        }
        self.retired = c.remember("message", "an old turn, folded away", namespace="galatea")
        c.forget(self.retired)
        self.store = Path(c.path)
        self.app = PionirApp(runtime)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    # ---- wire -----------------------------------------------------------------------------
    def raw(self, path: str, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = dict(headers or {})
        conn.putrequest("GET", path, skip_host="Host" in headers)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, json.loads(body.decode("utf-8") or "{}")

    def signed(self, path: str, client: str = "desktop", *, nonce: str | None = None,
               sign_path: str | None = None, token: str | None = None, **extra: str):
        stamp = str(int(time.time()))
        once = nonce or secrets.token_hex(16)
        key = token if token is not None else self.app.auth.tokens[client]
        sig = auth.request_sig(key, "GET", sign_path or path, stamp, once, b"")
        return self.raw(path, {"X-Pionir-Client": client, "X-Pionir-Ts": stamp,
                               "X-Pionir-Nonce": once, "X-Pionir-Sig": sig, **extra})

    # ---- who may read ---------------------------------------------------------------------
    def test_only_a_signed_owner_client_reads_it(self) -> None:
        path = "/api/library/overview"
        status, out = self.raw(path)
        self.assertEqual(status, 401, out)                       # loopback alone is not enough
        status, out = self.raw(path, {"Authorization": f"Bearer {self.app.auth.tokens['crew']}"})
        self.assertEqual(status, 403, out)                       # a bot may not read the owner's view
        status, out = self.raw(path, {"Authorization": f"Bearer {self.app.auth.tokens['desktop']}"})
        self.assertEqual(status, 401, out)                       # the desktop signs; never a bearer
        status, out = self.raw(path, {"Authorization": "Bearer " + "x" * 43})
        self.assertEqual(status, 401, out)
        status, out = self.signed(path, "crew")
        self.assertEqual(status, 403, out)                       # signed, but not an owner surface
        status, out = self.signed(path, token="y" * 43)
        self.assertEqual(status, 401, out)                       # signed with the wrong key
        status, out = self.signed(path, sign_path="/api/library/entries")
        self.assertEqual(status, 401, out)                       # a signature for another read
        status, out = self.signed("/api/library/entries?namespace=galatea",
                                  sign_path="/api/library/entries?namespace=lessons")
        self.assertEqual(status, 401, out)                       # the query is signed too
        status, out = self.signed(path)
        self.assertEqual(status, 200, out)
        # the dashboard's own bearer (its session is the usual way) is an owner surface too
        status, out = self.raw(path, {"Authorization": f"Bearer {self.app.auth.tokens['dashboard']}"})
        self.assertEqual(status, 200, out)

    def test_a_signed_read_is_taken_once_and_the_host_must_be_local(self) -> None:
        status, _ = self.signed("/api/library/overview", nonce="n" * 32)
        self.assertEqual(status, 200)
        status, out = self.signed("/api/library/overview", nonce="n" * 32)
        self.assertEqual(status, 401, out)
        self.assertIn("replayed", out["reason"])
        status, out = self.signed("/api/library/overview", Host="evil.example")
        self.assertEqual(status, 403, out)

    # ---- what it answers ------------------------------------------------------------------
    def test_overview_counts_every_namespace_and_kind(self) -> None:
        status, out = self.signed("/api/library/overview")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["total"], 6)
        self.assertEqual(out["retired"], 1)
        self.assertEqual(out["namespaces"][0]["namespace"], "lessons")   # the shared shelf first
        by_ns = {n["namespace"]: n for n in out["namespaces"]}
        self.assertEqual(by_ns["lessons"]["count"], 3)
        self.assertEqual(by_ns["galatea"]["kinds"], {"fact": 1, "episode": 1})
        self.assertEqual(out["kinds"]["lesson"], 3)
        self.assertGreater(out["size_bytes"], 0)
        self.assertIsInstance(out["last_ts"], float)
        self.assertEqual(out["recall"], "lexical")
        status, out = self.signed("/api/library/overview?x=1")
        self.assertEqual(status, 400, out)

    def test_entries_browse_newest_first_with_keyset_paging(self) -> None:
        status, page = self.signed("/api/library/entries?namespace=lessons&limit=2")
        self.assertEqual(status, 200, page)
        self.assertEqual([e["id"] for e in page["entries"]], [self.ids["lesson_c"], self.ids["lesson_b"]])
        self.assertEqual(page["next_before"], self.ids["lesson_b"])
        status, page2 = self.signed(f"/api/library/entries?namespace=lessons&limit=2&before={page['next_before']}")
        self.assertEqual([e["id"] for e in page2["entries"]], [self.ids["lesson_a"]])
        self.assertIsNone(page2["next_before"])
        # kind filter; retired rows only when asked
        _, facts = self.signed("/api/library/entries?kind=fact")
        self.assertEqual([e["id"] for e in facts["entries"]], [self.ids["private"]])
        _, live = self.signed("/api/library/entries?namespace=galatea")
        self.assertNotIn(self.retired, [e["id"] for e in live["entries"]])
        _, all_ = self.signed("/api/library/entries?namespace=galatea&retired=1")
        self.assertIn(self.retired, [e["id"] for e in all_["entries"]])
        self.assertFalse(next(e for e in all_["entries"] if e["id"] == self.retired)["active"])

    def test_text_search_takes_the_words_literally(self) -> None:
        _, out = self.signed("/api/library/entries?mode=text&q=" + quote("100%"))
        self.assertEqual([e["id"] for e in out["entries"]], [self.ids["lesson_c"]])
        _, out = self.signed("/api/library/entries?mode=text&q=" + quote("a_b"))
        self.assertEqual([e["id"] for e in out["entries"]], [self.ids["note"]])
        _, out = self.signed("/api/library/entries?mode=text&q=" + quote("%"))
        self.assertEqual([e["id"] for e in out["entries"]], [self.ids["lesson_c"]])   # not "everything"
        _, out = self.signed("/api/library/entries?mode=text&q=" + quote("' OR 1=1 --"))
        self.assertEqual(out["entries"], [])

    def test_recall_search_ranks_like_the_bots_recall(self) -> None:
        status, out = self.signed("/api/library/entries?q=" + quote("fiverr ack scrooge"))
        self.assertEqual(status, 200, out)
        self.assertEqual(out["mode"], "recall")
        self.assertTrue(out["entries"])
        self.assertTrue(all("score" in e for e in out["entries"]))
        _, scoped = self.signed("/api/library/entries?namespace=lessons&q=" + quote("fiverr"))
        self.assertTrue(all(e["namespace"] == "lessons" for e in scoped["entries"]))

    def test_an_entry_in_full_with_its_links_kept_inside_its_namespace(self) -> None:
        status, e = self.signed(f"/api/library/entry?id={self.ids['lesson_a']}")
        self.assertEqual(status, 200, e)
        self.assertIn("send the numeric id", e["text"])
        self.assertEqual(e["links"], ["failure:scrooge"])
        # links to lesson_b (lessons) - never to galatea's private fact sharing the slug
        self.assertEqual([x["id"] for x in e["links_to"]], [self.ids["lesson_b"]])
        self.assertEqual([x["id"] for x in e["same_slug"]], [self.ids["lesson_c"]])
        self.assertEqual(e["same_slug_total"], 2)
        # lesson_b is linked from lesson_a (and not from anything in another namespace)
        _, b = self.signed(f"/api/library/entry?id={self.ids['lesson_b']}")
        self.assertEqual([x["id"] for x in b["linked_from"]], [self.ids["lesson_a"]])
        # galatea's episode links failure:fiverr.ack, but that slug lives in lessons: nothing crosses
        _, ep = self.signed(f"/api/library/entry?id={self.ids['episode']}")
        self.assertEqual(ep["links_to"], [])
        status, out = self.signed("/api/library/entry?id=999999")
        self.assertEqual(status, 404, out)

    def test_every_bad_argument_is_refused(self) -> None:
        bad = [
            "/api/library/entries?namespace=" + quote("les sons"),
            "/api/library/entries?namespace=" + quote("x' OR '1'='1"),
            "/api/library/entries?kind=" + "k" * 65,
            "/api/library/entries?limit=0",
            "/api/library/entries?limit=101",
            "/api/library/entries?limit=-3",
            "/api/library/entries?before=abc",
            "/api/library/entries?q=" + "q" * 201,
            "/api/library/entries?q=" + quote("line\nbreak"),
            "/api/library/entries?mode=sql&q=x",
            "/api/library/entries?limit=5&limit=6",
            "/api/library/entry",
            "/api/library/entry?id=1.5",
            "/api/library/entry?id=" + quote("1 OR 1=1"),
        ]
        for path in bad:
            status, out = self.signed(path)
            self.assertEqual(status, 400, f"{path}: {out}")
        status, _ = self.signed("/api/library/nothing")
        self.assertEqual(status, 404)

    def test_no_read_changes_a_byte_of_the_store(self) -> None:
        self.app.runtime.cortex.close()        # nothing else holds the file (closing twice is fine)
        before = _digest(self.store)
        for path in ("/api/library/overview", "/api/library/entries?namespace=lessons",
                     "/api/library/entries?mode=text&q=fiverr",
                     f"/api/library/entry?id={self.ids['lesson_a']}",
                     f"/api/library/entry?id={self.ids['lesson_b']}"):
            status, out = self.signed(path)
            self.assertEqual(status, 200, f"{path}: {out}")
        self.assertEqual(_digest(self.store), before)


class ReadOnlyByConstruction(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "memory.db"
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT)")
        db.execute("INSERT INTO memories(text) VALUES ('x')")
        db.commit()
        db.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_connection_cannot_write_even_if_query_only_is_turned_off(self) -> None:
        db = library.open_ro(self.path)
        try:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO memories(text) VALUES ('y')")
            db.execute("PRAGMA query_only = OFF")        # mode=ro still holds
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM memories")
        finally:
            db.close()
        check = sqlite3.connect(self.path)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)
        check.close()

    def test_a_missing_store_is_refused_never_created(self) -> None:
        missing = Path(self._tmp.name) / "nope" / "memory.db"
        with self.assertRaises(FileNotFoundError):
            library.open_ro(missing)
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        status, out = library.serve("/api/library/overview", "", client="desktop", refused=None,
                                    store=lambda: (missing, None))
        self.assertEqual(status, 503, out)
        self.assertFalse(missing.exists())

    def test_the_door_checks_the_caller_before_touching_the_store(self) -> None:
        touched: list[int] = []

        def store():
            touched.append(1)
            return self.path, None

        for client, refused, want in ((None, None, 401), (None, "invalid signature", 401),
                                      ("crew", None, 403), ("phone", None, 403),
                                      ("galatea", None, 403), ("anonymous", None, 403)):
            status, _ = library.serve("/api/library/overview", "", client=client,
                                      refused=refused, store=store)
            self.assertEqual(status, want, client)
        self.assertEqual(touched, [])
        self.assertEqual(library.READERS, frozenset({"desktop", "dashboard"}))


if __name__ == "__main__":
    unittest.main()
