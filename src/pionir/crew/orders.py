"""The order desk: every paid "I do it for you" order answered, by fixed template, for the
owner's yes.

Dokaz sells work done for the client: **Small $149** (one script or automation, 3 business
days), **Standard $399** (a small tool with a simple interface, 5 business days) and
**Custom** (quoted by the owner) - and **Find it for me $19** (``find``: a report of where
to buy something, within 2 business days, a full refund if nothing is found; researched and
reported by ``contracts.finder``, finder.py). Paid in full up front through Stripe; email
only, no calls; every client email approved by the owner on Discord first. A paid ``find``
order gets its own acknowledgement (``FIND_ACK_*``, still the ACK kind).

**No model, no words.** Every email this worker sends is one of the three templates below
(``ACK_*``, ``QUOTE_*``, ``DECLINE_*``) with the order's own id, package and the name the
client gave filled in. It cannot invent a promise, a price or a date: there is nowhere for
one to come from. The owner can read and edit the templates here.

One run:

1. **The orders**: ``Job("client.orders", {})``. Pionir unavailable, or the capability not
   set up, is an honest ``UNAVAILABLE`` / ``NOT_CONFIGURED`` - never "no orders" - and the
   run then does nothing else at all.
2. **Follow up** every email waiting on the owner (``ctx.approval``): approved and done is
   SENT; denied, failed or unknown is not. Nothing is ever assumed sent.
3. **New emails**, oldest order first, at most ``MAX_EMAILS_PER_RUN`` submitted a run:

   - every brief is **screened** (``SCREENS``, a plain list of patterns below). A flag is
     not a refusal: it becomes the DECLINE email, parked for the owner - his approval sends it,
     his denial leaves the order to him;
   - a paid order, not flagged: the ACKNOWLEDGEMENT (only when its package and amount are
     the ones sold; anything else is held for the owner, and no email goes);
   - a quote request, not flagged: the QUOTE acknowledgement;
   - a quote paid through its pay link (``paid``, a custom order with its quote's first part
     paid): the QUOTE-PAID acknowledgement, stating what was paid and, for a deposit, the
     balance due on delivery; its sending moves the order to ``in_progress``;
   - ``quoted``, from day 10 of its quote while the pay link is still open and unpaid: the
     ONE reminder (``client.quote_reminder``; Scrooge also refuses a second);
   - awaiting payment: nothing (older than ``ABANDONED_AFTER``: an abandoned checkout);
   - every other status is the owner's already: nothing.

   **Quote cards.** Every custom order waiting on a price (``quote_requested`` or ``quoted``,
   its brief not flagged - or flagged and the owner denied the decline) gets ONE quote card
   in the owner's Discord channel (``quotes.card``, at most ``MAX_CARDS_PER_RUN`` a run). The
   owner replies to it with the price; the Discord gate turns that reply into the quote
   email card for his yes (pionir/quotes.py). This desk never writes, suggests or checks a
   price: the only amounts it ever puts in an email are the ones Scrooge says were quoted
   and paid.

   Each email is built, then **checked** (``check_email``, fail closed) on the exact payload
   that would be submitted, then submitted as ``Job("client.email", {order_id, to, subject,
   body_text})``. Pionir parks it for the owner.
4. **Status**, only once the email is SENT: an acknowledgement moves the order to
   ``in_progress``, a decline to ``declined`` (``client.set_status``) - and only from the
   status it had when the email went, so an order the owner has moved on is left alone.
   A declined PAID order is a refund the owner issues by hand in Stripe: this worker
   counts it and never refunds anything.

**Never twice.** Every email this worker ever submitted has an entry in its record
(``contracts.orders.json``); an order never gets a second email of the same kind, nor an
acknowledgement after a decline (or the reverse). The one exception is a submission that
never reached Pionir (``unreachable``), retried on up to ``RETRY_UNREACHABLE`` runs. As a
second guard, an email whose subject Pionir's order already lists among its messages is
recorded as sent, not sent again.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass

from .blog import FORGOTTEN_AFTER, _clip, _Unreadable, read_record, record_path, save_record
from .figures import Figure
from .hands import Job, outcome_of
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base

ORDERS = "client.orders"
EMAIL = "client.email"
SET_STATUS = "client.set_status"
REMIND = "client.quote_reminder"
QUOTE_CARD = "quotes.card"
MAX_CARDS_PER_RUN = 3
HIRE_URL = "https://api.dokaz.net/hire"     # the one link an email may carry
MAX_EMAILS_PER_RUN = 2
RETRY_UNREACHABLE = 5                       # runs a never-delivered email is retried
# An email the owner approved whose SEND then failed for a passing reason (the mail provider
# or Scrooge unavailable - not a refusal) is offered to him again, up to this many times.
# Found by the phase 4a end-to-end run: otherwise a paid client whose acknowledgement hit a
# mail outage would never be acknowledged. The order's own messages guard against a double
# send (``_already_sent``).
RETRY_UNDELIVERED = 3
RETRYABLE = {"unreachable": RETRY_UNREACHABLE, "undelivered": RETRY_UNDELIVERED}
RETRY_STATUS = 5                            # runs a status update is retried
ABANDONED_AFTER = 3 * 86400                 # an unpaid checkout older than this is abandoned

STATUSES = ("awaiting_payment", "paid", "in_progress", "delivered", "declined", "refunded",
            "quote_requested", "quoted", "balance_due", "balance_paid")
PAID_OR_LATER = frozenset({"paid", "in_progress", "delivered", "balance_due", "balance_paid"})

ACK, QUOTE_ACK, DECLINE = "acknowledgement", "quote_acknowledgement", "decline"
QUOTE_PAID_ACK, REMINDER = "quote_paid_acknowledgement", "quote_reminder"
# the order status an email's sending moves the order to, and the statuses it moves it from
AFTER_SENT = {ACK: ("in_progress", ("paid",)),
              QUOTE_PAID_ACK: ("in_progress", ("paid",)),
              DECLINE: ("declined", ("paid", "quote_requested"))}
# the capability each kind of email is sent through (anything not listed: client.email)
CAPABILITY = {REMINDER: REMIND}


@dataclass(frozen=True)
class Package:
    title: str
    price_cents: int
    days: int               # business days from payment
    included: str


# What is sold. A paid order for anything else, or for another amount, is held for the owner.
FIND = "find"          # "Find it for me": researched by contracts.finder (finder.py)
PACKAGES = {
    "small": Package("Small", 14900, 3, "one script or automation"),
    "standard": Package("Standard", 39900, 5, "a small tool with a simple interface"),
    FIND: Package("Find it for me", 1900, 2,
                  "a report of where to buy it, with prices and links"),
}

# ---- the templates: every word a client ever gets from this worker ------------------------
# {name} is the name the client gave (or "there" when it is not a plain name), {order_id}
# the order's id. Nothing else is filled in but what is named in each.

ACK_SUBJECT = "Your Dokaz order {order_id} is confirmed"
ACK_BODY = """Hello {name},

Thank you for your order. Your payment has been received and your order is confirmed.

Order: {order_id}
Package: {package} ({price})
What's included: {included}
Turnaround: {days} business days from your payment

How it works:
- Every update, and the delivery itself, comes by email only, to this address.
- The delivery is a zip file with a short how-to, sent to you as a private link.
- One round of revisions is included free.
- If we can't deliver, you'll receive a full refund.

If we need anything more from you, we'll ask by email.

