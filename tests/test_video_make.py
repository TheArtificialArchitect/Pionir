"""Narration, render, package, and the approval gate, on one real tiny render.

The voice is a fake tone (Kokoro is proven separately by the live sample); ffmpeg and Pillow
are real, so the video, captions and thumbnail are the genuine files.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from video_support import (
    HAVE_FFMPEG,
    HAVE_PIL,
    FakeSynth,
    FakeWriter,
    good_script,
    load,
    make_niche,
    write_pack,
)

from pionir.adapters import video as video_adapter
from pionir.adapters.video import UPLOAD, VideoAdapter, VideoSettings
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp
from pionir.video.narrate import KokoroSynth, VoiceUnavailable, narrate, sentences
from pionir.video.package import (
    PackageError,
    load_package,
    queue_dir,
    sha256_file,
    verify_package,
)
from pionir.video.pipeline import make_video, upload_payload
from pionir.video.render import caption_chunks, srt_time
from pionir.video.script import ScriptRejected, check_script

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
CAN_RENDER = HAVE_FFMPEG and HAVE_PIL
SIZE = (480, 270)


def settings(root: Path, **kw: Any) -> PionirSettings:
    return PionirSettings(
        state_root=root, atani_command=("pionir-test-no-such-binary",),
        daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
        bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
        embed_model=None, evict_to_fit=False, content_url=None, **kw)


class NarrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.niche, self.pack = load(self.folder, with_image=False)
        self.script = check_script(good_script(with_image=False), self.niche, self.pack)

    def test_each_sentence_gets_a_cue_inside_the_audio_and_the_wav_is_that_long(self) -> None:
        synth = FakeSynth()
        narration = narrate(self.script, synth, "af_nicole", self.folder / "n.wav")
        self.assertEqual(synth.spoken, [c.text for c in narration.cues])
        for earlier, later in zip(narration.cues, narration.cues[1:]):
            self.assertLessEqual(earlier.end, later.start)
        self.assertLessEqual(narration.cues[-1].end, narration.duration)
        with wave.open(str(narration.wav)) as wav:
            self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), narration.duration,
                                   places=2)
        self.assertEqual(len(narration.scene_spans), len(self.script.scenes))

    def test_a_voice_that_returns_nothing_is_an_error_not_a_silent_video(self) -> None:
        class Mute(FakeSynth):
            def synthesize(self, text, voice):
                return b""
        with self.assertRaises(VoiceUnavailable):
            narrate(self.script, Mute(), "af_nicole", self.folder / "n.wav")

    def test_sentences_split(self) -> None:
        self.assertEqual(sentences("One. Two! Three?"), ["One.", "Two!", "Three?"])

    def test_kokoro_is_asked_for_the_cpu_provider_only(self) -> None:
        model, voices = self.folder / "m.onnx", self.folder / "v.bin"
        model.write_bytes(b"x")
        voices.write_bytes(b"x")
        seen: dict[str, Any] = {}
        fake_ort = mock.MagicMock()

        def session(path, providers=None):
            seen["providers"] = providers
            return "session"
        fake_ort.InferenceSession = session
        fake_kokoro = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"onnxruntime": fake_ort,
                                             "kokoro_onnx": fake_kokoro}):
            KokoroSynth(model, voices)._engine()
        self.assertEqual(seen["providers"], ["CPUExecutionProvider"])

    def test_missing_voice_files_are_said_plainly(self) -> None:
        with self.assertRaises(VoiceUnavailable):
            KokoroSynth(self.folder / "no.onnx", self.folder / "no.bin")._engine()


class CaptionTests(unittest.TestCase):
    def test_srt_time_format(self) -> None:
        self.assertEqual(srt_time(3725.5), "01:02:05,500")

    @unittest.skipUnless(HAVE_PIL, "Pillow")
    def test_caption_chunks_never_overlap_and_cover_the_cues(self) -> None:
        from pionir.social.card import SEMIBOLD, _font
        from pionir.video.narrate import Cue
        cues = [Cue(0, 1.0, 4.0, "A fairly long sentence that has to be wrapped across lines "
                                 "of the caption band so that it needs two chunks at least."),
                Cue(0, 4.5, 6.0, "Short.")]
        chunks = caption_chunks(cues, _font(40, SEMIBOLD), 900)
        self.assertGreaterEqual(len(chunks), 3)
        for a, b in zip(chunks, chunks[1:]):
            self.assertLessEqual(a.end, b.start + 1e-6)
        self.assertAlmostEqual(chunks[0].start, 1.0, places=2)
        self.assertAlmostEqual(chunks[-1].end, 6.0, places=2)


@unittest.skipUnless(CAN_RENDER, "needs ffmpeg and Pillow")
class RenderedVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.work = cls.root / "work"
        cls.work.mkdir()
        cls.pack_path = write_pack(cls.work)
        cls.synth = FakeSynth()
        cls.made = make_video(make_niche(live=False), cls.pack_path, cls.root / "video",
                              writer=FakeWriter(good_script()), synth=cls.synth,
                              now=lambda: NOW, size=SIZE)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_the_render_is_a_real_video_of_the_right_shape(self) -> None:
        package = self.made.package
        manifest = package.manifest
        self.assertEqual(self.made.problems, ())
        self.assertTrue(package.video.is_file())
        self.assertGreater(package.video.stat().st_size, 5_000)
        self.assertEqual(manifest["size_bytes"], package.video.stat().st_size)
        from pionir.video.render import probe_duration
        probed = probe_duration(package.video)
        self.assertAlmostEqual(probed, manifest["duration_seconds"], delta=0.6)
        self.assertGreater(probed, 5)
        self.assertEqual((manifest["width"], manifest["height"]), SIZE)

    def test_captions_thumbnail_script_and_manifest_exist_and_the_wav_is_removed(self) -> None:
        folder = self.made.package.dir
        for name in ("captions.srt", "thumbnail.png", "script.json", "manifest.json", "video.mp4"):
            self.assertTrue((folder / name).is_file(), name)
        self.assertFalse((folder / "narration.wav").exists())
        self.assertFalse((folder / "frames").exists())
        srt = (folder / "captions.srt").read_text(encoding="utf-8")
        self.assertRegex(srt, r"(?m)^1\n00:00:\d\d,\d{3} --> 00:00:\d\d,\d{3}\n")
        self.assertIn("The harbor opened in 1851", srt.replace(chr(10), " "))

    def test_the_manifest_pins_the_video_and_carries_the_disclosure_and_credits(self) -> None:
        from pionir.video.disclosure import DISCLOSURE
        m = self.made.package.manifest
        self.assertEqual(m["video_sha256"], hashlib.sha256(
            self.made.package.video.read_bytes()).hexdigest())
        self.assertTrue(m["description"].rstrip().endswith(DISCLOSURE))
        self.assertIn("Library of Congress", m["description"])
        self.assertIsNone(m["uploaded"])
        self.assertEqual([s["id"] for s in m["sources"]], ["p1", "p2"])
        self.assertEqual(verify_package(load_package(self.root / "video", m["id"])), [])

    def test_every_line_of_the_final_script_still_names_its_sources(self) -> None:
        doc = json.loads((self.made.package.dir / "script.json").read_text(encoding="utf-8"))
        for scene in doc["scenes"]:
            for line in scene["lines"]:
                self.assertTrue(line["sources"] or line["connective"])

    def test_a_small_sample_is_drawn_at_full_size_then_scaled_so_words_are_not_split(self) -> None:
        srt = (self.made.package.dir / "captions.srt").read_text(encoding="utf-8")
        self.assertNotRegex(srt, r"harbo\nr")
        self.assertIn("The harbor opened in 1851", srt.replace(chr(10), " "))

    def test_a_size_that_is_not_sixteen_by_nine_is_refused(self) -> None:
        from pionir.video.render import RenderUnavailable, render_video
        work = self.root / "w-size"
        work.mkdir()
        niche, pack = load(work)
        script = check_script(good_script(), niche, pack)
        narration = narrate(script, FakeSynth(), "af_nicole", work / "n.wav")
        for bad in ((480, 480), (481, 271), (64, 36)):
            with self.assertRaises(RenderUnavailable):
                render_video(script, narration, niche, pack, work / "o", size=bad)

    def test_a_video_from_a_niche_that_is_not_live_is_built_but_never_offered(self) -> None:
        self.assertIsNone(self.made.parked)
        self.assertIn("not live", self.made.not_parked_because)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not CAN_RENDER:
            raise unittest.SkipTest("needs ffmpeg and Pillow")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        if CAN_RENDER:
            cls._tmp.cleanup()

    def build(self, name: str, **niche_kw: Any):
        work = self.root / f"w-{name}"
        work.mkdir()
        return make_video(make_niche(**niche_kw), write_pack(work), self.root / name,
                          writer=FakeWriter(good_script()), synth=FakeSynth(), now=lambda: NOW,
                          size=SIZE)

    def test_a_tampered_video_fails_verification(self) -> None:
        made = self.build("tamper", live=False)
        with made.package.video.open("ab") as handle:
            handle.write(b"x")
        self.assertTrue(any("changed" in p for p in verify_package(made.package)))

    def test_an_example_niche_package_never_verifies(self) -> None:
        made = self.build("ex", example=True, live=False)
        self.assertTrue(any("example" in p for p in made.problems), made.problems)
        self.assertIn("example", made.not_parked_because)

    def test_a_description_that_lost_the_disclosure_fails_verification(self) -> None:
        made = self.build("nodisc", live=False)
        path = made.package.dir / "manifest.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["description"] = doc["description"].replace("made with AI", "made")
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertTrue(verify_package(load_package(self.root / "nodisc", doc["id"])))

    def test_a_traversing_id_is_not_an_id(self) -> None:
        for bad in ("../x", "a/b", "", "A", "x" * 200):
            with self.assertRaises(PackageError):
                load_package(self.root, bad)

    def test_a_rejected_script_builds_nothing(self) -> None:
        bad = good_script()
        bad["scenes"][0]["lines"][0] = {"text": "The harbor opened in 1851."}
        work = self.root / "w-rej"
        work.mkdir()
        synth = FakeSynth()
        with self.assertRaises(ScriptRejected):
            make_video(make_niche(), write_pack(work), self.root / "rej",
                       writer=FakeWriter(bad), synth=synth, size=SIZE)
        self.assertEqual(synth.spoken, [])              # not one word was spoken
        self.assertFalse(queue_dir(self.root / "rej").exists())


@unittest.skipUnless(CAN_RENDER, "needs ffmpeg and Pillow")
class ApprovalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.video_dir = self.root / "video"
        self.adapter = VideoAdapter(VideoSettings(video_dir=self.video_dir))
        runtime = build_runtime(settings(self.root / "state", video_dir=self.video_dir))
        self.addCleanup(runtime.cortex.close)
        self.app = PionirApp(runtime)
        work = self.root / "work"
        work.mkdir()
        self.parked: list[dict[str, Any]] = []

        def submit(payload):
            out = self.app.run_task(UPLOAD, dict(payload), permissions=[UPLOAD])
            self.parked.append(out)
            return out

        self.made = make_video(make_niche(), write_pack(work), self.video_dir,
                               writer=FakeWriter(good_script()), synth=FakeSynth(),
                               now=lambda: NOW, size=SIZE, submit=submit)

    def payload(self) -> dict[str, Any]:
        return upload_payload(self.made.package)

    def test_the_capability_is_privileged_approved_every_time_and_never_routed(self) -> None:
        cap = self.adapter.manifest.capabilities[0]
        self.assertEqual(cap.name, "video.youtube_upload")
        self.assertIs(cap.risk, RiskLevel.PRIVILEGED)
        self.assertTrue(cap.requires_approval)
        self.assertFalse(cap.routable)

    def test_a_finished_video_from_a_live_niche_parks_and_nothing_else_happens(self) -> None:
        self.assertEqual(self.made.problems, ())
        self.assertEqual(self.parked[0]["status"], "pending_approval")
        pending = self.app.approvals.pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("The old harbor", pending[0]["summary"])
        self.assertIsNone(self.made.package.manifest["uploaded"])

    def test_holding_the_permission_never_skips_the_card(self) -> None:
        for _ in range(2):
            out = self.app.run_task(UPLOAD, self.payload(), permissions=[UPLOAD])
            self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(len(self.app.approvals.pending()), 3)

    def test_a_card_that_could_never_run_is_refused_before_it_asks_for_a_yes(self) -> None:
        for change in ({"sha256": "0" * 64}, {"title": "Another title"},
                       {"video_id": "no-such-video-12345678"}, {"video_id": "../../etc"}):
            out = self.app.run_task(UPLOAD, {**self.payload(), **change}, permissions=[UPLOAD])
            self.assertNotEqual(out.get("status"), "pending_approval", change)
            self.assertFalse(out["ok"], change)
        self.assertEqual(len(self.app.approvals.pending()), 1)      # only the real one

    def test_extra_or_missing_payload_keys_are_refused(self) -> None:
        for payload in ({**self.payload(), "path": "C:/Windows"},
                        {k: v for k, v in self.payload().items() if k != "sha256"}):
            with self.assertRaises(AdapterProtocolError):
                self.adapter.validate(Task(UPLOAD, payload))

    def test_a_video_edited_after_the_card_was_shown_cannot_be_approved_through(self) -> None:
        card = self.app.approvals.pending()[0]
        with self.made.package.video.open("ab") as handle:
            handle.write(b"tamper")
        approved = self.app.approve(card["id"] if "id" in card else card["approval_id"])
        self.app.jobs.wait(approved.get("task_id", ""), 30)
        row = self.app.approvals.get(card.get("id") or card["approval_id"])
        self.assertNotIn("uploaded", json.dumps(row.get("result", {})).replace(
            '"uploaded": false', ""))
        self.assertIsNone(load_package(self.video_dir, self.made.package.id)
                          .manifest["uploaded"])

    def test_approving_uploads_nothing_and_says_so_with_no_network_touched(self) -> None:
        card = self.app.approvals.pending()[0]
        card_id = card.get("id") or card["approval_id"]
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network used")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            approved = self.app.approve(card_id)
            self.assertTrue(self.app.jobs.wait(approved["task_id"], 30))
        result = self.app.approvals.get(card_id)["result"]
        text = json.dumps(result)
        self.assertIn('"uploaded": false', text)
        self.assertIn("not built", text)
        self.assertEqual(sha256_file(self.made.package.video),
                         self.made.package.manifest["video_sha256"])
        self.assertIsNone(load_package(self.video_dir, self.made.package.id).manifest["uploaded"])

    def test_the_adapter_has_no_network_or_credential_code_at_all(self) -> None:
        source = Path(video_adapter.__file__).read_text(encoding="utf-8")
        for needle in ("urllib", "http.client", "requests", "socket", "googleapiclient",
                       "oauth", "token"):
            code = re.sub(r'""".*?"""', "", source, flags=re.S)
            code = re.sub(r"#.*", "", code)
            code = re.sub(r'"[^"\n]*"', '""', code)
            self.assertNotIn(needle, code.lower(), needle)

    def test_status_is_local_and_reports_the_staged_video(self) -> None:
        status = self.adapter.status()
        self.assertEqual(status["staged"], 1)
        self.assertEqual(status["live"], [])
        self.assertIn("not built", status["uploader"])

    def test_status_says_unavailable_when_the_niche_file_is_bad(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        adapter = VideoAdapter(VideoSettings(video_dir=self.video_dir, niches_file=bad))
        with self.assertRaises(AdapterUnavailable):
            adapter.status()


@unittest.skipUnless(CAN_RENDER, "needs ffmpeg and Pillow")
class ThreeInFlightTests(unittest.TestCase):
    """Three channels, one video each a week: three packages and three cards, none sharing state."""

    def test_three_niches_build_in_one_folder_and_park_three_separate_cards(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        video_dir = root / "video"
        runtime = build_runtime(settings(root / "state", video_dir=video_dir))
        self.addCleanup(runtime.cortex.close)
        app = PionirApp(runtime)
        made = []
        for name in ("alpha", "beta", "gamma"):
            work = root / f"w-{name}"
            work.mkdir()
            # the same script on purpose: only the niche tells the three ids apart
            made.append(make_video(
                make_niche(id=f"niche-{name}", series=[f"Series {name}"]), write_pack(work),
                video_dir, writer=FakeWriter(good_script()), synth=FakeSynth(),
                now=lambda: NOW, size=SIZE,
                submit=lambda p: app.run_task(UPLOAD, dict(p), permissions=[UPLOAD])))
        ids = {m.package.id for m in made}
        self.assertEqual(len(ids), 3)
        self.assertEqual(len({m.package.dir for m in made}), 3)
        self.assertTrue(all(m.problems == () for m in made))
        self.assertEqual([m.parked["status"] for m in made], ["pending_approval"] * 3)
        pending = app.approvals.pending()
        self.assertEqual(len(pending), 3)
        self.assertEqual(len({c.get("id") or c.get("approval_id") for c in pending}), 3)
        # approving one leaves the other two waiting and untouched
        first = pending[0].get("id") or pending[0].get("approval_id")
        done = app.approve(first)
        self.assertTrue(app.jobs.wait(done["task_id"], 30))
        self.assertEqual(len(app.approvals.pending()), 2)
        self.assertEqual(VideoAdapter(VideoSettings(video_dir=video_dir)).status()["staged"], 3)


if __name__ == "__main__":
    unittest.main()
