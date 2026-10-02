<#
.SYNOPSIS
  One-time setup of the Builds division's contained user. Run it yourself, ONCE, as
  administrator (right-click PowerShell > Run as administrator), signed in as YOURSELF.

.DESCRIPTION
  The crew's Builds division has a second Daedalus write small products overnight, and runs
  the products' tests. None of that may run as you. This makes a contained standard user,
  pionir-builds, and prints every step as it goes. Safe to run again (each step checks
  first); it is resumable: Pionir uses the sandbox only once the LAST step - the record -
  is written, and that happens only after the containment has been proven.

  What it changes on this machine (tools\remove-build-sandbox.ps1 undoes all of it):
    1. a local standard user "pionir-builds" (in Users only; never an admin; hidden from the
       sign-in screen; a random 44-character password nobody is shown)
    2. that password, DPAPI-encrypted for YOUR account only, in
       %USERPROFILE%\.pionir\secrets\pionir-builds.cred (you and SYSTEM only), and the
       user's SID in HKCU\Software\Pionir\BuildSandbox (so removal works without the record)
    3. %ProgramData%\PionirBuilds: a copy of your Python (3.12) with Daedalus's server
       packages installed from PyPI - every wheel pinned by version and sha256
       (tools\build-sandbox-requirements.txt) - and a copy of Daedalus's code. Read-only to
       pionir-builds; its python.exe carries a LOW integrity label, so everything the user
       runs from it runs at Low integrity and can write only Low-labelled places
    4. C:\src\daedalus-work: pionir-builds may Modify it and it is labelled Low; you keep
       Full control; no other ordinary user has access (inheritance from C:\src is cut)
    5. an explicit DENY (read, write, delete, change permissions, take ownership) for
       pionir-builds on C:\src (and on any folder in it that does not inherit), and on every
       other top-level data folder of every fixed drive that ordinary users can write -
       each one is listed as it is done
    6. Windows Firewall rules (group "Pionir builds"): pionir-builds' outbound traffic -
       any program - blocked except loopback; the dedicated Python (and node.exe, below)
       blocked except loopback; and a best-effort rule against loopback except the build gate
       (127.0.0.1:8773), which Windows does not enforce for ordinary programs. A rule Windows
       will not take, or a firewall profile that is off, STOPS the setup.
    7. OPTIONAL, for TypeScript products (a Cloudflare-Worker API tested with tsc and vitest):
       %ProgramData%\PionirBuilds\node\node.exe, a copy of YOUR node.exe with a LOW integrity
       label, and %ProgramData%\PionirBuilds\node-tools, typescript and vitest installed by
       YOUR npm (npm ci --ignore-scripts, from tools\build-sandbox-node\package-lock.json:
       every package pinned by version and integrity hash), read-only to pionir-builds.
       pionir-builds has no network, so nothing is ever installed as it. If no node is found
       this part is SKIPPED with a warning - the Python Builds division is set up regardless.
    8. %ProgramData%\PionirBuilds\setup.json, the record Pionir checks, written last (the
       node keys in it only when the node part was set up AND proven).
  It makes NO scheduled task, NO service and NO autostart of any kind. It changes nothing in
  your secrets folder: it only checks it.

  LOOPBACK IS OPEN, by the owner's decision of 2026-09-28 ("I'm okay with it reaching
  programs"): pionir-builds can connect to Ollama, Pionir and the other services on
  127.0.0.1. The setup reports each one it reaches as an ACCEPTED line and does not stop for
  it. What makes that acceptable is checked instead: your secrets folder grants pionir-builds
  (and every group it is in) nothing, and night builds refuse to run while any loopback
  service serves a caller that sends no token (Pionir with PIONIR_AUTH_COMPAT on, a Daedalus
  started without its token, Galatea trusting any loopback caller).

  Before the record is written it proves the containment by running AS pionir-builds - the
  dedicated Python, node.exe (when set up), git.exe and powershell.exe: each must fail to
  reach the internet; the Python and node must run at LOW integrity, write the sandbox and
  nothing else (C:\src, the data folders, C:\Users\Public, your profile, the node tools), and
  read none of your secrets. Node must also run tsc and vitest through the same kind of
  link Pionir makes in each test run. Then Pionir's own
  preflight runs the same proof through the exact path a build takes. Anything not contained
  stops the setup, and nothing is used.

.PARAMETER ResetPassword
  Make a new password (and credential file) even if the current one still works.

.PARAMETER NodeSource
  The node.exe (or the folder holding it) to copy for TypeScript products. Default: the node
  found on PATH. If there is none, the node part is skipped with a warning.