Thank you,
Dokaz"""

# A paid "Find it for me" order gets this acknowledgement instead of ACK_* (same kind, ACK:
# it moves the order to in_progress once sent, and the finder then researches it).
FIND_ACK_SUBJECT = "Your Dokaz find order {order_id} is confirmed"
FIND_ACK_BODY = """Hello {name},

Thank you for your order. Your payment has been received, and we're now searching for \
what you asked us to find.

Order: {order_id}
Package: {package} ({price})
What's included: {included}
Turnaround: a report within {days} business days from your payment

How it works:
- The report comes by email only, to this address.
- It lists where you can buy what you asked for, with prices and links.
- If we can't find it, you'll receive a full refund.
- If your request is unclear, we'll ask you one follow-up question by reply.

Thank you,
Dokaz"""

QUOTE_SUBJECT = "We've received your Dokaz request {order_id}"
QUOTE_BODY = """Hello {name},

Thank you for your request. We've received your brief.

Request: {order_id}
Package: Custom

We'll read your brief and reply with a quote within 2 business days. There is nothing to \
pay until you accept the quote.

Everything comes by email only, to this address.

Thank you,
Dokaz"""

DECLINE_SUBJECT = "About your Dokaz request {order_id}"
DECLINE_BODY = """Hello {name},

Thank you for your interest in Dokaz. We've read your brief for {order_id}, and we're \
sorry, but it is work we don't take on: {category}.

{money_line}

If we've misread your brief, reply to this email and tell us more.

Thank you,
Dokaz"""
DECLINE_PAID = "You'll receive a full refund of your payment."
DECLINE_UNPAID = "You haven't been charged anything."

# A quote paid through its pay link. {paid} and {balance} are the amounts Scrooge recorded
# for the owner's quote - never computed here.
QUOTE_PAID_SUBJECT = "Payment received for your Dokaz order {order_id}"
QUOTE_PAID_BODY = """Hello {name},

Thank you - your payment of {paid} for request {order_id} has been received, and work has \
started.

Order: {order_id}
Package: Custom (as quoted)
Delivery: within {days} of your payment

{balance_line}

How it works:
- Every update, and the delivery itself, comes by email only, to this address.
- One round of revisions is included free.
- If we can't deliver, you'll receive a full refund.

Thank you,
Dokaz"""
QUOTE_PAID_FULL = "That is the full price of your quote: there is nothing more to pay."
QUOTE_PAID_DEPOSIT = ("The balance of {balance} is due when the work is delivered: we'll email "
                      "you a payment link with it, and your files are released once it is paid.")

# The ONE reminder of an unpaid quote, from day 10. No link: it points to the quote email.
REMINDER_SUBJECT = "A reminder about your Dokaz quote {order_id}"
REMINDER_BODY = """Hello {name},

A short reminder: our quote for your request {order_id} ({price}) is still open. Its payment \
link, in our quote email, is valid until {expires} (UTC).

If you'd like to go ahead, use that link. If you have a question, or the quote doesn't suit \
you, just reply to this email.

