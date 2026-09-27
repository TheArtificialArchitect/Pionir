"""The website worker's sandbox: what Claude may do while building, and what a site must be
before it is zipped for the owner.

Claude is never run: the one test of the real command lines injects a fake
``subprocess.run``. Each test fails if the rule it names is reverted: a tool other than Write
available to the build, a tool at all available to the review, the owner's settings or MCP
servers loaded, a working directory that is not empty and deleted, the API key passed
through, a link followed out of the build directory, or a site with a script, an event
handler, an external or embedded resource, an invented link or contact, or placeholder text
passing the checks.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from pionir.crew.escalation import (
    MAX_SITE_FILES,
    REVIEW_DENIED,
    SITE_DENIED,
    claude_review_runner,
    claude_site_runner,
    collect_site,
    review_argv,
    site_argv,
)
from pionir.crew.fiverr import site
from pionir.crew.fiverr.checks import Guard, load_guard

# Built from pieces so no provider-shaped key sits in the source (GitHub push protection).
FAKE_STRIPE = "sk_" + "live_" + "ABCDEFGHIJKLMNOP123456"

BRIEF = ("Rosa Bakes, a bakery in Leeds. Sourdough, cakes. Email hello@rosabakes.co.uk, "
         "phone 0113 496 0000. Instagram: https://www.instagram.com/rosabakes")
HTML = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Rosa Bakes</title>'
        '<link rel="stylesheet" href="styles.css"></head><body><header><nav>'
        '<a href="#about">About</a></nav></header><main><section id="about"><h1>Rosa Bakes'
        '</h1><p>Sourdough and cakes in Leeds.</p><p><a href="mailto:hello@rosabakes.co.uk">'
        'hello@rosabakes.co.uk</a> or <a href="tel:+441134960000">0113 496 0000</a></p>'
        '<p><a href="https://www.instagram.com/rosabakes">Instagram</a></p></section></main>'
        '<footer>Rosa Bakes</footer></body></html>')
CSS = "body{font-family:system-ui,sans-serif;background:linear-gradient(#fff,#fdf6ee)}"
GUARD = Guard(markers=("c:\\users\\owner-home",))


def good(**changes) -> dict:
    files = {"index.html": HTML, "styles.css": CSS}
    files.update(changes)
    return {k: v for k, v in files.items() if v is not None}


class SandboxRuleTests(unittest.TestCase):
    """Every rule fails closed; each case would pass if its rule were removed."""

    def assertRefused(self, files, words) -> None:
        reasons = site.validate_site(files, BRIEF, GUARD)
        self.assertTrue(any(words in r for r in reasons), reasons)

    def test_a_good_site_passes(self) -> None:
        self.assertEqual(site.validate_site(good(), BRIEF, GUARD), [])

    def test_scripts_in_every_form_are_refused(self) -> None:
        for html, words in [
            (HTML.replace("</main>", "<script>alert(1)</script></main>"), "<script>"),
            (HTML.replace("</main>", "<SCRIPT src=x.js></SCRIPT></main>"), "<script>"),
            (HTML.replace("<h1>", '<h1 onclick="steal()">'), "event-handler"),
            (HTML.replace("<body>", '<body onload="x()">'), "event-handler"),
            (HTML.replace("#about", "javascript:alert(1)"), "javascript:"),
            (HTML.replace("#about", "java\tscript:alert(1)"), "not https"),
            (HTML.replace("#about", "&#106;avascript:alert(1)"), "javascript:"),
            (HTML.replace("</main>", '<iframe src="https://evil.test"></iframe></main>'),
             "<iframe>"),
            (HTML.replace("</main>", '<object data="x.swf"></object></main>'), "<object>"),
            (HTML.replace("</main>", '<form action="https://evil.test"></form></main>'),
             "<form>"),
            (HTML.replace("<head>", '<head><meta http-equiv="refresh" content="0;url=x">'),
             "http-equiv"),
            (HTML.replace("<head>", '<head><base href="https://evil.test/">'), "<base>"),
            (HTML.replace("</main>", "<!-- a comment --></main>"), "comment"),
            (HTML.replace("<h1>", '<h1 id="a" id="b">'), "repeats"),
            (HTML.replace("<h1>", '<h1 data-x="1">'), "not an allowed attribute"),
        ]:
            with self.subTest(words=words, html=html[-80:]):
                self.assertRefused(good(**{"index.html": html}), words)

    def test_the_review_found_bypasses_are_closed(self) -> None:
        # image-set() in a style attribute: Edge fetches it
        self.assertRefused(good(**{"index.html": HTML.replace(
            "<h1>", "<h1 style=\"background-image:image-set('https://evil.test/x' 1x)\">")}),
            "image-set()")
        for fn in ("-webkit-image-set", "image", "cross-fade", "element"):
            with self.subTest(fn=fn):
                self.assertRefused(good(**{"styles.css": f"b{{background:{fn}('x.png')}}"}),
                                   f"{fn}()")
        # an entity-encoded meta refresh to a local path: refused, and the decoded path and
        # the internal name it hid are seen too
        sneaky = HTML.replace("<head>", '<head><meta http-equiv="&#114;efresh" content="0;'
                              'url=file:///&#67;&#58;/&#85;sers/owner-home/&#112;ionir">')
        reasons = site.validate_site(good(**{"index.html": sneaky}), BRIEF, GUARD)
        text = " ".join(reasons)
        self.assertIn("http-equiv", text)
        self.assertIn("internal system", text)
        self.assertTrue("local path" in text or "personal data" in text, reasons)
        # CSS escapes and comments cannot spell a function past the check
        for css in ("b{background:u\\72 l(https://evil.test/x)}",
                    "b{background:u\\000072l(x)}",
                    "b{content:'/*'; background:url(x); content:'*/'}",
                    "@font-face{font-family:x}", "@\\69mport 'x.css';"):
            with self.subTest(css=css):
                self.assertTrue(site.validate_site(good(**{"styles.css": css}), BRIEF, GUARD))
        # a property off the list is refused even with a harmless value
        self.assertRefused(good(**{"styles.css": "b{mask-image:none}"}), "property")

    def test_external_and_embedded_resources_are_refused(self) -> None:
        for files, words in [
            (good(**{"index.html": HTML.replace("</main>", '<img src="https://cdn.test/a.png">'
                                                "</main>")}), "<img>"),
            (good(**{"index.html": HTML.replace("</main>", '<img src="logo.png"></main>')}),
             "<img>"),
            (good(**{"index.html": HTML.replace(
                'href="styles.css"', 'href="https://fonts.googleapis.com/css2?family=X"')}),
             "own .css files"),
            (good(**{"index.html": HTML.replace('rel="stylesheet"', 'rel="preload"')}),
             "own .css files"),
            (good(**{"styles.css": "@import url(https://fonts.test/x.css);"}), "@import"),
            (good(**{"styles.css": "body{background:url(https://evil.test/p.png)}"}), "url()"),
            (good(**{"styles.css": "body{background:url(data:image/png;base64,AAA)}"}),
             "url()"),
            (good(**{"index.html": HTML.replace("<h1>", '<h1 style="background:url(x.png)">')}),
             "url()"),
            (good(**{"styles.css": "div{behavior:url(x.htc)}"}), "script hook"),
            (good(**{"index.html": HTML.replace("</main>", '<svg><use xlink:href="https://'
                                                "evil.test/s.svg#i\"></use></svg></main>")}),
             "<svg>"),
        ]:
            with self.subTest(words=words):
                self.assertRefused(files, words)

    def test_only_html_and_css_files_with_plain_names(self) -> None:
        self.assertRefused(good(**{"app.js": "alert(1)"}), "not an allowed file")
        self.assertRefused(good(**{"logo.svg": "<svg/>"}), "not an allowed file")
        self.assertRefused(good(**{"../escape.html": HTML}), "not an allowed file")
        self.assertRefused(good(**{"index.html": None}), "no index.html")
        many = {f"p{i}.css": CSS for i in range(site.MAX_FILES + 1)}
        self.assertRefused(good(**many), "files were written")

    def test_links_are_checked(self) -> None:
        for bad, words in [
            ('href="#about"', "#nowhere", ),
            ('href="#about"', "missing.html"),
            ('href="https://www.instagram.com/rosabakes"', "https://www.facebook.com/rosa"),
            ('href="https://www.instagram.com/rosabakes"', "http://www.instagram.com/rosabakes"),
            ('href="https://www.instagram.com/rosabakes"', "//www.instagram.com/rosabakes"),
            ('href="mailto:hello@rosabakes.co.uk"', "mailto:orders@rosabakes.co.uk"),
            ('href="tel:+441134960000"', "tel:+441134969999"),
        ]:
            html = HTML.replace(bad, f'href="{words}"')
            with self.subTest(link=words):
                reasons = site.validate_site(good(**{"index.html": html}), BRIEF, GUARD)
                self.assertTrue(reasons, words)

    def test_hosts_and_addresses_match_exactly_never_as_substrings(self) -> None:
        for bad in ("https://www.instagram.com/rosabakes-evil",       # a longer path
                    "https://notinstagram.com/rosabakes",             # a longer host
                    "https://instagram.com.evil.test/rosabakes",      # a prefix host
                    "https://www.instagram.com/someone-else",
                    "mailto:ello@rosabakes.co.uk",                    # a piece of the address
                    "mailto:hello@rosabakes.co"):
            with self.subTest(link=bad):
                html = HTML.replace('href="https://www.instagram.com/rosabakes"',
                                    f'href="{bad}"')
                html = html.replace('href="mailto:hello@rosabakes.co.uk"', f'href="{bad}"')
                self.assertTrue(site.validate_site(good(**{"index.html": html}), BRIEF, GUARD))
        # a proper subdomain of a host the brief names, with no path given, is fine
        brief = BRIEF + " Our site: rosabakes.co.uk"
        html = HTML.replace('href="https://www.instagram.com/rosabakes"',
                            'href="https://shop.rosabakes.co.uk/cakes"')
        self.assertEqual(site.validate_site(good(**{"index.html": html}), brief, GUARD), [])
        visible = HTML.replace("Sourdough and cakes in Leeds.", "Write to lo@rosabakes.co.uk")
        self.assertRefused(good(**{"index.html": visible}), "email address the request")

    def test_a_page_link_resolves_and_its_fragment_exists(self) -> None:
        about = HTML.replace('id="about"', 'id="story"').replace('href="#about"',
                                                                  'href="#story"')
        ok = good(**{"about.html": about,
                     "index.html": HTML.replace('href="#about"', 'href="about.html#story"')})
        self.assertEqual(site.validate_site(ok, BRIEF, GUARD), [])
        bad = dict(ok, **{"index.html": HTML.replace('href="#about"',
                                                     'href="about.html#nope"')})
        self.assertTrue(site.validate_site(bad, BRIEF, GUARD))

    def test_invented_contacts_and_placeholders_are_refused(self) -> None:
        self.assertRefused(good(**{"index.html": HTML.replace(
            "Sourdough and cakes in Leeds.", "Call 0113 496 1234 or orders@rosabakes.co.uk")}),
            "the request does not give")
        self.assertRefused(good(**{"index.html": HTML.replace(
            "Sourdough and cakes in Leeds.", "Lorem ipsum dolor sit amet")}), "placeholder")
        self.assertRefused(good(**{"index.html": HTML.replace(
            "Sourdough and cakes in Leeds.", "[Your address here]")}), "placeholder")

    def test_the_estates_checks_run_on_the_site(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "k.txt").write_text(FAKE_STRIPE, encoding="utf-8")
            guard = load_guard(Path(d))
        reasons = site.validate_site(
            good(**{"styles.css": CSS + "/* " + FAKE_STRIPE + " */"}), BRIEF, guard)
        self.assertTrue(any("secret" in r or "Stripe" in r for r in reasons), reasons)
        reasons = site.validate_site(good(**{"index.html": HTML.replace(
            "Sourdough and cakes in Leeds.", "Made with Pionir")}), BRIEF, GUARD)
        self.assertTrue(any("internal system" in r for r in reasons), reasons)
        reasons = site.validate_site(good(**{"index.html": HTML.replace(
            "Sourdough and cakes in Leeds.", "C:\\Users\\owner-home\\site")}), BRIEF, GUARD)
        self.assertTrue(any("personal data" in r or "local path" in r for r in reasons))

    def test_the_review_answer_must_be_the_json_asked_for(self) -> None:
        self.assertEqual(site.parse_review('{"pass": true, "issues": []}'), (True, []))
        self.assertEqual(site.parse_review('```json\n{"pass": false, "issues": ["x"]}\n```'),
                         (False, ["x"]))
        self.assertEqual(site.parse_review('{"pass": true, "issues": ["an issue"]}')[0], False)
        self.assertIsNone(site.parse_review("Looks great!")[0])
        self.assertIsNone(site.parse_review('{"pass": "yes"}')[0])

    def test_the_package_is_a_zip_with_a_readme_that_passes_the_delivery_checks(self) -> None:
        from pionir.adapters.deliveries import SecretValues, inspect_zip
        with tempfile.TemporaryDirectory() as d:
            z = site.package(good(), Path(d))
            names = zipfile.ZipFile(z).namelist()
            self.assertEqual(sorted(names), ["README.txt", "index.html", "styles.css"])
            inspect_zip(z, SecretValues())                      # raises on any problem


class _Done:
    def __init__(self, stdout="", returncode=0, stderr="") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class RunnerTests(unittest.TestCase):
    def test_the_build_can_only_write_and_the_review_can_do_nothing(self) -> None:
        argv = site_argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "Write")
        # only writes UNDER the working directory are allowed: never a bare Write
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Write(./**)")
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "PowerShell", "Read", "Edit", "WebFetch", "WebSearch", "Agent"):
            self.assertIn(tool, denied)
        self.assertEqual(tuple(denied), SITE_DENIED)
        for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence",
                     "--disable-slash-commands"):
            self.assertIn(flag, argv)
        # acceptEdits: the one mode a live probe (escalation.SITE_MODE's note) showed both
        # writes inside the working directory and refuses one outside it; dontAsk denies
        # Write outright, and nothing wider (bypassPermissions, auto) is ever used
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv.count("--permission-mode"), 1)
        for wide in ("bypassPermissions", "auto", "--dangerously-skip-permissions",
                     "--allow-dangerously-skip-permissions", "--add-dir"):
            self.assertNotIn(wide, argv)
        review = review_argv()
        self.assertEqual(review[review.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--restricted", review)
        self.assertEqual(review[review.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", review)
        self.assertIn("Write", review[review.index("--disallowedTools") + 1].split(","))
        self.assertEqual(tuple(review[review.index("--disallowedTools") + 1].split(",")),
                         REVIEW_DENIED)

    def test_the_build_runs_in_an_empty_deleted_directory_without_the_api_key(self) -> None:
        seen = {}

        def run(argv, **kw):
            cwd = Path(kw["cwd"])
            seen.update(argv=argv, cwd=cwd, empty=list(cwd.iterdir()) == [],
                        env=kw["env"], input=kw["input"])
            (cwd / "index.html").write_text(HTML, encoding="utf-8")
            (cwd / "styles.css").write_text(CSS, encoding="utf-8")
            return _Done(json.dumps({"type": "result", "subtype": "success",
                                     "result": "DONE"}))

        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-should-not-pass"
        try:
            out = json.loads(claude_site_runner("the prompt", 5.0, run=run))
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertTrue(seen["empty"])
        self.assertFalse(seen["cwd"].exists())                  # deleted afterwards
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])
        self.assertEqual(seen["input"], "the prompt")           # on stdin, never argv
        self.assertNotIn("the prompt", seen["argv"])
        self.assertEqual(out["files"], {"index.html": HTML, "styles.css": CSS})

    def test_what_the_build_leaves_is_read_boundedly_and_links_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("the owner's file", encoding="utf-8")
            (Path(d) / "index.html").write_text(HTML, encoding="utf-8")
            try:
                os.symlink(secret, Path(d) / "leak.html")
                linked = True
            except OSError:
                linked = False                                  # no symlink rights here
            (Path(d) / "big.css").write_bytes(b"a" * 300_000)
            (Path(d) / "bin.html").write_bytes(b"\xff\xfe\x00")
            got = collect_site(d)
        self.assertEqual(sorted(got["files"]), ["index.html"])
        problems = " ".join(got["problems"])
        self.assertIn("too big", problems)
        self.assertIn("not UTF-8", problems)
        if linked:
            self.assertIn("leak.html", problems)
            self.assertNotIn("the owner's file", json.dumps(got))
        self.assertGreater(MAX_SITE_FILES, 2)

    def test_no_claude_run_gets_an_api_key_a_token_or_another_endpoint(self) -> None:
        from unittest import mock

        from pionir.crew import escalation
        seen = []

        def run(argv, **kw):
            seen.append(kw["env"])
            if argv[:2] == ["claude", "-p"] and "--output-format" not in argv:
                return _Done("DONE")                     # the plain escalation runner
            return _Done(json.dumps({"subtype": "success", "result": "DONE"}))

        leak = {"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://proxy.evil.test", "KEEP_ME": "yes"}
        with mock.patch.dict(os.environ, leak):
            claude_site_runner("p", 5.0, run=run)
            claude_review_runner("p", 5.0, run=run)
            escalation.claude_research_runner("p", 5.0, run=run)
            with mock.patch.object(escalation.subprocess, "run",
                                   side_effect=lambda argv, **kw: run(argv, **kw)):
                escalation.claude_cli_runner("p", 5.0)
        self.assertEqual(len(seen), 4)
        for env in seen:
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
                self.assertNotIn(name, env)
            self.assertEqual(env["KEEP_ME"], "yes")

    def test_a_failed_run_raises_and_the_review_gets_no_tools(self) -> None:
        with self.assertRaises(RuntimeError):
            claude_site_runner("p", 5.0, run=lambda argv, **kw: _Done("", 1, "boom"))
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            return _Done(json.dumps({"subtype": "success", "result": '{"pass": true}'}))

        self.assertEqual(claude_review_runner("p", 5.0, run=run), '{"pass": true}')
        self.assertEqual(seen["argv"], review_argv())


if __name__ == "__main__":
    unittest.main()
