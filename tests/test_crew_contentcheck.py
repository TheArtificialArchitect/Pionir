"""The fail-closed content check: every public post passes it on its exact final text.

Each test fails if the rule it names is reverted: a draft breaking the rule would pass.
A clean draft must pass, or the check would be a wall rather than a gate. The allowlist
is the point: a person, a place or a company that nobody thought to list blocks the post
and is NAMED in the reason (a denylist once published a friend's name, a city and a
workplace on this machine).
"""

import json
import tempfile
import unittest
from pathlib import Path

from pionir.crew import contentcheck as cc

DRAFT_ID = "2026-09-25-verify-email-before-sending"
Q = cc.utm_query(DRAFT_ID)

BODY = f"""Checking an email address before you send to it saves bounced messages and a \
damaged sender reputation.

## Why addresses go bad

People mistype their address when they sign up. A missing letter in the domain or a \
throwaway domain looks fine in a form, but the message never arrives.

## What a good check looks at

- **Syntax:** is the address shaped like an address at all?
- **MX records:** does the domain accept mail?
- **Disposable domains:** is it a throwaway inbox?

The Email Verify API runs these checks in one request and returns a score. You can call \
it from Python or JavaScript and send JSON over HTTPS.

```json
{{"email": "the address to check"}}
```

## Try it

- [Email Verify API guide](https://api.dokaz.net/docs/email-verification-api?{Q})
- [all Dokaz APIs](https://api.dokaz.net/?{Q})
"""


def clean(**over) -> dict:
    d = {"draft_id": DRAFT_ID, "slug": "verify-email-before-sending",
         "title": "Verify email addresses before you send",
         "description": "Why checking syntax, MX records and typos before you send keeps "
                        "your messages out of the void.",
         "body_md": BODY, "tags": ["email", "deliverability"]}
    d.update(over)
    return d


def with_body(extra: str) -> dict:
    return clean(body_md=BODY + "\n" + extra + "\n")


class CleanDraftTests(unittest.TestCase):
    def test_a_clean_draft_passes(self) -> None:
        self.assertEqual(cc.check(clean()), [])

    def test_allowlisted_names_pass_mid_sentence(self) -> None:
        self.assertEqual(cc.check(with_body(
            "It works the same with Stripe, Gumroad or Cloudflare, on Windows and Linux.")), [])

    def test_an_api_endpoint_in_code_needs_no_utm(self) -> None:
        # TRAFFIC.md never counts /v1/*: an endpoint is not a page a visitor lands on
        self.assertEqual(cc.check(with_body(
            "```\ncurl https://api.dokaz.net/v1/email/verify\n```")), [])


class FieldRuleTests(unittest.TestCase):
    def assertBlocked(self, draft, needle: str) -> None:
        reasons = cc.check(draft)
        self.assertTrue(any(needle in r for r in reasons), (needle, reasons))

    def test_the_publish_contract(self) -> None:
        self.assertBlocked(clean(slug="-leading"), "slug")
        self.assertBlocked(clean(slug="Has Caps"), "slug")
        self.assertBlocked(clean(slug="ab"), "slug")
        self.assertBlocked(clean(slug="a" * 81), "slug")
        self.assertBlocked(clean(title="Too short"), "title is 9 characters")
        self.assertBlocked(clean(title="x" * 121), "title is 121")
        self.assertBlocked(clean(description="Short."), "description is 6")
        self.assertBlocked(clean(body_md="Too short to be a post."), "body_md is")
        self.assertBlocked(clean(tags=[f"t{i}" for i in range(9)]), "9 tags")
        self.assertBlocked(clean(tags=["Not A Tag"]), "tag 'Not A Tag'")
        self.assertBlocked(clean(tags=["x" * 25]), "tag")
        self.assertBlocked(clean(draft_id="Bad Id"), "draft_id")
        self.assertBlocked(clean(draft_id="x" * 65), "draft_id")
        self.assertBlocked(clean(title="A title that goes\non two lines"), "one line")

    def test_a_missing_or_extra_field_blocks(self) -> None:
        d = clean()
        del d["tags"]
        self.assertBlocked(d, "tags is missing")
        self.assertBlocked(clean(author="someone"), "unknown field")

    def test_not_even_an_object_blocks_and_never_raises(self) -> None:
        for bad in (None, "a post", ["x"], 42):
            self.assertTrue(cc.check(bad))
        self.assertTrue(cc.check(clean(title=None, body_md=12, tags="email")))


