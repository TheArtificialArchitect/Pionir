"""Mending a draft so it passes the content check, never loosening the check (postfix.py).

The shapes here are the ones measured on 2026-10-07 in five real gemma3:12b blog drafts (all
five blocked) and in the blog record's 23 blocked drafts. Each test fails if its rule is
reverted - and the last ones fail if a mend ever lets through what the check refuses.
"""

import unittest

from pionir.crew import contentcheck, postfix
from pionir.crew.blog import SEEDS
from pionir.crew.registry import default_registry

T0 = 1_790_000_000.0

INTRO = ("Sharing contact details by hand is slow, and a scanned code is quicker for "
         "everyone who needs them.\n\n## Why it helps\n\nPeople type fewer details by hand, "
         "so fewer mistakes reach the address book. A code on a card or a flyer works "
         "anywhere a phone camera does, and it keeps working long after the event.\n\n")


def draft(body: str) -> dict:
    return {"title": "Share contact details with a code",
            "slug": "share-contact-details",
            "description": "How a scanned code can hand over contact details without anyone "
                           "typing them in by hand.",
            "body_md": INTRO + body, "tags": ["qr"]}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = default_registry().require("posting.blog")

    def check(self, raw: dict) -> list:
        d = self.worker.assemble(raw, SEEDS[5], {"used_slugs": []}, T0)
        return self.worker.check_draft(d), d


class AlwaysTests(Case):
    def test_a_vcard_code_block_is_removed_and_the_draft_passes(self) -> None:
        # live: 16 "names 'BEGIN' / 'FN' / 'TEL' ..." reasons and a javascript:/data: refusal
        body = ("The format looks like this:\n\n```\nBEGIN:VCARD\nVERSION:3.0\n"
                "FN:{full-name}\nTEL;TYPE=CELL:{mobile-phone}\nEND:VCARD\n```\n\n"
                "The API builds that text for you.\n")
        reasons, d = self.check(draft(body))
        self.assertEqual(reasons, [])
        self.assertNotIn("BEGIN", d["body_md"])
        self.assertNotIn("The format looks like this:", d["body_md"])
        self.assertIn("The API builds that text for you.", d["body_md"])
        self.assertTrue(any("removed a code block that is not JSON" in r
                            for r in d["repaired"]))

    def test_a_json_block_keeps_its_shape_and_loses_its_example_data(self) -> None:
        # live: "Acme Corp", "123 Main St", "INV-2024-001" written as example values
        body = ('```json\n{"Company": "Acme Corp", "street": "123 Main St", '
                '"invoice_number": "INV-2024-001", "phone": 5551234567, "qty": 2}\n```\n')
        reasons, d = self.check(draft(body))
        self.assertEqual(reasons, [])
        for gone in ("Acme", "Main St", "INV-2024", "5551234567"):
            self.assertNotIn(gone, d["body_md"])
        for kept in ('"company": "{company}"', '"invoice_number": "{invoice-number}"',
                     '"qty": 2'):
            self.assertIn(kept, d["body_md"])

    def test_an_html_tag_named_in_backticks_loses_its_brackets(self) -> None:
        reasons, d = self.check(draft("The tags sit in the `<head>` part of a page.\n"))
        self.assertEqual(reasons, [])
        self.assertIn("in the `head` part", d["body_md"])

    def test_title_case_labels_are_made_sentence_case_where_the_check_would_vouch(self) -> None:
        # live: "Streamlined Onboarding", "Extractive Summarisation", "Identifying Key
        # Sentences" - Title Case habit inside a bold list label, each a block
        body = ("- **Extractive Summarisation:** picks sentences.\n"
                "- **Key Sentences First:** read the key sentences before the rest.\n"
                "Extractive summarisation keeps the original wording.\n")
        reasons, d = self.check(draft(body))
        self.assertEqual(reasons, [])
        self.assertIn("- **Extractive summarisation:**", d["body_md"])
        self.assertIn("- **Key sentences first:**", d["body_md"])

    def test_a_label_ending_in_data_colon_is_not_a_data_url(self) -> None:
        # live: "1. **Prepare the vCard data:** ensure ..." - "data:**" read as a data: URL
        raw = draft("1. **Prepare the vCard data:** check every field.\n"
                    "2. **Send it:** one request.\n")
        reasons, d = self.check(raw)
        self.assertEqual(reasons, [])
        self.assertIn("1. **Prepare the vCard data**: check", d["body_md"])
        self.assertIn("2. **Send it:** one request.", d["body_md"])     # only where needed
        self.assertTrue(contentcheck.check(dict(raw, draft_id="2026-10-07-x")))   # unmended

    def test_allowlisted_names_in_a_label_keep_their_capitals(self) -> None:
        reasons, d = self.check(draft("- **Use The QR Code API:** one request.\n"))
        self.assertEqual(reasons, [])
        self.assertIn("**Use the QR Code API:**", d["body_md"])