Thank you,
Dokaz"""


# ---- the screening list: what the owner declines ------------------------------------------
@dataclass(frozen=True)
class Screen:
    key: str
    plain: str              # the category in plain words, as the decline email names it
    patterns: tuple         # a regex flags the brief; a tuple of regexes flags it when ALL match


def _words(*alternatives: str) -> str:
    """A regex matching any one of these as a whole word or phrase (each may be a regex)."""
    return r"\b(?:" + "|".join(alternatives) + r")\b"


def _near(first: str, second: str, gap: int = 40) -> str:
    """``first``, then ``second`` within ``gap`` characters."""
    return f"{first}.{{0,{gap}}}{second}"


_SCRAPE = _words(r"scrap(?:e|es|ed|er|ers|ing)", r"harvest\w*", r"crawl\w*", r"spider\w*")
_PERSONAL = _words(r"e-?mails?", r"e-?mail address\w*", "phones?", "phone numbers?",
                   "mobile numbers?", "numbers", "profiles?", "contacts?",
                   r"contact (?:details|info\w*)", "personal", "names", "addresses",
                   "followers", "members", "users", "people", "leads?", "linkedin",
                   "facebook", "instagram", "tiktok")
_SOCIAL = _words("instagram", "facebook", "tiktok", "twitter", r"x\.com", "linkedin", "reddit",
                 "youtube", "discord", "telegram", "whatsapp", "snapchat", "pinterest",
                 "threads", "social media", "social networks?")
# for the find screens: looking for something, and a person being looked for
_SEEK = _words(r"find\w*", r"locat\w*", r"track\w*(?: down)?", r"trac(?:e|es|ed|ing)",
               r"search\w* for", r"look\w* (?:up|for)", r"hunt\w* down", r"dig\w* up",
               r"identif\w*", "who is", "who owns")
_EX = r"ex(?:-?(?:wife|husband|girlfriend|boyfriend|partner|fiance\w*))?"
_PERSON = _words(r"some(?:one|body)(?! (?:to|who|that|selling|with|in|near)\b)", "a person", "this person", "that person",
                 "the person", "a man", "a woman", "a guy", "a girl", "my " + _EX,
                 _EX + "'?s?", r"(?:old|former|lost|long-lost) (?:friend|classmate|colleague|"
                 r"partner|flame|lover|roommate)s?", r"birth (?:mother|father|parents?)",
                 r"(?:biological|real) (?:mother|father|parents?)", "relatives?", "tenants?",
                 "debtors?", "neighbou?rs?", "a stranger")
_THEIRS = r"(?:his|her|their|someone'?s|somebody'?s|a person'?s|this person'?s|my " + _EX \
          + r"'?s|the owner'?s)"
_GUN = (r"(?<!glue )(?<!heat )(?<!nail )(?<!spray )(?<!paint )(?<!staple )(?<!grease )"
        r"(?<!caulk )(?<!massage )(?<!solder )(?<!caulking )(?<!soldering )guns?")

# Errs on flagging: a flag costs the owner one look; a miss costs him a job he refuses to do.
# A pattern flags the brief; a tuple of patterns flags it only when ALL of them match.
SCREENS = (
    Screen("personal_data", "collecting people's personal data, such as email addresses, "
           "phone numbers or profiles", (
               (_SCRAPE, _PERSONAL),
               _near(_words(r"collect\w*", r"extract\w*", r"gather\w*", r"pull\w*", r"grab\w*",
                            r"find\w*", "get", r"build\w*", r"compil\w*"),
                     _words(r"e-?mail address\w*", "phone numbers?", "mobile numbers?",
                            r"contact (?:details|info\w*|lists?)",
                            r"personal (?:data|details|info\w*)", "profiles")),
               _words(r"(?:e-?mail|phone|contact|lead)s?\s+(?:lists?|extract\w*|harvest\w*|"
                      r"finders?|scrap\w*|databases?)"),
               _words(r"lead gen\w*"),
               _near(_words(r"track\w*", r"monitor\w*", r"spy\w*", r"stalk\w*"),
                     _words("someone", "a person", "people", "employees?", "partner", "wife",
                            "husband", "girlfriend", "boyfriend", "location")),
           )),
    Screen("bypass", "getting around a website's captchas, logins, rate limits or terms of "
           "service", (
               _words("captchas?"),
               _near(_words(r"bypass\w*", r"circumvent\w*", r"get(?:ting)? (?:around|past)",
                            r"evad\w*", r"avoid\w*", r"defeat\w*", "beat",
                            r"break(?:ing)? (?:through|past)", r"crack\w*", r"solv\w*"),
                     _words("logins?", "log-ins?", "paywalls?", r"rate[- ]?limit\w*", "limits",
                            "bans?", "blocks?", "blocking", "detection", "cloudflare",
                            "anti-?bot", "bot protection", "protection", "terms")),
               _words(r"(?:rotating|residential)\s+prox(?:y|ies)", r"prox(?:y|ies)\s+rotation"),
               _words(r"undetect\w*", r"stealth\w*", r"anti-?detect\w*"),
               _words("terms of (?:service|use)"),
               _words(r"(?:sneaker|ticket|checkout|auto-?buy|scalp\w*)\s*bots?"),
           )),
    Screen("social_bots", "bots or automated accounts on social platforms", (
        (_words("bots?", r"automat\w*", r"auto-?(?:post|like|follow|comment|dm|repl)\w*",
                "mass", "bulk"), _SOCIAL),
        _words(r"(?:follow|like|view|comment|engagement)\s*(?:bots?|farms?)"),
        _words("fake (?:accounts?|followers|reviews?|likes|views|profiles?)"),
        _words(r"(?:multiple|many|mass|bulk)\s+accounts"),
    )),
    Screen("credentials", "handling other people's passwords, logins or credentials", (
        _words("passwords?", "credentials?", r"(?:login|log-in|sign-in)\s+(?:details|info\w*|data)"),
        _words(r"steal\w*", r"phish\w*", r"keylog\w*", r"brute[- ]?forc\w*", "account takeover"),
        _words("(?:2fa|otp|one-time) codes?"),
    )),
    Screen("money", "moving money, such as bank, card, payment or crypto transfers", (
        _words(r"(?:bank|wire|ach|sepa|swift|iban)\s*(?:transfers?|payments?|accounts?)"),
        _near(_words(r"transfer\w*", r"send\w*", r"mov\w*", r"withdraw\w*"),
              _words("money", "funds", r"crypto\w*", "bitcoin", "btc", "eth", "usdt"), 30),
        _words(r"crypto\w*", "bitcoin", "btc", "ethereum", "usdt", "wallets?", "defi", "nfts?",
               "trading bots?", "arbitrage", "payouts?"),
        _words("paypal", "venmo", "cash ?app", "zelle", "stripe"),
        _words(r"(?:credit|debit)\s+cards?", "card numbers?"),
    )),
    Screen("gambling", "gambling or betting", (
        _words(r"gambl\w*", "betting", "bets?", "casinos?", "poker", "sportsbooks?",
               r"lotter\w*", "roulette", "blackjack", "slot machines?"),
    )),
    Screen("spam", "sending bulk or unsolicited messages", (
        _words(r"(?:mass|bulk|cold|unsolicited)\s+(?:e-?mails?|e-?mailing|messages?|messaging|"
               r"dms?|sms|texts?|texting|outreach)"),
        _words(r"spam\w*"),
    )),
    Screen("harm", "breaking into systems, or software meant to cause harm", (
        _words(r"hack\w*", "malware", "ddos", r"exploit\w*", "ransomware", "botnets?",
               "spyware", "trojans?", "viruses"),
    )),
    # ---- written for "Find it for me" briefs, and screening every brief ----------------------
    Screen("people_finding", "finding a person, or anyone's address, contact details or "
           "whereabouts", (
               _near(_SEEK, _PERSON, 15),
               _near(_SEEK, _THEIRS + r"\s+(?:home |current |new |street |email |e-mail )?"
                     r"(?:address\w*|home|house|location|whereabouts|phone|number|mobile|"
                     r"e-?mail|contact\w*|workplace|job|employer|instagram|facebook|socials?)",
                     30),
               _words(r"where (?:he|she|they|my " + _EX + r"|this person|that person|"
                      r"the person) (?:lives?|lived|living|works?|working|moved|stays?|"
                      r"staying|is now|are now|is living|is staying|went|hangs? out)"),
               _words("whereabouts", r"home address\w*", r"current address\w*",
                      r"reverse (?:phone|number|email|e-mail|address|image) (?:lookup|search)",
                      r"people[- ]?search\w*", r"people[- ]?finders?", r"skip[- ]?trac\w*",
                      "background checks?",
                      r"(?:phone|mobile|cell) numbers? (?:of|for) (?:a|this|that|my|his|her|"
                      r"their|someone|somebody)",
                      r"(?:owner|owners) of (?:this|that|the|a) (?:car|number|phone|house|"
                      r"property|home|plate|licen[cs]e plate|vehicle|account)",
                      r"who owns (?:this|that|the|a) (?:car|number|phone|house|property|home|"
                      r"plate|licen[cs]e plate|vehicle|account)",
                      r"licen[cs]e plate (?:lookup|search|owner)"),
           )),
    Screen("weapons", "weapons, ammunition or weapon parts", (
        _words(_GUN, "firearms?", "handguns?", "rifles?", "shotguns?", "pistols?", "revolvers?",
               "carbines?", r"ammo\w*", "ammunition", "bullets",
               r"cartridges? for (?:a |my )?(?:gun|rifle|pistol|shotgun)",
               r"(?<!exhaust )(?<!car )(?<!muffler )(?:suppressors?|silencers?)"
               r"(?![^.]{0,40}\b(?:exhaust|muffler|car|van|motorbike|motorcycle|scooter|"
               r"generator|engine)\b)",
               r"glocks?", r"ar-?15s?", r"ak-?47s?", "ruger", "mossberg", "sig sauer",
               r"smith (?:&|and) wesson", "desert eagle", "uzis?", "mp5", "beretta",
               r"(?:lower|upper) receivers?", "80% lowers?", "bump stocks?",
               "auto sears?", "binary triggers?", r"high[- ]capacity magazines?",
               "switchblades?", r"butterfly kni(?:fe|ves)", "balisongs?",
               r"gravity kni(?:fe|ves)", "push daggers?", "daggers?", "brass knuckles?",
               "knuckle dusters?", r"(?:combat|fighting|tactical|throwing|assault|boot) "
               r"kni(?:fe|ves)", r"kni(?:fe|ves) (?:as|for) (?:a )?(?:weapon|self[- ]defen[cs]e)",
               "tasers?", "crossbows?", "explosives?", "grenades?", "detonators?",
               r"body armou?r", r"armou?r plates?", "weapons?", r"nunchak\w*", r"nunchuc?ks?"),
    )),
    Screen("drugs", "drugs, or medicines that need a prescription", (
        _words("drugs?", r"narcotic\w*", "controlled substances?",
               r"without (?:a |any )?(?:prescription|rx|script)",
               r"no (?:prescription|rx|script) (?:needed|required)",
               r"prescription[- ](?:only|drugs?|medicines?|medications?|meds|pills|tablets)",
               r"(?:online|overseas|canadian|mexican|indian|foreign) pharmac\w*",
               "opioids?", "opiates?", "oxycodone", "oxycontin", "fentanyl", "xanax",
               "alprazolam", "adderall", "valium", "diazepam", "codeine", "tramadol",
               "percocet", "vicodin", "hydrocodone", "morphine", "ritalin", "modafinil",
               "ketamine", "mdma", "ecstasy", "lsd", "cocaine", "heroin", r"meth(?:amphetamine)?",
               "psilocybin", "magic mushrooms", "shrooms", "dmt", "ghb", "cannabis",
               "marijuana", "thc",
               r"weed(?!\s*(?:killer|whacker|wacker|eater|trimmer|barrier|control|puller|"
               r"burner|block\w*|fabric|membrane|wand))",
               "kratom", "steroids?", "anabolic", "sarms?", "semaglutide", "ozempic", "wegovy",
               "mounjaro", "tirzepatide", "viagra", "cialis", "sildenafil", "tadalafil",
               "benzos?", "benzodiazepines?", "painkillers?", "sleeping pills", "antibiotics",
               r"research chemicals?", "poppers"),
    )),
    Screen("counterfeit", "counterfeit, replica, stolen or other illegal goods", (
        _words(r"counterfeit\w*", "replicas?", r"knock-?offs?", r"super ?fakes?",
               r"fake (?:ids?|id cards?|passports?|driver'?s? licen[cs]es?|licen[cs]es?|"
               r"documents?|papers|designer\w*|rolex\w*|watch(?:es)?|bags?|handbags?|purses?|"
               r"sneakers|shoes|jordans|diplomas?|degrees?|certificates?|money|bills|notes|"
               r"banknotes|currency|dollars|euros|pounds)",
               r"(?:mirror|aaa|1:1|1 ?to ?1) (?:quality|grade|copy|copies|replicas?)",
               r"(?<!was )(?<!were )(?<!got )(?<!been )(?<!my )(?<!is )(?<!our )stolen",
               "no questions asked", r"(?:fell|falls|fallen) off (?:a|the) (?:back of a )?"
               r"(?:truck|lorry)", r"serial numbers? (?:removed|filed|scratched|ground)",
               "unregistered", "untraceable", "black market", r"dark ?web", "darknet",
               r"illegal\w*", r"smuggl\w*", "under the counter", r"cloned (?:cards?|phones?)"),
    )),
)

_COMPILED = tuple((s, tuple(tuple(re.compile(p, re.IGNORECASE)
                                   for p in (pat if isinstance(pat, tuple) else (pat,)))
                             for pat in s.patterns)) for s in SCREENS)


def screen(brief) -> list:
    """The screens a brief trips, in ``SCREENS`` order: empty is clean. Anything that is
    not text is screened as nothing - and held elsewhere, never acknowledged unread."""
    text = " ".join(brief.split()) if isinstance(brief, str) else ""
    hits = []
    for s, patterns in _COMPILED:
        if any(all(rx.search(text) for rx in group) for group in patterns):
            hits.append(s)
    return hits


# ---- the check: fail closed, on the exact payload that would be submitted ------------------
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s()<>\[\]]+")
_DOMAIN = re.compile(r"(?i)(?<![\w@.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b|@")
_MONEY = re.compile(r"(?i)[$€£]\s?\d[\d,]*(?:\.\d+)?"
                    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:usd|dollars?|eur|euros?|gbp|pounds?|cents?)\b"
                    r"|\b(?:usd|eur|gbp)\s?\d[\d,]*(?:\.\d+)?")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_EMAIL = re.compile(r"[^\s@<>,;:\"'()\[\]]{1,64}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", re.IGNORECASE)
_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAME = re.compile(r"[^\W\d_]+(?:[ '’-][^\W\d_]+){0,5}")
_TRAILING = ".,;:!?"


def _cents_of(amount: str) -> int | None:
    """``$149`` -> 14900; a figure in another currency is never ours (None)."""
    if "€" in amount or "£" in amount or re.search(r"(?i)eur|gbp|pound", amount):
        return None
    digits = re.search(r"\d[\d,]*(?:\.\d+)?", amount)
    if not digits:
        return None
    value = float(digits.group(0).replace(",", ""))
    if re.search(r"(?i)cents?\b", amount):
        return round(value)
    return round(value * 100)


def check_email(payload: dict, order: dict, allowed_cents) -> list:
    """Every reason this email may not go out; empty is the only pass. ``allowed_cents`` is
    the one money amount the body may state - the order's package price - or a collection of
    them (a quote: what Scrooge recorded as quoted and paid); None: none."""
    allowed = (frozenset() if allowed_cents is None else frozenset({allowed_cents})
               if isinstance(allowed_cents, int) else frozenset(allowed_cents))
    reasons: list = []
    if set(payload) != {"order_id", "to", "subject", "body_text"}:
        reasons.append("the payload is not exactly order_id, to, subject, body_text")
    oid, to = payload.get("order_id"), payload.get("to")
    subject, body = payload.get("subject"), payload.get("body_text")
    if oid != order.get("id") or not isinstance(oid, str) or not _ORDER_ID.fullmatch(oid):
        reasons.append("order_id is not this order's plain id")
    if not isinstance(to, str) or to != order.get("email") or not _EMAIL.fullmatch(to):
        reasons.append("the recipient is not exactly the order's email address")
    if not isinstance(subject, str) or not 5 <= len(subject) <= 120:
        reasons.append("the subject must be 5 to 120 characters")
    elif "\n" in subject or "\r" in subject or _CONTROL.search(subject):
        reasons.append("the subject must be a single line")
    if not isinstance(body, str) or not 20 <= len(body) <= 5000:
        reasons.append("the body must be 20 to 5000 characters")
    elif _CONTROL.search(body):
        reasons.append("the body has control characters")
    for field, text in (("subject", subject), ("body", body)):
        if not isinstance(text, str):
            continue
        if "<" in text or ">" in text:
            reasons.append(f"the {field} is not plain text (it has < or >)")
        rest = text
        for m in _URL.finditer(text):
            url = m.group(0).rstrip(_TRAILING)
            if url != HIRE_URL:
                reasons.append(f"the {field} links somewhere other than {HIRE_URL}: {url}")
            rest = rest.replace(url, " ")
        for m in _DOMAIN.finditer(rest):
            reasons.append(f"the {field} names an address or a domain: {m.group(0)}")
        for m in _MONEY.finditer(text):
            if _cents_of(m.group(0)) not in allowed:
                reasons.append(f"the {field} states the amount {m.group(0).strip()}, which is "
                               + ("not the order's package price" if allowed
                                  else "not allowed in this email"))
    return reasons


# ---- the templates, filled ---------------------------------------------------------------
def greeting_name(name) -> str:
    """The name the client gave, when it is a plain name; otherwise ``there``."""
    s = " ".join(name.split()) if isinstance(name, str) else ""
    return s if s and len(s) <= 60 and _NAME.fullmatch(s) else "there"


def price_text(cents: int) -> str:
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents // 100:,}.{cents % 100:02d}"


# ---- the quote, as Scrooge lists it ------------------------------------------------------
def _cents_ok(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def quote_of(order: dict) -> dict | None:
    """The order's current quote as Scrooge listed it, when its shape is whole; else None.
    Every amount this desk ever writes about a quote comes from here."""
    q = order.get("quote") if isinstance(order, dict) else None
    if not isinstance(q, dict) or not isinstance(q.get("id"), str):
        return None
    total, deposit, days = q.get("total_cents"), q.get("deposit_cents"), q.get("days")
    first = q.get("first")
    if not (_cents_ok(total) and isinstance(deposit, int) and not isinstance(deposit, bool)
            and 0 <= deposit < total and isinstance(days, int) and not isinstance(days, bool)
            and days > 0 and isinstance(first, dict)):
        return None
    return q


def balance_owed(order: dict) -> int | None:
    """The balance (cents) a deposit order owes now - its deposit paid, its balance not -
    or None."""
    q = quote_of(order)
    if q is None or q["deposit_cents"] <= 0 or q["first"].get("state") != "paid":
        return None
    balance = q.get("balance")
    if isinstance(balance, dict) and balance.get("state") == "paid":
        return None
    return q["total_cents"] - q["deposit_cents"]


def _day(stamp) -> str | None:
    at = _epoch(stamp)
    return None if at is None else _dt.datetime.fromtimestamp(at, _dt.UTC).strftime("%Y-%m-%d")


def build_email(kind: str, order: dict, flags: tuple = ()) -> dict:
    """The exact ``client.email`` payload for this order. Pure."""
    oid = order.get("id")
    name = greeting_name(order.get("name"))
    if kind == ACK:
        pkg = PACKAGES[package_of(order)]
        subject_t, body_t = ((FIND_ACK_SUBJECT, FIND_ACK_BODY) if package_of(order) == FIND
                             else (ACK_SUBJECT, ACK_BODY))
        subject = subject_t.format(order_id=oid)
        body = body_t.format(name=name, order_id=oid, package=pkg.title,
                             price=price_text(pkg.price_cents), included=pkg.included,
                             days=pkg.days)
    elif kind == QUOTE_ACK:
        subject = QUOTE_SUBJECT.format(order_id=oid)
        body = QUOTE_BODY.format(name=name, order_id=oid)
    elif kind == QUOTE_PAID_ACK:
        q = quote_of(order)
        paid = q["first"]["amount_cents"]
        days = q["days"]
        subject = QUOTE_PAID_SUBJECT.format(order_id=oid)
        body = QUOTE_PAID_BODY.format(
            name=name, order_id=oid, paid=price_text(paid),
            days=f"{days} business day{'' if days == 1 else 's'}",
            balance_line=(QUOTE_PAID_DEPOSIT.format(
                balance=price_text(q["total_cents"] - q["deposit_cents"]))
                if q["deposit_cents"] else QUOTE_PAID_FULL))
    elif kind == REMINDER:
        q = quote_of(order)
        subject = REMINDER_SUBJECT.format(order_id=oid)
        body = REMINDER_BODY.format(name=name, order_id=oid, price=price_text(q["total_cents"]),
                                    expires=_day(q["first"].get("expires_at")))
    elif kind == DECLINE:
        subject = DECLINE_SUBJECT.format(order_id=oid)
        category = " and ".join(s.plain for s in flags[:2]) or "work outside what we do"
        body = DECLINE_BODY.format(name=name, order_id=oid, category=category,
                                   money_line=DECLINE_PAID if order.get("status") == "paid"
                                   else DECLINE_UNPAID)
    else:
        raise ValueError(f"no template for {kind!r}")
    return {"order_id": oid, "to": order.get("email"), "subject": subject, "body_text": body}


def package_of(order: dict) -> str:
    p = order.get("package")
    return p.strip().lower() if isinstance(p, str) else ""


def held_reason(order: dict) -> str | None:
    """Why a paid, unflagged order may not be acknowledged by template, or None."""
    oid = order.get("id")
    if not isinstance(oid, str) or not _ORDER_ID.fullmatch(oid):
        return "its id is not a plain order id"
    pkg = PACKAGES.get(package_of(order))
    if pkg is None:
        return (f"its package is {_clip(order.get('package'), 30)!r}, not one with a fixed "
                "price and turnaround (a custom order's are the owner's quote)")
    amount = order.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount != pkg.price_cents:
        return (f"it was paid {amount!r} cents, not the {pkg.title} price of "
                f"{pkg.price_cents} cents")
    brief = order.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        return "it has no brief to read"
    return None


def _epoch(v) -> float | None:
    """A timestamp as epoch seconds (seconds, milliseconds or ISO 8601), or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if not math.isfinite(x):
            return None
        return x / 1000.0 if x > 1e11 else x
    if isinstance(v, str) and v.strip():
        s = v.strip()
        try:
            return _epoch(float(s))
        except ValueError:
            pass
        try:
            d = _dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.UTC)
        return d.timestamp()
    return None


