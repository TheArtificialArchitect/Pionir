"""What a secret looks like - redacted before the Library answers.

The pattern set is ``secret_patterns.json`` beside this file: a byte-identical copy of
Pionir Desktop's ``shared/secret-patterns.json``, the ONE source of truth for both
scrubbers (this one, and the desktop's TypeScript one). The file carries its own test
vectors; both suites run them against their own implementation, and each compares the
two copies when both repos are on disk.

Order: values known to be secrets (``known``: this server's client tokens), then the
named shapes (vendor keys, private-key blocks, ``password = ...``), then bare
high-entropy tokens (a charset-mix + entropy rule that leaves words, paths, ids and
hashes alone).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

PATTERN_FILE = Path(__file__).with_name("secret_patterns.json")


@lru_cache(maxsize=1)
def pattern_doc() -> dict[str, Any]:
    return json.loads(PATTERN_FILE.read_text(encoding="utf-8"))


def _flags(spec: dict[str, Any]) -> int:
    return re.IGNORECASE if "i" in spec.get("flags", "") else 0


@lru_cache(maxsize=1)
def _compiled() -> tuple[list[tuple[dict[str, Any], re.Pattern[str]]], re.Pattern[str]]:
    doc = pattern_doc()
    # JS and Python read these the same way; \d/\w are avoided in the file (Python's are
    # Unicode-wide), so ASCII classes are spelled out
    pats = [(p, re.compile(p["re"], _flags(p))) for p in doc["patterns"]]
    return pats, re.compile(doc["entropy"]["re"])


def secretish(value: str) -> bool:
    """A value that looks like a secret rather than a word: a digit, an uppercase
    letter, or one of the listed symbols."""
    symbols = pattern_doc()["secretish_symbols"]
    return bool(re.search(r"[0-9A-Z]", value)) or any(c in symbols for c in value)


def _cls(c: str) -> str:
    if "a" <= c <= "z":
        return "l"
    if "A" <= c <= "Z":
        return "u"
    if "0" <= c <= "9":
        return "d"
    return "s"


def entropy_bits(s: str) -> float:
    n = len(s)
    return -sum(k / n * math.log2(k / n) for k in Counter(s).values()) if n else 0.0


def looks_like_token(s: str) -> bool:
    """A bare run of token characters that is a key - not a word, a path, an id, a hash."""
    e = pattern_doc()["entropy"]
    if re.fullmatch(r"[0-9a-fA-F]+", s):
        return False
    if any(re.fullmatch(r"[a-z-]{4,}", seg) for seg in s.split("/")):
        return False
    if len({_cls(c) for c in s}) < e["min_classes"]:
        return False
    changes = sum(1 for a, b in zip(s, s[1:]) if _cls(a) != _cls(b))
    if changes / (len(s) - 1) < e["min_class_changes"]:
        return False
    return entropy_bits(s) >= e["min_bits"]


def _template(t: str, m: re.Match[str]) -> str:
    return re.sub(r"\{(\d)\}", lambda g: m.group(int(g.group(1))) or "", t)


def scrub_patterns(text: str) -> str:
    """The shared patterns and the entropy rule (no known values)."""
    doc = pattern_doc()
    red = doc["replacement"]
    pats, entropy = _compiled()
    out = text
    for spec, rx in pats:
        def repl(m: re.Match[str], spec: dict[str, Any] = spec) -> str:
            if spec.get("value_must") == "secretish" and not secretish(m.group(spec.get("value_group", 0)) or ""):
                return m.group(0)
            return _template(spec["replace"], m) if "replace" in spec else red
        out = rx.sub(repl, out)
    return entropy.sub(lambda m: red if looks_like_token(m.group(0)) else m.group(0), out)


def scrub_text(text: str, known: Iterable[str] = ()) -> str:
    red = pattern_doc()["replacement"]
    for value in sorted({k.strip() for k in known if k and len(k.strip()) >= 6}, key=len, reverse=True):
        text = text.replace(value, red)
    return scrub_patterns(text)


def scrub_value(value: Any, known: Iterable[str] = ()) -> Any:
    """Deep: every string in a JSON-able value, keys too."""
    known = tuple(known)
    if isinstance(value, str):
        return scrub_text(value, known)
    if isinstance(value, list):
        return [scrub_value(v, known) for v in value]
    if isinstance(value, dict):
        return {scrub_text(k, known) if isinstance(k, str) else k:
                pattern_doc()["replacement"] if secret_key(k, v) else scrub_value(v, known)
                for k, v in value.items()}
    return value


@lru_cache(maxsize=1)
def _secret_keys() -> re.Pattern[str]:
    spec = pattern_doc()["secret_keys"]
    return re.compile(spec["re"], _flags(spec))


def secret_key(key: Any, value: Any) -> bool:
    """A string under a key named like a credential (structured data): redacted whole."""
    return (isinstance(key, str) and isinstance(value, str)
            and len(value) >= pattern_doc()["secret_keys"]["min_length"]
            and bool(_secret_keys().match(key)))
