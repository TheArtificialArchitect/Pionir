"""No client's work leaves the machine without the owner's yes - and never with a secret in it.

``client.deliver`` parks on every call, runs once after an approval and never after a
denial. Before it is parked the zip must pass every check (a README, no path tricks, no
executables, no files that hold secrets, none of the owner's real secret values or any
common key format, the pinned sha256, not too big, not a bomb); on approval it is all
checked again from the bytes on disk, then uploaded, emailed and marked delivered. These
tests pin each rule, the upload -> email -> status sequence and its failure answers, the
download link never reaching a log or the audit ledger in full, the Discord card, the
``deliveries`` command, and the real urllib opener against a real loopback server.

Scrooge is faked at the HTTP opener with the real opener's signature; nothing touches the
network, and the owner's real secrets are never read (every secrets folder is a temp one).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from test_client_adapter import (
    CLIENT,
    OPS_TOKEN,
    ORDER_ID,
    SCROOGE,
    FakeScrooge,
    _Response,
    _settings,
)
from test_discord_gate import API, CHANNEL, OWNER, FakeDiscord
from test_discord_gate import TOKEN as DISCORD_TOKEN

from pionir import cli
from pionir.adapters import deliveries
from pionir.adapters.clients import (
    CHECK_BEFORE_RETRY,
    DELIVER,
    EMAIL,
    NOT_EMAILED,
    TOKEN_REJECTED,
    ClientAdapter,
    ClientSettings,
    check_delivery,
    client_settings,
)
from pionir.adapters.deliveries import load_secrets
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.discord_gate import (
    DiscordGate,
    DiscordGateSettings,
    client_deliver_line,
    render_request,
)
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp

LINK = "https://api.dokaz.net/d/" + hashlib.sha256(b"the private link").hexdigest()
ZIP_NAME = "signup-setup.zip"
PLANTED = "PLANTED-api-key-9f8e7d6c5b4a"
INSTAGRAM_TOKEN = "IGQVJ-instagram-long-lived-token-0042"
ELSEWHERE_TOKEN = "configured-elsewhere-token-7788"
SSH_LINE = "QyNTUxOQAAACDfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeAAAAJhfakefake"
SSH_KEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
           "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
           f"{SSH_LINE}\n"
           "-----END OPENSSH PRIVATE KEY-----\n")
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakePublicKeyValueForTests owner@host"

GOOD_FILES: dict[str, bytes] = {
    "README.md": b"# Your signup form check\n\nRun install.ps1, then open the form.\n",
    "src/": b"",
    "src/check.py": b"import re\n\nprint('checking addresses')\n",
    "src/install.ps1": b"Write-Host 'installing the check'\n",
    "docs/public-key.txt": PUBLIC_KEY.encode(),   # a PUBLIC key is not a secret
}
BODY = ("Hi Sam,\n\nYour signup form setup is ready. Download it here (the link works for "
        "7 days):\n{link}\n\nThe README inside says how to install it.\n\nBest,\nIan at "
        "Dokaz\n")


def make_zip(files: dict[str, bytes], *, symlink: str | None = None,
             raw_names: dict[str, str] | None = None) -> bytes:
    """A zip of ``files`` (a name ending in / is a folder). ``symlink`` adds a symlink
    entry; ``raw_names`` renames entries AFTER zipfile's own clean-up (backslashes)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 25, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in (raw_names or {}):
                info.filename = info.orig_filename = raw_names[name]
            if name.endswith("/"):
                info.external_attr = (0o40755 << 16) | 0x10
            archive.writestr(info, data)
        if symlink:
            info = zipfile.ZipInfo(symlink, date_time=(2026, 9, 25, 0, 0, 0))
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"/etc/passwd")
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def a_delivery(data: bytes, **over: Any) -> dict[str, Any]:
    base = {"order_id": ORDER_ID, "to": CLIENT, "zip_name": ZIP_NAME,
            "zip_sha256": sha(data), "subject": "Your signup form setup is ready",
            "body_text": BODY}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


class DeliverScrooge(FakeScrooge):
    """FakeScrooge plus Scrooge's delivery routes (the contract built in parallel)."""

    def __init__(self) -> None:
        super().__init__()
        self.uploads: list[bytes] = []
        self.revoked: list[str] = []
        self.stored_sha: str | None = None       # what Scrooge claims it stored

    def _handle(self, request: Any, timeout: float | None) -> _Response:
        url = request.full_url
        parts = urllib.parse.urlsplit(url)
        step = {"/dash/orders/delivery": "delivery",
                "/dash/orders/delivery/revoke": "revoke"}.get(parts.path)
        if step is None:
            return super()._handle(request, timeout)
        raw = request.data or b""
        self.calls.append({
            "step": step, "method": request.get_method(), "url": url,
            "query": dict(urllib.parse.parse_qsl(parts.query)), "raw": raw,
            "body": json.loads(raw) if step == "revoke" else None, "timeout": timeout,
            "token": request.get_header("X-dash-token"),
            "content_type": request.get_header("Content-type"),
        })
        if self.down:
            raise urllib.error.URLError(f"connection refused: {url} token={OPS_TOKEN}")
        if request.get_header("X-dash-token") not in self.valid_tokens:
            self._error(url, 401, {"ok": False, "error": "unauthorized"})
        if step in self.forced:
            status, payload = self.forced[step]
            if status >= 400:
                self._error(url, status, payload)
            return _Response(status, payload)
        if step == "revoke":
            self.revoked.append(self.calls[-1]["body"]["delivery_id"])
            return _Response(200, {"ok": True, "revoked": True})
        self.uploads.append(raw)
        return _Response(200, {"ok": True, "delivery_id": f"dlv{len(self.uploads)}",
                               "url": LINK, "sha256": self.stored_sha or sha(raw),
                               "size": len(raw), "expires_at": "2026-10-02T00:00:00Z"})


