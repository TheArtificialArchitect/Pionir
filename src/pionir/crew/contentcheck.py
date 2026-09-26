"""The fail-closed check every public post passes before the owner is even asked.

The owner's rule for AI-written public content: **the model drafts; this check runs on the
exact final text; the owner approves every post.** ``check(draft)`` returns the reasons a
draft may not go out, in words; an empty list is the only pass. It is run on the very
strings that would be published - the worker adds its links BEFORE calling it and submits
the checked dict unchanged - so nothing is added after the check has looked.

Fail closed, everywhere. A past incident on this machine: a DENYLIST anonymiser published
a friend's name, a city and a workplace, each a category nobody had thought to deny. So
names are an ALLOWLIST (``content_allowlist.json``): every capitalised word or phrase that
is not at the start of a sentence must be on it, and even a sentence-initial one must be
vouched for (a common opening word, or a word the post also uses in lower case) - "Marko
said" at the start of a sentence is exactly how a name would slip through otherwise.
Anything the check cannot vouch for blocks the post and is named in the reason.

The rules, one function each:

- ``_fields``     the publish contract: slug, title, description, body_md, tags, draft_id
- ``_markup``     Markdown only: no raw HTML, no HTML comments or entities, no
                  ``javascript:`` / ``vbscript:`` / ``data:`` URLs
- ``_links``      https only, only to the Dokaz hosts, and every page link carries the
                  post's UTM tags (C:\\src\\Scrooge\\docs\\TRAFFIC.md); ``api.dokaz.net/v1/*``
                  is an API endpoint, never counted as traffic, so it needs none. The source
                  is ``blog`` unless the caller names another from ``UTM_SOURCES`` (the
                  dev.to cross-post's is ``devto``); nothing else about the rule changes
- ``_personal``   no email addresses or @-handles, phone numbers, IP addresses, street
                  addresses
- ``_names``      the proper-noun allowlist
- ``_business``   no business numbers: no first-person claims with figures, no "our
                  revenue" / "we made", no counts of customers or users, no money amounts
- ``_internal``   none of the owner's internal systems is named in public text

``check_social(post)`` runs the same crew's rules on an Instagram post (no Markdown, no links
at all, a Title-Case headline on a card): Pionir's ``social.post.check_post`` first, then
``_no_links``, ``_personal``, the names rule, ``_business`` and ``_internal`` on the headline,
each point and the caption, and ``_hashtag_words`` on the hashtags.

Deterministic and model-free: nothing here imports a model or touches a network. It does run
Pionir's own publish validator (`adapters.content.check_draft`) first, so this check can never
pass a draft that Pionir would then refuse: three separately-written validators drifting apart is
exactly how a shared vocabulary fails (HEAD 3.8).
"""
from __future__ import annotations

import ipaddress
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pionir.adapters.content import check_draft, reserved_email
from pionir.social.card import CardTooLong, layout
from pionir.social.post import check_post

from . import words as _words

ALLOWLIST_PATH = Path(__file__).with_name("content_allowlist.json")

FIELDS = ("draft_id", "slug", "title", "description", "body_md", "tags")

# Where a post may link. Nothing else: not a partner, not a reference, not a citation.
LINK_HOSTS = frozenset({"api.dokaz.net", "dokazindustries.com", "www.dokazindustries.com",
                        "dokaz.gumroad.com"})

# The blog's UTM tags. TRAFFIC.md: utm_source is the platform, utm_medium the channel,
# utm_campaign the post's slug (``utm_campaign``). NOTE: TRAFFIC.md's source table has no
# "blog" yet; its own rule is to add a new platform to the table in the same change.
UTM_SOURCE = "blog"
UTM_MEDIUM = "referral"
UTM_CAMPAIGN_MAX = 40
# Every source a post's links may carry, each tied to where that copy of the post is
# published: the blog's own posts say ``blog``, the same post cross-posted to dev.to says
# ``devto`` (TRAFFIC.md lists it). A caller names one; any other blocks the post.
UTM_SOURCE_DEVTO = "devto"
UTM_SOURCES = frozenset({UTM_SOURCE, UTM_SOURCE_DEVTO})

# The owner's internal systems. Never in public text, in any case, in any field.
INTERNAL_NAMES = ("Pionir", "Moss", "Galatea", "Atani", "Scrooge", "Skopos", "Hearth", "Bryo",
                  "Daedalus", "Melete", "Nyx", "Voodoo", "Theo")

