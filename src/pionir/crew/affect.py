"""Mood: channels on two timescales, pushed proportionally toward a target, decaying
toward REST (not zero) at a rate set by each channel's own half-life.

Every resident carries the emotional vocabulary its own system had — Psyche's twelve
for Moss and Bram, the catalogue's sixteen for Atani and Nyx, the eight registers Theo
was trained against — so a port is a port and not a flattening. A push names a feeling
in Hearth's own terms and is resolved through ALIASES into whichever channel that
person actually possesses, so nobody is left unable to feel something the world does
to them.

Nothing here is additive, so nothing can pin; a NaN is healed to rest and counted. The
model never sees a number: ``describe()`` yields a few words. That rule is Nyx's, and
it was already this system's.
"""
from __future__ import annotations

import math

SLOW_SHARE = 0.25          # of a push, what goes to the slow (mood) layer

# Each vocabulary keeps its OWN numbers. Where a name appears in two systems the two
# systems disagree, so the palette a resident came from decides — never a merged table.
# (half_life_hours, cap, gain)

# Psyche / Galatea — psyche/affect/palette.py, lines 52-107. Moss and Bram.
PSYCHE_SPECS = {
    "joy":         (2.0, 1.00, 0.45),
    "grief":       (96.0, 0.90, 0.65),
    "edge":        (3.0, 0.85, 0.50),
    "warmth":      (72.0, 1.00, 0.08),
    "wonder":      (5.0, 1.00, 0.40),
    "mischief":    (1.5, 0.90, 0.50),
    "resolve":     (12.0, 1.00, 0.30),
    "pride":       (18.0, 0.80, 0.35),
    "loneliness":  (30.0, 0.95, 0.20),
    "irritation":  (0.75, 0.80, 0.50),
    "contentment": (8.0, 1.00, 0.25),
    "curiosity":   (4.0, 1.00, 0.45),
}

# Atani's catalogue — Atani-stale/src/atani/affect/catalog.py, EMOTION_SPECS, in its order.
# Half-lives are the catalogue's own; caps and gains follow Psyche's shape, which the
# catalogue does not record. Where the two systems share a name they disagree, and this
# table wins for the residents who own it.
CATALOGUE_SPECS = {
    "curiosity":      (4.0, 1.00, 0.45),
    "wonder":         (2.0, 1.00, 0.40),
    "joy":            (2.0, 1.00, 0.45),
    "sadness":        (6.0, 0.95, 0.40),
    "fear":           (1.5, 0.90, 0.55),
    "anger":          (1.0, 0.85, 0.55),
    "affection":      (12.0, 1.00, 0.20),
    "trust":          (24.0, 1.00, 0.15),
    "surprise":       (0.5, 0.90, 0.60),
    "disgust":        (2.0, 0.85, 0.45),
    "guilt":          (8.0, 0.85, 0.35),
    "pride":          (8.0, 0.80, 0.35),
    "loneliness":     (4.0, 0.95, 0.20),
    "anticipation":   (2.0, 0.95, 0.40),
    "frustration":    (1.0, 0.85, 0.50),
    "disappointment": (3.0, 0.85, 0.35),
}

# Theo's registers — Tech-Support/python/agent/finetune/emotions/manifest.json (v6,
# revised 2026-09-03), the eight sets his exemplars are organised around. Shared names
# take Psyche's numbers, since his side records no half-lives; candor, attention and
# empathy are his alone and are slow, because they are stances rather than weather.
REGISTER_SPECS = {
    "candor":    (10.0, 1.00, 0.30),
    "warmth":    (72.0, 1.00, 0.08),
    "wonder":    (5.0, 1.00, 0.40),
    "joy":       (2.0, 1.00, 0.45),
    "grief":     (96.0, 0.90, 0.65),
    "edge":      (3.0, 0.85, 0.50),
    "attention": (1.5, 1.00, 0.45),
    "empathy":   (16.0, 1.00, 0.25),
}