#>
[CmdletBinding()]
param(
    [string]$SandboxRoot = "C:\src\daedalus-work",
    [string]$SrcRoot = "C:\src",
    [string]$DaedalusSrc = "C:\src\Tech-Support\daedalus",
    [string]$PythonSource = "",
    [string]$InstallDir = "",
    [string]$NodeSource = "",
    [switch]$ResetPassword
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$User = "pionir-builds"
$RuleGroup = "Pionir builds"
$GatePort = 8773
if (-not $InstallDir) { $InstallDir = Join-Path $env:ProgramData "PionirBuilds" }
$PyDir = Join-Path $InstallDir "python"
$PyExe = Join-Path $PyDir "python.exe"
$DaedalusCopy = Join-Path $InstallDir "daedalus"
$NodeDir = Join-Path $InstallDir "node"
$NodeExe = Join-Path $NodeDir "node.exe"
$NodeTools = Join-Path $InstallDir "node-tools"
$NodeManifestDir = Join-Path $PSScriptRoot "build-sandbox-node"
$NodeEnabled = $false
$nodeRules = @()
$Record = Join-Path $InstallDir "setup.json"
$SecretsDir = Join-Path $env:USERPROFILE ".pionir\secrets"
$CredFile = Join-Path $SecretsDir "pionir-builds.cred"
$PionirSrc = Join-Path (Split-Path $PSScriptRoot -Parent) "src"
$LoopbackDecision = "accepted by the owner, 2026-09-28 (`"I'm okay with it reaching programs`")"
# every loopback service pionir-builds can reach, and what stands between it and harm
$LoopbackServices = @(
    @{ name = "Ollama"; port = 11434; guard = "NO token (Ollama has none): pull from any registry = network egress as you; delete/create models" },
    @{ name = "Pionir API"; port = 8780; guard = "client token for privileged work and approvals; builds refuse to run while PIONIR_AUTH_COMPAT is on" },
    @{ name = "crew API"; port = 8782; guard = "crew token for writes; builds refuse to run while PIONIR_AUTH_COMPAT is on" },
    @{ name = "Daedalus (yours)"; port = 8771; guard = "DAEDALUS_TOKEN; builds refuse to run while it answers without one" },
    @{ name = "Melete"; port = 8770; guard = "MELETE_TOKEN" },
    @{ name = "Galatea (Moss)"; port = 8799; guard = "builds refuse to run while she serves a loopback caller with no token" },
    @{ name = "Pionir Desktop relay"; port = 8830; guard = "per-run token (only its world and events reads are open)" }
)
$RegKey = "HKCU:\Software\Pionir\BuildSandbox"
$Requirements = Join-Path $PSScriptRoot "build-sandbox-requirements.txt"
$NotLoopback = @("0.0.0.0-126.255.255.255", "128.0.0.0-255.255.255.255",
                 "::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")
$Loopback = @("127.0.0.0-127.255.255.255", "::1")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:Problems = @()

function Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Did([string]$text) { Write-Host "    DONE     $text" -ForegroundColor Green }
function Had([string]$text) { Write-Host "    ALREADY  $text" -ForegroundColor DarkGreen }
function Fail([string]$text) {
    Write-Host "    STOPPED  $text" -ForegroundColor Red
    Write-Host ""
    Write-Host "Setup stopped. Nothing is used until it completes: fix the above and run it again." -ForegroundColor Red
    exit 1
}
function Run-Icacls([string[]]$argv, [switch]$Soft) {
    Write-Host ("    icacls " + ($argv -join " ")) -ForegroundColor DarkGray
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"      # PS 5.1 turns native stderr into a terminating error under Stop: -Soft could never soften
    try { $out = & icacls.exe @argv 2>&1 } finally { $ErrorActionPreference = $eap }
    $failed = ($out | Select-String -Pattern "Failed processing (\d+) files" |
        ForEach-Object { [int]$_.Matches[0].Groups[1].Value } | Measure-Object -Sum).Sum
    if ($LASTEXITCODE -ne 0 -or $failed) {
        $msg = "icacls $($argv[0]) exited $LASTEXITCODE ($failed item(s) failed)"
        if ($Soft) { $script:Problems += $msg; Write-Host "    PROBLEM  $msg" -ForegroundColor Yellow }
        else { Fail $msg }
    }
}

Write-Host ""
Write-Host "Pionir build sandbox - setup" -ForegroundColor Cyan

# ---- 0. who and where ----------------------------------------------------------------------
Step "Checking this session"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "run this in an ELEVATED PowerShell (Run as administrator)"
}
$OwnerSid = $identity.User.Value
$OwnerName = $identity.Name
if (-not (Test-Path (Join-Path $env:USERPROFILE ".pionir"))) {
    Fail "no .pionir folder in $env:USERPROFILE - run this signed in as the account Pionir runs as (the credential is encrypted for THIS account only)"
}
Write-Host "    owner       $OwnerName ($OwnerSid)"
Write-Host "    sandbox     $SandboxRoot"
Write-Host "    install     $InstallDir"
if (-not (Test-Path (Join-Path $DaedalusSrc "daedalus\server.py"))) { Fail "Daedalus's code is not at $DaedalusSrc" }
if (-not (Test-Path $Requirements)) { Fail "no $Requirements" }
$seclogon = Get-Service seclogon -ErrorAction SilentlyContinue
if ($null -eq $seclogon -or $seclogon.StartType -eq "Disabled") {
    Fail "the Secondary Logon service (seclogon) is disabled; Pionir needs it to start a process as $User. Set it to Manual yourself (this script changes no service) and run this again."
}
$off = @(Get-NetFirewallProfile | Where-Object { -not $_.Enabled })
if ($off.Count) { Fail ("Windows Firewall is OFF for profile(s) " + (($off | ForEach-Object Name) -join ", ") + ": the rules would do nothing. Turn it on and run this again.") }
Had "Secondary Logon available; Windows Firewall on for every profile"

