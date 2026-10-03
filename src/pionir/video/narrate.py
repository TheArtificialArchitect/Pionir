"""Narration: Moss's Kokoro voice, on the CPU, one sentence at a time.

Synthesising sentence by sentence is what makes the captions exact: each sentence's start and
end is its place in the audio, not an estimate and not a transcription pass. The voice runs
through an ONNX session built with ``CPUExecutionProvider`` only, so it cannot reach the GPU
and the stage takes no GPU lease: the card stays Moss's. Tests inject a fake synthesiser.

Audio is written with the standard library (``wave``, 16-bit mono); numpy is needed only by
the real Kokoro path, where the model hands back float samples.
"""
from __future__ import annotations

import os
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .script import Script

LEAD_IN = 0.5
LINE_GAP = 0.25
SCENE_GAP = 0.7
_SENTENCE = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[])")


class VoiceUnavailable(RuntimeError):
    """The voice cannot run here (model files or packages missing); says what is missing."""


class Synthesizer(Protocol):
    sample_rate: int

    def synthesize(self, text: str, voice: str) -> bytes:
        """Mono 16-bit little-endian PCM at ``sample_rate``."""
        ...


@dataclass(frozen=True, slots=True)
class Cue:
    scene: int
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Narration:
    wav: Path
    cues: tuple[Cue, ...]
    scene_spans: tuple[tuple[float, float], ...]
    duration: float
    sample_rate: int


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE.split(text.strip()) if p.strip()]
    return parts or [text.strip()]


class KokoroSynth:
    """Kokoro v1.0 on onnxruntime's CPU provider, the same files Galatea speaks from."""

    def __init__(self, model: Path | None = None, voices: Path | None = None) -> None:
        folder = Path(os.environ.get("LOCALAPPDATA", "")) / "Galatea" / "voice"
        self._model = Path(model or os.environ.get("PIONIR_KOKORO_MODEL")
                           or folder / "kokoro-v1.0.onnx")
        self._voices = Path(voices or os.environ.get("PIONIR_KOKORO_VOICES")
                            or folder / "voices-v1.0.bin")
        self.sample_rate = 24000
        self._tts = None

    def _engine(self):
        if self._tts is None:
            for path in (self._model, self._voices):
                if not path.is_file():
                    raise VoiceUnavailable(f"Kokoro file missing: {path}")
            try:
                import onnxruntime
                from kokoro_onnx import Kokoro
            except ImportError as error:
                raise VoiceUnavailable(f"kokoro-onnx / onnxruntime not installed: {error}") from error
            session = onnxruntime.InferenceSession(
                str(self._model), providers=["CPUExecutionProvider"])
            self._tts = Kokoro.from_session(session, str(self._voices))
        return self._tts

    def synthesize(self, text: str, voice: str) -> bytes:
        import numpy
        samples, rate = self._engine().create(text, voice=voice, speed=1.0, lang="en-us")
        self.sample_rate = int(rate)
        clipped = numpy.clip(numpy.asarray(samples, dtype="float32"), -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2").tobytes()


def _silence(seconds: float, rate: int) -> bytes:
    return b"\x00\x00" * int(round(seconds * rate))


def narrate(script: Script, synth: Synthesizer, voice: str, out_wav: Path) -> Narration:
    """Speak the script into ``out_wav``; the cues say when each sentence is on screen."""
    chunks: list[bytes] = []
    cues: list[Cue] = []
    spans: list[tuple[float, float]] = []
    rate = None
    cursor = 0.0

    def add(pcm: bytes) -> float:
        chunks.append(pcm)
        return len(pcm) / 2 / rate

    for si, scene in enumerate(script.scenes):
        scene_start = cursor
        for li, line in enumerate(scene.lines):
            for sentence in sentences(line.text):
                pcm = synth.synthesize(sentence, voice)
                rate = rate or synth.sample_rate
                if synth.sample_rate != rate:
                    raise VoiceUnavailable("the voice changed sample rate mid-script")
                if not pcm:
                    raise VoiceUnavailable(f"the voice returned no audio for: {sentence[:60]!r}")
                if cursor == 0.0 and not cues:
                    cursor += add(_silence(LEAD_IN, rate))
                start = cursor
                cursor += add(pcm)
                cues.append(Cue(si, start, cursor, sentence))
                cursor += add(_silence(LINE_GAP, rate))
        cursor += add(_silence(SCENE_GAP - LINE_GAP, rate))
        spans.append((scene_start, cursor))
    if rate is None:
        raise VoiceUnavailable("the script has nothing to say")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(chunks))
    return Narration(out_wav, tuple(cues), tuple(spans), cursor, rate)