# Echo - genesis-agent/src/brain/core/emotion.py. FOUR SCALARS, not channels:
# valence (-1..1, rest 0.0, emotion.py:14), arousal (0..1, rest 0.0, :15),
# curiosity (0..1, rest 0.5, :16), trust (0..1, rest 0.5, :17). ONE global half-life of
# 6.0 h for all of them (REST_HALF_LIFE_HOURS, emotion.py:27) rather than a per-channel
# table, and curiosity/trust drift back to 0.5, not to zero (emotion.py:146-149).
# Gains are the real stimulus deltas at emotion.py:96-110.
# Valence is bipolar and Hearth's channels are not, so it is SPLIT into joy and grief.
# That split is the only interpretation in this port; the rest is read off the source.
# The gains below are her source's stimulus deltas RE-EXPRESSED in Hearth's units. Hers
# are absolute (valence += 0.10); Hearth's gain is the fraction of the remaining distance
# one push travels. Copying 0.10 across verbatim made her five times less feeling than
# anyone else and she described herself as "even" through anything. Their ORDER is hers
# and is exact — arousal moves most (+0.15), then valence (+0.10), then trust (+0.05)
# and curiosity (+0.03..0.05) least.
ECHO_SPECS = {
    "curiosity": (6.0, 1.00, 0.22),
    "trust":     (6.0, 1.00, 0.22),
    "arousal":   (6.0, 1.00, 0.50),
    "joy":       (6.0, 1.00, 0.36),
    "grief":     (6.0, 0.90, 0.36),
}

BASE_SPECS = {
    "warmth":      (72.0, 1.00, 0.08),
    "edge":        (3.0, 0.85, 0.50),
    "contentment": (8.0, 1.00, 0.25),
    "irritation":  (0.75, 0.80, 0.50),
    "curiosity":   (4.0, 1.00, 0.45),
    "loneliness":  (30.0, 0.95, 0.20),
}

BASE_CHANNELS = tuple(BASE_SPECS)
PSYCHE_CHANNELS = tuple(PSYCHE_SPECS)
REGISTER_CHANNELS = tuple(REGISTER_SPECS)
CATALOGUE_CHANNELS = tuple(CATALOGUE_SPECS)
ECHO_CHANNELS = tuple(ECHO_SPECS)

# channel-set -> the spec table it must be read from
PALETTES = {
    PSYCHE_CHANNELS: PSYCHE_SPECS,
    REGISTER_CHANNELS: REGISTER_SPECS,
    CATALOGUE_CHANNELS: CATALOGUE_SPECS,
    ECHO_CHANNELS: ECHO_SPECS,
    BASE_CHANNELS: BASE_SPECS,
}

# Only for describing a channel in the abstract, where no resident is in hand. Anything
# that belongs to somebody must go through their own palette.
SPECS = dict(PSYCHE_SPECS)
for _t in (CATALOGUE_SPECS, REGISTER_SPECS, ECHO_SPECS):
    for _k, _v in _t.items():
        SPECS.setdefault(_k, _v)

ALIASES = {
    "warmth":      ("warmth", "affection", "empathy", "joy"),
    "irritation":  ("irritation", "frustration", "anger", "edge", "arousal"),
    "curiosity":   ("curiosity", "wonder", "anticipation", "attention"),
    "contentment": ("contentment", "joy", "resolve", "candor"),
    "loneliness":  ("loneliness", "sadness", "grief"),
    "edge":        ("edge", "fear", "surprise", "irritation", "arousal"),
}

