"""The node part of tools\\setup-build-sandbox.ps1 and tools\\remove-build-sandbox.ps1.

The scripts cannot run here (they make a user, firewall rules and ACLs, and need elevation),
so these tests read them, like test_build_sandbox_setup_script.py. They hold in place: node is
OPTIONAL (no node means a warning and a record without node keys, never a failure of the Python
part), node.exe is a Low-labelled copy, typescript and vitest come from the pinned lockfile via
the OWNER's npm with no install scripts and are read-only to the sandbox user, node.exe is
firewalled to loopback by its own recorded rule, node is proven contained AS the sandbox user
before anything is recorded, and the node keys are the very last thing written into setup.json.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from pionir import build_sandbox as bs

TOOLS = Path(__file__).resolve().parents[1] / "tools"
SETUP = TOOLS / "setup-build-sandbox.ps1"
REMOVE = TOOLS / "remove-build-sandbox.ps1"
MANIFEST = TOOLS / "build-sandbox-node" / "package.json"
LOCK = TOOLS / "build-sandbox-node" / "package-lock.json"


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _parses(path: Path) -> str:
    command = ("$t = $null; $e = $null; [void][System.Management.Automation.Language.Parser]"
               f"::ParseFile('{path}', [ref]$t, [ref]$e); $e.Count")
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                           command], capture_output=True, text=True, timeout=120, check=False)
    return done.stdout.strip() + done.stderr


class NodeSetupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SETUP.read_text(encoding="utf-8")
        self.node = _section(self.text, "# ---- 3b. Node", "# ---- 4. the sandbox folder")

    def test_it_still_parses_and_takes_a_node_source(self) -> None:
        self.assertIn("[string]$NodeSource", self.text)
        self.assertIn(".PARAMETER NodeSource", self.text[:self.text.index("[CmdletBinding()]")])
        # the default is the node on PATH
        self.assertIn("Get-Command node", self.node)
        self.assertIn("$NodeSource = $foundNode.Source", self.node)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell")
    def test_both_scripts_parse_in_windows_powershell(self) -> None:
        self.assertEqual(_parses(SETUP), "0")
        self.assertEqual(_parses(REMOVE), "0")

    def test_a_missing_node_is_a_warning_and_never_a_failure(self) -> None:
        skip = _section(self.node, "if (-not $NodeSource -or", "else {")
        self.assertIn("$nodeSkip =", skip)
        self.assertNotIn("Fail ", skip)
        self.assertIn("if (-not $NodeEnabled) {", self.node)
        warn = _section(self.node, "if (-not $NodeEnabled) {", "} else {")
        self.assertIn("WARNING", warn)
        self.assertIn("SKIPPED", warn)
        self.assertIn("NO node keys", warn)
        self.assertNotIn("Fail ", warn)
        self.assertNotIn("exit", warn)
        # nothing node-ish happens unless it is enabled: the firewall rule, the probe, the keys
        self.assertIn("if ($NodeEnabled) {", self.text[self.text.index("-Program $NodeExe") - 400:])
        self.assertRegex(self.text, r"(?s)if \(\$NodeEnabled\) \{\s*\$doc\[\"node\"\]")
        self.assertIn("WITHOUT node", self.text)

    def test_node_exe_is_a_low_labelled_copy_in_the_install_folder(self) -> None:
        self.assertIn('$NodeDir = Join-Path $InstallDir "node"', self.text)
        self.assertIn('$NodeExe = Join-Path $NodeDir "node.exe"', self.text)
        self.assertIn("Copy-Item -LiteralPath $NodeSource -Destination $NodeExe", self.node)
        self.assertIn('Run-Icacls @($NodeExe, "/setintegritylevel", "low")', self.node)
        self.assertLess(self.node.index("Copy-Item -LiteralPath $NodeSource"),
                        self.node.index("/setintegritylevel"))
        # the pinned vitest/vite need a recent node: an old one is skipped, not recorded
        self.assertIn("Major -ge 23", self.node)
        self.assertIn("Minor -ge 12", self.node)
        self.assertIn("Minor -ge 19", self.node)

    def test_the_tools_are_pinned_installed_by_the_owner_and_read_only_to_the_user(self) -> None:
        npm = [line for line in self.node.splitlines() if "$npmArgs" in line and "@(" in line]
        self.assertEqual(len(npm), 1)
        for flag in ('"ci"', '"--ignore-scripts"', '"--no-audit"', '"--no-fund"'):
            self.assertIn(flag, npm[0])
        self.assertNotIn('"install"', self.node)
        # from the pinned manifest and lockfile (integrity hashes) next to the script
        self.assertIn('$NodeManifestDir = Join-Path $PSScriptRoot "build-sandbox-node"', self.text)
        self.assertIn("package-lock.json", self.node)
        self.assertIn("& $NpmCmd @npmArgs", self.node)
        # by YOU: nothing in the node part runs a program with the sandbox user's credential
        self.assertNotIn("-Credential", self.node)
        self.assertNotIn("$cred", self.node)
        # versions come from the manifest and are checked by running the tools
        self.assertIn("$NodeTsVersion = [string]$manifest.dependencies.typescript", self.node)
        self.assertIn("$NodeVtVersion = [string]$manifest.dependencies.vitest", self.node)
        self.assertIn('"Version $NodeTsVersion"', self.node)
        self.assertIn('"vitest/$NodeVtVersion *"', self.node)
        # read and run, never write
        grant = _section(self.node, "Run-Icacls @($NodeTools, \"/inheritance:r\"",
                         'Did "${NodeTools}')
        self.assertIn("*${UserSid}:(OI)(CI)RX", grant)
        # 2026-10-07: icacls's W is FILE_GENERIC_WRITE, which carries SYNCHRONIZE and
        # READ_CONTROL; denied, they beat the RX allow and node got EPERM opening tsc.js, so
        # the probe left node out of the record and every TypeScript build was refused. The
        # deny names the write rights one by one, never W/R/GW/GR, after removing old denies.
        deny = '"/deny", "*${UserSid}:(OI)(CI)(WD,AD,WEA,WA,D,DC,WDAC,WO)"'
        self.assertIn(deny, grant)
        self.assertNotIn("(W,", grant)
        self.assertLess(grant.index('"/remove:d", "*${UserSid}"'), grant.index(deny))
        self.assertNotIn("*${UserSid}:(OI)(CI)F", grant)
        self.assertNotIn("*${UserSid}:(OI)(CI)M", grant)

    def test_node_exe_has_its_own_loopback_only_rule_which_is_recorded(self) -> None:
        rule = _section(self.text, "if ($NodeEnabled) {\n        $name = ", "$sddl = ")
        self.assertIn("-Direction Outbound -Action Block", rule)
        self.assertIn("-Program $NodeExe -RemoteAddress $NotLoopback", rule)
        self.assertIn("-Group $RuleGroup", rule)
        self.assertIn("$rules += $name", rule)
        self.assertIn("$nodeRules += $name", rule)
        self.assertIn('$doc["node_firewall_rules"] = @($nodeRules)', self.text)
        # the group's rules are removed and re-made on every run: idempotent
        self.assertIn("Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue "
                      "| Remove-NetFirewallRule", self.text)

    def test_node_is_proven_contained_as_the_sandbox_user(self) -> None:
        probe = _section(self.text, "$nodeProbe = @'", "'@")
        for name in ("write the sandbox", "write C:\\\\src", "read your profile",
                     "write your profile", "list your secrets folder", "read your secret ",
                     "write the install folder", "write the node tools folder",
                     "write the node tools packages", "reach the internet (1.1.1.1:443)",
                     "Mandatory Label"):
            self.assertIn(name, probe, name)
        self.assertIn("process.version", probe)          # node --version works
        self.assertIn("'--version'", probe)               # tsc and vitest run through the links
        # run AS the sandbox user, with the copied node.exe
        run = _section(self.text, "Start-Process -FilePath $NodeExe", "-LoadUserProfile")
        self.assertIn("-Credential $cred", run)
        verdict = _section(self.text, "if ($NodeEnabled -and -not $nodeProbeRan)",
                           "# Low integrity")
        self.assertIn('$nodeWant = if ($k -eq "node: write the sandbox" -or', verdict)
        self.assertIn("$contained = $false", verdict)
        self.assertIn("$nodeLow -eq $true", verdict)
        # a node that could not be proven is LEFT OUT of the record, never recorded
        self.assertIn("$NodeEnabled = $false", verdict)
        self.assertEqual(verdict.count("$NodeEnabled = $false"), 2)

    def test_the_node_keys_are_written_into_the_record_last(self) -> None:
        write = self.text.index("[IO.File]::WriteAllText($Record")
        keys = self.text.index('$doc["node"] = $NodeExe')
        self.assertLess(keys, write)
        for before in ("# Low integrity", "if ($NodeEnabled -and -not $nodeProbeRan)",
                       "if (-not $contained)", "# ---- 9. Pionir's own preflight",
                       "# ---- 7. your secrets"):
            self.assertLess(self.text.index(before), keys, before)
        # only the node keys sit between the doc and the single write
        between = self.text[self.text.index("}\n# the node keys"):write]
        self.assertEqual(sorted(re.findall(r'\$doc\["(\w+)"\]', between)),
                         ["node", "node_firewall_rules", "node_tools", "node_typescript",
                          "node_version", "node_vitest", "node_workers_types"])
        self.assertIn("if ($NodeEnabled) {", between)
        self.assertEqual(self.text.count("WriteAllText($Record"), 1)
        # the first doc (without node) is what the version-3 record is
        self.assertIn(f"version = {bs.RECORD_VERSION};", self.text)
        self.assertEqual(bs.RECORD_VERSION, 3)
        self.assertEqual(set(bs.NODE_KEYS), {"node", "node_tools", "node_firewall_rules",
                                             "node_version", "node_typescript", "node_vitest",
                                             "node_workers_types"})

    def test_the_test_links_are_junctions_made_by_the_owner_and_removed_safely(self) -> None:
        self.assertIn("mklink /J", self.text)
        self.assertIn("Remove-ProbeLinks", self.text)
        # links are unlinked (cmd rmdir) BEFORE Remove-Item -Recurse ever sees the tree
        gather = self.text.index("Remove-ProbeLinks                      #")
        self.assertLess(gather, self.text.index("Remove-Item -Recurse -Force $probeDir"))


class RemoveScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = REMOVE.read_text(encoding="utf-8")

    def test_the_node_firewall_rule_is_removed(self) -> None:
        rules = _section(self.text, 'Step "Firewall rules"', 'Step "The deny entries"')
        # the whole group (which holds node's rule) ...
        self.assertIn("Get-NetFirewallRule -Group $RuleGroup", rules)
        self.assertIn("Remove-NetFirewallRule", rules)
        # ... and the node rules the record names, by name
        self.assertIn("node_firewall_rules", rules)
        self.assertIn("Get-NetFirewallRule -DisplayName", rules)
        self.assertIn("the node firewall rule", rules)

    def test_the_node_files_go_with_the_install_folder(self) -> None:
        self.assertIn("node.exe", self.text[:self.text.index("[CmdletBinding()]")])
        folder = _section(self.text, 'Step "The install folder"', 'Step "The credential')
        self.assertIn("Remove-Item -Recurse -Force $InstallDir", folder)

    def test_it_is_still_windows_powershell_5_1(self) -> None:
        code = "\n".join(line for line in self.text.splitlines()
                         if not line.lstrip().startswith("#"))
        for op in ("&&", "||", "??", "?."):
            self.assertNotIn(op, code, op)


class NodeManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.lock = json.loads(LOCK.read_text(encoding="utf-8"))

    def test_every_tool_is_pinned_exactly(self) -> None:
        deps = self.manifest["dependencies"]
        self.assertEqual(set(deps), {"typescript", "vitest", "@cloudflare/workers-types"})
        for name, version in deps.items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+(?:\.\d+)?$", name)    # no ^, no ~, no range
        self.assertNotIn("scripts", self.manifest)                            # nothing runs on install
        self.assertTrue(self.manifest["private"])

    def test_the_lockfile_pins_every_package_by_integrity_hash(self) -> None:
        self.assertGreaterEqual(self.lock["lockfileVersion"], 2)
        packages = {k: v for k, v in self.lock["packages"].items() if k}
        self.assertGreater(len(packages), 10)
        for key, pkg in packages.items():
            self.assertTrue(pkg.get("integrity", "").startswith("sha512-"), key)
            self.assertTrue(pkg.get("resolved", "").startswith("https://registry.npmjs.org/"), key)
        for name, version in self.manifest["dependencies"].items():
            self.assertEqual(packages[f"node_modules/{name}"]["version"], version)

    def test_the_versions_match_what_scrooge_builds_with(self) -> None:
        scrooge = Path(r"C:\src\Scrooge\worker\package.json")
        if not scrooge.is_file():
            self.skipTest("Scrooge is not checked out here")
        dev = json.loads(scrooge.read_text(encoding="utf-8")).get("devDependencies", {})
        for name in ("typescript", "vitest"):
            major = dev[name].lstrip("^~").split(".")[0]
            self.assertEqual(self.manifest["dependencies"][name].split(".")[0], major, name)


if __name__ == "__main__":
    unittest.main()
