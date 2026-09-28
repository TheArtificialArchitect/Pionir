<#
.SYNOPSIS
  One-time setup of the Builds division's sandbox user. Run it yourself, ONCE, as
  administrator (right-click PowerShell > Run as administrator), signed in as YOURSELF.

.DESCRIPTION
  The crew's Builds division has Daedalus write small products overnight and then runs the
  products' tests. None of that may run as you. This makes a contained standard user,
  pionir-builds, for it, and prints every step as it goes. Safe to run again: each step
  checks first and only does what is missing.

  What it changes on this machine:
    1. a local standard user "pionir-builds" (member of Users only; never an admin; hidden
       from the sign-in screen; a random 40-character password nobody is shown)
    2. that password, encrypted with DPAPI for YOUR account only, in
       %USERPROFILE%\.pionir\secrets\pionir-builds.cred - so Pionir, running as you, can
       start a process as pionir-builds, and nothing else can read it
    3. the sandbox folder (C:\src\daedalus-work): pionir-builds may Modify it; you keep Full
       control; no other ordinary user has access (inheritance from C:\src is cut)
    4. C:\src itself: an explicit DENY of write/delete for pionir-builds, so it cannot change
       any of your other repos (C:\src otherwise lets every signed-in user modify it)
    5. a dedicated Python for it, copied from yours into %ProgramData%\PionirBuilds\python
       (read-only to pionir-builds, writable only by administrators), with Daedalus's server
       packages (fastapi, uvicorn, requests, PyYAML) installed from PyPI during this setup
    6. two Windows Firewall rules (group "Pionir builds"): block every outbound connection
       except to loopback (127.0.0.0/8 and ::1) for that Python, and for the pionir-builds
       user itself - so Ollama on 127.0.0.1:11434 still answers and nothing else does
    7. a record of all this in %ProgramData%\PionirBuilds\setup.json, which Pionir checks
       before it builds anything
  It makes NO scheduled task, NO service and NO autostart of any kind. Pionir starts
  processes as pionir-builds only while a build runs overnight, and kills them after.

  At the end it proves the containment by running the dedicated Python AS pionir-builds:
  it must be able to write the sandbox, must NOT be able to write C:\src\Pionir or read
  your profile, must NOT reach the internet, and must reach loopback.

  Undo all of it with tools\remove-build-sandbox.ps1.

.PARAMETER ResetPassword
  Make a new password (and credential file) even if the current one still works.
