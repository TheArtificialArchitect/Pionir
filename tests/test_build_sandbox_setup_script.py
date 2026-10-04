"""tools\\setup-build-sandbox.ps1: what the owner runs, once, as administrator.

It cannot run here (it makes a user, firewall rules and ACLs), so these tests read it. They
hold the owner's decision of 2026-09-28 in place - loopback reachability is reported as
ACCEPTED and never stops the setup - and everything else fail-closed: internet egress, any
write outside the sandbox, a secret the sandbox user could read, and Low integrity, which is
required (never assumed) both from its own probe and from Pionir's preflight, before the
record Pionir checks is written.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

from pionir import build_sandbox as bs

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "setup-build-sandbox.ps1"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


class SetupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _text()

    def test_the_user_description_fits_windows_limit(self) -> None:
        # New-LocalUser refuses a -Description over 48 characters; the first run failed on it
        for m in re.finditer(r'-Description "([^"]*)"', self.text):
            self.assertLessEqual(len(m.group(1)), 48, m.group(1))
        self.assertIn("-Description", self.text)

    def test_it_is_windows_powershell_5_1(self) -> None:
        # no PowerShell 7 operators: the owner runs it in Windows PowerShell 5.1
        code = "\n".join(line for line in self.text.splitlines()
                         if not line.lstrip().startswith("#"))
        code = re.sub(r'@"\n.*?\n"@', "", code, flags=re.S)          # the Python probe
        code = re.sub(r"@'\n.*?\n'@", "", code, flags=re.S)          # the node probe (JavaScript)
        for op in ("&&", "||", "??", "?."):
            self.assertNotIn(op, code, op)
        self.assertIsNone(re.search(r"\)\s*\?\s*[^\s]+\s*:", code))  # a ternary

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell")
    def test_it_parses_in_windows_powershell(self) -> None:
        command = ("$t = $null; $e = $null; [void][System.Management.Automation.Language.Parser]"
                   f"::ParseFile('{SCRIPT}', [ref]$t, [ref]$e); $e.Count")
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                               command], capture_output=True, text=True, timeout=120,
                              check=False)
        self.assertEqual(done.stdout.strip(), "0", done.stdout + done.stderr)

    def test_the_package_probe_cannot_stop_the_script_when_a_package_is_missing(self) -> None:
        probe = _section(self.text, 'if (Test-Path $PyExe) {', 'if ($ready)')
        self.assertIn('$ErrorActionPreference = "Continue"', probe)
        self.assertIn("finally { $ErrorActionPreference = $eap }", probe)
        self.assertNotIn("2>$null", probe)

    def test_the_pinned_install_is_retried_but_still_fails_closed(self) -> None:
        install = _section(self.text, "for ($try = 1;", "Did \"copied Python and installed")
        self.assertIn("$try -le 3", install)
        self.assertIn("--require-hashes", self.text)
        self.assertIn('Fail "pip install failed 3 times', install)

    def test_the_install_runs_from_the_host_python_because_the_sandbox_one_is_firewalled(self) -> None:
        install = _section(self.text, "$HostPy = ", "Did \"copied Python and installed")
        self.assertIn("& $HostPy @pipArgs", install)
        self.assertNotIn("& $PyExe @pipArgs", install)
        self.assertIn('"--target", $SitePackages', install)
        self.assertIn("--only-binary=:all:", install)

    def test_the_packages_get_readable_permissions_on_every_run(self) -> None:
        # 2026-10-04: pip --target, run elevated, kept its staging folder's ACL (SYSTEM,
        # Administrators, the owner) on every package: pionir-builds was "Access is denied",
        # and a rerun said ALREADY and skipped the install - so the reset must not live in it
        reset = '\nRun-Icacls @($SitePackages, "/reset", "/T", "/C", "/Q")\n'
        self.assertIn(reset, self.text)                 # top level: the ALREADY path too
        self.assertNotIn('"/Q") -Soft', self.text[self.text.index(reset):][:len(reset) + 8])
        self.assertLess(self.text.index('Did "copied Python and installed'),
                        self.text.index(reset))
        self.assertLess(self.text.index(reset), self.text.index("# ---- 8. prove it"))

    def test_the_readiness_check_wants_real_module_files(self) -> None:
        probe = _section(self.text, 'if (Test-Path $PyExe) {', 'if ($ready)')
        self.assertIn("assert fastapi.FastAPI and pytest.__file__", probe)

    def test_the_packages_are_imported_as_the_sandbox_user_before_the_record(self) -> None:
        probe = _section(self.text, '$pyProbe = @"', '"@')
        self.assertIn("for mod in ('fastapi', 'uvicorn', 'requests', 'yaml', 'pytest'):", probe)
        self.assertIn("getattr(m, '__file__', None)", probe)        # a namespace package fails
        self.assertIn("'imports': imports", probe)
        verdict = _section(self.text, "# Daedalus's packages, imported AS $User",
                           "# ---- 9. Pionir's own preflight")
        self.assertIn('if ([string]$p.Value -eq "ok")', verdict)
        self.assertIn('if ($null -eq $pyImports) { $unreadable += ', verdict)
        self.assertIn('/reset /T /C /Q', verdict[verdict.index("Fail "):])
        self.assertLess(self.text.index(verdict), self.text.index("WriteAllText($Record"))

    def test_loopback_is_reported_as_accepted_never_a_stop(self) -> None:
        loop = _section(self.text, "# loopback: reported, never a stop",
                        "if (-not $contained)")
        self.assertIn("ACCEPTED", loop)
        self.assertIn("$LoopbackDecision", loop)
        self.assertNotIn("$contained", loop)
        self.assertNotIn("Fail ", loop)
        self.assertIn("2026-09-28", self.text[:self.text.index("$LoopbackServices")])
        # loopback probes are not among the fail-closed results
        probe = _section(self.text, '$pyProbe = @"', '"@')
        checks = probe[:probe.index("for name, port in")]
        self.assertNotIn("127.0.0.1", checks)
        self.assertIn("loopback[name]", probe)

    def test_every_other_check_is_fail_closed(self) -> None:
        probe = _section(self.text, '$pyProbe = @"', '"@')
        for name in ("write C:\\\\src", "read your profile", "write your profile",
                     "list your secrets folder", "'read your secret '",
                     "write Public Documents", "write C:\\\\ProgramData",
                     "write the Windows temp folder", "write the install folder",
                     "reach the internet (1.1.1.1:443)"):
            self.assertIn(name, probe, name)
        verdict = _section(self.text, "$contained = $true", "# Low integrity")
        # only writing the sandbox may be allowed; everything else must be blocked
        self.assertIn('$want = if ($k -eq "python: write the sandbox") { "allowed" } '
                      'else { "blocked" }', verdict)
        self.assertIn("$contained = $false", verdict)
        self.assertIn('$results["powershell.exe: reach the internet"]', self.text)
        self.assertIn('$results["git.exe: reach the internet"]', self.text)
        self.assertIn("Fail \"the containment did not hold", self.text)

    def test_low_integrity_is_required_not_assumed(self) -> None:
        low = _section(self.text, "# Low integrity", "# loopback: reported")
        self.assertIn("if ($null -eq $rid)", low)
        self.assertIn("[int]$rid -ne 0x1000", low)
        self.assertEqual(low.count("$contained = $false"), 2)
        # ... and Pionir's own preflight, through spawn, must pass before the record
        pre = _section(self.text, "# ---- 9. Pionir's own preflight", "# ---- 10. the record")
        self.assertIn("from pionir.build_sandbox import _main", pre)
        self.assertIn("preflight --python $PyExe", pre)
        self.assertIn("if ($null -eq $pre)", pre)
        self.assertIn("if ($preCode -ne 0 -or -not $pre.ok)", pre)
        self.assertEqual(pre.count("Fail "), 2)

    def test_the_secrets_folder_is_checked_for_the_user_and_its_groups(self) -> None:
        acl = _section(self.text, "# ---- 7. your secrets", "# ---- 8. prove it")
        for sid in ("$UserSid", '"S-1-1-0"', '"S-1-5-11"', '"S-1-5-32-545"', '"S-1-5-4"'):
            self.assertIn(sid, acl, sid)
        self.assertIn("Get-LocalGroupMember", acl)
        self.assertIn("$_.SID.Value -eq $UserSid }) { $reach += $g.SID.Value }", acl)
        self.assertIn("if ($reach -contains $sid) { $exposed += ", acl)
        self.assertIn("if ($exposed.Count)", acl)
        self.assertIn("Fail ", acl[acl.index("if ($exposed.Count)"):])

    def test_the_record_is_written_last_and_is_what_pionir_accepts(self) -> None:
        record_at = self.text.index("[IO.File]::WriteAllText($Record")
        for before in ("# ---- 7. your secrets", "if (-not $contained)",
                       "# ---- 9. Pionir's own preflight"):
            self.assertLess(self.text.index(before), record_at, before)
        self.assertEqual(self.text.count("WriteAllText($Record"), 1)
        doc = _section(self.text, "$doc = [ordered]@{", "}\n[IO.File]")
        self.assertIn(f"version = {bs.RECORD_VERSION};", doc)
        self.assertIn("low_integrity = $true; secrets_readable = 0", doc)


if __name__ == "__main__":
    unittest.main()