# ---- 1. the user ---------------------------------------------------------------------------
Step "The local user $User"
function New-Password {
    $alphabet = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_!#%+="
    $bytes = New-Object byte[] 40
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $chars = foreach ($b in $bytes) { $alphabet[$b % $alphabet.Length] }
    return (-join $chars) + "aA1!"
}
$existing = Get-LocalUser -Name $User -ErrorAction SilentlyContinue
$needPassword = $ResetPassword -or -not (Test-Path $CredFile)
$secure = $null
if ($null -eq $existing) {
    $secure = ConvertTo-SecureString (New-Password) -AsPlainText -Force
    New-LocalUser -Name $User -Password $secure -PasswordNeverExpires -UserMayNotChangePassword `
        -AccountNeverExpires -Description "Pionir overnight builds, not an admin" | Out-Null
    Did "created the user $User"
    $needPassword = $true
} else {
    Had "the user $User exists"
    if ($needPassword) {
        $secure = ConvertTo-SecureString (New-Password) -AsPlainText -Force
        Set-LocalUser -Name $User -Password $secure -PasswordNeverExpires $true
        Did "set a new random password"
    }
}
$UserSid = (Get-LocalUser -Name $User).SID.Value
Write-Host "    sid         $UserSid"
if (Get-LocalGroupMember -SID "S-1-5-32-544" | Where-Object { $_.SID.Value -eq $UserSid }) {
    Fail "$User is an administrator; remove it from Administrators and run this again"
}
if (Get-LocalGroupMember -SID "S-1-5-32-545" | Where-Object { $_.SID.Value -eq $UserSid }) {
    Had "$User is in Users (standard user)"
} else {
    Add-LocalGroupMember -SID "S-1-5-32-545" -Member $UserSid
    Did "added $User to Users (standard user; allowed to log on locally)"
}
$hide = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
if (-not (Test-Path $hide)) { New-Item -Path $hide -Force | Out-Null }
New-ItemProperty -Path $hide -Name $User -Value 0 -PropertyType DWord -Force | Out-Null
Did "hid $User from the sign-in screen"

# ---- 2. the credential and the SID ------------------------------------------------------------
Step "The credential (DPAPI, for $OwnerName only) and the SID"
if ($needPassword) {
    New-Item -ItemType Directory -Force -Path (Split-Path $CredFile) | Out-Null
    $secure | ConvertFrom-SecureString | Set-Content -Path $CredFile -Encoding Ascii
    Run-Icacls @($CredFile, "/inheritance:r", "/grant:r", "*${OwnerSid}:F", "*S-1-5-18:F")
    Did "wrote $CredFile (you and SYSTEM only)"
} else { Had "$CredFile exists (use -ResetPassword to make a new one)" }
New-Item -Path $RegKey -Force | Out-Null
New-ItemProperty -Path $RegKey -Name "Sid" -Value $UserSid -PropertyType String -Force | Out-Null
New-ItemProperty -Path $RegKey -Name "User" -Value $User -PropertyType String -Force | Out-Null
Did "kept the SID in $RegKey (removal works without the record)"

# ---- 3. the install folder: Python, its packages, Daedalus's code ---------------------------
Step "The dedicated Python and Daedalus's code in $InstallDir"
if (-not $PythonSource) { $PythonSource = (& python -c "import sys; print(sys.base_prefix)").Trim() }
$ver = (& (Join-Path $PythonSource "python.exe") -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ($ver -ne "3.12") { Fail "the pinned wheels are for CPython 3.12; $PythonSource is $ver" }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Run-Icacls @($InstallDir, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
    "*S-1-5-32-544:(OI)(CI)F", "*${OwnerSid}:(OI)(CI)RX", "*${UserSid}:(OI)(CI)RX")
Did "${InstallDir}: Administrators and SYSTEM Full; you and $User read-only"
$ready = $false
if (Test-Path $PyExe) {
    & $PyExe -I -c "import fastapi, uvicorn, requests, yaml" 2>$null
    $ready = ($LASTEXITCODE -eq 0)
}
if ($ready) { Had "$PyExe has Daedalus's server packages" } else {
    Write-Host "    robocopy $PythonSource -> $PyDir (without its site-packages)" -ForegroundColor DarkGray
    & robocopy.exe $PythonSource $PyDir /MIR /NFL /NDL /NJH /NJS /NP /XD (Join-Path $PythonSource "Lib\site-packages") | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy exited $LASTEXITCODE" }
    & $PyExe -I -m ensurepip --default-pip | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "ensurepip failed" }
    $pipArgs = @("-I", "-m", "pip", "install", "--isolated", "--require-hashes", "--only-binary=:all:",
                 "--no-cache-dir", "--no-deps", "--disable-pip-version-check",
                 "--no-warn-script-location", "-r", $Requirements)
    Write-Host ("    $PyExe " + ($pipArgs -join " ")) -ForegroundColor DarkGray
    & $PyExe @pipArgs
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed (a hash did not match, or PyPI did not answer)" }
    Did "copied Python and installed the pinned, hashed wheels"
}
Write-Host "    robocopy $DaedalusSrc -> $DaedalusCopy" -ForegroundColor DarkGray
& robocopy.exe $DaedalusSrc $DaedalusCopy /MIR /NFL /NDL /NJH /NJS /NP /XD __pycache__ .git gym | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "robocopy of Daedalus exited $LASTEXITCODE" }
Did "copied Daedalus's code (pionir-builds may not read C:\src)"
foreach ($exe in @($PyExe, (Join-Path $PyDir "pythonw.exe"))) {
    Run-Icacls @($exe, "/setintegritylevel", "low")
}
Did "labelled python.exe and pythonw.exe Low: everything run from them runs at Low integrity"

# ---- 3b. Node (optional): node.exe and the pinned TypeScript tools --------------------------
Step "Node (for TypeScript products): a Low-labelled node.exe and pinned typescript + vitest"
$nodeSkip = ""
if (-not $NodeSource) {
    $foundNode = Get-Command node -ErrorAction SilentlyContinue
    if ($foundNode) { $NodeSource = $foundNode.Source }
}
if ($NodeSource -and (Test-Path -LiteralPath $NodeSource -PathType Container)) { $NodeSource = Join-Path $NodeSource "node.exe" }
$NpmCmd = ""
$NodeVersion = ""
$NodeTsVersion = ""
$NodeVtVersion = ""
$NodeWtVersion = ""
if (-not $NodeSource -or -not (Test-Path -LiteralPath $NodeSource -PathType Leaf)) {
    $nodeSkip = "no node.exe was found on PATH (and none was given with -NodeSource)"
} else {
    $nodeVer = $null
    try {
        $NodeVersion = ([string](& $NodeSource --version | Select-Object -First 1)).Trim()
        $nodeVer = [version]($NodeVersion.TrimStart("v"))
    } catch { $nodeVer = $null }
    $npmNext = Join-Path (Split-Path $NodeSource -Parent) "npm.cmd"
    if (Test-Path -LiteralPath $npmNext) { $NpmCmd = $npmNext }
    else {
        $npmFound = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if ($npmFound) { $NpmCmd = $npmFound.Source }
    }
    if ($null -eq $nodeVer) { $nodeSkip = "$NodeSource did not report a version" }
    elseif (-not (($nodeVer.Major -ge 23) -or ($nodeVer.Major -eq 22 -and $nodeVer.Minor -ge 12) -or ($nodeVer.Major -eq 20 -and $nodeVer.Minor -ge 19))) {
        $nodeSkip = "$NodeSource is node $NodeVersion; the pinned vitest/vite need node 20.19+ or 22.12+"
    }
    elseif (-not $NpmCmd) { $nodeSkip = "npm was not found next to $NodeSource or on PATH" }
    elseif (-not ((Test-Path (Join-Path $NodeManifestDir "package.json")) -and (Test-Path (Join-Path $NodeManifestDir "package-lock.json")))) {
        Fail "no pinned manifest in $NodeManifestDir (package.json and package-lock.json)"
    }
    else { $NodeEnabled = $true }
}
if (-not $NodeEnabled) {
    Write-Host "    WARNING  the node part is SKIPPED: $nodeSkip." -ForegroundColor Yellow
    Write-Host "             Python products are set up as usual; the record will have NO node keys, so TypeScript" -ForegroundColor Yellow
    Write-Host "             products are not built or tested. Install node and run this script again to add it." -ForegroundColor Yellow
} else {
    Write-Host "    node        $NodeSource ($NodeVersion)"
    Write-Host "    npm         $NpmCmd"
    New-Item -ItemType Directory -Force -Path $NodeDir, $NodeTools | Out-Null
    # node.exe: a copy in the install folder (read-only to pionir-builds), labelled Low like python.exe
    $nodeSrcHash = (Get-FileHash -LiteralPath $NodeSource -Algorithm SHA256).Hash
    if ((Test-Path -LiteralPath $NodeExe) -and ((Get-FileHash -LiteralPath $NodeExe -Algorithm SHA256).Hash -eq $nodeSrcHash)) {
        Had "$NodeExe is a copy of $NodeSource"
    } else {
        Copy-Item -LiteralPath $NodeSource -Destination $NodeExe -Force
        Did "copied $NodeSource -> $NodeExe"
    }
    Run-Icacls @($NodeExe, "/setintegritylevel", "low")
    Did "labelled node.exe Low: everything run from it runs at Low integrity"
    # typescript + vitest: installed by YOUR npm (you have the network; pionir-builds has none),
    # from the pinned manifest and its lockfile (integrity hashes), with no install scripts
    $manifest = Get-Content (Join-Path $NodeManifestDir "package.json") -Raw | ConvertFrom-Json
    $NodeTsVersion = [string]$manifest.dependencies.typescript
    $NodeVtVersion = [string]$manifest.dependencies.vitest
    $NodeWtVersion = [string]$manifest.dependencies."@cloudflare/workers-types"
    $lockSrc = Join-Path $NodeManifestDir "package-lock.json"
    $lockHere = Join-Path $NodeTools "package-lock.json"
    $tsJs = Join-Path $NodeTools "node_modules\typescript\lib\tsc.js"
    $vtMjs = Join-Path $NodeTools "node_modules\vitest\vitest.mjs"
    $wtDir = Join-Path $NodeTools "node_modules\@cloudflare\workers-types"
    function Get-PkgVersion([string]$dir) {
        $f = Join-Path $dir "package.json"
        if (-not (Test-Path -LiteralPath $f)) { return "" }
        return [string](Get-Content -LiteralPath $f -Raw | ConvertFrom-Json).version
    }
    $sameLock = (Test-Path -LiteralPath $lockHere) -and ((Get-FileHash -LiteralPath $lockHere -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $lockSrc -Algorithm SHA256).Hash)
    if ($sameLock -and (Test-Path -LiteralPath $tsJs) -and (Test-Path -LiteralPath $vtMjs) -and
        ((Get-PkgVersion (Join-Path $NodeTools "node_modules\typescript")) -eq $NodeTsVersion) -and
        ((Get-PkgVersion (Join-Path $NodeTools "node_modules\vitest")) -eq $NodeVtVersion) -and
        ((Get-PkgVersion $wtDir) -eq $NodeWtVersion)) {
        Had "typescript $NodeTsVersion, vitest $NodeVtVersion and workers-types $NodeWtVersion are installed in $NodeTools"
    } else {
        if (Test-Path -LiteralPath (Join-Path $NodeTools "node_modules")) { Remove-Item -Recurse -Force -LiteralPath (Join-Path $NodeTools "node_modules") }
        Copy-Item -LiteralPath (Join-Path $NodeManifestDir "package.json") -Destination (Join-Path $NodeTools "package.json") -Force
        Copy-Item -LiteralPath $lockSrc -Destination $lockHere -Force
        $npmArgs = @("ci", "--ignore-scripts", "--no-audit", "--no-fund")
        Write-Host ("    $NpmCmd " + ($npmArgs -join " ") + "   (in $NodeTools)") -ForegroundColor DarkGray
        $eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"      # npm writes progress to stderr: not a failure
        Push-Location $NodeTools
        try { & $NpmCmd @npmArgs 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
        finally { Pop-Location; $ErrorActionPreference = $eap }
        if ($LASTEXITCODE -ne 0) { Fail "npm ci failed (exit $LASTEXITCODE): an integrity hash did not match, or the registry did not answer" }
        Did "installed typescript $NodeTsVersion, vitest $NodeVtVersion and workers-types $NodeWtVersion (npm ci --ignore-scripts, lockfile integrity)"
    }
    $tsSaid = ([string](& $NodeExe $tsJs --version | Select-Object -First 1)).Trim()
    $vtSaid = ([string](& $NodeExe $vtMjs --version | Select-Object -First 1)).Trim()
    if ($tsSaid -ne "Version $NodeTsVersion") { Fail "tsc reports '$tsSaid', not 'Version $NodeTsVersion'" }
    if ($vtSaid -notlike "vitest/$NodeVtVersion *") { Fail "vitest reports '$vtSaid', not 'vitest/$NodeVtVersion'" }
    if ((Get-PkgVersion $wtDir) -ne $NodeWtVersion) { Fail "@cloudflare/workers-types is not at the pinned $NodeWtVersion in $NodeTools" }
    Did "node runs tsc ($tsSaid) and vitest ($vtSaid) from $NodeTools"
    # read-only to pionir-builds: it may read and run the tools, never change them
    Run-Icacls @($NodeTools, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F", "*${OwnerSid}:(OI)(CI)RX", "*${UserSid}:(OI)(CI)RX")
    Run-Icacls @($NodeTools, "/deny", "*${UserSid}:(OI)(CI)(W,D,DC,WDAC,WO)")
    Did "${NodeTools}: $User may read and run it, and is denied every write"
}

# ---- 4. the sandbox folder ------------------------------------------------------------------
Step "The sandbox folder $SandboxRoot"
if (-not (Test-Path $SandboxRoot)) { New-Item -ItemType Directory -Path $SandboxRoot | Out-Null; Did "created $SandboxRoot" }
else { Had "$SandboxRoot exists" }
if ((Get-Item $SandboxRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "$SandboxRoot is a link or junction" }
Run-Icacls @($SandboxRoot, "/inheritance:r", "/grant:r",
    "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "*${OwnerSid}:(OI)(CI)F",
    "*${UserSid}:(OI)(CI)M")
Run-Icacls @($SandboxRoot, "/setintegritylevel", "(OI)(CI)low")
if (Get-ChildItem -Force $SandboxRoot) { Run-Icacls @("$SandboxRoot\*", "/reset", "/T", "/C", "/Q") -Soft }
Did "${User}: Modify, labelled Low; you, Administrators, SYSTEM: Full; nobody else"

# ---- 5. deny everything else ------------------------------------------------------------------
Step "Deny $User reading or writing $SrcRoot (except the sandbox)"
$deny = "*${UserSid}:(OI)(CI)(R,W,D,DC,WDAC,WO)"
Run-Icacls @($SrcRoot, "/deny", $deny)
$protected = @()
foreach ($d in Get-ChildItem $SrcRoot -Directory -Force -Recurse -Depth 2 -ErrorAction SilentlyContinue) {
    if ($d.FullName.StartsWith($SandboxRoot, [StringComparison]::OrdinalIgnoreCase)) { continue }
    try { $isProtected = (Get-Acl $d.FullName).AreAccessRulesProtected } catch { $isProtected = $true }
    if ($isProtected) {
        Run-Icacls @($d.FullName, "/deny", $deny) -Soft
        $protected += $d.FullName
        Write-Host "    also denied (does not inherit): $($d.FullName)"
    }
}
Did "explicit deny on $SrcRoot$(if ($protected.Count) { " and $($protected.Count) folder(s) that do not inherit" })"

Step "Deny $User the other data folders any signed-in user can write"
$broad = @("S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-4")      # Everyone, Auth Users, Users, Interactive
$skip = @("Windows", "Program Files", "Program Files (x86)", "ProgramData", "Users",
          '$Recycle.Bin', "System Volume Information", "Recovery", "PerfLogs", "Config.Msi",
          "Documents and Settings", "OneDriveTemp", "WindowsApps")
$writeBits = [Security.AccessControl.FileSystemRights]"WriteData, AppendData, Write, Modify, FullControl"
$dataRoots = @()
foreach ($drive in Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3") {
    foreach ($d in Get-ChildItem ($drive.DeviceID + "\") -Directory -Force -ErrorAction SilentlyContinue) {
        if ($skip -contains $d.Name) { continue }
        if ($d.FullName -ieq $SrcRoot -or $d.FullName -ieq $InstallDir) { continue }
        try { $acl = Get-Acl $d.FullName } catch { continue }
        $open = $acl.Access | Where-Object {
            $_.AccessControlType -eq "Allow" -and ($_.FileSystemRights -band $writeBits) -and
            ($broad -contains $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value)
        }
        if ($open) {
            Run-Icacls @($d.FullName, "/deny", $deny) -Soft
            $dataRoots += $d.FullName
            Write-Host "    denied: $($d.FullName)"
        }
    }
}
Did "denied $($dataRoots.Count) data folder(s)"

# ---- 6. the firewall ----------------------------------------------------------------------------
Step "Windows Firewall: $User reaches nothing but loopback"
Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$rules = @()
try {
    foreach ($exe in @($PyExe, (Join-Path $PyDir "pythonw.exe"))) {
        $name = "Pionir builds - block outbound - " + (Split-Path $exe -Leaf)
        New-NetFirewallRule -DisplayName $name -Group $RuleGroup -Direction Outbound -Action Block `
            -Program $exe -RemoteAddress $NotLoopback -Profile Any | Out-Null
        Did "rule: $name (every address but loopback)"; $rules += $name
    }
    if ($NodeEnabled) {
        $name = "Pionir builds - block outbound - " + (Split-Path $NodeExe -Leaf)
        New-NetFirewallRule -DisplayName $name -Group $RuleGroup -Direction Outbound -Action Block `
            -Program $NodeExe -RemoteAddress $NotLoopback -Profile Any | Out-Null
        Did "rule: $name (every address but loopback)"; $rules += $name; $nodeRules += $name
    }
    $sddl = "D:(A;;CC;;;$UserSid)"
    $name = "Pionir builds - block outbound - user $User"
    New-NetFirewallRule -DisplayName $name -Group $RuleGroup -Direction Outbound -Action Block `
        -LocalUser $sddl -RemoteAddress $NotLoopback -Profile Any | Out-Null
    Did "rule: $name (any program the user runs: git, PowerShell, anything)"; $rules += $name
    $name = "Pionir builds - block loopback - user $User"
    New-NetFirewallRule -DisplayName $name -Group $RuleGroup -Direction Outbound -Action Block `
        -LocalUser $sddl -RemoteAddress $Loopback -Protocol TCP `
        -RemotePort @("1-$($GatePort - 1)", "$($GatePort + 1)-65535") -Profile Any | Out-Null
    Did "rule: $name (best effort: Windows does not filter loopback for ordinary programs; loopback is $LoopbackDecision)"; $rules += $name
} catch {
    Fail "Windows refused a firewall rule ($($_.Exception.Message)); the user cannot be contained without it"
}