# The publish contract's field rules
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,78})[a-z0-9]")
_DRAFT_ID = re.compile(r"[a-z0-9-]{1,64}")
_TAG = re.compile(r"[a-z0-9-]{1,24}")
LIMITS = {"title": (10, 120), "description": (50, 300), "body_md": (300, 30000)}
MAX_TAGS = 8


def utm_campaign(draft_id: str) -> str:
    """The post's campaign, derived from its id (``YYYY-MM-DD-slug``) so the worker that
    tags the links and the check that verifies them can never disagree. TRAFFIC.md: at
    most 40 characters of ``a-z 0-9 . _ -``."""
    return str(draft_id or "")[:UTM_CAMPAIGN_MAX].strip("-")


def utm_query(draft_id: str, source: str = UTM_SOURCE) -> str:
    return (f"utm_source={source}&utm_medium={UTM_MEDIUM}"
            f"&utm_campaign={utm_campaign(draft_id)}")


# ---- the allowlist -----------------------------------------------------------------

OPENERS_KEY = "sentence_openers"


@lru_cache(maxsize=4)
def _load(path: Path = ALLOWLIST_PATH) -> tuple:
    """(names, openers) as lower-case strings. Refuses a file that lists an internal
    system anywhere: that would be the allowlist vouching for exactly what must never go
    out (and "moss" and "hearth" are also ordinary words someone might add as openers)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    names: set = set()
    openers: set = set()
    for key, value in doc.items():
        if key.startswith("_"):
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip()
                                                  for v in value):
            raise ValueError(f"content allowlist: {key!r} must be a list of words")
        (openers if key == OPENERS_KEY else names).update(v.strip().lower() for v in value)
    internal = {n.lower() for n in INTERNAL_NAMES}
    bad = sorted(n for n in names | openers if set(n.split()) & internal)
    if bad:
        raise ValueError(f"content allowlist lists internal systems: {', '.join(bad)}")
    return frozenset(names), frozenset(openers)


def load_allowlist(path: Path = ALLOWLIST_PATH) -> frozenset:
    """Every name a post may write with a capital letter, anywhere."""
    return _load(path)[0]


def load_openers(path: Path = ALLOWLIST_PATH) -> frozenset:
    """Common words that may OPEN a sentence (or a heading, or a list item) with a capital,
    and nowhere else. Not names: "People" and "Verify" open sentences; "Marko" must not."""
    return _load(path)[1]


# ---- small helpers -----------------------------------------------------------------

def _texts(draft: dict) -> list:
    """(field, text) for every string that would be published."""
    out = []
    for key in ("title", "description", "body_md", "slug", "draft_id"):
        v = draft.get(key)
        if isinstance(v, str):
            out.append((key, v))
    tags = draft.get("tags")
    if isinstance(tags, list):
        out.append(("tags", " ".join(t for t in tags if isinstance(t, str))))
    return out


def _prose(draft: dict) -> list:
    """The published text with its URLs blanked: links are checked by ``_links``, and a
    word inside a URL must not vouch for a name."""
    return [(k, _URL.sub(" ", draft[k])) for k in ("title", "description", "body_md")
            if isinstance(draft.get(k), str)]


def _snip(s: str, n: int = 60) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


# ---- 1. the publish contract ---------------------------------------------------------

def _fields(draft) -> list:
    if not isinstance(draft, dict):
        return [f"the draft is {type(draft).__name__}, not an object"]
    reasons = []
    extra = sorted(set(draft) - set(FIELDS))
    if extra:
        reasons.append(f"unknown field(s) {', '.join(extra)}; a post has exactly "
                       f"{', '.join(FIELDS)}")
    for key in FIELDS:
        if key not in draft:
            reasons.append(f"{key} is missing")
    slug = draft.get("slug")
    if "slug" in draft and (not isinstance(slug, str) or not _SLUG.fullmatch(slug)):
        reasons.append(f"slug {_snip(slug)!r} must be 3-80 of a-z 0-9 - with no leading or "
                       "trailing -")
    did = draft.get("draft_id")
    if "draft_id" in draft and (not isinstance(did, str) or not _DRAFT_ID.fullmatch(did)):
        reasons.append(f"draft_id {_snip(did)!r} must be 1-64 of a-z 0-9 -")
    for key, (lo, hi) in LIMITS.items():
        v = draft.get(key)
        if key not in draft:
            continue
        if not isinstance(v, str):
            reasons.append(f"{key} is {type(v).__name__}, not text")
        elif not lo <= len(v) <= hi:
            reasons.append(f"{key} is {len(v)} characters; it must be {lo}-{hi}")
        elif key != "body_md" and ("\n" in v or "\r" in v):
            reasons.append(f"{key} must be one line")
    tags = draft.get("tags")
    if "tags" in draft:
        if not isinstance(tags, list):
            reasons.append(f"tags is {type(tags).__name__}, not a list")
        else:
            if len(tags) > MAX_TAGS:
                reasons.append(f"{len(tags)} tags; at most {MAX_TAGS}")
            for t in tags:
                if not isinstance(t, str) or not _TAG.fullmatch(t):
                    reasons.append(f"tag {_snip(t)!r} must be 1-24 of a-z 0-9 -")
    return reasons


# ---- 2. Markdown only ----------------------------------------------------------------

_HTML_TAG = re.compile(r"<\s*[A-Za-z!/?]")
_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
_BAD_SCHEME = re.compile(r"(?i)\b(?:java|vb)\s*script\s*:|\bdata:(?!\s|$)")
# characters that hide or reorder text: a name split by a zero-width space passes a
# naive reader and not a person
_INVISIBLE = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b-\u200f"
                        "\u202a-\u202e\u2060-\u2064\ufeff]")


def _markup(texts: list) -> list:
    reasons = []
    for field, text in texts:
        m = _HTML_TAG.search(text)
        if m:
            reasons.append(f"{field} has raw HTML or an HTML comment "
                           f"({_snip(text[m.start():m.start() + 30], 30)!r}); Markdown only")
        m = _ENTITY.search(text)
        if m:
            reasons.append(f"{field} has an HTML entity ({m.group(0)!r}); Markdown only")
        m = _BAD_SCHEME.search(text)
        if m:
            reasons.append(f"{field} has a {m.group(0).split(':')[0].strip().lower()}: URL")
        if _INVISIBLE.search(text):
            reasons.append(f"{field} has invisible or control characters")
    return reasons


# Markdown the site's renderer does not draw: it is published as literal text ("| a | b |",
# "&gt; quote", "---", "# Big"), so a post using it looks broken to a reader. Measured against
# the real renderer in the blog end-to-end run. Supported: ## and ### headings, paragraphs,
# lists, **bold**, *italic*, `code`, ``` fences and links.
_UNRENDERED = (
    (re.compile(r"^\s{0,3}#(?!#)\s"), "a # heading (use ## or ###)"),
    (re.compile(r"^\s{0,3}#{4,}\s"), "a #### heading (use ## or ###)"),
    (re.compile(r"^\s{0,3}>"), "a > blockquote"),
    (re.compile(r"^\s{0,3}\|"), "a | table"),
    (re.compile(r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$"), "a --- rule"),
    (re.compile(r"^\s{0,3}~~~"), "a ~~~ fence (use ```)"),
)


def _renderable(draft: dict) -> list:
    body = draft.get("body_md")
    if not isinstance(body, str):
        return []
    reasons, fenced = [], False
    for line in body.splitlines():
        if re.match(r"^\s{0,3}```", line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for pattern, what in _UNRENDERED:
            if pattern.match(line):
                reasons.append(f"body_md uses {what}, which the site shows as literal text")
    return reasons


# ---- 3. links --------------------------------------------------------------------------

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>()\[\]{}\"'`]+")
_MD_TARGET = re.compile(r"\]\(\s*<?([^)\s>]*)")
_MD_REF = re.compile(r"(?m)^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
_WWW = re.compile(r"(?i)(?<![\w/.@-])www\.[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_BARE_DOMAIN = re.compile(
    r"(?i)(?<![\w/@.-])((?:[a-z0-9-]+\.)+(?:com|net|org|io|co|dev|app|ai|me|info|biz|us|uk|"
    r"de|rs|eu|xyz|site|online|shop|store|tech|so|gg|ly|to|tv))(?![\w-])")


def links_in(text: str) -> list:
    """Every link target in a piece of Markdown: full URLs, inline and reference targets."""
    found = [m.group(0).rstrip(".,;:!?") for m in _URL.finditer(text)]
    found += [m.group(1) for m in _MD_TARGET.finditer(text)]
    found += [m.group(1) for m in _MD_REF.finditer(text)]
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _link_reason(url: str, campaign: str | None, source: str = UTM_SOURCE) -> str | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return f"link {_snip(url)!r} cannot be read"
    if parts.scheme.lower() != "https":
        return f"link {_snip(url)!r} is not an https link to a Dokaz site"
    if host not in LINK_HOSTS or port is not None or "@" in parts.netloc:
        return (f"link {_snip(url)!r} goes to {host or 'no host'}; posts link only to "
                f"{', '.join(sorted(LINK_HOSTS))}")
    if host == "api.dokaz.net" and parts.path.startswith("/v1/"):
        return None          # an API endpoint, not a page: TRAFFIC.md never counts /v1/*
    q = parse_qs(parts.query, keep_blank_values=True)
    want = {"utm_source": source, "utm_medium": UTM_MEDIUM}
    if campaign:
        want["utm_campaign"] = campaign
    for key, value in want.items():
        if q.get(key) != [value]:
            return (f"link {_snip(url)!r} does not carry {key}={value} (this post's UTM tags, "
                    "TRAFFIC.md)")
    if not campaign:
        return f"link {_snip(url)!r} has no campaign: the draft_id is not usable"
    return None


def _links(draft: dict, texts: list, source: str = UTM_SOURCE) -> list:
    did = draft.get("draft_id")
    campaign = utm_campaign(did) if isinstance(did, str) and _DRAFT_ID.fullmatch(did) else None
    reasons = []
    for field, text in texts:
        for url in links_in(text):
            why = _link_reason(url, campaign, source)
            if why:
                reasons.append(f"{field}: {why}")
        for m in _WWW.finditer(text):
            reasons.append(f"{field}: {m.group(0)!r} is a bare www address; links are full "
                           "https URLs with UTM tags")
        stripped = _URL.sub(" ", text)
        for m in _BARE_DOMAIN.finditer(stripped):
            if m.group(1).lower() not in LINK_HOSTS:
                reasons.append(f"{field}: names the website {m.group(1)!r}")
    return reasons


# ---- 4. personal data --------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_][A-Za-z0-9_.]*")
_OBFUSCATED_AT = re.compile(r"(?i)[\[(]\s*at\s*[\])]")
_PHONE = re.compile(r"(?<![\w+.])\+?\(?\d[\d\s().\-]{5,22}\d(?![\w])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)")
# a run of hex digits and colons with at least two colons; ipaddress decides (so a clock
# time like 12:30:45 is not one)
_IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(?![\w:])")


def _is_ipv6(s: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv6Address)
    except ValueError:
        return False
_STREET_SUFFIX = (r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|"
                  r"Way|Place|Pl|Square|Sq|Terrace|Parkway|Pkwy|Highway|Hwy|Ulica|Bulevar|Trg)")
_STREET = re.compile(rf"\b\d{{1,5}}[A-Za-z]?,?\s+(?:[A-Z][\w.'-]*\s+){{1,4}}{_STREET_SUFFIX}\b")
_STREET_AFTER = re.compile(rf"\b(?:[A-Z][\w.'-]*\s+){{1,4}}{_STREET_SUFFIX}\.?,?\s+\d{{1,5}}\b")
_PO_BOX = re.compile(r"(?i)\bp\.?\s*o\.?\s*box\b|\b(?:suite|apt|apartment)\s*#?\s*\d+")


def _is_phone(s: str) -> bool:
    digits = sum(c.isdigit() for c in s)
    if not 7 <= digits <= 15:
        return False
    core = s.strip("()+ ")
    if _ISO_DATE.fullmatch(core) or re.fullmatch(r"\d+\.\d+", core) or _IPV4.fullmatch(core):
        return False
    # a plain number of fewer than ten digits is a number, not a phone
    return not (core.isdigit() and not s.startswith("+") and digits < 10)


def _personal(texts: list) -> list:
    reasons = []
    for field, text in texts:
        for m in _EMAIL.finditer(text):
            if not reserved_email(m.group(0)):
                reasons.append(f"{field} has an email address ({m.group(0)!r})")
        for m in _HANDLE.finditer(_EMAIL.sub(" ", text)):
            reasons.append(f"{field} has an @-handle ({m.group(0)!r})")
        if _OBFUSCATED_AT.search(text):
            reasons.append(f"{field} has a spelled-out email address")
        for m in _PHONE.finditer(text):
            if _is_phone(m.group(0)):
                reasons.append(f"{field} has a phone number ({m.group(0).strip()!r})")
        for m in _IPV4.finditer(text):
            reasons.append(f"{field} has an IP address ({m.group(0)!r})")
        for m in _IPV6.finditer(text):
            if _is_ipv6(m.group(0)):
                reasons.append(f"{field} has an IP address ({m.group(0)!r})")
        for rx in (_STREET, _STREET_AFTER, _PO_BOX):
            for m in rx.finditer(text):
                reasons.append(f"{field} has a street address ({_snip(m.group(0), 40)!r})")
    return reasons


# ---- 5. names: the allowlist -----------------------------------------------------------

_TOKEN = re.compile(r"\w+(?:['\u2019-]\w+)*")
_BOUNDARY = re.compile(r"[.!?:\u2026][\"'\u201d\u2019)\]*_]*\s+")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _segments(body: str) -> list:
    """(line, is_code) pieces of a Markdown text: fenced blocks and inline code are code."""
    out, fenced = [], False
    for line in body.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            out.append((line, True))
            continue
        if fenced:
            out.append((line, True))
            continue
        parts = line.split("`")
        if len(parts) > 1:
            # keep the whole line for sentence position; mark code spans by blanking them
            prose = "".join(p if i % 2 == 0 else " " * (len(p) + 2)
                            for i, p in enumerate(parts))
            code = " ".join(p for i, p in enumerate(parts) if i % 2 == 1)
            out.append((prose[:len(line)], False))
            out.append((code, True))
        else:
            out.append((line, False))
    return out


# The dictionary and what counts as an ordinary word live in words.py, shared with the
# leaders' grounding, so the crew's two names rules read one word list and cannot drift.
# Here an ordinary word is one the dictionary holds ONLY as a common word: a form it also
# enters as a proper noun ("Mark", "Target") is never ordinary in a post. The input carries
# no private data - the drafting prompt holds a topic, nothing about anyone - so what this
# rule guards against is an invented or real public name, and the owner still reads every
# post before it goes live.
WORDS_PATH = _words.WORDS_PATH
_dictionary = _words.dictionary
_ordinary_word = _words.ordinary_word
_forms = _words.forms


def _capitalised(tok: str, code: bool) -> bool:
    if tok[0].isupper():
        return True
    # "iPhone", "eBay": a brand with its capital inside. In code that is just camelCase.
    return not code and tok[0].isalpha() and any(c.isupper() for c in tok[1:])


def _allowed(tokens: list, allow: frozenset) -> bool:
    head, last = " ".join(tokens[:-1]), tokens[-1]
    for form in _forms(last):
        phrase = f"{head} {form}" if head else form
        if phrase.lower() in allow:
            return True
    return False


def _initial(line: str, start: int) -> bool:
    prefix = line[:start]
    ends = list(_BOUNDARY.finditer(prefix))
    seg = prefix[ends[-1].end():] if ends else prefix
    seg = seg.split("|")[-1]
    return re.search(r"\w", seg) is None


def unknown_names(texts: list, allow: frozenset | None = None,
                  openers: frozenset | None = None, *,
                  headings: frozenset = frozenset({"title"})) -> list:
    """Capitalised words and phrases the allowlist does not vouch for, in order.

    Mid-sentence, only the allowlist vouches. A word that opens a sentence (or a heading,
    a list item, a table cell) is vouched for by the allowlist, by the list of common
    sentence openers, or by the post also using the same word in lower case - never by
    its position alone, or "Marko said..." would pass.

    Headings and the title are the exception measured in the first real run: the model
    writes them in Title Case ("A Practical Guide"), where capitals carry no signal. There
    EVERY word may be vouched for by the post using it in lower case elsewhere - which an
    invented name ("Acme Corp") never is. Known gap, unchanged: a name that is also an
    ordinary word the post uses ("Mark"). ``headings`` names the fields written in Title
    Case (the blog's title; a social card's headline)."""
    allow = load_allowlist() if allow is None else allow
    openers = load_openers() if openers is None else openers
    lower_words = {t.lower() for _f, text in texts for t in _TOKEN.findall(text)
                   if t == t.lower() and any(c.isalpha() for c in t)}
    found: list = []
    for field, text in texts:
        for line, code in _segments(text):
            heading = not code and (field in headings or _HEADING.match(line) is not None)
            toks = [(m.group(0), m.start()) for m in _TOKEN.finditer(line)]
            i = 0
            while i < len(toks):
                if not _capitalised(toks[i][0], code):
                    i += 1
                    continue
                j = i + 1          # a phrase: capitalised tokens joined by single spaces
                while (j < len(toks) and _capitalised(toks[j][0], code)
                       and line[toks[j - 1][1] + len(toks[j - 1][0]):toks[j][1]] == " "):
                    j += 1
                found += _uncovered([t for t, _s in toks[i:j]],
                                    not code and _initial(line, toks[i][1]), allow,
                                    openers | lower_words, heading=heading)
                i = j
    return found


def _uncovered(phrase: list, initial: bool, allow: frozenset, common: frozenset, *,
               heading: bool = False) -> list:
    """Cover a phrase with the longest allowed sub-phrases; what is left over is unknown.
    ``common`` (openers and the post's own lower-case words) vouches only for the first
    word, and only when it opens a sentence - or, in a heading, for any word. A sentence-
    opening adverb ("Traditionally,") is vouched for by its suffix."""
    out, run, k = [], [], 0
    while k < len(phrase):
        step = 0
        for end in range(len(phrase), k, -1):
            if _allowed(phrase[k:end], allow):
                step = end - k
                break
        if not step and ((k == 0 and initial) or heading) \
                and any(f.lower() in common for f in _forms(phrase[k])):
            step = 1
        if not step and k == 0 and initial and _ADVERB.fullmatch(phrase[0]):
            step = 1
        if not step and _ordinary_word(phrase[k]):
            step = 1
        if step:
            if run:
                out.append(" ".join(run))
                run = []
            k += step
        else:
            run.append(phrase[k])
            k += 1
    if run:
        out.append(" ".join(run))
    return out


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
# Sentence-opening adverbs, by suffix. Chosen so no common given name matches (Kimberly,
# Emily, Holly, Beverly, Shelly all fail it); a name still needs the allowlist.
_ADVERB = re.compile(r"[A-Z][a-z]{3,}(?:ally|ously|ively|ately|ently|antly|arly|ically)")


# "Acme Corp", "Blue Sky Ltd": a company is a company even when every word of its name is an
# ordinary word, so a capitalised phrase before a company suffix blocks on its own.
_COMPANY = _words.COMPANY


def _names(draft: dict) -> list:
    return _names_in(_prose(draft))


def _names_in(prose: list, headings: frozenset = frozenset({"title"})) -> list:
    """The names rule on (field, text) pairs whose URLs are already blanked."""
    reasons, seen = [], set()
    for _field, text in prose:
        for m in _COMPANY.finditer(text):
            if m.group(0).lower() not in seen:
                seen.add(m.group(0).lower())
                reasons.append(f"names the company {m.group(0)!r} (a person, place or company "
                               "blocks the post)")
    for name in unknown_names(prose, headings=headings):
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        reasons.append(f"names {name!r}, which is not on the allowlist of names a post may "
                       "use (a person, place or company blocks the post)")
    return reasons


# ---- 6. business numbers -------------------------------------------------------------------

_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_FIRST_PERSON = re.compile(r"(?i)\b(?:we|we've|we're|we'd|we'll|our|ours|us|i|i've|i'm|"
                           r"my|mine)\b")
_NUMBER_WORDS = (r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|twenty|"
                 r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|hundreds|thousand|"
                 r"thousands|million|millions|billion")
_FIGURE = re.compile(rf"(?i)\d|%|\b(?:{_NUMBER_WORDS})\b")
_BUSINESS_NOUNS = (r"customers?|clients?|users?|subscribers?|sign-?ups?|sales|orders?|"
                   r"downloads?|installs?|revenue|profits?|income|earnings|mrr|arr|visitors?|"
                   r"traffic|members?|buyers?|deals?|growth|businesses|companies|teams|"
                   r"developers")
_BUSINESS_NOUN = re.compile(rf"(?i)\b(?:{_BUSINESS_NOUNS})\b")
_OUR_BUSINESS = re.compile(r"(?i)\bour\s+(?:\w+\s+)?(?:revenue|profits?|income|earnings|"
                           r"sales|margins?|mrr|arr|turnover|customers|clients|users|"
                           r"subscribers|buyers|growth|traffic)\b")
_WE_EARNED = re.compile(r"(?i)\b(?:we|i)(?:'ve|\s+have|\s+had)?\s+(?:already\s+|just\s+)?"
                        r"(?:made|earned|sold|grossed|raised|netted|billed)\b")
_COUNT_OF = re.compile(rf"(?i)(?:\d[\d,.]*\s*(?:k|m|\+)?|\b(?:{_NUMBER_WORDS}))\s+"
                       r"(?:happy\s+|paying\s+|active\s+)?"
                       r"(?:customers|clients|users|subscribers|buyers|sign-?ups|downloads|"
                       r"installs|businesses|companies|teams|developers)\b")
_MONEY = re.compile(r"(?i)[$\u20ac\u00a3\u00a5]\s?\d|\b\d[\d,.]*\s?(?:k\s)?(?:usd|eur|gbp|"
                    r"dollars?|euros?|pounds?|bucks)\b")


def _business(texts: list, who: str = "the blog") -> list:
    reasons = []
    for field, text in texts:
        if field in ("slug", "draft_id"):
            continue
        for sentence in _SENTENCES.split(text):
            s = sentence.strip()
            if not s:
                continue
            why = None
            if _OUR_BUSINESS.search(s) or _WE_EARNED.search(s):
                why = "a first-person business claim"
            elif _FIRST_PERSON.search(s) and _FIGURE.search(s) and _BUSINESS_NOUN.search(s):
                why = "a first-person business claim with a figure"
            elif _COUNT_OF.search(s):
                why = "a count of customers or users"
            elif _MONEY.search(s):
                why = f"a money amount ({who} has no source for any price or sum)"
            if why:
                reasons.append(f"{field} states {why}: {_snip(s)!r}; {who} must not state "
                               "business numbers")
    return reasons


# ---- 7. internal systems -----------------------------------------------------------------------

_INTERNAL = re.compile(r"(?i)(?<![a-z0-9])(" + "|".join(INTERNAL_NAMES) + r")(?![a-z0-9])")


def _internal(texts: list) -> list:
    reasons, seen = [], set()
    for field, text in texts:
        for m in _INTERNAL.finditer(text):
            key = (field, m.group(1).lower())
            if key not in seen:
                seen.add(key)
                reasons.append(f"{field} names the internal system {m.group(1)!r}")
    return reasons


# ---- the check -----------------------------------------------------------------------------------

def check(draft, *, utm_source: str = UTM_SOURCE) -> list:
    """Every reason this draft may not be published, in words. Empty means it passed.

    ``utm_source`` is where this copy of the post is published (one of ``UTM_SOURCES``):
    every Dokaz page link must carry exactly it. The blog's is the default; the dev.to
    cross-post names ``devto``. Every other rule is the same for both.

    Fail closed: a draft that is not even an object, a source nobody listed, or a rule that
    cannot run, blocks."""
    reasons = _fields(draft)
    if utm_source not in UTM_SOURCES:
        reasons.append(f"utm_source {_snip(utm_source)!r} is not one of "
                       f"{', '.join(sorted(UTM_SOURCES))}; the links cannot be checked")
    if not isinstance(draft, dict):
        return reasons
    texts = _texts(draft)
    try:
        check_draft(draft)
    except Exception as exc:  # noqa: BLE001 - Pionir's refusal, or its check failing, both block
        reasons.append(f"Pionir's publish check refuses it: {exc}")
    try:
        reasons += _markup(texts)
        reasons += _renderable(draft)
        reasons += _links(draft, texts, utm_source)
        reasons += _personal(texts)
        reasons += _names(draft)
        reasons += _business(texts)
        reasons += _internal(texts)
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is a block, never a pass
        reasons.append(f"the content check could not run: {type(exc).__name__}: {exc}")
    out, seen = [], set()
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ---- social posts ------------------------------------------------------------------------------
#
# An Instagram post is not a blog post: no Markdown, no links at all (a caption cannot link;
# the bio does), a Title-Case headline on a card, short points, a caption and lower-case
# hashtags. Pionir's own post check (pionir.social.post.check_post) runs first - the same
# function the adapter runs before parking and before publishing, so the crew can never pass
# a post Pionir would refuse - and then the crew's rules above run on each piece of text.

SOCIAL_WHO = "a social post"
# Hashtag words the dictionary lacks. Lower case only, and only ever read inside a hashtag:
# none of them may be written with a capital anywhere ("Dev" is also a first name).
HASHTAG_WORDS = frozenset({"dev", "devs", "saas", "nocode", "lowcode", "fintech", "martech",
                           "indie", "js"})


def _social_texts(post: dict) -> list:
    """(field, text) for every piece of text that would appear on the card or the caption."""
    out = []
    if isinstance(post.get("headline"), str):
        out.append(("headline", post["headline"]))
    points = post.get("points")
    if isinstance(points, list):
        out += [(f"points[{i}]", p) for i, p in enumerate(points) if isinstance(p, str)]
    if isinstance(post.get("caption"), str):
        out.append(("caption", post["caption"]))
    return out


def _no_links(texts: list) -> list:
    """A social post links to nothing, not even a Dokaz page: the bio link does that."""
    reasons = []
    for field, text in texts:
        found = links_in(text) + [m.group(0) for m in _WWW.finditer(text)]
        found += [m.group(1) for m in _BARE_DOMAIN.finditer(_URL.sub(" ", text))]
        for target in dict.fromkeys(found):
            reasons.append(f"{field} has a link, URL or domain ({_snip(target, 40)!r}); a "
                           "social post links to nothing, the bio link does that")
    return reasons


def _hashtag_words(tags: list) -> list:
    """A hashtag is lower case, so the names rule cannot see it: "#janedoe" or "#seattle"
    would pass it. Instead every hashtag must break into ordinary dictionary words or names
    on the allowlist ("emaildeliverability" = email + deliverability); one that does not is
    named and blocks. Fail closed: a word the dictionary does not know blocks the tag."""
    common, _proper = _dictionary()
    allowed = {re.sub(r"[^a-z0-9]", "", n) for n in load_allowlist()} | HASHTAG_WORDS
    internal = {n.lower() for n in INTERNAL_NAMES}

    def word(piece: str) -> bool:
        if piece in internal:
            return False
        return piece in allowed or (len(piece) > 1 and piece.isalpha() and piece in common)

    def splits(s: str) -> bool:
        ok = [True] + [False] * len(s)
        for end in range(1, len(s) + 1):
            ok[end] = any(ok[start] and word(s[start:end])
                          for start in range(max(0, end - 30), end))
        return ok[-1]

    reasons = []
    for tag in tags:
        pieces = [p for p in tag.split("_") if p]
        if not pieces or not all(splits(p) for p in pieces):
            reasons.append(f"hashtag {_snip(tag, 40)!r} is not made of ordinary words or "
                           "allowlisted names (a person, place or company blocks the post)")
    return reasons


def _card_fits(post: dict) -> list:
    """The crew's own statement of the fit rule, so a card that cannot hold the text is
    named as such even when Pionir's check stopped at an earlier field."""
    headline, points = post.get("headline"), post.get("points")
    if not isinstance(headline, str) or not isinstance(points, list) \
            or not all(isinstance(p, str) for p in points):
        return []
    try:
        layout(headline.strip(), [p.strip() for p in points])
    except CardTooLong as exc:
        return [f"card: {exc}"]
    return []


def check_social(post) -> list:
    """Every reason this social post may not go out, in words. Empty means it passed.

    ``post`` is the exact payload that would be submitted: draft_id, headline, points,
    caption, hashtags and card_sha. Pionir's post check runs first; then the crew's rules -
    the card fits, no links, personal data, names (with the dictionary; the headline is
    Title Case), business figures, internal system names - on the headline, each point and
    the caption, and the names and internal-name rules on the hashtags. The crew also
    requires ``card_sha``: the approval must pin the exact image the owner is shown.

    Fail closed: a post that is not an object, or a rule that cannot run, blocks."""
    if not isinstance(post, dict):
        return [f"the post is {type(post).__name__}, not an object"]
    reasons: list = []
    try:
        check_post(post)
    except ValueError as exc:
        reasons.append(f"Pionir's post check refuses it: {exc}")
    except Exception as exc:  # noqa: BLE001 - its check failing is a block, never a pass
        reasons.append(f"Pionir's post check could not run: {type(exc).__name__}: {exc}")
    if "card_sha" not in post:
        reasons.append("card_sha is missing: the post must pin the exact card the owner "
                       "approves")
    texts = _social_texts(post)
    tags = post.get("hashtags")
    tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
    tag_text = [("hashtags", " ".join(tags))] if tags else []
    did = post.get("draft_id")
    id_text = [("draft_id", did)] if isinstance(did, str) else []
    rules = (
        lambda: _card_fits(post),
        lambda: _markup(texts),
        lambda: _no_links(texts),
        lambda: _personal(texts),
        lambda: _names_in([(k, _URL.sub(" ", t)) for k, t in texts],
                          headings=frozenset({"headline"})),
        lambda: _hashtag_words(tags),
        lambda: _business(texts, who=SOCIAL_WHO),
        lambda: _internal(texts + tag_text + id_text),
    )
    for rule in rules:
        try:
            reasons += rule()
        except Exception as exc:  # noqa: BLE001 - a rule that cannot run is a block
            reasons.append(f"the content check could not run: {type(exc).__name__}: {exc}")
    return list(dict.fromkeys(reasons))