def _oldest_first(order: dict):
    at = _epoch(order.get("created_at"))
    return (at if at is not None else math.inf, str(order.get("created_at")), order["id"])


def _orders_of(result) -> list | None:
    """The order list from Pionir's answer, or None when it holds none."""
    for doc in (result, result.get("result") if isinstance(result, dict) else None):
        if isinstance(doc, dict) and isinstance(doc.get("orders"), list):
            return None if doc.get("ok") is False else doc["orders"]
    return None


_NOT_SET_UP = re.compile(r"(?i)not[ _-]?configured|not set up|unknown capability|"
                         r"no such capability|not wired|no capability")


class OrderDesk(_Base):
    """``contracts.orders``: every order answered by fixed template, for the owner's yes."""

    record_what = "the order desk's own record of every email it submitted"

    def record_path(self, state_dir):
        return record_path(state_dir, self.worker_id)

    @staticmethod
    def _blank() -> dict:
        return {"emails": [], "seen_paid": [], "cards": []}

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        return self._blank() if doc is None else {**self._blank(), **doc}

    def save(self, state_dir, rec: dict) -> None:
        save_record(self.record_path(state_dir), rec)

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the desk cannot keep its "
                             "record, and without it could email a client twice",
                             retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: orders are read and clients "
                             "emailed only through Pionir", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); "
                             "refusing to email anyone, since it could email a client twice",
                             retryable=False)
        got = self._read_orders(ctx)
        if isinstance(got, Err):
            return got          # nothing seen, nothing done: never "no orders"
        orders, malformed = got.value
        by_id = {o["id"]: o for o in orders}
        events: list = []
        new_paid = self._note_new_paid(ctx, rec, orders, events)
        self._follow_up(ctx, rec, by_id, events)
        ready, held = self._send_new(ctx, rec, orders, events)
        self._post_cards(ctx, rec, orders, events)
        self._set_statuses(ctx, rec, by_id, events)
        self.save(ctx.state_dir, rec)
        return Ok((*events, self._tally(ctx, rec, orders, malformed=malformed, ready=ready,
                                        held=held, new_paid=new_paid)))

    def _read_orders(self, ctx: WorkContext) -> Result:
        out = ctx.job(Job(ORDERS, {}, what="read the client orders"))
        if out.status == "done":
            listed = _orders_of(out.result)
            if listed is None:
                return self._err(ErrorKind.MALFORMED, "Pionir answered client.orders without "
                                 "an order list; the orders are UNKNOWN, not zero")
            orders, malformed = [], 0
            for o in listed:
                if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"].strip():
                    orders.append(o)
                else:
                    malformed += 1
            ids = [o["id"] for o in orders]
            if len(set(ids)) != len(ids):
                return self._err(ErrorKind.MALFORMED, "Pionir listed one order id twice; "
                                 "refusing to act on an ambiguous order list")
            return Ok((orders, malformed))
        why = out.error or f"Pionir said {out.status}"
        if out.status == "failed" and _NOT_SET_UP.search(why):
            return self._err(ErrorKind.NOT_CONFIGURED, f"client.orders is not set up in "
                             f"Pionir ({_clip(why, 160)}); the orders are UNKNOWN, not zero",
                             retryable=False)
        return self._err(ErrorKind.UNAVAILABLE, f"could not read the orders ({out.status}: "
                         f"{_clip(why, 160)}); they are UNKNOWN, not zero")

    # ---- new paid orders: money, said once --------------------------------------------------
    def _note_new_paid(self, ctx: WorkContext, rec: dict, orders: list, events: list) -> list:
        seen = set(rec["seen_paid"])
        new = []
        for o in sorted(orders, key=_oldest_first):
            if o.get("status") != "paid" or o["id"] in seen:
                continue
            rec["seen_paid"].append(o["id"])
            new.append(o["id"])
            amount = o.get("amount_cents")
            figs = ([Figure(amount, "usd_cents", "paid order amount", window="now")]
                    if isinstance(amount, int) and not isinstance(amount, bool) else [])
            log.info("%s: NEW PAID ORDER %s (%s)", self.worker_id, o["id"], package_of(o))
            events.append(self._event(ctx, "order.new_paid", {
                "order_id": o["id"], "package": _clip(package_of(o), 30)}, figs))
        return new

    # ---- 2. what became of the emails already waiting --------------------------------------
    def _follow_up(self, ctx: WorkContext, rec: dict, by_id: dict, events: list) -> None:
        for e in rec["emails"]:
            if e.get("status") != "pending_approval" or not e.get("approval_id"):
                continue
            if ctx.approval is None:
                return
            got = ctx.approval(e["approval_id"]) or {}
            state = got.get("status")
            e["checked_at"] = ctx.now
            if state in ("pending", "running", "unreachable"):
                continue
            if state == "unknown":
                order = by_id.get(e.get("order_id"))
                if order is not None and _already_sent(order, e.get("subject")):
                    self._settle(ctx, rec, e, "sent", "Pionir no longer lists the approval, "
                                 "and the order's messages show it sent", events)
                elif ctx.now - float(e.get("submitted_at") or ctx.now) > FORGOTTEN_AFTER:
                    self._settle(ctx, rec, e, "unknown", "Pionir no longer lists this approval; "
                                 "it was never seen sent", events)
                continue
            if state == "denied":
                self._settle(ctx, rec, e, "denied", f"the owner did not approve it "
                             f"({got.get('reason') or 'denied'}); left to him", events)
            elif state == "approved":
                out = outcome_of(e.get("capability") or EMAIL, got.get("result"))
                if out.ran:
                    self._settle(ctx, rec, e, "sent", "approved by the owner and sent", events)
                else:
                    self._settle(ctx, rec, e, "failed", out.error or f"Pionir said "
                                 f"{out.status}", events)
            elif state == "approved_failed":
                out = outcome_of(e.get("capability") or EMAIL, got.get("result"))
                order = by_id.get(e.get("order_id"))
                if order is not None and _already_sent(order, e.get("subject")):
                    self._settle(ctx, rec, e, "sent", "the send reported a failure, but the "
                                 "order's messages show it sent", events)
                elif _refused(got.get("result")):
                    self._settle(ctx, rec, e, "failed", out.error or "the send was refused",
                                 events)
                else:
                    self._settle(ctx, rec, e, "undelivered", (out.error or "the send failed")
                                 + "; it will be offered to the owner again", events)
            else:
                log.warning("%s: approval %s has a status nobody knows (%r); still waiting",
                            self.worker_id, e["approval_id"], state)

    # ---- 3. new emails ---------------------------------------------------------------------
    def _plan(self, order: dict, now: float | None = None) -> tuple:
        """``(kind, flags)`` for the email this order needs, ``("held", why)``, or ``(None,
        ())`` for nothing."""
        status = order.get("status")
        if status == "quoted":
            return (REMINDER, ()) if _reminder_due(order, now) else (None, ())
        if status == "paid" and package_of(order) == "custom":
            q = quote_of(order)
            if q is None or q["first"].get("state") != "paid" \
                    or not _cents_ok(q["first"].get("amount_cents")):
                return "held", ("a custom order paid without a quote paid through its pay link: "
                                "the owner confirms it by hand")
            if not _ORDER_ID.fullmatch(order["id"]):
                return "held", "its id is not a plain order id"
            return QUOTE_PAID_ACK, ()
        if status not in ("paid", "quote_requested"):
            return None, ()
        flags = tuple(screen(order.get("brief")))
        if flags:
            return DECLINE, flags
        if status == "quote_requested":
            if not isinstance(order.get("brief"), str) or not order["brief"].strip():
                return "held", "it has no brief to read"
            if not _ORDER_ID.fullmatch(order["id"]):
                return "held", "its id is not a plain order id"
            return QUOTE_ACK, ()
        why = held_reason(order)
        return ("held", why) if why else (ACK, ())

    def _send_new(self, ctx: WorkContext, rec: dict, orders: list, events: list) -> tuple:
        """Submit what the oldest orders need, at most ``MAX_EMAILS_PER_RUN``. Returns (how
        many emails are ready for a later run, the paid orders held for the owner)."""
        submitted = ready = 0
        held: list = []
        for order in sorted(orders, key=_oldest_first):
            kind, extra = self._plan(order, ctx.now)
            if kind is None:
                continue
            if kind == "held":
                if order.get("status") == "paid" and not self._touched(rec, order["id"]):
                    held.append({"order_id": order["id"], "why": extra})
                continue
            quote_id = (quote_of(order) or {}).get("id") if kind == REMINDER else None
            if self._closed(rec, order["id"], kind, quote_id):
                continue
            flags = extra
            email = build_email(kind, order, flags)
            entry = {"order_id": order["id"], "kind": kind, "to": email["to"],
                     "subject": email["subject"], "was_paid": order.get("status") == "paid",
                     "from_status": order.get("status"),
                     "flags": [s.key for s in flags]}
            if quote_id:
                entry["quote_id"] = quote_id
            if _already_sent(order, email["subject"]):
                rec["emails"].append(entry)
                self._settle(ctx, rec, entry, "sent", "the order's messages already show "
                             "this email sent; not sent again", events)
                continue
            pkg = PACKAGES.get(package_of(order))
            reasons = check_email(email, order, _allowed_amounts(kind, order, pkg))
            if reasons:
                self._blocked(ctx, rec, entry, reasons, events)
                continue
            if submitted >= MAX_EMAILS_PER_RUN:
                ready += 1
                continue
            if quote_id:
                email = {**email, "quote_id": quote_id}
            self._submit(ctx, rec, entry, email, events)
            submitted += 1
        return ready, held

    @staticmethod
    def _attempts(rec: dict, oid: str, kind: str, quote_id: str | None = None) -> list:
        return [e for e in rec["emails"] if e.get("order_id") == oid and e.get("kind") == kind
                and (quote_id is None or e.get("quote_id") == quote_id)]

    def _closed(self, rec: dict, oid: str, kind: str, quote_id: str | None = None) -> bool:
        """True when this order must never get this email (again): one was submitted, sent,
        blocked or given up on - or it got the opposite answer (acknowledged vs declined). A
        reminder is once per QUOTE: a re-quote may have its own."""
        tries = self._attempts(rec, oid, kind, quote_id)
        if any(e.get("status") not in RETRYABLE for e in tries):
            return True
        for status, cap in RETRYABLE.items():
            if sum(1 for e in tries if e.get("status") == status) >= cap:
                return True
        opposite = {ACK: (DECLINE,), QUOTE_ACK: (DECLINE,), QUOTE_PAID_ACK: (DECLINE,),
                    DECLINE: (ACK, QUOTE_ACK, QUOTE_PAID_ACK)}.get(kind, ())
        return any(e.get("status") not in RETRYABLE
                   for k in opposite for e in self._attempts(rec, oid, k))

    @staticmethod
    def _touched(rec: dict, oid: str) -> bool:
        return any(e.get("order_id") == oid and e.get("status") not in RETRYABLE
                   for e in rec["emails"])

    def _blocked(self, ctx: WorkContext, rec: dict, entry: dict, reasons: list,
                 events: list) -> None:
        """An email the check stopped: recorded once, never retried (the check and the
        template are deterministic), and the order left to the owner."""
        entry.update(status="blocked", reasons=[_clip(r, 200) for r in reasons[:12]],
                     settled_at=ctx.now)
        rec["emails"].append(entry)
        log.warning("%s: the %s for %s was BLOCKED by the template check and NOT submitted: "
                    "%s", self.worker_id, entry["kind"], entry["order_id"], "; ".join(reasons))
        events.append(self._event(ctx, "order.email_blocked", {
            "order_id": entry["order_id"], "email": entry["kind"],
            "reasons": [_clip(r, 90) for r in reasons[:3]]}))

    def _submit(self, ctx: WorkContext, rec: dict, entry: dict, email: dict,
                events: list) -> None:
        capability = CAPABILITY.get(entry["kind"], EMAIL)
        if capability != EMAIL:
            entry["capability"] = capability
        out = ctx.job(Job(capability, dict(email),
                          what=f"email the client of order {entry['order_id']} the "
                               f"{entry['kind'].replace('_', ' ')}"))
        entry.update(submitted_at=ctx.now, status=out.status, task_id=out.task_id,
                     approval_id=out.approval_id)
        rec["emails"].append(entry)
        if out.status == "pending_approval":
            log.info("%s: the %s for %s is PENDING the owner's approval (approval %s)",
                     self.worker_id, entry["kind"], entry["order_id"], out.approval_id)
            events.append(self._event(ctx, "order.email_pending", {
                "order_id": entry["order_id"], "email": entry["kind"],
                "flags": entry["flags"], "approval_id": out.approval_id}))
        elif out.status == "done":
            # sent without being parked: the owner did NOT approve it. His rule is broken and
            # it is Pionir's gate that must hold it - said loudly; the email did go out.
            log.error("%s: %s ran WITHOUT the owner's approval for %s; it must be "
                      "approval-gated in Pionir", self.worker_id, capability, entry["order_id"])
            entry["approved_by_owner"] = False
            self._settle(ctx, rec, entry, "sent", "Pionir sent it without parking it for the "
                         "owner", events)
        elif out.status == "failed":
            self._settle(ctx, rec, entry, "failed", out.error or "Pionir said failed", events)
        elif out.status == "unreachable":
            self._settle(ctx, rec, entry, "unreachable", out.error or "never reached Pionir",
                         events)
        else:       # still running: not sent, never assumed to be, and never sent again
            self._settle(ctx, rec, entry, "unknown", out.error or f"Pionir said {out.status}",
                         events)

    def _settle(self, ctx: WorkContext, rec: dict, e: dict, status: str, why: str,
                events: list) -> None:
        e.update(status=status, why=_clip(why, 300), settled_at=ctx.now)
        if status == "sent":
            want = AFTER_SENT.get(e.get("kind"))
            if want is not None:
                e["status_wanted"] = want[0]
            log.info("%s: the %s for %s was SENT", self.worker_id, e["kind"], e["order_id"])
            events.append(self._event(ctx, "order.email_sent", {
                "order_id": e["order_id"], "email": e["kind"]}))
            return
        log.warning("%s: the %s for %s is %s: %s", self.worker_id, e.get("kind"),
                    e.get("order_id"), status, why)
        events.append(self._event(ctx, "order.email_not_sent", {
            "order_id": e["order_id"], "email": e["kind"], "status": status,
            "why": _clip(why, 160)}))

    # ---- the quote cards: one per custom order waiting on the owner's price ---------------
    def _wants_card(self, rec: dict, order: dict) -> bool:
        if order.get("status") not in ("quote_requested", "quoted") \
                or package_of(order) != "custom" or not _ORDER_ID.fullmatch(order["id"]):
            return False
        if not isinstance(order.get("brief"), str) or not order["brief"].strip():
            return False
        if screen(order.get("brief")):
            # flagged: the decline is the owner's first question; a card only once he has
            # denied the decline (he wants to quote it after all)
            return any(e.get("status") == "denied" for e in self._attempts(rec, order["id"],
                                                                           DECLINE))
        return True

    def _post_cards(self, ctx: WorkContext, rec: dict, orders: list, events: list) -> None:
        posted = 0
        for order in sorted(orders, key=_oldest_first):
            if posted >= MAX_CARDS_PER_RUN or not self._wants_card(rec, order):
                continue
            tries = [c for c in rec["cards"] if c.get("order_id") == order["id"]]
            if any(c.get("status") == "posted" for c in tries) \
                    or len(tries) >= RETRY_UNREACHABLE:
                continue
            out = ctx.job(Job(QUOTE_CARD, {"order_id": order["id"], "package": "Custom",
                                           "brief": order["brief"][:6000]},
                              what=f"post the quote card of order {order['id']}"))
            posted += 1
            ok = out.status == "done" and isinstance(out.result, dict) \
                and out.result.get("ok") is not False
            entry = {"order_id": order["id"], "at": ctx.now,
                     "status": "posted" if ok else "not_posted",
                     "why": None if ok else _clip(out.error or f"Pionir said {out.status}", 200)}
            rec["cards"].append(entry)
            if ok:
                log.info("%s: the quote card for %s is up for the owner", self.worker_id,
                         order["id"])
                events.append(self._event(ctx, "order.quote_card", {"order_id": order["id"]}))
            else:
                log.warning("%s: the quote card for %s was not posted: %s", self.worker_id,
                            order["id"], entry["why"])

    # ---- 4. the order's status, once its email is sent -------------------------------------
    def _set_statuses(self, ctx: WorkContext, rec: dict, by_id: dict, events: list) -> None:
        for e in rec["emails"]:
            want = e.get("status_wanted")
            if e.get("status") != "sent" or not want or e.get("status_state"):
                continue
            order = by_id.get(e.get("order_id"))
            if order is None:
                continue            # not listed this time: asked again next run
            current = order.get("status")
            if current == want:
                e.update(status_state="done", status_set_at=ctx.now)
                continue
            if current not in AFTER_SENT[e["kind"]][1]:
                e.update(status_state="skipped", status_why=f"the order is now {current!r}; "
                         "left as the owner set it")
                log.warning("%s: %s is %r, not moved to %s", self.worker_id, e["order_id"],
                            current, want)
                continue
            out = ctx.job(Job(SET_STATUS, {"order_id": e["order_id"], "status": want},
                              what=f"mark order {e['order_id']} {want}"))
            if out.status == "done":
                e.update(status_state="done", status_set_at=ctx.now)
                order["status"] = want
                events.append(self._event(ctx, "order.status_set", {
                    "order_id": e["order_id"], "status": want}))
            elif out.status == "pending_approval":
                e.update(status_state="parked", status_approval_id=out.approval_id)
                log.warning("%s: Pionir parked the status update of %s", self.worker_id,
                            e["order_id"])
            else:
                e["status_tries"] = int(e.get("status_tries") or 0) + 1
                e["status_why"] = _clip(out.error or f"Pionir said {out.status}", 200)
                if e["status_tries"] >= RETRY_STATUS:
                    e["status_state"] = "failed"
                    log.error("%s: gave up marking %s %s: %s", self.worker_id, e["order_id"],
                              want, e["status_why"])
                    events.append(self._event(ctx, "order.status_not_set", {
                        "order_id": e["order_id"], "status": want, "why": e["status_why"]}))

    # ---- what the leader reads --------------------------------------------------------------
    def _event(self, ctx: WorkContext, kind: str, payload: dict, figures=()):
        # no model wrote any of it: the ids, statuses and amounts are Pionir's, the words the
        # templates'. It never carries a client's name, address or brief.
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx: WorkContext, rec: dict, orders: list, *, malformed: int, ready: int,
               held: list, new_paid: list):
        emails = rec["emails"]

        def sent(kind: str) -> list:
            return [e for e in emails if e.get("kind") == kind and e.get("status") == "sent"]

        def n(*statuses) -> int:
            return sum(1 for e in emails if e.get("status") in statuses)

        by_status: dict = {s: 0 for s in STATUSES}
        unknown_status = 0
        for o in orders:
            if o.get("status") in by_status:
                by_status[o["status"]] += 1
            else:
                unknown_status += 1
        by_id = {o["id"]: o for o in orders}
        acked = {e["order_id"] for e in sent(ACK)}
        declined = {e["order_id"] for e in sent(DECLINE)}
        flagged_waiting, paid_waiting, quotes_waiting = [], [], []
        for o in orders:
            if o.get("status") not in ("paid", "quote_requested"):
                continue
            flags = screen(o.get("brief"))
            if flags:
                if o["id"] not in declined:
                    flagged_waiting.append({"order_id": o["id"], "status": o["status"],
                                            "flags": [s.key for s in flags]})
            elif o["status"] == "paid":
                if o["id"] not in acked:
                    paid_waiting.append(o["id"])
            else:
                quotes_waiting.append(o["id"])
        refunds = [e["order_id"] for e in sent(DECLINE) if e.get("was_paid")
                   and (by_id.get(e["order_id"]) or {}).get("status") != "refunded"]
        refund_cents = 0
        for oid in refunds:
            amount = (by_id.get(oid) or {}).get("amount_cents")
            if isinstance(amount, int) and not isinstance(amount, bool):
                refund_cents += amount
        abandoned = 0
        for o in orders:
            at = _epoch(o.get("created_at"))
            if o.get("status") == "awaiting_payment" and at is not None \
                    and ctx.now - at > ABANDONED_AFTER:
                abandoned += 1
        quote_states = {"open": 0, "expired": 0}
        for o in orders:
            q = quote_of(o)
            if o.get("status") == "quoted" and q is not None \
                    and q["first"].get("state") in quote_states:
                quote_states[q["first"]["state"]] += 1
        revenue = 0
        for o in orders:
            amount = o.get("amount_cents")
            if o.get("status") in PAID_OR_LATER and isinstance(amount, int) \
                    and not isinstance(amount, bool):
                revenue += amount
        gave_up = 0         # emails that never reached Pionir in RETRY_UNREACHABLE tries
        for oid, kind in {(e.get("order_id"), e.get("kind")) for e in emails}:
            tries = self._attempts(rec, oid, kind)
            if len(tries) >= RETRY_UNREACHABLE                     and all(e.get("status") == "unreachable" for e in tries):
                gave_up += 1
        pending = [{"order_id": e["order_id"], "email": e["kind"]} for e in emails
                   if e.get("status") == "pending_approval"]
        figures = [Figure(len(orders), "count", "orders listed", window="now")]
        figures += [Figure(by_status[s], "count", f"orders with status {s}", window="now")
                    for s in STATUSES]
        if unknown_status:
            figures.append(Figure(unknown_status, "count", "orders with an unknown status",
                                  window="now"))
        figures += [
            Figure(len(new_paid), "count", "new paid orders", window="this_run"),
            Figure(len(paid_waiting), "count", "paid orders waiting for acknowledgement",
                   window="now"),
            Figure(len(held), "count", "paid orders held for the owner", window="now"),
            Figure(len(sent(ACK)), "count", "acknowledgements sent", window="all_time"),
            Figure(len(sent(QUOTE_ACK)), "count", "quote acknowledgements sent",
                   window="all_time"),
            Figure(len(quotes_waiting), "count", "quote requests waiting for the owner",
                   window="now"),
            Figure(quote_states["open"], "count", "quotes emailed, waiting for payment",
                   window="now"),
            Figure(quote_states["expired"], "count", "quotes expired unpaid", window="now"),
            Figure(sum(1 for o in orders if o.get("status") == "balance_due"), "count",
                   "deliveries held for an unpaid balance", window="now"),
            Figure(len(sent(QUOTE_PAID_ACK)), "count", "quote payment confirmations sent",
                   window="all_time"),
            Figure(len(sent(REMINDER)), "count", "quote reminders sent", window="all_time"),
            Figure(sum(1 for c in rec.get("cards") or [] if c.get("status") == "posted"),
                   "count", "quote cards posted for the owner", window="all_time"),
            Figure(len(flagged_waiting), "count", "flagged orders awaiting the owner",
                   window="now"),
            Figure(len(sent(DECLINE)), "count", "declines sent", window="all_time"),
            Figure(len(refunds), "count", "refunds to issue by hand in Stripe", window="now"),
            Figure(refund_cents, "usd_cents", "refunds to issue by hand in Stripe",
                   window="now"),
            Figure(abandoned, "count", "abandoned checkouts", window="now"),
            Figure(revenue, "usd_cents", "paid revenue", window="orders_listed"),
            Figure(len(pending), "count", "client emails pending the owner's approval",
                   window="now"),
            Figure(n("denied"), "count", "client emails denied", window="all_time"),
            Figure(n("failed", "unknown") + gave_up, "count", "client emails failed",
                   window="all_time"),
            Figure(n("undelivered"), "count",
                   "approved client emails that failed to send (offered to the owner again)",
                   window="all_time"),
            Figure(n("blocked"), "count", "client emails blocked by the template check",
                   window="all_time"),
            Figure(sum(1 for e in emails if e.get("status_state") == "failed"), "count",
                   "order status updates failed", window="all_time"),
        ]
        return make_output(self, kind="order.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"new_paid_orders": new_paid[:10],
                                    "pending_approval": pending[-5:],
                                    "flagged": flagged_waiting[:5], "held": held[:5],
                                    "refunds_to_issue": refunds[:10],
                                    "emails_ready_next_run": ready,
                                    "malformed_orders": malformed},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})


