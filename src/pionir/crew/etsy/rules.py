"""What an Etsy listing (or a print-on-demand design) may say - checked fail-closed, twice.

The crew runs this before it submits anything, and the Pionir adapters run it again before
anything is parked and again before anything is sent (``pionir.adapters.etsy`` /
``pionir.adapters.printify`` import it), so a listing that passes the crew can never be
refused later for a different reason, and one that does not pass is never shown to Etsy.

Etsy's rules this encodes (2025-2026):

- AI use must be disclosed in the listing description (Etsy removed thousands of listings
  for missing it in Q1 2026): every description must carry ``AI_DISCLOSURE`` word for word;
  a listing without it is refused here and in the adapter.
- The Creativity Standards (10 Jun 2025): nothing that resells someone else's work, brand or
  character. So no brand, trademark, franchise or celebrity term (``BRANDS``), and - the
  blog's Hunspell lesson (``pionir.crew.words``) - no proper noun at all: a capitalised word
  the en_US dictionary does not hold as a common word is refused. Fail closed: a word the
  dictionary does not know is a name.
- Title: at most 140 characters; ``% : & +`` at most once each; no ``$ ^ ` ``.
- Tags: exactly 13, each 1-20 characters of a-z, 0-9 and single spaces, no repeats.

Customer-facing claims fail closed: no promise words (``CLAIMS``: guaranteed, official,
certified, instant results ...), no links, domains, @, #, e-mail, phone or IP address, and
only plain printable characters.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from pionir.adapters.content import _contact_problem
from pionir.crew.words import ACRONYM, DUAL, NAME, ORDINARY, _spellings, dictionary, word_kind

# Word for word in every description. It is a statement about how the item was made that
# is true by construction: the text and layout come from the crew's model and code, and
# nothing reaches Etsy without the owner's approval of the exact listing.
AI_DISCLOSURE = ("AI disclosure: this item's text and layout were created with the help of "
                 "AI tools, then reviewed and approved by the shop owner before it was "
                 "listed.")
DIGITAL_NOTE = ("This is a digital download: no physical item will be shipped. The files "
                "are available to download once payment is confirmed.")
POD_NOTE = ("This item is printed on demand by a print partner once you order, so each "
            "one is made for you.")
REQUIRED = {"digital": (AI_DISCLOSURE, DIGITAL_NOTE), "pod": (AI_DISCLOSURE, POD_NOTE)}

TITLE_MAX = 140
TITLE_MIN = 15
TAGS_EXACTLY = 13
TAG_MAX = 20
DESCRIPTION = (200, 6000)
LABEL = (2, 28)
PHRASE = (3, 40)
PHRASE_WORDS = 6

# Brand, trademark, franchise, platform and celebrity terms (lower case, matched as whole
# words, case-insensitively). Not a complete list of the world's marks - the proper-noun
# rule below catches any capitalised name; this catches the ones people write in lower case
# ("excel", "goodnotes", "canva") and the ones that are also ordinary words ("notion").
BRAND_WORDS = frozenset(["adidas", "airbnb", "alexa", "amazon", "android", "audible", "barbie", "batman", "birkenstock", "blippi", "bluey", "canva", "carhartt", "chanel", "chatgpt", "cinderella", "cocomelon", "coke", "cricut", "crocs", "disney", "disneyland", "doordash", "dropbox", "dunkin", "encanto", "etsy", "excel", "facebook", "fendi", "ferrari", "fifa", "fitbit", "fortnite", "garfield", "gmail", "goodnotes", "google", "gucci", "hilton", "hogwarts", "hulu", "ikea", "instagram", "ipad", "iphone", "itunes", "kindle", "kitchenaid", "kleenex", "lakers", "lego", "lululemon", "mandalorian", "marriott", "marvel", "mcdonalds", "microsoft", "minecraft", "mlb", "mls", "moana", "nascar", "nba", "netflix", "nfl", "nhl", "nike", "nintendo", "notability", "onenote", "openai", "outlook", "owala", "pandora", "patagonia", "paypal", "peloton", "pepsi", "pikachu", "pinterest", "pixar", "playstation", "pokemon", "prada", "quickbooks", "reddit", "roblox", "rolex", "sanrio", "shopify", "simpsons", "smurfs", "snapchat", "snoopy", "sony", "spiderman", "spotify", "squishmallow", "squishmallows", "starbucks", "superman", "swiftie", "tiktok", "tesla", "tupperware", "uber", "venmo", "versace", "walmart", "whatsapp", "wimbledon", "xbox", "yankees", "yeti", "youtube", "avengers"])
BRAND_PHRASES = ("google sheets", "google docs", "harry potter", "hello kitty",
                 "louis vuitton", "star wars", "super bowl", "taylor swift", "apple watch",
                 "numbers app", "coca cola", "disney world", "wonder woman", "sesame street",
                 "looney tunes", "scooby doo", "paw patrol", "peppa pig", "ms rachel",
                 "stanley cup", "stanley tumbler", "lilo and stitch", "mickey mouse",
                 "minnie mouse", "notion template", "zoom background", "baby yoda",
                 "spider man", "olympic games", "remarkable tablet", "dallas cowboys")
# A word that is a brand only when written as a name ("Target", "Apple", "Gap", "Shell").
_CAPITALISED_BRANDS = frozenset({"Target", "Apple", "Gap", "Shell", "Dove", "Subway",
                                 "Notion", "Zoom", "Numbers", "Frozen", "Supreme", "Stanley",
                                 "Stitch", "Hermes", "Gemini", "Peanuts", "Cars", "Olaf"})

# Promises a listing may not make: they are claims a buyer could hold the shop to.
CLAIMS = ("guarantee", "guaranteed", "official", "officially", "licensed", "certified",
          "endorsed", "accredited", "best seller", "bestseller", "best-selling", "#1",
          "number one", "instant results", "money back", "refund", "lifetime access",
          "free shipping", "100%", "proven", "cure", "cures", "medical", "therapy",
          "doctor", "lose weight", "weight loss", "get rich", "passive income",
          "risk free", "risk-free", "scientifically", "clinically", "award winning",
          "award-winning", "as seen on", "dupe", "inspired by", "fan art", "parody")

# Words the dictionary does not hold that a listing may still use: file formats and paper.
FORMAT_WORDS = frozenset({"xlsx", "pdf", "pdfs", "png", "diy", "a4", "a5", "us"})
ACRONYMS = frozenset({"PDF", "PDFs", "XLSX", "AI", "DIY", "PNG", "US", "A4", "A5"})
# Hunspell enters these as proper nouns; in a planner they are not anyone's name.
CALENDAR = frozenset({"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                      "Sunday", "January", "February", "March", "April", "May", "June",
                      "July", "August", "September", "October", "November", "December"})

# Printable ASCII minus < > { } [ ] ` $ ^ \ ~ @ # (nothing tag- or markup-shaped, no
# handles or hashtags), plus typographic quotes, dashes and the ellipsis. The | stays: Etsy
# titles separate their phrases with it.
_ALLOWED = re.compile(r"[ !\"%&'()*+,\-./0-9:;=?A-Z_a-z|‘’“”–—…\n]*")
_URLISH = re.compile(r"(?i)[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|co|"
                     r"dev|app|ai|me|info|biz|us|uk|de|eu|xyz|site|online|shop|store|tech|"
                     r"ly|to|tv|gg)\b")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_SENTENCE_START = re.compile(r"(?:^|[.!?:]\s+|\n\s*(?:[-*]\s+)?)$")
_TAG = re.compile(r"[a-z0-9]+(?: [a-z0-9]+)*")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'/-]*")


def _whole(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"[\s-]+")
                      + r"(?![a-z0-9])", re.IGNORECASE)


_BRAND_PATTERNS = tuple((t, _whole(t)) for t in sorted(set(BRAND_PHRASES) | BRAND_WORDS))
_CLAIM_PATTERNS = tuple((c, re.compile(r"(?<![a-z0-9])" + re.escape(c) + r"(?![a-z0-9])",
                                       re.IGNORECASE)) for c in CLAIMS)


def brand_problem(text: str) -> str | None:
    """A brand, trademark, franchise or celebrity term, or None."""
    for term, pattern in _BRAND_PATTERNS:
        if pattern.search(text):
            return f"names a brand or trademark ({term!r}); Etsy's Creativity Standards " \
                   "forbid using other people's brands or characters"
    for tok in _TOKEN.findall(text):
        if tok in _CAPITALISED_BRANDS:
            return f"names a brand or trademark ({tok!r})"
    return None


def claim_problem(text: str) -> str | None:
    for claim, pattern in _CLAIM_PATTERNS:
        if pattern.search(text):
            return f"makes a claim the shop could be held to ({claim!r}); customer-facing " \
                   "claims fail closed"
    return None


def chars_problem(text: str, *, multiline: bool = False) -> str | None:
    if not _ALLOWED.fullmatch(text):
        bad = next(c for c in text if not _ALLOWED.fullmatch(c))
        return f"has a character a listing may not use ({bad!r})"
    if not multiline and "\n" in text:
        return "must be one line"
    if _URLISH.search(text):
        return "has a link, URL or domain (a listing links to nothing)"
    return _contact_problem(text)


def _known_lower(word: str) -> bool:
    common, _proper = dictionary()
    w = word.replace("’", "'")
    if w in FORMAT_WORDS or len(w) < 3:
        return True
    for part in w.split("-"):
        if not part:
            continue
        if part in FORMAT_WORDS or len(part) < 3:
            continue
        stem = part[:-2] if part.endswith("'s") else part.rstrip("'")
        if not any(v in common for v in _spellings(stem)):
            return False
    return True


def name_problem(text: str) -> str | None:
    """A proper noun (a person, place, company or product) or an unknown word, or None.

    Capitalised words: an ordinary English word is fine; an acronym only from ``ACRONYMS``;
    a word the dictionary holds as both ("Mark", "Target") only at the start of a sentence;
    a name or a word the dictionary does not know, never. Lower-case words must be in the
    dictionary too (a lower-case brand is still a brand; a misspelling is not shipped)."""
    for m in _TOKEN.finditer(text):
        tok = m.group(0).rstrip("'’-")
        if not tok:
            continue
        if tok[0].islower():
            if not _known_lower(tok):
                return f"uses a word the dictionary does not know ({tok[:30]!r}); fail closed"
            continue
        if tok in CALENDAR or len(tok) == 1:
            continue
        kind = word_kind(tok)
        if kind == ORDINARY:
            continue
        if kind == ACRONYM:
            if tok in ACRONYMS:
                continue
            return f"uses an acronym that may be a brand ({tok[:30]!r})"
        if kind == DUAL and _SENTENCE_START.search(text[:m.start()]):
            continue
        what = "a name" if kind in (NAME, DUAL) else "an unknown word"
        return f"uses {what} ({tok[:30]!r}): listings name no person, place, brand or " \
               "product"
    return None


def text_problems(key: str, text, *, multiline: bool = False) -> list[str]:
    """Every rule for one piece of listing text, as ``"<key>: <why>"`` reasons."""
    if not isinstance(text, str):
        return [f"{key}: required, text"]
    out = []
    for problem in (chars_problem(text, multiline=multiline), brand_problem(text),
                    claim_problem(text), name_problem(text)):
        if problem:
            out.append(f"{key}: {problem}")
    return out


def title_problems(title) -> list[str]:
    if not isinstance(title, str):
        return ["title: required, text"]
    out = []
    if title != title.strip() or "  " in title:
        out.append("title: no leading, trailing or double spaces")
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        out.append(f"title: {TITLE_MIN}-{TITLE_MAX} characters (this is {len(title)})")
    for ch in "%:&+":
        if title.count(ch) > 1:
            out.append(f"title: {ch!r} at most once (Etsy's rule)")
    for ch in "$^`":
        if ch in title:
            out.append(f"title: Etsy refuses {ch!r} in a title")
    return out + text_problems("title", title)


def tag_problems(tags, *, exactly: int = TAGS_EXACTLY) -> list[str]:
    if not isinstance(tags, list) or len(tags) != exactly:
        return [f"tags: exactly {exactly} (this is "
                f"{len(tags) if isinstance(tags, list) else 'not a list'})"]
    out = []
    for i, tag in enumerate(tags):
        if not isinstance(tag, str) or not _TAG.fullmatch(tag) or len(tag) > TAG_MAX:
            out.append(f"tags[{i}]: 1-{TAG_MAX} characters of a-z, 0-9 and single spaces")
            continue
        out += text_problems(f"tags[{i}]", tag)
    if len({t for t in tags if isinstance(t, str)}) != len(tags):
        out.append("tags: each tag once")
    return out


def description_problems(description, *, kind: str) -> list[str]:
    if kind not in REQUIRED:
        return [f"kind: {kind!r} is not a listing kind ({', '.join(REQUIRED)})"]
    if not isinstance(description, str):
        return ["description: required, text"]
    out = []
    low, high = DESCRIPTION
    if not low <= len(description) <= high:
        out.append(f"description: {low}-{high} characters (this is {len(description)})")
    for sentence in REQUIRED[kind]:
        if sentence not in description:
            what = "the AI disclosure" if sentence == AI_DISCLOSURE else "the required note"
            out.append(f"description: must carry {what} word for word: {sentence!r}")
    return out + text_problems("description", description, multiline=True)


def listing_problems(title, description, tags, *, kind: str,
                     tags_exactly: int = TAGS_EXACTLY) -> list[str]:
    """Every reason this listing may not go to Etsy; empty means it may be submitted."""
    return (title_problems(title) + description_problems(description, kind=kind)
            + tag_problems(tags, exactly=tags_exactly))


def check_listing(title, description, tags, *, kind: str,
                  tags_exactly: int = TAGS_EXACTLY) -> None:
    """``listing_problems`` as a ValueError naming the first reason (the adapters' form)."""
    problems = listing_problems(title, description, tags, kind=kind,
                                tags_exactly=tags_exactly)
    if problems:
        raise ValueError(problems[0])


def label_problems(key: str, label) -> list[str]:
    """A row label inside a spreadsheet (a category, a habit, a goal)."""
    if not isinstance(label, str) or not _LABEL.fullmatch(label) \
            or not LABEL[0] <= len(label) <= LABEL[1] or "  " in label:
        return [f"{key}: {LABEL[0]}-{LABEL[1]} characters of letters, digits, spaces, "
                "& ' / -"]
    return text_problems(key, label)


def phrase_problems(phrase) -> list[str]:
    """A print-on-demand design's words: short, one line, nobody's name or brand."""
    if not isinstance(phrase, str):
        return ["phrase: required, text"]
    out = []
    if not PHRASE[0] <= len(phrase) <= PHRASE[1] or phrase != phrase.strip():
        out.append(f"phrase: {PHRASE[0]}-{PHRASE[1]} characters, trimmed")
    if len(phrase.split()) > PHRASE_WORDS:
        out.append(f"phrase: at most {PHRASE_WORDS} words")
    return out + text_problems("phrase", phrase)


def keyword_problems(keyword) -> list[str]:
    """A search keyword the scout may measure: lower case words, no brand. (It is never
    published; it only steers what is made, so the proper-noun rule is the brand rule.)"""
    if not isinstance(keyword, str) or not _TAG.fullmatch(keyword) or len(keyword) > 40:
        return ["keyword: 2-40 characters of a-z, 0-9 and single spaces"]
    problem = brand_problem(keyword) or claim_problem(keyword)
    return [f"keyword: {problem}"] if problem else []


def words_of(texts: Iterable[str]) -> set[str]:
    return {w.lower() for t in texts for w in re.findall(r"[a-z0-9]+", t.lower())}