class MarkupTests(unittest.TestCase):
    def test_raw_html_blocks(self) -> None:
        reasons = cc.check(with_body("Some <b>bold</b> text."))
        self.assertTrue(any("raw HTML" in r for r in reasons), reasons)

    def test_an_html_comment_blocks(self) -> None:
        reasons = cc.check(with_body("<!-- a hidden note -->"))
        self.assertTrue(any("raw HTML" in r for r in reasons), reasons)

    def test_an_html_entity_blocks(self) -> None:
        reasons = cc.check(with_body("Use &lt;script&gt; nowhere."))
        self.assertTrue(any("entity" in r for r in reasons), reasons)

    def test_a_javascript_link_blocks(self) -> None:
        reasons = cc.check(with_body("[click](javascript:alert(1))"))
        self.assertTrue(any("javascript: URL" in r for r in reasons), reasons)

    def test_data_and_vbscript_urls_block(self) -> None:
        for bad in ("![x](data:image/png;base64,AAAA)", "[x](vbscript:msgbox)"):
            reasons = cc.check(with_body(bad))
            self.assertTrue(any("URL" in r for r in reasons), (bad, reasons))

    def test_invisible_characters_block(self) -> None:
        reasons = cc.check(with_body("a hidden\u200bword"))
        self.assertTrue(any("invisible" in r for r in reasons), reasons)


class LinkTests(unittest.TestCase):
    def test_an_off_allowlist_host_blocks_and_is_named(self) -> None:
        reasons = cc.check(with_body(f"[docs](https://example.com/page?{Q})"))
        self.assertTrue(any("example.com" in r for r in reasons), reasons)

    def test_a_lookalike_host_blocks(self) -> None:
        for url in (f"https://api.dokaz.net.evil.io/?{Q}", f"https://dokaz.net/?{Q}",
                    f"https://user@api.dokaz.net/?{Q}", f"https://api.dokaz.net:8443/?{Q}"):
            reasons = cc.check(with_body(f"[x]({url})"))
            self.assertTrue(any("link" in r for r in reasons), (url, reasons))

    def test_a_plain_http_link_blocks(self) -> None:
        reasons = cc.check(with_body(f"[x](http://api.dokaz.net/?{Q})"))
        self.assertTrue(any("not an https link" in r for r in reasons), reasons)

    def test_a_relative_link_blocks(self) -> None:
        reasons = cc.check(with_body("[x](/docs/qr-code-api)"))
        self.assertTrue(any("not an https link" in r for r in reasons), reasons)

    def test_a_link_missing_utm_blocks(self) -> None:
        reasons = cc.check(with_body("[x](https://dokaz.gumroad.com/l/obol)"))
        self.assertTrue(any("utm_source=blog" in r for r in reasons), reasons)

    def test_a_link_with_another_posts_campaign_blocks(self) -> None:
        other = cc.utm_query("2026-01-01-some-other-post")
        reasons = cc.check(with_body(f"[x](https://dokazindustries.com/tools/?{other})"))
        self.assertTrue(any("utm_campaign" in r for r in reasons), reasons)

    def test_a_bare_url_is_a_link_too(self) -> None:
        reasons = cc.check(with_body("See https://api.dokaz.net/docs for more."))
        self.assertTrue(any("utm_source" in r for r in reasons), reasons)

    def test_a_bare_domain_or_www_address_blocks(self) -> None:
        reasons = cc.check(with_body("Compare it with competitor.io or www.dokazindustries.com."))
        self.assertTrue(any("competitor.io" in r for r in reasons), reasons)
        self.assertTrue(any("www" in r for r in reasons), reasons)

    def test_the_campaign_follows_traffic_md(self) -> None:
        # at most 40 characters of a-z 0-9 . _ -
        c = cc.utm_campaign("2026-09-25-" + "a-very-long-slug-that-goes-on-and-on-and-on")
        self.assertLessEqual(len(c), 40)
        self.assertRegex(c, r"^[a-z0-9._-]+$")


class PersonalDataTests(unittest.TestCase):
    def assertBlocked(self, extra: str, needle: str) -> None:
        reasons = cc.check(with_body(extra))
        self.assertTrue(any(needle in r for r in reasons), (extra, reasons))

    def test_an_email_address_blocks(self) -> None:
        self.assertBlocked("Write to someone at jane.doe@example.org for help.", "email address")

    def test_an_at_handle_blocks(self) -> None:
        self.assertBlocked("Say hi to @janedoe about it.", "@-handle")

    def test_a_phone_number_blocks(self) -> None:
        for phone in ("+381 64 123 4567", "(555) 123-4567", "555.123.4567"):
            self.assertBlocked(f"Call {phone} to ask.", "phone number")

    def test_an_ip_address_blocks(self) -> None:
        self.assertBlocked("The server sits at 192.168.10.24.", "IP address")
        self.assertBlocked("Or at 2001:db8:85a3::8a2e:370:7334 instead.", "IP address")

    def test_a_street_address_blocks(self) -> None:
        self.assertBlocked("The office is at 221 Baker Street.", "street address")
        self.assertBlocked("Post it to PO Box 12.", "street address")

    def test_dates_and_plain_numbers_are_not_phones(self) -> None:
        self.assertEqual(cc.check(with_body(
            "A code holds up to 2953 bytes, and it was checked on 2026-09-25.")), [])


