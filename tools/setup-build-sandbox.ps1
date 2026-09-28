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
       any program - blocked except loopback; the dedicated Python blocked except loopback;
       and a best-effort rule against loopback except the build gate (127.0.0.1:8773), which
       Windows does not enforce for ordinary programs. A rule Windows will not take, or a
       firewall profile that is off, STOPS the setup.
    7. %ProgramData%\PionirBuilds\setup.json, the record Pionir checks, written last.
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
  dedicated Python, git.exe and powershell.exe: each must fail to reach the internet; the
  Python must run at LOW integrity, write the sandbox and nothing else (C:\src, the data
  folders, C:\Users\Public, your profile), and read none of your secrets. Then Pionir's own
  preflight runs the same proof through the exact path a build takes. Anything not contained
  stops the setup, and nothing is used.

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
$GatePort = 8773
if (-not $InstallDir) { $InstallDir = Join-Path $env:ProgramData "PionirBuilds" }
$PyDir = Join-Path $InstallDir "python"
$PyExe = Join-Path $PyDir "python.exe"
$DaedalusCopy = Join-Path $InstallDir "daedalus"
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
    $out = & icacls.exe @argv 2>&1
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
        -AccountNeverExpires -Description "Pionir: contained user for overnight builds (no admin)" | Out-Null
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
          "Documents and Settings", "OneDriveTemp")
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
$loopbackPorts = [ordered]@{}
foreach ($svc in $LoopbackServices) { $loopbackPorts[$svc.name] = $svc.port }
$targets = @{ sandbox = (Join-Path $probeDir "w.txt"); src = (Join-Path $SrcRoot "probe-$User.txt");
              srcfile = $srcFile; profile = (Join-Path $env:USERPROFILE ".pionir");
              profilewrite = (Join-Path $env:USERPROFILE ".pionir\probe-$User.txt");
              public = (Join-Path $env:PUBLIC "Documents\probe-$User.txt");
              programdata = (Join-Path $env:ProgramData "probe-$User.txt");
              wintemp = (Join-Path $env:windir "Temp\probe-$User.txt");
              install = (Join-Path $InstallDir "probe-$User.txt");
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
Remove-Item -Recurse -Force $probeDir -ErrorAction SilentlyContinue
Remove-Item (Join-Path $SrcRoot "probe-$User.txt") -ErrorAction SilentlyContinue
$contained = $true
foreach ($k in $results.Keys) {
    $v = $results[$k]
    $want = if ($k -eq "python: write the sandbox") { "allowed" } else { "blocked" }
    if ($v.StartsWith($want) -or $v -like "*not tested*") { Did "$k : $v" }
    else { Write-Host "    NOT CONTAINED  $k : $v" -ForegroundColor Red; $contained = $false }
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
[IO.File]::WriteAllText($Record, ($doc | ConvertTo-Json -Depth 4), $utf8NoBom)
Did "wrote $Record"
Write-Host ""
Write-Host "Done. The sandbox is proven. Night builds still wait for Pionir's own checks at each build:" -ForegroundColor Cyan
Write-Host "  PIONIR_AUTH_COMPAT off, your Daedalus holding its token, Galatea requiring her token from loopback." -ForegroundColor Cyan
Write-Host "Undo with tools\remove-build-sandbox.ps1." -ForegroundColor Cyan