#>
[CmdletBinding()]
param(
    [string]$SandboxRoot = "C:\src\daedalus-work",
    [string]$SrcRoot = "C:\src",
    [string]$DaedalusSrc = "C:\src\Tech-Support\daedalus",
    [string]$PythonSource = "",
    [string]$InstallDir = "",
    [switch]$ResetPassword
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$User = "pionir-builds"
$RuleGroup = "Pionir builds"
if (-not $InstallDir) { $InstallDir = Join-Path $env:ProgramData "PionirBuilds" }
$PyDir = Join-Path $InstallDir "python"
$PyExe = Join-Path $PyDir "python.exe"
$Record = Join-Path $InstallDir "setup.json"
$CredFile = Join-Path $env:USERPROFILE ".pionir\secrets\pionir-builds.cred"
$NotLoopback = @("0.0.0.0-126.255.255.255", "128.0.0.0-255.255.255.255",
                 "::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")

function Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Did([string]$text) { Write-Host "    DONE     $text" -ForegroundColor Green }
function Had([string]$text) { Write-Host "    ALREADY  $text" -ForegroundColor DarkGreen }
function Warn([string]$text) { Write-Host "    WARNING  $text" -ForegroundColor Yellow }
function Fail([string]$text) { Write-Host "    FAILED   $text" -ForegroundColor Red; exit 1 }
function Run-Icacls([string[]]$argv) {
    Write-Host ("    icacls " + ($argv -join " ")) -ForegroundColor DarkGray
    & icacls.exe @argv | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "icacls exited $LASTEXITCODE" }
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
if (-not (Test-Path (Join-Path $DaedalusSrc "daedalus\server.py"))) {
    Fail "Daedalus's code is not at $DaedalusSrc"
}
$seclogon = Get-Service seclogon -ErrorAction SilentlyContinue
if ($null -eq $seclogon -or $seclogon.StartType -eq "Disabled") {
    Warn "the Secondary Logon service (seclogon) is disabled; Pionir needs it to start a process as $User. Set it to Manual yourself (this script changes no service)."
}
$fw = Get-NetFirewallProfile | Where-Object { -not $_.Enabled }
if ($fw) { Warn ("Windows Firewall is OFF for profile(s): " + (($fw | ForEach-Object Name) -join ", ") + " - the outbound rules do nothing there") }

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
if ($null -eq $existing) {
    $plain = New-Password
    $secure = ConvertTo-SecureString $plain -AsPlainText -Force
    New-LocalUser -Name $User -Password $secure -PasswordNeverExpires -UserMayNotChangePassword `
        -AccountNeverExpires -Description "Pionir: contained user for overnight builds (no admin)" | Out-Null
    Did "created the user $User"
    $needPassword = $true
} else {
    Had "the user $User exists"
    if ($needPassword) {
        $plain = New-Password
        $secure = ConvertTo-SecureString $plain -AsPlainText -Force
        Set-LocalUser -Name $User -Password $secure -PasswordNeverExpires $true
        Did "set a new random password"
    }
}
$UserSid = (Get-LocalUser -Name $User).SID.Value
Write-Host "    sid         $UserSid"
$admins = Get-LocalGroupMember -SID "S-1-5-32-544" | Where-Object { $_.SID.Value -eq $UserSid }
if ($admins) { Fail "$User is an administrator; remove it from Administrators and run this again" }
$usersGroup = Get-LocalGroupMember -SID "S-1-5-32-545" | Where-Object { $_.SID.Value -eq $UserSid }
if ($usersGroup) { Had "$User is in Users (standard user)" } else {
    Add-LocalGroupMember -SID "S-1-5-32-545" -Member $UserSid
    Did "added $User to Users (standard user; allowed to log on locally)"
}
$hide = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
if (-not (Test-Path $hide)) { New-Item -Path $hide -Force | Out-Null }
New-ItemProperty -Path $hide -Name $User -Value 0 -PropertyType DWord -Force | Out-Null
Did "hid $User from the sign-in screen"

# ---- 2. the credential ----------------------------------------------------------------------
Step "The credential (DPAPI, for $OwnerName only)"
if ($needPassword) {
    New-Item -ItemType Directory -Force -Path (Split-Path $CredFile) | Out-Null
    $secure | ConvertFrom-SecureString | Set-Content -Path $CredFile -Encoding Ascii
    Run-Icacls @($CredFile, "/inheritance:r", "/grant:r", "*${OwnerSid}:F", "*S-1-5-18:F")
    Did "wrote $CredFile (readable by you and SYSTEM only)"
    $plain = $null
} else {
    Had "$CredFile exists (use -ResetPassword to make a new one)"
}

# ---- 3. the sandbox folder ------------------------------------------------------------------
Step "The sandbox folder $SandboxRoot"
if (-not (Test-Path $SandboxRoot)) {
    New-Item -ItemType Directory -Path $SandboxRoot | Out-Null
    Did "created $SandboxRoot"
} else { Had "$SandboxRoot exists" }
$item = Get-Item $SandboxRoot -Force
if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "$SandboxRoot is a link or junction" }
Run-Icacls @($SandboxRoot, "/inheritance:r", "/grant:r",
    "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "*${OwnerSid}:(OI)(CI)F",
    "*${UserSid}:(OI)(CI)M")
if (Get-ChildItem -Force $SandboxRoot) {
    Run-Icacls @("$SandboxRoot\*", "/reset", "/T", "/C", "/Q")
}
Did "${User}: Modify; you, Administrators, SYSTEM: Full; nobody else (inheritance from C:\src cut)"

# ---- 4. the rest of C:\src ------------------------------------------------------------------
Step "Deny $User writing anywhere else in $SrcRoot"
Run-Icacls @($SrcRoot, "/deny", "*${UserSid}:(OI)(CI)(W,D,DC,WDAC,WO)")
Did "explicit deny on $SrcRoot (write, delete, change permissions, take ownership) for $User"

# ---- 5. the owner's profile --------------------------------------------------------------------
Step "Your profile stays unreadable to $User"
$open = (Get-Acl $env:USERPROFILE).Access | Where-Object {
    $_.AccessControlType -eq "Allow" -and
    ($_.IdentityReference.Value -match "Everyone|Authenticated Users|\\Users$|$User")
}
if ($open) { Warn ("your profile grants: " + (($open | ForEach-Object { $_.IdentityReference.Value }) -join ", ") + " - $User may be able to read it") }
else { Had "$env:USERPROFILE grants no ordinary user access" }

# ---- 6. the dedicated interpreter ------------------------------------------------------------------
Step "The dedicated Python in $PyDir"
if (-not $PythonSource) {
    $PythonSource = (& python -c "import sys; print(sys.base_prefix)").Trim()
}
if (-not (Test-Path (Join-Path $PythonSource "python.exe"))) { Fail "no python.exe in $PythonSource" }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Run-Icacls @($InstallDir, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
    "*S-1-5-32-544:(OI)(CI)F", "*${OwnerSid}:(OI)(CI)RX", "*${UserSid}:(OI)(CI)RX")
Did "${InstallDir}: Administrators and SYSTEM Full; you and $User read-only"
$ready = $false
if (Test-Path $PyExe) {
    & $PyExe -c "import fastapi, uvicorn, requests, yaml" 2>$null
    $ready = ($LASTEXITCODE -eq 0)
}
if ($ready) { Had "$PyExe exists with Daedalus's server packages" } else {
    Write-Host "    robocopy $PythonSource -> $PyDir (without its site-packages)" -ForegroundColor DarkGray
    & robocopy.exe $PythonSource $PyDir /E /NFL /NDL /NJH /NJS /NP /XD (Join-Path $PythonSource "Lib\site-packages") | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy exited $LASTEXITCODE" }
    & $PyExe -m ensurepip --default-pip | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "ensurepip failed" }
    Write-Host "    $PyExe -m pip install fastapi uvicorn requests PyYAML" -ForegroundColor DarkGray
    & $PyExe -m pip install --disable-pip-version-check --no-warn-script-location fastapi uvicorn requests PyYAML
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
    Did "copied Python to $PyDir and installed Daedalus's server packages"
}

# ---- 7. the firewall ----------------------------------------------------------------------------
Step "Windows Firewall: $User reaches loopback only"
Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$rules = @()
foreach ($exe in @($PyExe, (Join-Path $PyDir "pythonw.exe"))) {
    $name = "Pionir builds - block outbound - " + (Split-Path $exe -Leaf)
    New-NetFirewallRule -DisplayName $name -Group $RuleGroup -Direction Outbound -Action Block `
        -Program $exe -RemoteAddress $NotLoopback -Profile Any | Out-Null
    Did "rule: $name (every remote address except loopback)"
    $rules += $name
}
$name = "Pionir builds - block outbound - user $User"
try {
    New-NetFirewallRule -DisplayName $name -Group $RuleGroup -Direction Outbound -Action Block `
        -LocalUser "D:(A;;CC;;;$UserSid)" -RemoteAddress $NotLoopback -Profile Any | Out-Null
    Did "rule: $name (any program run as $User - git, PowerShell, anything it starts)"
    $rules += $name
} catch {
    Warn "Windows refused the user-scoped rule ($($_.Exception.Message)); only the interpreter is blocked"
}

# ---- 8. the record --------------------------------------------------------------------------------
Step "The record Pionir checks"
$doc = [ordered]@{
    version = 1; user = $User; sid = $UserSid; owner = $OwnerName; owner_sid = $OwnerSid
    python = $PyExe; sandbox_root = (Resolve-Path $SandboxRoot).Path; src_root = $SrcRoot
    daedalus_src = (Resolve-Path $DaedalusSrc).Path; credential = $CredFile
    firewall_group = $RuleGroup; firewall_rules = $rules
    set_up_at = (Get-Date).ToString("o")
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($Record, ($doc | ConvertTo-Json -Depth 4), $utf8NoBom)
Did "wrote $Record"

# ---- 9. prove it ---------------------------------------------------------------------------------
Step "Proving the containment, as $User"
$probeOut = Join-Path $SandboxRoot ".setup-probe.json"
$probe = @"
import json, os, socket, sys
out = {}
def attempt(name, fn):
    try:
        fn(); out[name] = "allowed"
    except Exception as exc:
        out[name] = "blocked (" + type(exc).__name__ + ")"
def write(path):
    with open(path, "w") as f: f.write("x")
    os.remove(path)
def connect(host, port):
    s = socket.create_connection((host, port), timeout=5); s.close()
attempt("write the sandbox", lambda: write(r"$SandboxRoot\.probe-write"))
attempt("write C:\\src\\Pionir", lambda: write(r"$SrcRoot\Pionir\.probe-write"))
attempt("read your profile", lambda: os.listdir(r"$env:USERPROFILE\.pionir"))
attempt("reach the internet (1.1.1.1:443)", lambda: connect("1.1.1.1", 443))
attempt("reach loopback Ollama (127.0.0.1:11434)", lambda: connect("127.0.0.1", 11434))
json.dump(out, open(r"$probeOut", "w"), indent=1)
"@
$probeFile = Join-Path $SandboxRoot ".setup-probe.py"
[IO.File]::WriteAllText($probeFile, $probe, $utf8NoBom)
$cred = New-Object System.Management.Automation.PSCredential(".\$User", (ConvertTo-SecureString (Get-Content $CredFile) ))
Remove-Item $probeOut -ErrorAction SilentlyContinue
Start-Process -FilePath $PyExe -ArgumentList @("`"$probeFile`"") -Credential $cred -WorkingDirectory $SandboxRoot `
    -WindowStyle Hidden -Wait -LoadUserProfile
if (-not (Test-Path $probeOut)) { Warn "the probe did not run as $User (is the Secondary Logon service allowed to start?)" }
else {
    $got = Get-Content $probeOut -Raw | ConvertFrom-Json
    $want = [ordered]@{
        "write the sandbox" = "allowed"; "write C:\src\Pionir" = "blocked"
        "read your profile" = "blocked"; "reach the internet (1.1.1.1:443)" = "blocked"
        "reach loopback Ollama (127.0.0.1:11434)" = "allowed" }
    foreach ($k in $want.Keys) {
        $v = [string]$got.$k
        $ok = $v.StartsWith($want[$k])
        if ($k -like "*Ollama*" -and -not $ok) { Warn "$k : $v (is Ollama running? loopback must stay open)" }
        elseif ($ok) { Did "$k : $v" } else { Warn "$k : $v  <-- NOT CONTAINED" }
    }
}
Remove-Item $probeFile, $probeOut -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. Pionir's Builds division can now run. Undo with tools\remove-build-sandbox.ps1." -ForegroundColor Cyan
