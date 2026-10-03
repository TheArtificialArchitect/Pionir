"""The script gate: nothing unsourced reaches the voice. Each rule has a test that fails
when the rule is removed."""
from __future__ import annotations

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path

from video_support import FakeWriter, good_script, load, make_niche, pack_document

from pionir.video import niche as niche_module
from pionir.video.passages import PassageError, host_allowed, load_pack
from pionir.video.script import ScriptRejected, check_script
from pionir.video.writer import OllamaWriter, build_prompt, generate_script


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.niche, self.pack = load(self.folder, with_image=False)

    def problems(self, raw) -> list[str]:
        with self.assertRaises(ScriptRejected) as caught:
            check_script(raw, self.niche, self.pack)
        return caught.exception.problems

    def mutated(self, fn):
        raw = copy.deepcopy(good_script(with_image=False))
        fn(raw)
        return raw


class AcceptedTests(Base):
    def test_a_fully_sourced_script_passes(self) -> None:
        script = check_script(good_script(with_image=False), self.niche, self.pack)
        self.assertEqual(script.title, "The old harbor")
        self.assertEqual([p.id for p in script.passages], ["p1", "p2"])
        self.assertIn("The harbor opened in 1851.", script.narration)


class UnsourcedClaimTests(Base):
    def test_a_line_with_no_source_is_blocked(self) -> None:
        def strip(raw):
            raw["scenes"][0]["lines"][0] = {"text": "The harbor opened in 1851."}
        self.assertTrue(any("unsourced" in p for p in self.problems(self.mutated(strip))))

    def test_a_line_citing_a_source_id_that_does_not_exist_is_blocked(self) -> None:
        def invent(raw):
            raw["scenes"][0]["lines"][0]["sources"] = ["p99"]
        self.assertTrue(self.problems(self.mutated(invent)))

    def test_a_number_not_in_the_cited_passage_is_blocked(self) -> None:
        def change(raw):
            raw["scenes"][0]["lines"][0]["text"] = "The harbor opened in 1861."
        self.assertTrue(any("1861" in p for p in self.problems(self.mutated(change))))

    def test_a_name_not_in_the_cited_passage_is_blocked(self) -> None:
        def add(raw):
            raw["scenes"][0]["lines"][0]["text"] = "Captain Vancouver opened the harbor in 1851."
        self.assertTrue(any("Vancouver" in p for p in self.problems(self.mutated(add))))

    def test_a_number_cited_from_the_wrong_passage_is_blocked(self) -> None:
        def swap(raw):
            raw["scenes"][1]["lines"][1]["sources"] = ["p1"]   # two hundred feet is in p2
        self.assertTrue(self.problems(self.mutated(swap)))

    def test_a_connective_cannot_smuggle_a_fact(self) -> None:
        def smuggle(raw):
            raw["scenes"][0]["lines"][1] = {"text": "Then in 1900 everything changed.",
                                            "connective": True}
        self.assertTrue(self.problems(self.mutated(smuggle)))

    def test_a_long_connective_is_blocked(self) -> None:
        def long(raw):
            raw["scenes"][0]["lines"][1] = {"text": " ".join(["and"] * 20), "connective": True}
        self.assertTrue(self.problems(self.mutated(long)))

    def test_too_many_connectives_are_blocked(self) -> None:
        def many(raw):
            raw["scenes"][0]["lines"] = [{"text": "Now, the ships.", "connective": True}] * 3 + [
                {"text": "The harbor opened in 1851.", "sources": ["p1"]}]
        self.assertTrue(self.problems(self.mutated(many)))

    def test_a_title_with_an_unbacked_number_is_blocked(self) -> None:
        def retitle(raw):
            raw["title"] = "The harbor of 1799"
        self.assertTrue(self.problems(self.mutated(retitle)))

    def test_a_script_citing_nothing_is_blocked(self) -> None:
        self.assertTrue(self.problems({"title": "x", "summary": "y", "scenes": []}))

    def test_junk_is_blocked_not_crashed_on(self) -> None:
        for junk in (None, [], "text", {"scenes": "no"}):
            self.problems(junk)

    def test_an_unknown_scene_type_and_an_unknown_image_are_blocked(self) -> None:
        def scene(raw):
            raw["scenes"][0]["type"] = "hologram"
        self.assertTrue(self.problems(self.mutated(scene)))

        def image(raw):
            raw["scenes"][0]["type"] = "image"
            raw["scenes"][0]["image"] = "i404"
        self.assertTrue(self.problems(self.mutated(image)))


