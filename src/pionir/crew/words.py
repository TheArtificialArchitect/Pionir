"""The English dictionary the crew's names rules share: an ordinary word is not a name.

A names rule without a dictionary cannot tell "Verification" from "Verizon": it sees two
capitalised words. Both of the crew's names rules had to learn that the hard way - the
content check blocked six of six blog drafts on words like "Implementing", and the
leaders' grounding rejected report after report for "Division", "Report", "Data" and
"Stale" in a Title-Case headline. So both read ONE word list, here, and cannot drift.

The list is every surface form of Hunspell en_US (tools/build_wordlist.py; licence in
words_en.LICENSE), split by how the dictionary enters a word: common words in lower case,
proper nouns (names, places, companies) capitalised. Some words are both ("Target",
"Mark", "May", "Smith"); callers decide how far to trust those.
"""
from __future__ import annotations

import gzip
import re
from functools import lru_cache
from pathlib import Path

WORDS_PATH = Path(__file__).with_name("words_en.txt.gz")


@lru_cache(maxsize=1)
def dictionary() -> tuple:
    """(common, proper): common words in lower case, proper nouns capitalised."""
    text = gzip.decompress(WORDS_PATH.read_bytes()).decode("utf-8")
    common, _, proper = text.partition("# proper\n")
    return (frozenset(common.split("\n")[1:]) - {""}, frozenset(proper.split("\n")) - {""})


def forms(tok: str) -> list:
    """A token and the forms it may be read as: without a possessive, and an acronym's
    plural ("APIs") without its "s"."""
    out = [tok]
    for suffix in ("'s", "’s", "'", "’"):
        if tok.endswith(suffix) and len(tok) > len(suffix):
            out.append(tok[:-len(suffix)])
    if re.fullmatch(r"[A-Z0-9]{2,}s", tok):          # APIs, PDFs, URLs, IDs
        out.append(tok[:-1])
    return out


def ordinary_word(tok: str) -> bool:
    """A capitalised token that is just an English word ("Verification", "Implementing",
    "Real-time"), not a name.

    Fail closed: a form the dictionary enters as a proper noun ("Mark", "Target",
    "Seattle", "Kimberly") is never ordinary, a word it does not know ("Milica",
    "Verizon") is never ordinary, and an all-capitals token (an acronym) is not a word."""
    common, proper = dictionary()
    for form in forms(tok):
        form = form.replace("’", "'")
        if len(form) > 1 and form.isupper():
            return False
        parts = form.split("-")
        # A Title-Case compound ("Opt-In", "Real-Time") capitalises its later parts by
        # habit, so only the first part's capital can mark a name: "In" is a proper noun
        # (indium) in Hunspell, "in" is not.
        if all(p and (i > 0 or p not in proper) and (p.lower() in common or p in common) and
               not (len(p) > 1 and p.isupper()) for i, p in enumerate(parts)):
            return True
    return False


# What a model writes for an English word the en_US list does not hold: a negated
# participle ("Unconfigured", "Unacknowledged") or a British spelling ("Acknowledgement",
# "Cancelled", "Prioritised"). Deliberately narrow: "Unbounce" and "Autodesk" stay names.
_NEGATED = re.compile(r"(?i)^(?:un|non-?)([a-z]{3,}(?:ed|ing|able))$")
_BRITISH = ((r"isation$", "ization"), (r"ise$", "ize"), (r"ised$", "ized"),
            (r"ising$", "izing"), (r"our$", "or"), (r"ours$", "ors"), (r"ement$", "ment"),
            (r"ements$", "ments"), (r"lled$", "led"), (r"lling$", "ling"), (r"tre$", "ter"),
            (r"ogue$", "og"))


def _spellings(word: str) -> list:
    out = [word]
    m = _NEGATED.match(word)
    if m:
        out.append(m.group(1))
    for pat, rep in _BRITISH:
        if re.search(pat, word):
            out.append(re.sub(pat, rep, word))
    return out


# "Acme Corp", "Blue Sky Ltd": a company is a company even when every word of its name is an
# ordinary word, so a capitalised phrase before a company suffix is a name on its own.
COMPANY = re.compile(r"\b((?:[A-Z][\w&'’-]*\s+){0,3}[A-Z][\w&'’-]*)\s+"
                     r"(?:Inc|Corp|Corporation|LLC|Ltd|Limited|GmbH|Co|Company|Group|Labs)\b")

ACRONYM, ORDINARY, DUAL, NAME = "acronym", "ordinary", "dual", "name"


def word_kind(tok: str) -> str:
    """How a capitalised token reads, for a rule that must tell a name from a word:

    - ``ACRONYM``: all capitals ("API", "PDFs", "HTTP").
    - ``ORDINARY``: an English word the dictionary holds only as a common word
      ("Division", "Report", "Stale"), including a Title-Case compound of them and the
      narrow spelling variants above.
    - ``DUAL``: a common word the dictionary ALSO enters as a proper noun ("Target",
      "Mark", "May", "No") - the caller decides from context.
    - ``NAME``: a proper noun only ("Kimberly", "Verizon") or a word the dictionary does
      not know at all ("Etsy", "Gumroad"). Fail closed: unknown is a name."""
    common, proper = dictionary()
    kinds = []
    for form in forms(tok):
        form = form.replace("’", "'")
        if len(form) > 1 and form.isupper():
            return ACRONYM
        parts = form.split("-")
        if not all(parts):
            continue
        if not all(any(v.lower() in common or v in common for v in _spellings(p))
                   for p in parts):
            continue
        # only the first part's capital can mark a name (see ordinary_word)
        kinds.append(DUAL if parts[0] in proper else ORDINARY)
    if ORDINARY in kinds:
        return ORDINARY
    return DUAL if DUAL in kinds else NAME