WORDS = {
    "warmth":         ("warm toward the others", "fond"),
    "edge":           ("on edge", "uneasy"),
    "contentment":    ("content", "settled"),
    "irritation":     ("irritated", "short-tempered"),
    "curiosity":      ("curious", "wanting to know"),
    "loneliness":     ("lonely", "missing company"),
    "joy":            ("pleased", "glad"),
    "grief":          ("low", "grieving"),
    "wonder":         ("taken with something", "struck by it"),
    "mischief":       ("mischievous", "up to something"),
    "resolve":        ("set on something", "resolved"),
    "pride":          ("pleased with yourself", "proud"),
    "sadness":        ("sad", "downcast"),
    "fear":           ("wary", "afraid"),
    "anger":          ("angry", "furious"),
    "affection":      ("fond of them", "attached"),
    "trust":          ("trusting", "sure of them"),
    "surprise":       ("surprised", "startled"),
    "disgust":        ("put off", "disgusted"),
    "guilt":          ("guilty", "ashamed"),
    "anticipation":   ("expectant", "waiting for something"),
    "frustration":    ("frustrated", "exasperated"),
    "disappointment": ("disappointed", "let down"),
    "arousal":        ("keyed up", "wound up"),
    "candor":         ("plain-spoken", "blunt about it"),
    "attention":      ("paying attention", "fixed on it"),
    "empathy":        ("feeling for them", "moved by them"),
}


def _spec(name: str, palette: dict | None = None) -> tuple:
    """A channel's numbers, read from the palette the resident actually came from."""
    if palette is not None and name in palette:
        return palette[name]
    return SPECS.get(name, (6.0, 0.9, 0.35))