class _Case(unittest.TestCase):
    """A hermetic runtime; the client adapter talks to DeliverScrooge, reads a temp
    deliveries folder and temp secrets (never the owner's real ones)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        self.token_file = self.secrets / "scrooge-ops-token.txt"
        self.token_file.write_text(OPS_TOKEN + "\n", encoding="utf-8")
        (self.secrets / "planted.txt").write_text(PLANTED + "\n", encoding="utf-8")
        (self.secrets / "instagram.json").write_text(json.dumps(
            {"access_token": INSTAGRAM_TOKEN, "user_id": "1784"}), encoding="utf-8")
        self.elsewhere = self.root / "elsewhere" / "other-token.txt"
        self.elsewhere.parent.mkdir()
        self.elsewhere.write_text(ELSEWHERE_TOKEN, encoding="utf-8")
        self.ssh = self.root / "ssh"
        self.ssh.mkdir()
        (self.ssh / "id_ed25519").write_text(SSH_KEY, encoding="utf-8")
        (self.ssh / "id_ed25519.pub").write_text(PUBLIC_KEY, encoding="utf-8")
        self.deliveries = self.root / "deliveries"
        self.folder = self.deliveries / ORDER_ID
        self.folder.mkdir(parents=True)
        self.world = DeliverScrooge()
        self.adapter = ClientAdapter(ClientSettings(
            base_url=SCROOGE, token_file=self.token_file, deliveries_dir=self.deliveries,
            secrets_dir=self.secrets, secret_files=(self.elsewhere,), ssh_dir=self.ssh),
            opener=self.world)
        runtime = build_runtime(_settings(self.root / "state", content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)
        self.data = self.put(GOOD_FILES)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def put(self, files: dict[str, bytes], name: str = ZIP_NAME, **kw: Any) -> bytes:
        data = make_zip(files, **kw)
        (self.folder / name).write_bytes(data)
        return data

    def park(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        out = self.app.run_task(DELIVER, payload or a_delivery(self.data),
                                permissions=[DELIVER])
        self.assertEqual(out["status"], "pending_approval", out)
        return out

    def approve(self, out: dict[str, Any]) -> dict[str, Any]:
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(out["approval_id"])

    def deliver(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way a delivery goes out."""
        return self.approve(self.park(payload))

    def execute(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return dict(self.adapter.execute(Task(DELIVER, payload or a_delivery(self.data)))
                    .output)


# ---- the gate ---------------------------------------------------------------------------
class GateTests(_Case):
    def test_the_declaration(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        cap = caps[DELIVER]
        self.assertIs(cap.risk, RiskLevel.PRIVILEGED)
        self.assertTrue(cap.requires_approval)
        self.assertFalse(cap.routable)
        self.assertEqual(cap.required_permissions, frozenset({DELIVER}))

    def test_it_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            self.park()
        pending = self.app.approvals.pending()
        self.assertEqual(len(pending), 3)
        self.assertIn(f"order {ORDER_ID}", pending[0]["summary"])
        self.assertEqual(self.world.calls, [])

    def test_an_approved_delivery_uploads_emails_and_marks_delivered_once(self) -> None:
        row = self.deliver()
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), ["delivery", "email", "status"])
        upload, email, status = self.world.calls
        self.assertEqual(upload["method"], "POST")
        self.assertEqual(upload["url"].split("?")[0], f"{SCROOGE}/dash/orders/delivery")
        self.assertEqual(upload["query"], {"order_id": ORDER_ID, "filename": ZIP_NAME})
        self.assertEqual(upload["raw"], self.data)             # exactly the approved bytes
        self.assertEqual(upload["content_type"], "application/zip")
        self.assertEqual(upload["token"], OPS_TOKEN)
        self.assertEqual(upload["timeout"], 180)
        self.assertEqual(email["body"], {"order_id": ORDER_ID, "to": CLIENT,
                                         "subject": "Your signup form setup is ready",
                                         "body_text": BODY.replace("{link}", LINK)})
        self.assertEqual(email["timeout"], 30)
        self.assertEqual(status["body"], {"order_id": ORDER_ID, "status": "delivered"})
        self.assertEqual(row["result"]["result"], {
            "ok": True, "delivery_id": "dlv1", "url": LINK,
            "expires_at": "2026-10-02T00:00:00Z", "emailed": True, "status": "delivered"})
        self.assertIn("client:delivery:dlv1", row["result"]["evidence"])
        self.assertFalse(self.app.approve(row["id"])["ok"])       # never twice
        self.assertEqual(len(self.world.calls), 3)

    def test_a_denied_delivery_never_runs(self) -> None:
        aid = self.park()["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.world.calls, [])

    def test_a_missing_token_is_unavailable_before_parking(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
            self.adapter.validate(Task(DELIVER, a_delivery(self.data)))
        out = self.app.run_task(DELIVER, a_delivery(self.data), permissions=[DELIVER])
        self.assertEqual(out["error"]["type"], "AdapterUnavailable")
        self.assertEqual(self.app.approvals.pending(), [])


# ---- the checks before parking --------------------------------------------------------
PE = b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0" + b"\0" * 64
# (the words the refusal uses for it) -> a sample. Each sample hits its own pattern.
KEY_SAMPLES = {
    "a private key block": b"-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n",
    "an AWS access key id": b"aws_key = AKI" + b"AIOSFODNN7EXAMPLE\n",
    "a Stripe live secret key": b"STRIPE=sk_live" + b"_4eC39HqLyjWDarjtT1zdp7dc\n",
    "a Stripe live restricted key": b"STRIPE=rk_live" + b"_51H8abcdEFGH1234\n",
    "an Anthropic API key": b"key: sk-ant" + b"-api03-AbCdEfGhIjKlMnOp\n",
    "a GitHub token (ghp_": b"token ghp" + b"_16C7e42F292c6912E7710c838347Ae178B4a\n",
    "a GitHub token (github_pat_": b"github_pat" + b"_11ABCDEFG0123456789_abcdefghijklmnop\n",
    "a Slack token": b"SLACK=xoxb" + b"-1234567890-abcdefghijkl\n",
    "a Google API key": b"maps: AIza" + b"SyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI" + b"\n",
    "a Discord bot token":
        b"DISCORD=MTk" + b"4NjIyNDgzNDcxOTI1MjQ4.Cl2FMQ.ZnCjm1XVW7vRze4b7Cq4se7kKWs\n",
    "a webhook signing secret": b"WEBHOOK=whsec" + b"_MfKQ9r8GKYqrTwjUPD8ILPZIo2La\n",
    "a JWT": b"auth: ey" + b"JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl\n",
}


class ZipRuleTests(_Case):
    def refused(self, files: dict[str, bytes] | None = None, *, payload: Any = None,
                **kw: Any) -> str:
        """Each rule: refused by validate() (so never parked), refused at execute()."""
        data = self.put(files, **kw) if files is not None else self.data
        payload = payload or a_delivery(data)
        with self.assertRaises(AdapterProtocolError) as err:
            self.adapter.validate(Task(DELIVER, payload))
        message = str(err.exception)
        self.assertIn("client.deliver refused by Pionir", message)
        out = self.app.run_task(DELIVER, payload, permissions=[DELIVER])
        self.assertEqual(out["status"], "error", out)
        self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])
        return message

    def test_the_good_zip_passes(self) -> None:
        self.adapter.validate(Task(DELIVER, a_delivery(self.data)))
        for name in ("readme.txt", "HOWTO.md", "Howto.TXT", "README.MD"):
            with self.subTest(name):
                files = {k: v for k, v in GOOD_FILES.items() if k != "README.md"}
                data = self.put({name: b"how to use it\n", **files})
                self.adapter.validate(Task(DELIVER, a_delivery(data)))

    def test_a_readme_or_howto_at_the_top_level_is_required(self) -> None:
        files = {k: v for k, v in GOOD_FILES.items() if k != "README.md"}
        self.assertIn("no README or HOWTO", self.refused(files))
        self.assertIn("no README or HOWTO",
                      self.refused({**files, "docs/README.md": b"nested\n"}))
        self.assertIn("no README or HOWTO",
                      self.refused({**files, "README.pdf": b"%PDF\n"}))

    def test_a_zip_of_one_project_folder_passes(self) -> None:
        # the shape most people make: right-click a folder, "compress" - project/README.md
        data = self.put({f"signup-check/{k}": v for k, v in GOOD_FILES.items()})
        self.adapter.validate(Task(DELIVER, a_delivery(data)))
        # but two top-level folders with the README in one of them is not "the top level"
        files = {k: v for k, v in GOOD_FILES.items() if k != "README.md"}
        self.assertIn("no README or HOWTO",
                      self.refused({**{f"a/{k}": v for k, v in files.items()},
                                    "b/README.md": b"which project?\n"}))

    def test_a_service_token_held_in_settings_is_scanned_for(self) -> None:
        from pionir.adapters.deliveries import DeliveryProblem, inspect_zip, load_secrets
        token = "daedalus-" + "q7" * 12
        data = make_zip({**GOOD_FILES, "config.py": f"TOKEN = '{token}'\n".encode()})
        path = Path(self._tmp.name) / "scan-me.zip"
        path.write_bytes(data)
        secrets = load_secrets(None, (), None, (("the Daedalus token", token),))
        with self.assertRaises(DeliveryProblem) as caught:
            inspect_zip(path, secrets, pinned_sha256=hashlib.sha256(data).hexdigest(),
                        root=path.parent)
        self.assertIn("the Daedalus token", str(caught.exception))
        self.assertNotIn(token, str(caught.exception))

    def test_path_tricks(self) -> None:
        cases = {
            "..": ({**GOOD_FILES, "../evil.txt": b"x"}, {}, "climbs out"),
            "a nested ..": ({**GOOD_FILES, "src/../../evil.txt": b"x"}, {}, "climbs out"),
            "an absolute path": ({**GOOD_FILES, "/etc/evil.txt": b"x"}, {}, "absolute"),
            "a drive letter": ({**GOOD_FILES, "C:/evil.txt": b"x"}, {}, "drive letter"),
            "a backslash": ({**GOOD_FILES, "x.txt": b"x"}, {"x.txt": "..\\evil.txt"},
                            "backslash"),
            "a control character": ({**GOOD_FILES, "a\x01b.txt": b"x"}, {},
                                    "control character"),
        }
        for label, (files, raw, words) in cases.items():
            with self.subTest(label):
                self.assertIn(words, self.refused(files, raw_names=raw))

    def test_no_symlinks(self) -> None:
        self.assertIn("symlink", self.refused(GOOD_FILES, symlink="src/link"))

    def test_no_executables_or_archives(self) -> None:
        for name in ("tool.exe", "lib/x.DLL", "setup.msi", "a.scr", "b.com", "run.bat",
                     "run.cmd", "x.vbs", "app.jar"):
            with self.subTest(name):
                self.assertIn("executable", self.refused({**GOOD_FILES, name: b"x"}))
        with self.subTest("a Windows executable under another name"):
            self.assertIn("Windows executable",
                          self.refused({**GOOD_FILES, "notes.txt": PE}))
        with self.subTest("ELF"):
            self.assertIn("ELF", self.refused({**GOOD_FILES, "run": b"\x7fELF\x02\x01"}))
        with self.subTest("a zip inside"):
            self.assertIn("archive inside",
                          self.refused({**GOOD_FILES, "more.zip": make_zip(GOOD_FILES)}))

    def test_files_that_hold_secrets(self) -> None:
        for name in (".env", "app/.env", ".env.local", ".ENV.production", "id_rsa",
                     "keys/id_rsa.pub", "server.pem", "tls.key", ".npmrc", ".pypirc",
                     "credentials", "aws/credentials.json", ".git/config", "src/.git/HEAD"):
            with self.subTest(name):
                self.refused({**GOOD_FILES, name: b"harmless\n"})
        # scripts are fine: .py, .ps1 and .js are what clients are sent
        data = self.put({**GOOD_FILES, "a.js": b"x", "b.sh": b"x", ".envrc.md": b"x"})
        self.adapter.validate(Task(DELIVER, a_delivery(data)))

    def test_the_owners_real_secret_values_block_it_by_file_never_by_value(self) -> None:
        cases = {
            "a planted secret": (PLANTED, self.secrets / "planted.txt"),
            "the ops token": (OPS_TOKEN, self.token_file),
            "a JSON secret's value": (INSTAGRAM_TOKEN, self.secrets / "instagram.json"),
            "a configured token file elsewhere": (ELSEWHERE_TOKEN, self.elsewhere),
            "a line of an SSH private key": (SSH_LINE, self.ssh / "id_ed25519"),
        }
        for label, (value, source) in cases.items():
            with self.subTest(label):
                message = self.refused({**GOOD_FILES, "src/config.py":
                                        f"API_KEY = '{value}'\n".encode()})
                self.assertIn(str(source), message)                 # WHICH secret file
                self.assertIn("'src/config.py'", message)           # and where
                self.assertNotIn(value, message)                    # never the value
        with self.subTest("in the entry's name"):
            message = self.refused({**GOOD_FILES, f"{PLANTED}.txt": b"x"})
            self.assertIn("planted.txt", message)

    def test_every_common_key_format(self) -> None:
        for label, sample in KEY_SAMPLES.items():
            with self.subTest(label):
                message = self.refused({**GOOD_FILES, "notes/setup.txt": sample})
                self.assertIn(f"something shaped like {label}", message)
                self.assertNotIn(sample.decode().strip(), message)
                matched = [p for _l, p in deliveries.KEY_PATTERNS if p.search(sample)]
                self.assertEqual(len(matched), 1, label)     # each pattern is needed
        self.assertEqual(len(KEY_SAMPLES), len(deliveries.KEY_PATTERNS))
        # a README that only NAMES a key format passes
        data = self.put({**GOOD_FILES, "notes.md": b"Put your sk_live_ key and your "
                                                    b"ghp_ token in the settings page.\n"})
        self.adapter.validate(Task(DELIVER, a_delivery(data)))

    def test_the_pinned_sha_size_and_shape(self) -> None:
        self.assertIn("not the pinned", self.refused(
            payload=a_delivery(self.data, zip_sha256=sha(b"other"))))
        (self.folder / ZIP_NAME).write_bytes(b"\0" * (deliveries.MAX_ZIP_BYTES + 1))
        self.assertIn("too big", self.refused(payload=a_delivery(self.data)))
        junk = b"this is not a zip at all"
        (self.folder / ZIP_NAME).write_bytes(junk)
        self.assertIn("not a zip", self.refused(payload=a_delivery(junk)))
        (self.folder / ZIP_NAME).unlink()
        self.assertIn("no file at", self.refused(payload=a_delivery(junk)))

    def test_a_zip_bomb(self) -> None:
        self.assertIn("zip bomb", self.refused({**GOOD_FILES,
                                                "big.txt": b"\0" * 3_000_000}))
        with mock.patch.object(deliveries, "MAX_UNCOMPRESSED", 1000):
            self.assertIn("zip bomb", self.refused({**GOOD_FILES, "data.txt": b"a" * 2000}))

    def test_the_payload(self) -> None:
        bad = {
            "no {link}": {"body_text": BODY.replace("{link}", "soon")},
            "{link} twice": {"body_text": BODY + "Again: {link}\n"},
            "a path in zip_name": {"zip_name": "sub/x.zip"},
            "a backslash in zip_name": {"zip_name": "sub\\x.zip"},
            "..": {"zip_name": "..zip"},
            "an inner ..": {"zip_name": "a..zip"},      # only the '..' rule catches this
            "not a zip name": {"zip_name": "x.exe"},
            "a bad sha": {"zip_sha256": "ABC"},
            "an uppercase sha": {"zip_sha256": sha(self.data).upper()},
            "an extra field": {"cc": "x@evil.example"},
            "a missing field": {"subject": None},
            "a foreign link": {"body_text": BODY + "See https://evil.example.com/x\n"},
            "html": {"body_text": BODY + "<b>hi</b>\n"},
            "a bad order id": {"order_id": "../../x"},
        }
        for label, over in bad.items():
            with self.subTest(label):
                payload = a_delivery(self.data, **over)
                with self.assertRaises(ValueError):
                    check_delivery(payload)
                self.refused(payload=payload)
                with self.assertRaises(AdapterProtocolError):      # and at execute()
                    self.adapter.execute(Task(DELIVER, payload))
        self.assertEqual(check_delivery(a_delivery(self.data))["body_text"], BODY)