class NameAllowlistTests(unittest.TestCase):
    def test_a_persons_name_mid_sentence_blocks_and_is_named(self) -> None:
        reasons = cc.check(with_body("This tip came from Jane Doe, who uses it daily."))
        self.assertTrue(any("'Jane Doe'" in r for r in reasons), reasons)

    def test_a_place_and_a_workplace_block(self) -> None:
        reasons = cc.check(with_body("It is popular in Belgrade and at Acme Corp too."))
        named = " ".join(reasons)
        self.assertIn("'Belgrade'", named)
        self.assertIn("'Acme Corp'", named)

    def test_a_name_at_the_start_of_a_sentence_is_not_vouched_for_by_position(self) -> None:
        reasons = cc.check(with_body("Marko uses the check every morning."))
        self.assertTrue(any("'Marko'" in r for r in reasons), reasons)

    def test_a_name_after_an_abbreviation_still_blocks(self) -> None:
        reasons = cc.check(with_body("Ask a friend, e.g. Milica, to try it."))
        self.assertTrue(any("'Milica'" in r for r in reasons), reasons)

    def test_a_name_in_a_heading_or_in_code_blocks(self) -> None:
        reasons = cc.check(with_body("## Lessons from Petrovic"))
        self.assertTrue(any("'Petrovic'" in r for r in reasons), reasons)
        reasons = cc.check(with_body('```json\n{"from": "Jovana Jovanovic"}\n```'))
        self.assertTrue(any("Jovana" in r for r in reasons), reasons)

    def test_a_name_in_the_title_or_description_blocks(self) -> None:
        reasons = cc.check(clean(title="How Dragana verifies email addresses"))
        self.assertTrue(any("'Dragana'" in r for r in reasons), reasons)

    def test_a_common_word_may_open_a_sentence(self) -> None:
        self.assertEqual(cc.check(with_body("Disposable inboxes vanish. Verify first.")), [])

    def test_the_allowlist_holds_no_internal_system(self) -> None:
        names = cc.load_allowlist() | cc.load_openers()
        for internal in cc.INTERNAL_NAMES:
            self.assertNotIn(internal.lower(), names)
        self.assertIn("dokaz", names)
        self.assertNotIn("scrooge", names)

    def test_an_allowlist_that_lists_an_internal_system_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "allow.json"
            path.write_text(json.dumps({"brand": ["Dokaz", "Scrooge"]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                cc.load_allowlist(path)


class BusinessNumberTests(unittest.TestCase):
    def assertBlocked(self, extra: str) -> None:
        reasons = cc.check(with_body(extra))
        self.assertTrue(any("business numbers" in r for r in reasons), (extra, reasons))

    def test_a_first_person_revenue_claim_blocks(self) -> None:
        self.assertBlocked("Last month our revenue doubled.")
        self.assertBlocked("We made enough to cover the servers.")

    def test_a_first_person_customer_count_blocks(self) -> None:
        self.assertBlocked("We have 500 customers who rely on it.")
        self.assertBlocked("Over two thousand developers use it every week.")

    def test_a_money_amount_blocks(self) -> None:
        self.assertBlocked("It costs $9 a month.")

    def test_figures_that_are_not_about_the_business_pass(self) -> None:
        self.assertEqual(cc.check(with_body(
            "Our check scores an address from 0 to 100 in one request.")), [])


class InternalNameTests(unittest.TestCase):
    def test_an_internal_system_name_blocks(self) -> None:
        for name in ("Scrooge", "Moss", "Pionir", "Atani"):
            reasons = cc.check(with_body(f"This post was planned by {name}."))
            self.assertTrue(any(f"internal system {name!r}" in r for r in reasons),
                            (name, reasons))

    def test_an_internal_name_in_the_slug_or_tags_blocks(self) -> None:
        reasons = cc.check(clean(slug="how-scrooge-counts", tags=["galatea"]))
        self.assertTrue(any("slug names the internal system" in r for r in reasons), reasons)
        self.assertTrue(any("tags names the internal system" in r for r in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