# ---- 7. your secrets: nothing in them may be readable by the user ----------------------------
Step "Your secrets folder: $User, and every group it is in, may read none of it"
if (-not (Test-Path $SecretsDir)) { Fail "no $SecretsDir" }
# the SIDs a process of the user carries: its own, the well-known ones every signed-in local
# user has, and every local group it is a member of
$reach = @($UserSid, "S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-4", "S-1-2-0", "S-1-2-1",
           "S-1-5-113", "S-1-5-15", "S-1-5-32-546", "S-1-5-7")
foreach ($g in Get-LocalGroup) {
    try { $members = @(Get-LocalGroupMember -Group $g -ErrorAction Stop) } catch { continue }
    if ($members | Where-Object { $_.SID -and $_.SID.Value -eq $UserSid }) { $reach += $g.SID.Value }
}
$ownerOnly = @($OwnerSid, "S-1-5-18", "S-1-5-32-544")
$exposed = @()
$alsoReads = @{}
$secretItems = @(Get-Item -Force $SecretsDir) + @(Get-ChildItem -Force -Recurse $SecretsDir -ErrorAction SilentlyContinue)
foreach ($item in $secretItems) {
    try { $acl = Get-Acl -LiteralPath $item.FullName } catch { Fail "cannot read the permissions of $($item.FullName)" }
    foreach ($ace in $acl.Access) {
        if ($ace.AccessControlType -ne "Allow") { continue }
        if ($ace.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $bits = [int]$ace.FileSystemRights
        # ReadData/ListDirectory, GENERIC_ALL or GENERIC_READ (the sign bit)
        if (-not ((($bits -band 1) -ne 0) -or (($bits -band 0x10000000) -ne 0) -or ($bits -lt 0))) { continue }
        try { $sid = $ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $sid = [string]$ace.IdentityReference }
        if ($reach -contains $sid) { $exposed += "$($item.FullName) ($($ace.IdentityReference))" }
        elseif ($ownerOnly -notcontains $sid) { $alsoReads[[string]$ace.IdentityReference] = $true }
    }
}
if ($exposed.Count) {
    foreach ($e in $exposed) { Write-Host "    READABLE BY $User  $e" -ForegroundColor Red }
    Fail "$User could read your secrets (above). Make each one yours only (icacls <file> /inheritance:r /grant:r `"$($OwnerName):F`") and run this again."
}
Did "no permission in $SecretsDir reaches $User or a group it is in ($($secretItems.Count) item(s))"
foreach ($who in $alsoReads.Keys) {
    Write-Host "    NOTE     $who (not $User) can also read some of your secrets; not this sandbox's concern, but worth a look" -ForegroundColor Yellow
}

# ---- 8. prove it -------------------------------------------------------------------------------
Step "Proving the containment, as $User"
$cred = New-Object System.Management.Automation.PSCredential(".\$User", (ConvertTo-SecureString (Get-Content $CredFile)))
$probeDir = Join-Path $SandboxRoot ".setup-probe"
New-Item -ItemType Directory -Force $probeDir | Out-Null
$srcFile = (Get-ChildItem $SrcRoot -File -Recurse -Depth 1 -ErrorAction SilentlyContinue |
    Where-Object { -not $_.FullName.StartsWith($SandboxRoot, [StringComparison]::OrdinalIgnoreCase) } |
    Select-Object -First 1).FullName
$secretFiles = @(Get-ChildItem -Force -Recurse -File $SecretsDir -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
# the links Pionir makes in each TypeScript test run: a real node_modules folder in the sandbox
# with one junction per package to the read-only tools (made by YOU, the owner, as Pionir does)
function Remove-ProbeLinks {
    foreach ($n in @("typescript", "vitest", "@cloudflare\workers-types")) {
        $l = Join-Path $probeDir "nm\$n"
        if (Test-Path -LiteralPath $l) { & cmd.exe /c rmdir "`"$l`"" | Out-Null }
    }
}
Remove-ProbeLinks
if ($NodeEnabled) {
    New-Item -ItemType Directory -Force (Join-Path $probeDir "nm") | Out-Null
    New-Item -ItemType Directory -Force (Join-Path $probeDir "nm\@cloudflare") | Out-Null
    foreach ($n in @("typescript", "vitest", "@cloudflare\workers-types")) {
        & cmd.exe /c mklink /J "`"$(Join-Path $probeDir "nm\$n")`"" "`"$(Join-Path $NodeTools "node_modules\$n")`"" | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "could not make a junction to $NodeTools\node_modules\$n" }
    }
}
$loopbackPorts = [ordered]@{}
foreach ($svc in $LoopbackServices) { $loopbackPorts[$svc.name] = $svc.port }
$targets = @{ sandbox = (Join-Path $probeDir "w.txt"); src = (Join-Path $SrcRoot "probe-$User.txt");
              srcfile = $srcFile; profile = (Join-Path $env:USERPROFILE ".pionir");
              profilewrite = (Join-Path $env:USERPROFILE ".pionir\probe-$User.txt");
              public = (Join-Path $env:PUBLIC "Documents\probe-$User.txt");
              programdata = (Join-Path $env:ProgramData "probe-$User.txt");
              wintemp = (Join-Path $env:windir "Temp\probe-$User.txt");
              install = (Join-Path $InstallDir "probe-$User.txt");
              nodetools = (Join-Path $NodeTools "probe-$User.txt");
              nodemodules = (Join-Path $NodeTools "node_modules\probe-$User.txt");
              nodebeside = (Join-Path $probeDir "nm\probe.txt");
              nodetsc = (Join-Path $probeDir "nm\typescript\lib\tsc.js");
              nodevitest = (Join-Path $probeDir "nm\vitest\vitest.mjs");
              secrets = $secretFiles; secretsdir = $SecretsDir;
              roots = $dataRoots; loopback = $loopbackPorts }