# ---- at execute: everything again, then Scrooge ------------------------------------------
class ExecuteTests(_Case):
    def test_a_zip_changed_after_parking_is_refused_at_execute(self) -> None:
        out = self.park()
        self.put({**GOOD_FILES, "src/extra.py": b"print('added later')\n"})
        row = self.approve(out)
        self.assertEqual(row["status"], "approved_failed")
        result = row["result"]["result"]
        self.assertIn("not the pinned", result["refused"])
        self.assertIn("nothing was uploaded or emailed", result["refused"])
        self.assertEqual(self.world.calls, [])

    def test_a_secret_added_after_parking_is_refused_at_execute(self) -> None:
        # the zip does not change, but a value in it becomes a secret after parking
        data = self.put({**GOOD_FILES, "x.txt": b"brand-new-secret-xxxxxxxx"})
        out = self.park(a_delivery(data))
        (self.secrets / "new-token.txt").write_text("brand-new-secret-xxxxxxxx",
                                                    encoding="utf-8")
        row = self.approve(out)
        self.assertEqual(row["status"], "approved_failed")
        self.assertIn("new-token.txt", row["result"]["result"]["refused"])
        self.assertEqual(self.world.calls, [])

    def test_scrooge_storing_other_bytes_is_refused_and_revoked(self) -> None:
        self.world.stored_sha = sha(b"something else")
        row = self.deliver()
        self.assertEqual(row["status"], "approved_failed")
        result = row["result"]["result"]
        self.assertIn("different bytes", result["refused"])
        self.assertIn("revoked", result["refused"])
        self.assertTrue(result["revoked"])
        self.assertEqual(self.world.steps(), ["delivery", "revoke"])   # never emailed
        self.assertEqual(self.world.revoked, ["dlv1"])
        self.assertNotIn(LINK, json.dumps(row))

    def test_a_revoke_that_fails_says_so(self) -> None:
        self.world.stored_sha = sha(b"something else")
        self.world.forced = {"revoke": (500, {"ok": False, "error": "boom"})}
        out = self.execute()
        self.assertIn("could NOT be revoked", out["refused"])
        self.assertIn("dlv1", out["refused"])
        self.assertFalse(out["revoked"])

    def test_a_link_that_is_not_a_dokaz_download_link_is_revoked(self) -> None:
        self.world.forced = {"delivery": (200, {
            "ok": True, "delivery_id": "dlv9", "url": "https://evil.example/d/" + "0" * 64,
            "sha256": sha(self.data), "size": len(self.data), "expires_at": "x"})}
        out = self.execute()
        self.assertIs(out["ok"], False)
        self.assertIn("not a Dokaz download link", out["unavailable"])
        self.assertEqual(self.world.steps(), ["delivery", "revoke"])

    def test_an_email_failure_after_the_upload_never_claims_delivery(self) -> None:
        cases = [
            (409, {"ok": False, "error": "to: does not match the order"}, "refused", None),
            (503, {"ok": False, "error": "mail not configured"}, "unavailable",
             "nothing was sent"),
            (500, {"ok": False, "error": "boom"}, "unavailable", CHECK_BEFORE_RETRY),
        ]
        for status, body, kind, words in cases:
            with self.subTest(status=status):
                self.world.calls.clear()
                self.world.forced = {"email": (status, body)}
                out = self.execute()
                self.assertIs(out["ok"], False)
                self.assertIs(out["emailed"], False)
                self.assertIn(NOT_EMAILED, out[kind])
                self.assertIn(NOT_EMAILED, out["error"])
                self.assertNotIn("status", out)
                self.assertEqual(out["uploaded"]["url"], LINK)     # the owner needs it
                self.assertEqual(out["uploaded"]["sha256"], sha(self.data))
                if words:
                    self.assertIn(words, out[kind])
                self.assertEqual(self.world.steps(), ["delivery", "email"])  # no status
        row = self.deliver()                                     # through the gate
        self.assertEqual(row["status"], "approved_failed")
        self.assertIn(NOT_EMAILED, row["result"]["result"]["error"])

    def test_a_status_failure_after_the_email_is_still_delivered(self) -> None:
        self.world.forced = {"status": (409, {"ok": False, "error": "status: no"})}
        out = self.execute()
        self.assertIs(out["ok"], True)
        self.assertIs(out["emailed"], True)
        self.assertNotIn("status", out)
        self.assertIn("status: no", out["status_error"])
        self.assertIn("was emailed the link", out["status_error"])

    def test_the_upload_mapping(self) -> None:
        cases = [
            (409, {"ok": False, "error": "order is not paid"}, "refused", "order is not paid"),
            (400, {"ok": False, "error": "filename: bad"}, "refused", "filename: bad"),
            (413, {"ok": False, "error": "too large"}, "refused", "too big"),
            (401, {"ok": False, "error": "no"}, "unavailable", TOKEN_REJECTED),
            (403, {"ok": False, "error": "no"}, "unavailable", TOKEN_REJECTED),
            (429, {"ok": False, "error": "slow"}, "unavailable", "rate limiting"),
            (500, {"ok": False, "error": "boom"}, "unavailable", "nothing was emailed"),
            (200, {"stored": True}, "unavailable", "without ok: true"),
            (200, {"ok": True, "url": LINK}, "unavailable", "no usable delivery_id"),
        ]
        for status, body, kind, words in cases:
            with self.subTest(status=status, body=body):
                self.world.calls.clear()
                self.world.forced = {"delivery": (status, body)}
                out = self.execute()
                self.assertIs(out["ok"], False)
                self.assertIn(words, out[kind])
                self.assertEqual(self.world.steps(), ["delivery"])   # never emailed
        self.world.forced = {}
        self.world.down = True
        out = self.execute()
        self.assertIn("unreachable", out["unavailable"])
        self.assertIn("nothing was emailed", out["unavailable"])

    def test_the_link_never_reaches_a_log_or_the_audit_ledger_in_full(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        try:
            ok = self.deliver()
            self.world.forced = {"email": (500, {"ok": False, "error": f"echo {LINK}"})}
            failed = self.deliver()
            self.world.forced = {"status": (500, {"ok": False, "error": f"echo {LINK}"})}
            lagging = self.deliver()
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        # the results carry it - the owner needs the link
        self.assertEqual(ok["result"]["result"]["url"], LINK)
        self.assertEqual(failed["result"]["result"]["uploaded"]["url"], LINK)
        self.assertEqual(lagging["result"]["result"]["url"], LINK)
        logs = capture.getvalue()
        self.assertIn(f"...{LINK[-6:]}", logs)                  # the log shows its tail
        self.assertNotIn(LINK, logs)
        self.assertNotIn(LINK[:-6], logs)
        audit = (self.root / "state" / "audit" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("client", audit)
        self.assertNotIn(LINK[-40:], audit)
        for row in (ok, failed, lagging):
            self.assertNotIn(LINK[-40:], row["summary"])
        # the answers that describe a failure do not repeat it (they reach the ledger)
        self.assertNotIn(LINK[-40:], failed["result"]["result"]["error"])
        self.assertNotIn(LINK[-40:], lagging["result"]["result"]["status_error"])
        self.assertIn("echo <link>", lagging["result"]["result"]["status_error"])


# ---- the Discord card --------------------------------------------------------------------
class CardTests(_Case):
    def gate(self) -> tuple[DiscordGate, FakeDiscord]:
        token_file = self.root / "discord-bot-token.txt"
        token_file.write_text(DISCORD_TOKEN, encoding="utf-8")
        fake = FakeDiscord()
        gate = DiscordGate.for_app(
            self.app,
            DiscordGateSettings(state_root=self.root / "state", channel_id=CHANNEL,
                                owner_user_id=OWNER, token_file=token_file, api_base=API,
                                poll_seconds=0.01),
            opener=fake, sleep=lambda _s: None)
        return gate, fake

    def test_the_card_shows_the_files_the_scan_and_the_whole_email(self) -> None:
        self.park()
        gate, fake = self.gate()
        self.assertTrue(gate.run_once())
        posts = [p["content"] for p in fake.posts()]
        head, text = posts[0], "\n".join(posts)
        self.assertEqual(head.split("\n")[0], client_deliver_line(CLIENT))
        self.assertEqual(head.split("\n")[0],
                         "\U0001f4e6 **DELIVERS TO A CLIENT** - the zip below is uploaded "
                         f"to a private link and emailed to `{CLIENT}` if you approve.")
        self.assertIn(f"**Order:** `{ORDER_ID}`", text)
        self.assertIn(f"**To:** `{CLIENT}`", text)
        self.assertIn(f"`{ZIP_NAME}` - {len(self.data):,} bytes", text)
        self.assertIn(f"sha256 `{sha(self.data)[:16]}`", text)
        files = [n for n in GOOD_FILES if not n.endswith("/")]
        self.assertIn(f"**The files in it ({len(files)}), in full:**", text)
        for name in files:
            self.assertIn(f"{name}  ({len(GOOD_FILES[name]):,} bytes)", text)
        values = len(load_secrets(self.secrets, (self.elsewhere, self.token_file),
                                  self.ssh))
        self.assertGreaterEqual(values, 5)
        self.assertIn(f"secrets scan: clean ({len(files)} files, {values} secret values "
                      "checked)", text)
        self.assertIn(BODY.replace("{link}", "<private download link>"), text)
        self.assertNotIn("{link}", text.split("**Full payload:**")[0])
        self.assertEqual(self.world.calls, [])              # showing it sends nothing
        for secret in (OPS_TOKEN, PLANTED, INSTAGRAM_TOKEN, SSH_LINE):
            self.assertNotIn(secret, text)

    def test_a_zip_changed_after_parking_turns_the_card_into_do_not_approve(self) -> None:
        self.park()
        self.put({**GOOD_FILES, "src/extra.py": b"later\n"})
        gate, fake = self.gate()
        self.assertTrue(gate.run_once())
        head = fake.posts()[0]["content"]
        self.assertIn("DO NOT APPROVE", head)
        self.assertIn("not the pinned", head)
        self.assertNotIn("secrets scan: clean", head)

    def test_without_an_inspector_the_card_says_it_could_not_look(self) -> None:
        row = {"id": "a1", "capability": DELIVER, "payload": a_delivery(self.data),
               "summary": "s"}
        text = render_request(row, OWNER)
        self.assertIn("could not be inspected", text)
        self.assertNotIn("secrets scan: clean", text)


# ---- python -m pionir deliveries ---------------------------------------------------------
class DeliveriesCommandTests(_Case):
    def run_cli(self) -> tuple[int, dict[str, Any], str]:
        settings = _settings(self.root / "state", deliveries_dir=self.deliveries,
                             ops_token_file=self.token_file)
        with mock.patch("pionir.adapters.clients.client_settings",
                        lambda _c: self.adapter.settings), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.deliveries(settings)
        return code, json.loads(printed.getvalue()), printed.getvalue()

    def test_it_lists_each_zip_and_whether_it_passes(self) -> None:
        self.put({**GOOD_FILES, "src/config.py": f"KEY='{PLANTED}'".encode()},
                 name="leaky.zip")
        self.put({k: v for k, v in GOOD_FILES.items() if k != "README.md"},
                 name="no-readme.zip")
        (self.deliveries / "not-an-order").mkdir()
        code, report, printed = self.run_cli()
        self.assertEqual(code, 0, printed)
        self.assertEqual(report["status"], "ok")
        folders = {row["order_id"]: row for row in report["orders"]}
        self.assertEqual(set(folders), {ORDER_ID, "not-an-order"})
        bad_folder, order = folders["not-an-order"], folders[ORDER_ID]
        self.assertNotIn("problem", order)
        self.assertIn("not an order id", bad_folder["problem"])
        zips = {z["name"]: z for z in order["zips"]}
        self.assertEqual(set(zips), {ZIP_NAME, "leaky.zip", "no-readme.zip"})
        self.assertTrue(zips[ZIP_NAME]["passes"])
        self.assertEqual(zips[ZIP_NAME]["sha256"], sha(self.data))
        self.assertEqual(zips[ZIP_NAME]["size"], len(self.data))
        self.assertFalse(zips["leaky.zip"]["passes"])
        self.assertIn("planted.txt", zips["leaky.zip"]["problem"])
        self.assertIn("no README", zips["no-readme.zip"]["problem"])
        self.assertNotIn(PLANTED, printed)
        self.assertNotIn(OPS_TOKEN, printed)
        self.assertEqual(self.world.calls, [])

    def test_no_folder_and_the_wiring(self) -> None:
        empty = ClientAdapter(ClientSettings(deliveries_dir=self.root / "none",
                                             secrets_dir=None, ssh_dir=None))
        code, report = empty.list_deliveries()
        self.assertEqual((code, report["status"]), (1, "empty"))
        with mock.patch.object(cli, "deliveries", return_value=0) as command:
            self.assertEqual(cli.main(["deliveries"]), 0)
        command.assert_called_once_with()


# ---- settings --------------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_defaults_env_and_the_scanned_token_files(self) -> None:
        settings = PionirSettings(state_root=Path("C:/x"))
        self.assertEqual(settings.deliveries_path, Path.home() / ".pionir" / "deliveries")
        self.assertEqual(settings.secrets_path, Path.home() / ".pionir" / "secrets")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"PIONIR_STATE_ROOT": root,
                         "PIONIR_DELIVERIES_DIR": str(Path(root) / "d"),
                         "PIONIR_DISCORD_TOKEN_FILE": str(Path(root) / "bot.txt")}
        ):
            configured = PionirSettings.from_environment()
            self.assertEqual(configured.deliveries_path, Path(root) / "d")
            made = client_settings(configured)
        self.assertEqual(made.deliveries_dir, Path(root) / "d")
        self.assertEqual(made.secrets_dir, Path.home() / ".pionir" / "secrets")
        self.assertEqual(made.ssh_dir, Path.home() / ".ssh")
        for path in (configured.content_token_path, configured.instagram_token_path,
                     configured.devto_key_path, Path(root) / "bot.txt"):
            self.assertIn(path, made.secret_files)
        self.assertEqual(made.token_file, configured.ops_token_path)

    def test_the_link_placeholder_is_checked_with_a_real_length_link(self) -> None:
        # a body that fits only because {link} is short is refused: the real link is long
        body = "Hi, get it here: {link} " + "x" * (5000 - 24)
        self.assertEqual(len(body), 5000)
        with self.assertRaisesRegex(ValueError, "body_text: 20-5000"):
            check_delivery({"order_id": ORDER_ID, "to": CLIENT, "zip_name": ZIP_NAME,
                            "zip_sha256": "0" * 64, "subject": "Your files",
                            "body_text": body})
        self.assertEqual(EMAIL, "client.email")


