"""The dashboard's Mail tab, read as source: each rule fails if it is reverted.

Sender-written text is hostile input. The tab must set it with textContent only, show no
link or anchor, fetch nothing from inside a message, show a failure as a failure rather than
an empty inbox, and touch only the two GET routes.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "pionir"


def _mail_js() -> str:
    html = (SRC / "web" / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("// ---- Mail:")
    end = html.index("// ---- end Mail ----")
    return html[start:end]


def _mail_markup() -> str:
    html = (SRC / "web" / "dashboard.html").read_text(encoding="utf-8")
    start = html.index('<div id="view-mail"')
    return html[start:html.index('<div id="view-brain">')]


class SenderTextIsInertTests(unittest.TestCase):
    def test_nothing_is_written_as_markup(self) -> None:
        js = _mail_js()
        self.assertNotRegex(js, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|createContextualFragment|DOMParser|eval\(|new Function")

    def test_sender_text_goes_in_through_textContent(self) -> None:
        js = _mail_js()
        self.assertIn(".textContent=text", js)
        for field in ("u.from", "u.subject", "u.body", "u.to"):
            self.assertIn(field, js)

    def test_links_are_text_not_anchors(self) -> None:
        js = _mail_js()
        self.assertNotRegex(js, r"""createElement\(\s*["']a["']|mailEl\(\s*["']a["']|href|window\.open|location\s*=|\.src\s*=|<img|<a\b""")
        markup = _mail_markup()
        self.assertNotRegex(markup, r"<a\b|href=|<img|<iframe|<form")

    def test_nothing_inside_a_message_is_fetched(self) -> None:
        js = _mail_js()
        urls = re.findall(r'mailGet\(([^)]*)\)', js)
        self.assertEqual(len(urls), 3)  # the helper's definition + the two routes
        self.assertEqual(len(re.findall(r"\bfetch\(", js)), 1)
        self.assertNotRegex(js, r"\bXMLHttpRequest\b|\bsendBeacon\b|\bWebSocket\b")

    def test_attachments_are_named_never_opened(self) -> None:
        js = _mail_js()
        self.assertIn("names only", js)
        self.assertNotRegex(js, r"Blob|createObjectURL|download=|\.download\b|atob\(")


class FailureIsShownTests(unittest.TestCase):
    def test_a_failed_inbox_is_not_drawn_as_an_empty_one(self) -> None:
        js = _mail_js()
        failed = js[js.index("if(!res.ok){"):js.index("const msgs=")]
        self.assertIn("could not be read, so nothing is listed", failed)
        self.assertIn("mailState(mailWhy(res.j,res.status),true)", failed)
        self.assertNotIn("The inbox is empty", failed)

    def test_ok_needs_the_server_to_say_ok_true(self) -> None:
        js = _mail_js()
        self.assertRegex(js, r"ok:\s*r\.ok\s*&&\s*!!j\s*&&\s*j\.ok\s*===\s*true")

    def test_every_state_has_its_plain_words(self) -> None:
        js = _mail_js()
        for state in ("not_configured", "unavailable", "not_found"):
            self.assertIn(f'state==="{state}"', js)
        self.assertIn("j.error", js)

    def test_a_network_error_is_a_failure_too(self) -> None:
        js = _mail_js()
        self.assertGreaterEqual(js.count("Pionir is not answering"), 2)

    def test_the_failure_line_is_marked_bad(self) -> None:
        self.assertIn('"mail-state"+(bad?" bad":"")', _mail_js())


class ReadOnlyTests(unittest.TestCase):
    def test_only_the_two_get_routes(self) -> None:
        js = _mail_js()
        routes = set(re.findall(r'"(/api/[^"?]*)', js))
        self.assertEqual(routes, {"/api/mail/inbox", "/api/mail/message/"})
        self.assertNotRegex(js, r"POST|PUT|DELETE|PATCH|method\s*:")

    def test_there_is_no_send_or_delete_control(self) -> None:
        markup = _mail_markup().lower()
        self.assertNotRegex(markup, r"\bsend\b|\bdelete\b|\breply\b|\bforward\b|\barchive\b|<textarea|<input")
        self.assertIn('id="mail-refresh"', markup)

    def test_the_tab_is_wired_and_only_polls_while_visible(self) -> None:
        html = (SRC / "web" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('data-view="mail"', html)
        self.assertRegex(html, r'\$id\("view-mail"\)\.hidden\s*=\s*v!=="mail"')
        self.assertIn('if(v==="mail") loadMail();', html)
        self.assertRegex(_mail_js(), r"!\$id\(\"view-mail\"\)\.hidden\s*&&\s*!document\.hidden")


if __name__ == "__main__":
    unittest.main()