def _refused(result) -> bool:
    """Whether a failed send was a refusal (Scrooge or Pionir said no - final) rather than a
    passing outage (retryable). Looks for a ``refused`` reason anywhere in the result."""
    if isinstance(result, dict):
        if result.get("refused"):
            return True
        return any(_refused(v) for v in result.values() if isinstance(v, dict))
    return False


def _already_sent(order: dict, subject) -> bool:
    """Pionir's order lists this exact subject among the messages sent to the client."""
    msgs = order.get("messages")
    return isinstance(subject, str) and isinstance(msgs, list) and any(
        isinstance(m, dict) and m.get("subject") == subject for m in msgs)


def _allowed_amounts(kind: str, order: dict, pkg):
    """The money amounts an email of this kind may state: the package price for an ACK;
    for a quote email, exactly what Scrooge recorded as quoted and paid; else none."""
    if kind == ACK and pkg:
        return pkg.price_cents
    q = quote_of(order)
    if kind == QUOTE_PAID_ACK and q is not None:
        return {q["first"]["amount_cents"], q["total_cents"] - q["deposit_cents"]} \
            if q["deposit_cents"] else {q["first"]["amount_cents"]}
    if kind == REMINDER and q is not None:
        return {q["total_cents"]}
    return None


def _reminder_due(order: dict, now: float | None) -> bool:
    """Day 10 of the quote or later, its pay link still open and unpaid, no reminder yet."""
    q = quote_of(order)
    if q is None or now is None or q.get("reminded_at"):
        return False
    first = q["first"]
    if first.get("state") != "open":
        return False
    remind_from, expires = _epoch(q.get("remind_from")), _epoch(first.get("expires_at"))
    return remind_from is not None and expires is not None and remind_from <= now < expires