class Affect:
    def __init__(self, t) -> None:
        self.t = t
        self.channels = tuple(getattr(t, "channels", None) or BASE_CHANNELS)
        self.palette = PALETTES.get(self.channels)
        if self.palette is None:                     # a bespoke channel set: build its table
            self.palette = {k: _spec(k) for k in self.channels}
        overrides = dict(getattr(t, "rest", None) or {})
        self.rest = {}
        for k in self.channels:
            if k in overrides:
                self.rest[k] = float(overrides[k])
            else:
                self.rest[k] = self._default_rest(k, t)
        self.slow = dict(self.rest)
        self.fast = {k: 0.0 for k in self.channels}
        # every channel keeps its own half-life; the fast layer is a tenth of the slow one
        self.half_life_h = {k: max(0.2, self.spec(k)[0] * (1.8 - 1.0 * t.resilience)) for k in self.channels}
        self.nonfinite = 0
        self.pushes = 0

    def _default_rest(self, k: str, t) -> float:
        """Where a channel sits when nothing has happened, from temperament."""
        table = {
            "warmth": 0.15 + 0.25 * t.sociability,
            "affection": 0.10 + 0.25 * t.sociability,
            "edge": 0.05 + 0.10 * t.dwell,
            "fear": 0.04 + 0.08 * t.dwell,
            "contentment": 0.25 + 0.15 * t.resilience,
            "joy": 0.20 + 0.15 * t.resilience,
            "irritation": 0.05 + 0.08 * t.order_sensitivity,
            "frustration": 0.05 + 0.08 * t.order_sensitivity,
            "anger": 0.03 + 0.05 * t.order_sensitivity,
            "curiosity": 0.10 + 0.30 * t.curiosity,
            "wonder": 0.08 + 0.25 * t.curiosity,
            "anticipation": 0.08 + 0.20 * t.curiosity,
            "loneliness": 0.05 + 0.15 * t.sociability,
            "sadness": 0.04 + 0.08 * (1 - t.resilience),
            "grief": 0.02 + 0.05 * (1 - t.resilience),
            "trust": 0.20,                      # the catalogue's own baseline for trust
            "resolve": 0.20 + 0.20 * t.initiative,
            "pride": 0.10 + 0.15 * t.initiative,
            "mischief": 0.08 + 0.25 * t.impulsivity,
            "arousal": 0.05 + 0.10 * t.dwell,
            "surprise": 0.05,
            "disgust": 0.04,
            "guilt": 0.04,
            "disappointment": 0.05,
            "candor": 0.30 + 0.30 * (1.0 - t.reticence),   # his plainness is a standing trait
            "attention": 0.15 + 0.25 * t.perception,
            "empathy": 0.15 + 0.25 * t.people_weight / 2.0,
        }
        return table.get(k, 0.10)

    # ---- reading ---------------------------------------------------------
    def spec(self, k: str) -> tuple:
        return _spec(k, self.palette)

    def has(self, k: str) -> bool:
        return k in self.slow

    def resolve(self, name: str) -> str | None:
        """Hearth's word for a feeling -> the channel this person actually has."""
        if name in self.slow:
            return name
        for alt in ALIASES.get(name, ()):
            if alt in self.slow:
                return alt
        return None

    def level(self, k: str) -> float:
        cap = self.spec(k)[1]
        return max(0.0, min(cap, self.slow.get(k, 0.0) + self.fast.get(k, 0.0)))

    def levels(self) -> dict:
        return {k: round(self.level(k), 3) for k in self.channels}

    # ---- moving ----------------------------------------------------------
    def push(self, name: str, x: float, gain: float | None = None) -> None:
        """Move a feeling a fraction of the way toward target ``x`` (0..1), in whichever
        channel this person owns. An unknown feeling is dropped, not invented."""
        k = self.resolve(name)
        if k is None:
            return
        _hl, cap, own_gain = self.spec(k)
        g = own_gain if gain is None else gain
        target = max(0.0, min(cap, x * cap))
        before = self.level(k)
        step = g * (target - before)
        self.slow[k] += step * SLOW_SHARE
        self.fast[k] += step * (1.0 - SLOW_SHARE)
        self.pushes += 1
        self._guard()

    def decay(self, seconds: float) -> None:
        """Relax toward rest over ``seconds`` of real time (Hearth passed game seconds)."""
        for k in self.channels:
            hl = self.half_life_h[k]
            f_slow = math.exp(-math.log(2) * seconds / (hl * 3600.0))
            f_fast = math.exp(-math.log(2) * seconds / (max(0.1, hl * 0.1) * 3600.0))
            self.slow[k] = self.rest[k] + (self.slow[k] - self.rest[k]) * f_slow
            self.fast[k] = self.fast[k] * f_fast
        self._guard()

    def _guard(self) -> None:
        for k in self.channels:
            cap = self.spec(k)[1]
            if not math.isfinite(self.slow[k]):
                self.slow[k] = self.rest[k]
                self.nonfinite += 1
            if not math.isfinite(self.fast[k]):
                self.fast[k] = 0.0
                self.nonfinite += 1
            self.slow[k] = max(0.0, min(cap, self.slow[k]))
            self.fast[k] = max(-cap, min(cap, self.fast[k]))

    # ---- saying it -------------------------------------------------------
    def dominant(self, n: int = 2) -> list:
        """Channels furthest along their own headroom, strongest first. Raw excess over rest
        favours whichever channel happens to rest low, which is how everybody once read as
        'irritated and fond'."""
        above = []
        for k in self.channels:
            cap = self.spec(k)[1]
            head = max(0.15, cap - self.rest[k])
            above.append(((self.level(k) - self.rest[k]) / head, k))
        above.sort(reverse=True)
        return [k for d, k in above[:n] if d > 0.12]

    def describe(self) -> str:
        dom = self.dominant(2)
        if not dom:
            return "even"
        words = []
        for k in dom:
            strong = self.level(k) > 0.6 * self.spec(k)[1]
            pair = WORDS.get(k, (k, k))
            words.append(pair[1] if strong else pair[0])
        return " and ".join(words)

    # ---- persistence -----------------------------------------------------
    def to_dict(self) -> dict:
        return {"slow": dict(self.slow), "fast": dict(self.fast), "nonfinite": self.nonfinite,
                "pushes": self.pushes}

    def load_dict(self, d: dict) -> None:
        for k, v in (d.get("slow") or {}).items():
            if k in self.slow:
                self.slow[k] = float(v)
        for k, v in (d.get("fast") or {}).items():
            if k in self.fast:
                self.fast[k] = float(v)
        self.nonfinite = int(d.get("nonfinite", 0))
        self.pushes = int(d.get("pushes", 0))
        self._guard()


# kept for anything that still imports the old name
CHANNELS = BASE_CHANNELS