# ---- the real opener -------------------------------------------------------------------------
class _Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, Any]]]

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urllib.parse.urlsplit(self.path).path
        record = {"method": self.command, "path": self.path, "body": body,
                  "headers": {k.lower(): v for k, v in self.headers.items()}}
        type(self).seen.append(record)
        if record["headers"].get("x-dash-token") != OPS_TOKEN:
            status, out = 401, {"ok": False, "error": "unauthorized"}
        elif path == "/dash/orders/delivery":
            status, out = 200, {"ok": True, "delivery_id": "d1", "url": LINK,
                                "sha256": sha(body), "size": len(body),
                                "expires_at": "2026-10-02T00:00:00Z"}
        elif path == "/dash/orders/email":
            status, out = 200, {"ok": True, "id": "m1"}
        elif path == "/dash/orders/status":
            status, out = 200, {"ok": True, **json.loads(body)}
        else:
            status, out = 404, {"ok": False, "error": "not found"}
        raw = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_POST = _reply

    def log_message(self, *_a: Any) -> None:
        pass


class RealOpenerTests(unittest.TestCase):
    """The adapter's real urllib opener against a real loopback HTTP server: the zip goes
    up as raw bytes with the timeout by keyword, then the email and the status."""

    def test_the_delivery_through_the_real_opener(self) -> None:
        seen: list[dict[str, Any]] = []
        handler = type("Handler", (_Recorder,), {"seen": seen})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "secrets" / "scrooge-ops-token.txt"
            token.parent.mkdir()
            token.write_text(OPS_TOKEN, encoding="utf-8")
            data = make_zip(GOOD_FILES)
            (root / "deliveries" / ORDER_ID).mkdir(parents=True)
            (root / "deliveries" / ORDER_ID / ZIP_NAME).write_bytes(data)
            adapter = ClientAdapter(ClientSettings(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}", token_file=token,
                deliveries_dir=root / "deliveries", secrets_dir=root / "secrets",
                ssh_dir=None))
            adapter.validate(Task(DELIVER, a_delivery(data)))
            out = dict(adapter.execute(Task(DELIVER, a_delivery(data))).output)
        self.assertEqual(out, {"ok": True, "delivery_id": "d1", "url": LINK,
                               "expires_at": "2026-10-02T00:00:00Z", "emailed": True,
                               "status": "delivered"})
        self.assertEqual([(r["method"], urllib.parse.urlsplit(r["path"]).path)
                          for r in seen],
                         [("POST", "/dash/orders/delivery"), ("POST", "/dash/orders/email"),
                          ("POST", "/dash/orders/status")])
        upload = seen[0]
        self.assertEqual(upload["body"], data)
        self.assertEqual(upload["headers"]["content-type"], "application/zip")
        self.assertEqual(upload["headers"]["x-dash-token"], OPS_TOKEN)
        self.assertEqual(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
            upload["path"]).query)), {"order_id": ORDER_ID, "filename": ZIP_NAME})
        self.assertEqual(json.loads(seen[1]["body"])["body_text"],
                         BODY.replace("{link}", LINK))


if __name__ == "__main__":
    unittest.main()
