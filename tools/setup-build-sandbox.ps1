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
       any program - blocked except loopback, and loopback blocked except the build gate
       (127.0.0.1:8773); the dedicated Python blocked except loopback. A rule Windows will
       not take, or a firewall profile that is off, STOPS the setup.
    7. %ProgramData%\PionirBuilds\setup.json, the record Pionir checks, written last.
  It makes NO scheduled task, NO service and NO autostart of any kind.

  Before the record is written it proves the containment by running AS pionir-builds - the
  dedicated Python, git.exe and powershell.exe: each must fail to reach the internet, the
  loopback services (Ollama, Pionir), C:\src and your profile, and the Python must be able to
  write the sandbox. Anything not contained stops the setup, and nothing is used.

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
$CredFile = Join-Path $env:USERPROFILE ".pionir\secrets\pionir-builds.cred"
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
Step "Windows Firewall: $User reaches the build gate on loopback, and nothing else"
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
    Did "rule: $name (loopback only to the build gate, port $GatePort)"; $rules += $name
} catch {
    Fail "Windows refused a firewall rule ($($_.Exception.Message)); the user cannot be contained without it"
}

# ---- 7. prove it -------------------------------------------------------------------------------
Step "Proving the containment, as $User"
$cred = New-Object System.Management.Automation.PSCredential(".\$User", (ConvertTo-SecureString (Get-Content $CredFile)))
$probeDir = Join-Path $SandboxRoot ".setup-probe"
New-Item -ItemType Directory -Force $probeDir | Out-Null
$srcFile = (Get-ChildItem $SrcRoot -File -Recurse -Depth 1 -ErrorAction SilentlyContinue |
    Where-Object { -not $_.FullName.StartsWith($SandboxRoot, [StringComparison]::OrdinalIgnoreCase) } |
    Select-Object -First 1).FullName
$targets = @{ sandbox = (Join-Path $probeDir "w.txt"); src = (Join-Path $SrcRoot "probe-$User.txt");
              srcfile = $srcFile; profile = (Join-Path $env:USERPROFILE ".pionir"); roots = $dataRoots }
$targets | ConvertTo-Json | Set-Content (Join-Path $probeDir "targets.json") -Encoding UTF8
$pyProbe = @"
import json, os, socket
t = json.load(open(r'$probeDir\targets.json', encoding='utf-8-sig'))
out = {}
def attempt(name, fn):
    try:
        fn(); out[name] = 'allowed'
    except Exception as exc:
        out[name] = 'blocked (' + type(exc).__name__ + ')'
def write(path):
    with open(path, 'w') as f: f.write('x')
    os.remove(path)
def connect(host, port):
    s = socket.create_connection((host, port), timeout=5); s.close()
attempt('write the sandbox', lambda: write(t['sandbox']))
attempt('write C:\\src', lambda: write(t['src']))
if t.get('srcfile'): attempt('read a file in C:\\src', lambda: open(t['srcfile'], 'rb').read(1))
attempt('read your profile', lambda: os.listdir(t['profile']))
for r in (t.get('roots') or []):
    attempt('write ' + r, lambda r=r: write(os.path.join(r, 'probe.txt')))
attempt('reach the internet (1.1.1.1:443)', lambda: connect('1.1.1.1', 443))
attempt('reach Ollama on loopback (127.0.0.1:11434)', lambda: connect('127.0.0.1', 11434))
attempt('reach Pionir on loopback (127.0.0.1:8780)', lambda: connect('127.0.0.1', 8780))
json.dump(out, open(r'$probeDir\python.json', 'w'), indent=1)
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
if (Test-Path "$probeDir\python.json") {
    $got = Get-Content "$probeDir\python.json" -Raw | ConvertFrom-Json
    foreach ($p in $got.PSObject.Properties) { $results["python: " + $p.Name] = [string]$p.Value }
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
if (-not $contained) {
    Fail "the containment did not hold (above). Pionir will not use the sandbox. If only loopback is open, Windows is not filtering loopback traffic for this user: builds must not run until that is solved."
}

# ---- 8. the record (last: Pionir uses the sandbox only once this exists) ---------------------
Step "The record Pionir checks"
if ($script:Problems.Count) { Fail ("some permissions could not be set: " + ($script:Problems -join "; ")) }
$doc = [ordered]@{
    version = 2; user = $User; sid = $UserSid; owner = $OwnerName; owner_sid = $OwnerSid
    python = $PyExe; sandbox_root = (Resolve-Path $SandboxRoot).Path; src_root = $SrcRoot
    daedalus_src = $DaedalusCopy; credential = $CredFile; gate_port = $GatePort
    denied_folders = @($SrcRoot) + $protected + $dataRoots
    firewall_group = $RuleGroup; firewall_rules = $rules
    set_up_at = (Get-Date).ToString("o")
}
[IO.File]::WriteAllText($Record, ($doc | ConvertTo-Json -Depth 4), $utf8NoBom)
Did "wrote $Record"
Write-Host ""
Write-Host "Done. Pionir's Builds division can now run. Undo with tools\remove-build-sandbox.ps1." -ForegroundColor Cyan