$targets | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $probeDir "targets.json") -Encoding UTF8
$pyProbe = @"
import ctypes, json, os, socket
from ctypes import wintypes
t = json.load(open(r'$probeDir\targets.json', encoding='utf-8-sig'))
checks, loopback = {}, {}
def attempt(name, fn):
    try:
        fn(); checks[name] = 'allowed'
    except Exception as exc:
        checks[name] = 'blocked (' + type(exc).__name__ + ')'
def write(path):
    with open(path, 'w') as f: f.write('x')
    os.remove(path)
def connect(host, port):
    s = socket.create_connection((host, port), timeout=5); s.close()
def integrity():
    k = ctypes.WinDLL('kernel32'); a = ctypes.WinDLL('advapi32')
    k.GetCurrentProcess.restype = wintypes.HANDLE
    a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    a.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    a.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    a.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    a.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    h = wintypes.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 8, ctypes.byref(h)): return None
    n = wintypes.DWORD(0)
    a.GetTokenInformation(h, 25, None, 0, ctypes.byref(n))
    buf = ctypes.create_string_buffer(max(n.value, 1))
    if not a.GetTokenInformation(h, 25, buf, n, ctypes.byref(n)): return None
    sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
    return int(a.GetSidSubAuthority(sid, a.GetSidSubAuthorityCount(sid)[0] - 1)[0])