class RetryTests(Base):
    def test_the_writer_is_told_why_and_a_bad_script_never_comes_back(self) -> None:
        bad = good_script(with_image=False)
        bad["scenes"][0]["lines"][0] = {"text": "The harbor opened in 1851."}
        writer = FakeWriter(bad, good_script(with_image=False))
        script = generate_script(writer, self.niche, self.pack)
        self.assertEqual(len(writer.calls), 2)
        self.assertTrue(any("unsourced" in f for f in writer.calls[1]))
        self.assertEqual(script.title, "The old harbor")

    def test_when_every_attempt_fails_the_last_rejection_is_raised(self) -> None:
        bad = good_script(with_image=False)
        bad["scenes"][0]["lines"][0] = {"text": "The harbor opened in 1851."}
        writer = FakeWriter(bad)
        with self.assertRaises(ScriptRejected):
            generate_script(writer, self.niche, self.pack, attempts=2)
        self.assertEqual(len(writer.calls), 2)


class LocalModelTests(Base):
    def test_the_request_asks_ollama_for_the_cpu_only(self) -> None:
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"message": {"content": json.dumps(good_script())}}).encode()

        class Opener:
            def open(self, request, timeout=None):
                seen["url"], seen["body"] = request.full_url, json.loads(request.data)
                return Response()

        writer = OllamaWriter(base_url="http://127.0.0.1:1", opener=Opener())
        self.assertEqual(writer.write(self.niche, self.pack, [])["title"], "The old harbor")
        self.assertEqual(seen["body"]["options"]["num_gpu"], 0)
        self.assertEqual(seen["body"]["options"]["temperature"], 0)
        self.assertTrue(seen["url"].startswith("http://127.0.0.1:1/"))

    def test_a_dead_model_is_a_rejection_not_a_crash(self) -> None:
        class Dead:
            def open(self, *a, **k):
                raise OSError("connection refused")
        with self.assertRaises(ScriptRejected):
            OllamaWriter(opener=Dead()).write(self.niche, self.pack, [])

    def test_the_prompt_carries_the_passages_and_the_rejection_reasons(self) -> None:
        prompt = build_prompt(self.niche, self.pack, ["unsourced line"])
        self.assertIn("[p1]", prompt)
        self.assertIn("unsourced line", prompt)

    def test_no_video_module_imports_the_scheduler_or_a_cloud_client(self) -> None:
        # No GPU lease is taken because nothing here can use the card; if a module starts
        # importing the scheduler, this fails and the question "does it need a lease" is asked.
        banned = {"scheduler", "shared_gpu", "anthropic", "openai"}
        folder = Path(niche_module.__file__).parent
        for path in list(folder.glob("*.py")) + [folder.parent / "adapters" / "video.py"]:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "")] + [a.name for a in node.names]
                for name in names:
                    self.assertFalse(banned & set(name.split(".")),
                                     f"{path.name} imports {name}")


class PackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.niche = make_niche()

    def write(self, doc) -> Path:
        path = self.folder / "pack.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_a_passage_from_a_host_the_source_does_not_own_is_refused(self) -> None:
        doc = pack_document()
        doc["passages"][0]["url"] = "https://evil.example/wiki/Harbor"
        with self.assertRaises(PassageError):
            load_pack(self.write(doc), self.niche)

    def test_a_source_the_niche_does_not_have_is_refused(self) -> None:
        doc = pack_document()
        doc["passages"][0]["source_id"] = "reddit"
        with self.assertRaises(PassageError):
            load_pack(self.write(doc), self.niche)

    def test_duplicate_ids_and_missing_image_files_are_refused(self) -> None:
        doc = pack_document()
        doc["passages"][1]["id"] = "p1"
        with self.assertRaises(PassageError):
            load_pack(self.write(doc), self.niche)
        doc = pack_document("missing.png")
        with self.assertRaises(PassageError):
            load_pack(self.write(doc), self.niche)

    def test_host_allowlist(self) -> None:
        hosts = ("en.wikipedia.org",)
        self.assertTrue(host_allowed("https://en.wikipedia.org/x", hosts))
        self.assertFalse(host_allowed("http://en.wikipedia.org/x", hosts))
        self.assertFalse(host_allowed("https://en.wikipedia.org.evil.example/x", hosts))
        self.assertFalse(host_allowed("https://user:pw@en.wikipedia.org/x", hosts))
        self.assertFalse(host_allowed("https://evil.example/en.wikipedia.org", hosts))


if __name__ == "__main__":
    unittest.main()
