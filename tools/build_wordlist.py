"""Build src/pionir/crew/words_en.txt.gz from a Hunspell en_US dictionary.

    npm pack dictionary-en          (data only; `pack` runs no install scripts)
    tar xzf dictionary-en-*.tgz
    python tools/build_wordlist.py package/index.dic package/index.aff

The content check uses the result to tell an ordinary English word written with a capital
("Verification", "Implement") from a proper noun ("Kimberly", "Seattle", "Target"): Hunspell
enters proper nouns capitalised and common words in lower case. Every surface form is
expanded here, once, so the check needs no affix engine at run time.

Source: dictionary-en 4.0.0 (wooorm/dictionaries, SCOWL-derived), licence (MIT AND BSD),
copied beside the output as words_en.LICENSE.
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "pionir" / "crew" / "words_en.txt.gz"


def parse_aff(path: Path) -> dict:
    rules: dict = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        if len(parts) == 4 and parts[0] in ("PFX", "SFX") and parts[2] in ("Y", "N"):
            kind, flag, cross, count = parts[0], parts[1], parts[2] == "Y", int(parts[3])
            entries = []
            for line in lines[i + 1:i + 1 + count]:
                _k, _f, strip, add, cond = line.split()[:5]
                strip = "" if strip == "0" else strip
                add = "" if add == "0" else add.split("/")[0]
                pat = re.compile((cond + "$") if kind == "SFX" else ("^" + cond))
                entries.append((strip, add, pat))
            rules[flag] = (kind, cross, entries)
            i += 1 + count
        else:
            i += 1
    return rules


def expand(word: str, flags: str, rules: dict) -> set:
    forms = {word}
    suffixed = set()
    for f in flags:
        kind, _cross, entries = rules.get(f, (None, False, ()))
        if kind != "SFX":
            continue
        for strip, add, pat in entries:
            if pat.search(word) and (not strip or word.endswith(strip)):
                suffixed.add((word[:len(word) - len(strip)] if strip else word) + add)
    forms |= suffixed
    for f in flags:
        kind, cross, entries = rules.get(f, (None, False, ()))
        if kind != "PFX":
            continue
        for strip, add, pat in entries:
            bases = [word] + (sorted(suffixed) if cross else [])
            for b in bases:
                if pat.search(b) and (not strip or b.startswith(strip)):
                    forms.add(add + b[len(strip):])
    return forms


def main(dic: str, aff: str) -> int:
    rules = parse_aff(Path(aff))
    common, proper = set(), set()
    for line in Path(dic).read_text(encoding="utf-8").splitlines()[1:]:
        word, _, flags = line.strip().partition("/")
        if not word or not any(c.isalpha() for c in word):
            continue
        for form in expand(word, flags, rules):
            (proper if form[:1].isupper() else common).add(form)
    body = "# common\n" + "\n".join(sorted(common)) + "\n# proper\n" + "\n".join(sorted(proper)) + "\n"
    OUT.write_bytes(gzip.compress(body.encode("utf-8"), mtime=0))
    print(f"{len(common)} common forms, {len(proper)} proper forms -> {OUT} "
          f"({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:3]))