try:
    rid = integrity()
except Exception:
    rid = None
attempt('write the sandbox', lambda: write(t['sandbox']))
attempt('write C:\\src', lambda: write(t['src']))
if t.get('srcfile'): attempt('read a file in C:\\src', lambda: open(t['srcfile'], 'rb').read(1))
attempt('read your profile', lambda: os.listdir(t['profile']))
attempt('write your profile', lambda: write(t['profilewrite']))
attempt('list your secrets folder', lambda: os.listdir(t['secretsdir']))
for s in (t.get('secrets') or []):
    attempt('read your secret ' + os.path.basename(s), lambda s=s: open(s, 'rb').read(1))
attempt('write Public Documents', lambda: write(t['public']))
attempt('write C:\\ProgramData', lambda: write(t['programdata']))
attempt('write the Windows temp folder', lambda: write(t['wintemp']))
attempt('write the install folder', lambda: write(t['install']))
for r in (t.get('roots') or []):
    attempt('write ' + r, lambda r=r: write(os.path.join(r, 'probe.txt')))
attempt('reach the internet (1.1.1.1:443)', lambda: connect('1.1.1.1', 443))
for name, port in (t.get('loopback') or {}).items():
    try:
        connect('127.0.0.1', int(port)); loopback[name] = 'reachable'
    except Exception as exc:
        loopback[name] = 'not reachable (' + type(exc).__name__ + ')'
json.dump({'integrity': rid, 'checks': checks, 'loopback': loopback},
          open(r'$probeDir\python.json', 'w'), indent=1)
