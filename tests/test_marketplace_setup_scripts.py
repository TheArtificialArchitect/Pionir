"""tools\\setup-apify.ps1 and tools\\setup-chrome-webstore.ps1: what the owner runs, once.

They talk to real accounts, so these tests read them: Windows PowerShell 5.1 only, the secret
read hidden and never printed, saved only AFTER one read-only check passed, with a user-only
ACL, in the file the adapters read, and the Chrome item record in the file the packager reads.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
APIFY = TOOLS / "setup-apify.ps1"
CHROME = TOOLS / "setup-chrome-webstore.ps1"


def code_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class SetupScriptTests(unittest.TestCase):
    def test_windows_powershell_5_1_only(self) -> None:
        for path in (APIFY, CHROME):
            code = code_of(path)
            for op in ("&&", "||", "??", "?."):
                self.assertNotIn(op, code, f"{path.name}: {op}")
            self.assertIsNone(re.search(r"\)\s*\?\s*[^\s]+\s*:", code), path.name)

    def test_the_secret_is_hidden_checked_then_saved_owner_only(self) -> None:
        for path, check in ((APIFY, "/v2/users/me"), (CHROME, "oauth2.googleapis.com/token")):
            code = code_of(path)
            self.assertIn("-AsSecureString", code, path.name)
            self.assertIn(check, code, path.name)
            self.assertLess(code.index(check), code.index("WriteAllText"), path.name)
            self.assertIn("/inheritance:r /grant:r", code, path.name)
            for line in code.splitlines():
                if "Write-Host" in line or "Write-Ok" in line:
                    self.assertNotRegex(line, r"\$(token|clientSecret|refresh)\b", path.name)

    def test_the_files_are_the_ones_pionir_reads(self) -> None:
        self.assertIn(r".pionir\secrets\apify-token.txt", code_of(APIFY))
        chrome = code_of(CHROME)
        self.assertIn(r".pionir\secrets\chrome-webstore.json", chrome)
        self.assertIn("chrome-items.json", chrome)
        self.assertIn("PIONIR_MARKETPLACES_DIR", chrome)
        for key in ("client_id", "client_secret", "refresh_token", "publisher_id"):
            self.assertIn(key, chrome)
        self.assertIn("https://www.googleapis.com/auth/chromewebstore", chrome)
        self.assertIn(":fetchStatus", chrome)              # the check is a read
        self.assertNotIn(":publish", chrome)
        self.assertNotIn(":upload", chrome)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell")
    def test_they_parse_in_windows_powershell(self) -> None:
        for path in (APIFY, CHROME):
            command = ("$t = $null; $e = $null; [void][System.Management.Automation.Language."
                       f"Parser]::ParseFile('{path}', [ref]$t, [ref]$e); $e.Count")
            done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                   "-Command", command], capture_output=True, text=True,
                                  timeout=120, check=False)
            self.assertEqual(done.stdout.strip(), "0", path.name + done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
