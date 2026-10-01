<#
.SYNOPSIS
  Undo tools\setup-build-sandbox.ps1. Run it yourself, as administrator, signed in as yourself.

.DESCRIPTION
  Removes everything the setup made, printing each step and checking each result:
    1. every process pionir-builds still has (killed first)
    2. the Windows Firewall rules in the group "Pionir builds"
    3. the deny entries for pionir-builds on C:\src, on the folders in it that do not
       inherit, and on the other data folders the setup listed
    4. the sandbox folder's own permissions: pionir-builds' access, the explicit
       SYSTEM/Administrators/owner entries the setup added and the Low label; it inherits
       from C:\src again (its contents - the product repos - are left where they are)
    5. %ProgramData%\PionirBuilds (the dedicated Python, the Daedalus copy, setup.json)
    6. the credential file and HKCU\Software\Pionir\BuildSandbox
    7. the sign-in screen entry, the user pionir-builds and its profile folder
  The SID is found from the account, the record, or the registry copy - whichever is left.
  Afterwards the Builds division reports "not configured" and runs nothing.
#>
[CmdletBinding()]
param(
    [string]$SandboxRoot = "C:\src\daedalus-work",
    [string]$SrcRoot = "C:\src",
    [string]$InstallDir = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$User = "pionir-builds"
$RuleGroup = "Pionir builds"
if (-not $InstallDir) { $InstallDir = Join-Path $env:ProgramData "PionirBuilds" }
$Record = Join-Path $InstallDir "setup.json"
$CredFile = Join-Path $env:USERPROFILE ".pionir\secrets\pionir-builds.cred"
$RegKey = "HKCU:\Software\Pionir\BuildSandbox"
$script:Failures = @()

function Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Did([string]$text) { Write-Host "    DONE     $text" -ForegroundColor Green }
function Had([string]$text) { Write-Host "    NOTHING  $text" -ForegroundColor DarkGray }
function Bad([string]$text) { Write-Host "    FAILED   $text" -ForegroundColor Red; $script:Failures += $text }
function Run-Icacls([string[]]$argv) {
    Write-Host ("    icacls " + ($argv -join " ")) -ForegroundColor DarkGray
    $out = & icacls.exe @argv 2>&1
    $failed = ($out | Select-String -Pattern "Failed processing (\d+) files" |
        ForEach-Object { [int]$_.Matches[0].Groups[1].Value } | Measure-Object -Sum).Sum
    if ($LASTEXITCODE -ne 0 -or $failed) { Bad "icacls $($argv[0]) exited $LASTEXITCODE ($failed item(s) failed)"; return $false }
    return $true
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this in an ELEVATED PowerShell (Run as administrator)." -ForegroundColor Red
    exit 1
}
$OwnerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Write-Host ""
Write-Host "Pionir build sandbox - removal" -ForegroundColor Cyan

# ---- the SID: from the account, the record, or the registry copy -----------------------------
$UserSid = $null
$doc = $null
$local = Get-LocalUser -Name $User -ErrorAction SilentlyContinue
if (Test-Path $Record) { try { $doc = Get-Content $Record -Raw | ConvertFrom-Json } catch { $doc = $null } }
if ($local) { $UserSid = $local.SID.Value }
elseif ($doc -and $doc.sid) { $UserSid = [string]$doc.sid }
elseif (Test-Path $RegKey) { $UserSid = (Get-ItemProperty -Path $RegKey -Name Sid -ErrorAction SilentlyContinue).Sid }
Write-Host "    sid         $(if ($UserSid) { $UserSid } else { '(not found)' })"

Step "Every process $User still has"
if ($local) {
    & taskkill.exe /F /FI "USERNAME eq $User" 2>&1 | Out-Null
    Start-Sleep -Seconds 1
    $left = @(& tasklist.exe /FI "USERNAME eq $User" /FO CSV /NH 2>$null | Where-Object { $_ -match '^"' })
    if ($left.Count) { Bad "processes of $User survived: $($left -join ' ')" } else { Did "none left" }
} else { Had "no user $User" }

Step "Firewall rules"
$rules = @(Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue)
if ($rules.Count) {
    $rules | Remove-NetFirewallRule
    if (@(Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue).Count) { Bad "firewall rules remain in '$RuleGroup'" }
    else { Did ("removed " + (($rules | ForEach-Object DisplayName) -join "; ")) }
} else { Had "no rules in the group '$RuleGroup'" }

Step "The deny entries"
if ($UserSid) {
    $denied = @($SrcRoot)
    if ($doc -and $doc.PSObject.Properties["denied_folders"]) { $denied += @($doc.denied_folders) }
    foreach ($f in ($denied | Select-Object -Unique)) {
        if (Test-Path $f) { if (Run-Icacls @($f, "/remove:d", "*$UserSid")) { Did "removed the deny on $f" } }
    }
} else { Had "no $User SID known" }

Step "The sandbox folder $SandboxRoot"
if (Test-Path $SandboxRoot) {
    $ok = $true
    $ok = (Run-Icacls @($SandboxRoot, "/inheritance:e")) -and $ok
    $grants = @("*S-1-5-18", "*S-1-5-32-544", "*$OwnerSid")
    if ($UserSid) { $grants += "*$UserSid" }
    $ok = (Run-Icacls (@($SandboxRoot, "/remove:g") + $grants)) -and $ok
    $ok = (Run-Icacls @($SandboxRoot, "/setintegritylevel", "(OI)(CI)medium")) -and $ok
    if ((Get-ChildItem -Force $SandboxRoot | Measure-Object).Count) {
        $ok = (Run-Icacls @("$SandboxRoot\*", "/reset", "/T", "/C", "/Q")) -and $ok
    }
    if ($ok) { Did "its own entries and the Low label are gone; it inherits from $SrcRoot again (contents kept)" }
} else { Had "$SandboxRoot does not exist" }

Step "The install folder"
if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) { Bad "could not delete $InstallDir" } else { Did "deleted $InstallDir" }
} else { Had "$InstallDir does not exist" }

Step "The credential and the registry copy"
if (Test-Path $CredFile) { Remove-Item -Force $CredFile; Did "deleted $CredFile" } else { Had "$CredFile does not exist" }
if (Test-Path $RegKey) { Remove-Item -Recurse -Force $RegKey; Did "deleted $RegKey" } else { Had "$RegKey does not exist" }

Step "The user $User"
$hide = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
if ((Test-Path $hide) -and (Get-ItemProperty -Path $hide -Name $User -ErrorAction SilentlyContinue)) {
    Remove-ItemProperty -Path $hide -Name $User; Did "removed the sign-in screen entry"
}
if ($UserSid) {
    $prof = Get-CimInstance Win32_UserProfile -Filter "SID='$UserSid'" -ErrorAction SilentlyContinue
    if ($prof) {
        try { $prof | Remove-CimInstance; Did "deleted the profile folder $($prof.LocalPath)" }
        catch { Bad "the profile $($prof.LocalPath) is in use; delete it after a restart" }
    }
}
if ($local) {
    Remove-LocalUser -Name $User
    if (Get-LocalUser -Name $User -ErrorAction SilentlyContinue) { Bad "the user $User is still there" } else { Did "deleted the user $User" }
} else { Had "no user $User" }

Write-Host ""
if ($script:Failures.Count) {
    Write-Host "Removal finished with $($script:Failures.Count) problem(s):" -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "Done. The Builds division now reports 'not configured' and runs nothing." -ForegroundColor Cyan