"@
[IO.File]::WriteAllText((Join-Path $probeDir "probe.py"), $pyProbe, $utf8NoBom)
Start-Process -FilePath $PyExe -ArgumentList @("-I", "`"$probeDir\probe.py`"") -Credential $cred `
    -WorkingDirectory $probeDir -WindowStyle Hidden -Wait -LoadUserProfile
$psProbe = "`$r = 'blocked'; try { `$c = New-Object Net.Sockets.TcpClient; `$c.Connect('1.1.1.1', 443); `$r = 'allowed' } catch {}; Set-Content -Path '$probeDir\powershell.txt' -Value `$r"
Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-NonInteractive", "-Command", $psProbe) `
    -Credential $cred -WorkingDirectory $probeDir -WindowStyle Hidden -Wait -LoadUserProfile
$gitExe = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
if ($gitExe) {
    $gp = Start-Process -FilePath $gitExe -ArgumentList @("-c", "http.lowSpeedTime=10", "ls-remote", "https://github.com/git/git", "HEAD") `
        -Credential $cred -WorkingDirectory $probeDir -WindowStyle Hidden -Wait -PassThru -LoadUserProfile
    $gitResult = if ($gp.ExitCode -eq 0) { "allowed" } else { "blocked (exit $($gp.ExitCode))" }
} else { $gitResult = "git.exe not found (not tested)" }
if ($NodeEnabled) {
    # the same proof for node.exe, AS pionir-builds: it must run at Low integrity, write the sandbox
    # (and next to the linked tools) and nothing else, read none of your secrets, and reach no
    # internet; and it must RUN tsc and vitest through links like the ones Pionir makes
    $nodeProbe = @'
// run by tools\setup-build-sandbox.ps1 as the contained user: node's half of the proof
const fs = require('fs');
const net = require('net');
const path = require('path');
const cp = require('child_process');
const t = JSON.parse(fs.readFileSync(process.argv[2], 'utf8').replace(/^\uFEFF/, ''));
const out = process.argv[3];
const checks = {};
const works = { version: process.version, low: null, tsc: null, vitest: null };
function attempt(name, fn) {
  try { fn(); checks[name] = 'allowed'; } catch (e) { checks[name] = 'blocked (' + String(e.code) + ')'; }
}
function write(p) { fs.writeFileSync(p, 'x'); fs.unlinkSync(p); }
attempt('write the sandbox', () => write(t.sandbox));
attempt('write beside the linked tools', () => write(t.nodebeside));
attempt('write C:\\src', () => write(t.src));
if (t.srcfile) attempt('read a file in C:\\src', () => fs.readSync(fs.openSync(t.srcfile, 'r'), Buffer.alloc(1), 0, 1, 0));
attempt('read your profile', () => fs.readdirSync(t.profile));
attempt('write your profile', () => write(t.profilewrite));
attempt('list your secrets folder', () => fs.readdirSync(t.secretsdir));
for (const s of (t.secrets || [])) {
  attempt('read your secret ' + path.basename(s), () => fs.readSync(fs.openSync(s, 'r'), Buffer.alloc(1), 0, 1, 0));
}
attempt('write Public Documents', () => write(t.public));
attempt('write C:\\ProgramData', () => write(t.programdata));
attempt('write the Windows temp folder', () => write(t.wintemp));
attempt('write the install folder', () => write(t.install));
attempt('write the node tools folder', () => write(t.nodetools));
attempt('write the node tools packages', () => write(t.nodemodules));
for (const r of (t.roots || [])) {
  attempt('write ' + r, () => write(path.join(r, 'probe.txt')));
}
try {
  const who = cp.execFileSync(path.join(process.env.SystemRoot, 'System32', 'whoami.exe'), ['/groups'], { encoding: 'utf8', timeout: 20000 });
  works.low = /Mandatory Label\\Low Mandatory Level/i.test(who);
} catch (e) { works.low = null; }
try {
  works.tsc = cp.execFileSync(process.execPath, [t.nodetsc, '--version'], { encoding: 'utf8', timeout: 90000 }).trim();
} catch (e) { works.tsc = null; }
try {
  works.vitest = cp.execFileSync(process.execPath, [t.nodevitest, '--version'], { encoding: 'utf8', timeout: 90000 }).trim();
} catch (e) { works.vitest = null; }
let done = false;
function finish() {
  if (done) return;
  done = true;
  fs.writeFileSync(out, JSON.stringify({ checks: checks, works: works }, null, 1));
}
const sock = net.connect({ host: '1.1.1.1', port: 443, timeout: 5000 });
sock.on('connect', () => { checks['reach the internet (1.1.1.1:443)'] = 'allowed'; sock.destroy(); finish(); });
sock.on('timeout', () => { checks['reach the internet (1.1.1.1:443)'] = 'blocked (timeout)'; sock.destroy(); finish(); });
sock.on('error', (e) => { checks['reach the internet (1.1.1.1:443)'] = 'blocked (' + String(e.code) + ')'; finish(); });
setTimeout(() => { if (!done) { checks['reach the internet (1.1.1.1:443)'] = 'blocked (no answer)'; finish(); process.exit(0); } }, 20000);
'@
    [IO.File]::WriteAllText((Join-Path $probeDir "probe-node.js"), $nodeProbe, $utf8NoBom)
    Start-Process -FilePath $NodeExe -ArgumentList @("`"$probeDir\probe-node.js`"", "`"$probeDir\targets.json`"", "`"$probeDir\node.json`"") `
        -Credential $cred -WorkingDirectory $probeDir -WindowStyle Hidden -Wait -LoadUserProfile
}

$results = [ordered]@{}
$loopbackSeen = [ordered]@{}
$rid = $null
if (Test-Path "$probeDir\python.json") {
    $got = Get-Content "$probeDir\python.json" -Raw | ConvertFrom-Json
    foreach ($p in $got.checks.PSObject.Properties) { $results["python: " + $p.Name] = [string]$p.Value }
    foreach ($p in $got.loopback.PSObject.Properties) { $loopbackSeen[$p.Name] = [string]$p.Value }
    $rid = $got.integrity
} else { $results["python probe"] = "did not run as $User" }
$results["powershell.exe: reach the internet"] = if (Test-Path "$probeDir\powershell.txt") { (Get-Content "$probeDir\powershell.txt" -Raw).Trim() } else { "did not run" }
$results["git.exe: reach the internet"] = $gitResult
$nodeResults = [ordered]@{}
$nodeLow = $null
$nodeTsc = ""
$nodeVitest = ""
$nodeProbeRan = $false
if ($NodeEnabled -and (Test-Path "$probeDir\node.json")) {
    $nodeProbeRan = $true
    $gotNode = Get-Content "$probeDir\node.json" -Raw | ConvertFrom-Json
    foreach ($p in $gotNode.checks.PSObject.Properties) { $nodeResults["node: " + $p.Name] = [string]$p.Value }
    if ($null -ne $gotNode.works.low) { $nodeLow = [bool]$gotNode.works.low }
    if ($null -ne $gotNode.works.tsc) { $nodeTsc = [string]$gotNode.works.tsc }
    if ($null -ne $gotNode.works.vitest) { $nodeVitest = [string]$gotNode.works.vitest }
}
Remove-ProbeLinks                      # the links first: Remove-Item must never follow them
Remove-Item -Recurse -Force $probeDir -ErrorAction SilentlyContinue
Remove-Item (Join-Path $SrcRoot "probe-$User.txt") -ErrorAction SilentlyContinue
$contained = $true
foreach ($k in $results.Keys) {
    $v = $results[$k]
    $want = if ($k -eq "python: write the sandbox") { "allowed" } else { "blocked" }
    if ($v.StartsWith($want) -or $v -like "*not tested*") { Did "$k : $v" }
    else { Write-Host "    NOT CONTAINED  $k : $v" -ForegroundColor Red; $contained = $false }
}
# node (when it is being set up): contained exactly as python is - anything but the two writes
# below must be blocked - and it must be Low and able to run tsc and vitest through the links.
# A node that cannot be proven (its probe did not run, or tsc/vitest do not run as the user)
# is simply LEFT OUT of the record: nothing unproven is ever recorded or used.
if ($NodeEnabled -and -not $nodeProbeRan) {
    Write-Host "    WARNING  node: its probe did not run as $User, so node is not proven contained - the node part is LEFT OUT of the record" -ForegroundColor Yellow
    $NodeEnabled = $false
}
if ($NodeEnabled) {
    foreach ($k in $nodeResults.Keys) {
        $v = $nodeResults[$k]
        $nodeWant = if ($k -eq "node: write the sandbox" -or $k -eq "node: write beside the linked tools") { "allowed" } else { "blocked" }
        if ($v.StartsWith($nodeWant)) { Did "$k : $v" }
        else { Write-Host "    NOT CONTAINED  $k : $v" -ForegroundColor Red; $contained = $false }
    }
    if ($nodeLow -eq $true) { Did "node: runs at LOW integrity when started as $User" }
    else {
        Write-Host "    NOT CONTAINED  node: it could not be shown to run at Low integrity (whoami /groups, run by node, lacks the Low label): the Low label on $NodeExe did not take effect" -ForegroundColor Red
        $contained = $false
    }
    $nodeTsOk = $nodeTsc -eq "Version $NodeTsVersion"
    $nodeVtOk = $nodeVitest -like "vitest/$NodeVtVersion *"
    if ($nodeTsOk -and $nodeVtOk) { Did "node: runs tsc ($nodeTsc) and vitest ($nodeVitest) as $User through a link into the read-only tools" }
    elseif ($contained) {
        Write-Host "    WARNING  node: as $User it could not run tsc/vitest through a link into $NodeTools (tsc said '$nodeTsc', vitest said '$nodeVitest') - the node part is LEFT OUT of the record" -ForegroundColor Yellow
        $NodeEnabled = $false
    }
}
# Low integrity (CreateProcessWithLogonW + the Low label on python.exe) has never been seen
# working on this machine before this line: it is required, never assumed
if ($null -eq $rid) {
    Write-Host "    NOT CONTAINED  python: its integrity level could not be read (the probe did not run, or could not open its own token)" -ForegroundColor Red
    $contained = $false
} elseif ([int]$rid -ne 0x1000) {
    Write-Host ("    NOT CONTAINED  python: runs at integrity 0x{0:x4}, not Low (0x1000): the Low label on {1} did not take effect when started as {2}" -f [int]$rid, $PyExe, $User) -ForegroundColor Red
    $contained = $false
} else { Did "python: runs at LOW integrity (0x1000) when started as $User" }
# loopback: reported, never a stop (the owner's decision), with what guards each service
foreach ($svc in $LoopbackServices) {
    $seen = if ($loopbackSeen.Contains($svc.name)) { $loopbackSeen[$svc.name] } else { "not probed" }
    if ($seen -eq "reachable") {
        Write-Host ("    ACCEPTED {0} on 127.0.0.1:{1} is reachable - {2}. Guard: {3}" -f $svc.name, $svc.port, $LoopbackDecision, $svc.guard) -ForegroundColor DarkYellow
    } else {
        Write-Host ("    INFO     {0} on 127.0.0.1:{1}: {2} now (reachable whenever it runs). Guard: {3}" -f $svc.name, $svc.port, $seen, $svc.guard) -ForegroundColor DarkGray
    }
}
if (-not $contained) {
    Fail "the containment did not hold (above). Pionir will not use the sandbox. (Loopback is not part of this: it is $LoopbackDecision.)"
}

# ---- 9. Pionir's own preflight, through the path a build takes -----------------------------
Step "Pionir's preflight: the dedicated Python started exactly as a build starts it"
$ownerPy = Join-Path $PythonSource "python.exe"
$eap = $ErrorActionPreference
$ErrorActionPreference = "Continue"      # its stderr is part of the report, not a failure
$preOut = @(& $ownerPy -I -c "import sys; sys.path.insert(0, sys.argv[1]); from pionir.build_sandbox import _main; raise SystemExit(_main(sys.argv[2:]))" $PionirSrc preflight --python $PyExe --sandbox $SandboxRoot --credential $CredFile --secrets $SecretsDir 2>&1)
$preCode = $LASTEXITCODE
$ErrorActionPreference = $eap
$preLine = $preOut | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith("{") } | Select-Object -Last 1
$pre = $null
if ($preLine) { try { $pre = $preLine | ConvertFrom-Json } catch { $pre = $null } }
if ($null -eq $pre) {
    foreach ($l in $preOut) { Write-Host "    $l" -ForegroundColor DarkGray }
    Fail "Pionir's preflight gave no report (exit $preCode): Low integrity through CreateProcessWithLogonW is NOT proven, so nothing is used"
}
if ($preCode -ne 0 -or -not $pre.ok) {
    foreach ($p in $pre.problems) { Write-Host "    NOT CONTAINED  $p" -ForegroundColor Red }
    Fail "Pionir's preflight did not prove the containment (above); nothing is used"
}
Did ("preflight as ${User}: integrity 0x{0:x4} (Low), {1} secret(s) tried, none readable" -f [int]$pre.report.integrity, [int]$pre.report.files)

