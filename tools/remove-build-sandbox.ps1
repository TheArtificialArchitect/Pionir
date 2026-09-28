<#
.SYNOPSIS
  Undo tools\setup-build-sandbox.ps1. Run it yourself, as administrator, signed in as yourself.

.DESCRIPTION
  Removes everything the setup made, printing each step:
    1. the Windows Firewall rules in the group "Pionir builds"
    2. the deny entry for pionir-builds on C:\src
    3. pionir-builds' access to the sandbox folder; the folder inherits from C:\src again
       (its contents - the product repos - are left where they are)
    4. %ProgramData%\PionirBuilds (the dedicated Python and setup.json)
    5. the credential file %USERPROFILE%\.pionir\secrets\pionir-builds.cred
    6. the sign-in screen entry, the user pionir-builds and its profile folder
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

function Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Did([string]$text) { Write-Host "    DONE     $text" -ForegroundColor Green }
function Had([string]$text) { Write-Host "    NOTHING  $text" -ForegroundColor DarkGray }
function Warn([string]$text) { Write-Host "    WARNING  $text" -ForegroundColor Yellow }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this in an ELEVATED PowerShell (Run as administrator)." -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "Pionir build sandbox - removal" -ForegroundColor Cyan

$UserSid = $null
$local = Get-LocalUser -Name $User -ErrorAction SilentlyContinue
if ($local) { $UserSid = $local.SID.Value }
elseif (Test-Path $Record) {
    try { $UserSid = (Get-Content $Record -Raw | ConvertFrom-Json).sid } catch { }
}

Step "Firewall rules"
$rules = Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue
if ($rules) { $rules | Remove-NetFirewallRule; Did ("removed " + (($rules | ForEach-Object DisplayName) -join "; ")) }
else { Had "no rules in the group '$RuleGroup'" }

Step "The deny on $SrcRoot"
if ($UserSid) {
    & icacls.exe $SrcRoot /remove:d "*$UserSid" | Out-Null
    Did "removed the deny entry for $User on $SrcRoot"
} else { Had "no $User SID known" }

Step "The sandbox folder $SandboxRoot"
if (Test-Path $SandboxRoot) {
    if ($UserSid) { & icacls.exe $SandboxRoot /remove:g "*$UserSid" /T /C /Q | Out-Null }
    & icacls.exe $SandboxRoot /inheritance:e | Out-Null
    Did "removed $User's access; the folder inherits from $SrcRoot again (its contents are kept)"
} else { Had "$SandboxRoot does not exist" }

Step "The dedicated Python and the record"
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir; Did "deleted $InstallDir" }
else { Had "$InstallDir does not exist" }

Step "The credential"
if (Test-Path $CredFile) { Remove-Item -Force $CredFile; Did "deleted $CredFile" }
else { Had "$CredFile does not exist" }

Step "The user $User"
$hide = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
if ((Test-Path $hide) -and (Get-ItemProperty -Path $hide -Name $User -ErrorAction SilentlyContinue)) {
    Remove-ItemProperty -Path $hide -Name $User; Did "removed the sign-in screen entry"
}
if ($UserSid) {
    $prof = Get-CimInstance Win32_UserProfile -Filter "SID='$UserSid'" -ErrorAction SilentlyContinue
    if ($prof) {
        try { $prof | Remove-CimInstance; Did "deleted the profile folder $($prof.LocalPath)" }
        catch { Warn "the profile $($prof.LocalPath) is in use; delete it after a restart" }
    }
}
if ($local) { Remove-LocalUser -Name $User; Did "deleted the user $User" }
else { Had "no user $User" }

Write-Host ""
Write-Host "Done. The Builds division now reports 'not configured' and runs nothing." -ForegroundColor Cyan