class NeverLoosensTests(Case):
    """What the check refuses, a mend by rule never lets through."""

    def test_a_name_in_a_label_is_still_refused(self) -> None:
        for body in ("- **Ask Marko Petrovic:** he knows.\n",
                     "- **Call Jane Doe:** she knows.\n",
                     "## Lessons From Acme Corp\n\nThey shipped it.\n",
                     "- **Built With Wappalyzer:** a scan.\n"):
            reasons, _d = self.check(draft(body))
            self.assertTrue(reasons, body)

    def test_a_tag_with_attributes_and_a_real_url_still_block(self) -> None:
        for body in ('Write `<meta name="generator">` in the page.\n',
                     "Read https://mailcheck-tools.com/docs first.\n",
                     "Mail jane@realmail.com for help.\n"):
            reasons, _d = self.check(draft(body))
            self.assertTrue(reasons, body)


class FromReasonsTests(Case):
    def test_the_checks_own_literals_become_placeholders_and_the_draft_passes(self) -> None:
        raw = draft("Mail jane@realmail.com or quote INV-2024-001, or ask @sales.\n")
        reasons, _d = self.check(raw)
        self.assertTrue(reasons)
        fixed, done = postfix.placeholders_from_reasons(raw, ("title", "description",
                                                              "body_md"), reasons)
        self.assertTrue(done)
        again, d = self.check(fixed)
        self.assertEqual(again, [])
        self.assertIn("{email-address}", d["body_md"])
        self.assertIn("{reference-number}", d["body_md"])
        self.assertIn("{handle}", d["body_md"])

    def test_a_named_person_is_not_a_placeholder_job(self) -> None:
        raw = draft("A friend, Jane Doe, swears by it.\n")
        reasons, _d = self.check(raw)
        fixed, done = postfix.placeholders_from_reasons(raw, ("body_md",), reasons)
        self.assertEqual((fixed, done), (raw, []))


class SentenceRepairTests(unittest.TestCase):
    def test_only_the_flagged_sentences_are_sent_and_replaced(self) -> None:
        raw = {"body_md": "## Using Firefox\n\nOpen the tools. Press F12 in Firefox to see "
                          "them.\n\n- **Streamlined Onboarding:** fast.\n\n```json\n"
                          '{"a": "Firefox"}\n```\n'}
        reasons = ["names 'Firefox', which is not on the allowlist",
                   "names 'F12', which is not on the allowlist",
                   "names 'Onboarding', which is not on the allowlist"]
        units = postfix.flagged_units(raw, ("body_md",), reasons)
        self.assertEqual([u["text"] for u in units],
                         ["Using Firefox", "Press F12 in Firefox to see them.",
                          "**Streamlined Onboarding:** fast."])
        prompt = postfix.rewrite_prompt(units, reasons)
        self.assertNotIn("Open the tools.", prompt)
        fixed, done = postfix.apply_rewrites(raw, units, {"rewrites": [
            {"id": 1, "text": "Using the browser"},
            {"id": 2, "text": "Press the developer tools key to see them."},
            {"id": 3, "text": "Not\na single line"},          # refused: not one line
            {"id": 9, "text": "unknown id"}]})
        self.assertEqual(len(done), 2)
        self.assertTrue(fixed["body_md"].startswith("## Using the browser\n\nOpen the tools. "
                                                    "Press the developer tools key"))
        self.assertIn("**Streamlined Onboarding:** fast.", fixed["body_md"])
        self.assertIn('{"a": "Firefox"}', fixed["body_md"])      # code is not prose

    def test_single_capitals_and_schemes_are_found_too(self) -> None:
        # live 2026-10-07: QR error-correction levels written "L, M, Q or H" (four names),
        # and a vCard post's "data:" - neither quoted as a word of two letters
        raw = {"body_md": "Pick level L or M for print. Most codes are fine.\n"
                          "The photo goes in as data:image for the card.\n"}
        reasons = ["names 'L', which is not on the allowlist",
                   "names 'M', which is not on the allowlist",
                   "body_md has a data: URL"]
        units = postfix.flagged_units(raw, ("body_md",), reasons)
        self.assertEqual([u["text"] for u in units],
                         ["Pick level L or M for print.",
                          "The photo goes in as data:image for the card."])

    def test_list_fields_are_repaired_in_place(self) -> None:
        raw = {"points": ["Ask Jane Doe today.", "Fine as it is."]}
        units = postfix.flagged_units(raw, ("points",), ["names 'Jane Doe', which is not"])
        fixed, done = postfix.apply_rewrites(raw, units, {"rewrites": [
            {"id": 1, "text": "Ask a colleague today."}]})
        self.assertEqual(fixed["points"], ["Ask a colleague today.", "Fine as it is."])
        self.assertEqual(raw["points"][0], "Ask Jane Doe today.")      # the input is kept
        self.assertEqual(len(done), 1)


if __name__ == "__main__":
    unittest.main()