# ---- 10. the record (last: Pionir uses the sandbox only once this exists) --------------------
Step "The record Pionir checks"
if ($script:Problems.Count) { Fail ("some permissions could not be set: " + ($script:Problems -join "; ")) }
$doc = [ordered]@{
    version = 3; user = $User; sid = $UserSid; owner = $OwnerName; owner_sid = $OwnerSid
    python = $PyExe; sandbox_root = (Resolve-Path $SandboxRoot).Path; src_root = $SrcRoot
    daedalus_src = $DaedalusCopy; credential = $CredFile; gate_port = $GatePort
    denied_folders = @($SrcRoot) + $protected + $dataRoots
    firewall_group = $RuleGroup; firewall_rules = $rules
    low_integrity = $true; secrets_readable = 0
    loopback = "open: $LoopbackDecision"
    set_up_at = (Get-Date).ToString("o")
}
# the node keys are optional, and written only now - after every probe above has passed
if ($NodeEnabled) {
    $doc["node"] = $NodeExe
    $doc["node_tools"] = $NodeTools
    $doc["node_firewall_rules"] = @($nodeRules)
    $doc["node_version"] = $NodeVersion
    $doc["node_typescript"] = $NodeTsVersion
    $doc["node_vitest"] = $NodeVtVersion
    $doc["node_workers_types"] = $NodeWtVersion
}
[IO.File]::WriteAllText($Record, ($doc | ConvertTo-Json -Depth 4), $utf8NoBom)
if ($NodeEnabled) { Did "wrote $Record (with node $NodeVersion, typescript $NodeTsVersion, vitest $NodeVtVersion)" }
else { Did "wrote $Record (WITHOUT node: TypeScript products are not built until node is set up)" }
Write-Host ""
Write-Host "Done. The sandbox is proven. Night builds still wait for Pionir's own checks at each build:" -ForegroundColor Cyan
Write-Host "  PIONIR_AUTH_COMPAT off, your Daedalus holding its token, Galatea requiring her token from loopback." -ForegroundColor Cyan
Write-Host "Undo with tools\remove-build-sandbox.ps1." -ForegroundColor Cyan
